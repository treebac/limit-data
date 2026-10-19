#!/usr/bin/env python3
"""
Buduje kafelki z limitami prędkości i fotoradarami dla aplikacji Limit.

Wejście: wycinek OpenStreetMap (.osm.pbf), najlepiej już przefiltrowany przez
`osmium tags-filter` (patrz .github/workflows/build.yml).
Wyjście (katalog OUT):
  index.json        – wersja danych, rozmiar siatki, liczba kafelków
  t/<i>_<j>.json    – kafelek ok. 5,5 × 5,5 km: {"w": [drogi], "c": [fotoradary]}

Format drogi:     [id, maxspeed, nazwa, highway, typ_limitu, [lat0, lon0, dlat1, dlon1, ...], (maxspeed:hgv, maxspeed:trailer, motorroad 0/1, zabudowa)]
                  zabudowa – tylko dla dróg bez limitu w danych: dla każdego odcinka między węzłami "1" (w mieście/wsi)
                  lub "0", albo jeden znak, gdy cała droga jest po jednej stronie; aplikacja szacuje wtedy 50 albo 90 km/h
                  współrzędne × 1e5, od drugiego punktu zapisywane jako różnice (krócej)
Format fotoradaru:[id, lat×1e5, lon×1e5, maxspeed, kierunek]
sections.json     – odcinkowe pomiary prędkości (relacje type=enforcement + enforcement=average_speed):
                    [id, maxspeed, nazwa, lat_od, lon_od, lat_do, lon_do, długość_m (w linii prostej), dwukierunkowy 0/1]

Dane © współtwórcy OpenStreetMap, licencja ODbL.
"""
import json, math, os, sys, time
from array import array
from collections import defaultdict
import osmium
try:
    import shapely
    from shapely import STRtree
except ImportError:          # bez shapely kafelki budują się jak dawniej, bez szacowania zabudowy
    shapely = None

HIGHWAYS = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
            "living_street", "motorway_link", "trunk_link", "primary_link", "secondary_link"}
TLAT, TLON = 0.05, 0.08   # musi się zgadzać z aplikacją (index.json to przekazuje)


def tile_range(lats, lons):
    for i in range(math.floor(min(lats) / TLAT), math.floor(max(lats) / TLAT) + 1):
        for j in range(math.floor(min(lons) / TLON), math.floor(max(lons) / TLON) + 1):
            yield f"{i}_{j}"


class SectionScan(osmium.SimpleHandler):
    """Pierwsze przejście: relacje odcinkowego pomiaru i potrzebne węzły (początek/koniec)."""
    def __init__(self):
        super().__init__()
        self.sections, self.need = [], set()

    def relation(self, r):
        t = r.tags
        if t.get("type") != "enforcement" or t.get("enforcement") != "average_speed":
            return
        nodes = lambda role: [m.ref for m in r.members if m.type == "n" and m.role == role]
        fr, to, dev = nodes("from"), nodes("to"), nodes("device")
        both = 0
        if not (fr and to):
            if len(dev) >= 2:
                fr, to, both = [dev[0]], [dev[-1]], 1   # kierunek nieznany
            else:
                return
        self.sections.append({"id": r.id, "maxspeed": t.get("maxspeed", ""),
                              "name": t.get("name") or t.get("description") or t.get("ref") or "",
                              "from": fr[0], "to": to[0], "both": both})
        self.need.update([fr[0], to[0]])


def dist_m(a, b):
    r = math.pi / 180
    x = (b[1] - a[1]) * r * math.cos((a[0] + b[0]) / 2 * r)
    y = (b[0] - a[0]) * r
    return 6371000 * math.hypot(x, y)


BUILT_LANDUSE = {"residential", "commercial", "retail"}
BUILT_PLACE = {"city", "town", "village", "suburb", "quarter", "neighbourhood", "hamlet"}
NEAR_DEG = 0.0004            # ok. 30–45 m: znak „obszar zabudowany” stoi zwykle tuż przed pierwszymi domami
NO_GUESS = {"motorway", "motorway_link", "residential", "living_street"}   # te mają limit domyślny z rodzaju drogi


def load_built_up(path):
    """Obszary zabudowane: osiedla, centra handlowe i granice miejscowości z OSM (wielokąty)."""
    if not shapely or not path or not os.path.exists(path):
        return None
    wkb = osmium.geom.WKBFactory()
    geoms = []
    fp = osmium.FileProcessor(path).with_areas().with_filter(osmium.filter.EntityFilter(osmium.osm.AREA))
    for a in fp:
        t = a.tags
        if t.get("landuse") not in BUILT_LANDUSE and t.get("place") not in BUILT_PLACE:
            continue
        try:
            g = shapely.from_wkb(wkb.create_multipolygon(a))
        except Exception:
            continue
        if g is not None and not g.is_empty:
            geoms.append(g)
    print(f"obszary zabudowane: {len(geoms)}", flush=True)
    return STRtree(geoms) if geoms else None


def mark_built_up(tree, todo):
    """todo: lista (rekord_drogi, szer[], dł[]) → dopisuje maskę zabudowy (pole 9): jeden znak na odcinek między węzłami.
    Odcinek jest „w zabudowie”, gdy co najmniej połowa punktów próbkowanych co ~50 m leży w mieście/wsi."""
    if tree is None or not todo:
        return 0
    CH = 300000
    k = 0
    while k < len(todo):
        lats, lons, segs = [], [], []        # segs: (indeks drogi w paczce, liczba próbek) w kolejności odcinków
        batch = []
        while k < len(todo) and len(lats) < CH:
            rec, la, lo = todo[k]; batch.append(rec)
            for i in range(1, len(la)):
                d = dist_m((la[i - 1], lo[i - 1]), (la[i], lo[i]))
                n = max(1, min(40, int(d // 50)))
                for j in range(n):
                    f = (j + 0.5) / n
                    lats.append(la[i - 1] + (la[i] - la[i - 1]) * f); lons.append(lo[i - 1] + (lo[i] - lo[i - 1]) * f)
                segs.append((len(batch) - 1, n))
            k += 1
        hit = set(tree.query(shapely.points(lons, lats), predicate="dwithin", distance=NEAR_DEG)[0].tolist())
        masks = [[] for _ in batch]
        p = 0
        for b, n in segs:
            ins = sum(1 for q in range(p, p + n) if q in hit)
            masks[b].append("1" if ins * 2 >= n else "0")
            p += n
        for rec, m in zip(batch, masks):
            m = "".join(m) or "0"
            if m.count(m[0]) == len(m):
                m = m[0]
            if len(rec) == 6:
                rec += ["", "", 0]
            rec.append(m)
    return len(todo)


GRID = 50   # siatka zabudowy w kafelku: 50 × 50 pól, każde ok. 110 × 110 m


def built_grids(tree, keys):
    """Dla każdego kafelka: bitmapa pól leżących w miejscowości (base64). Pozwala szacować 50/90 km/h
    także tam, gdzie w danych nie ma żadnej drogi (np. droga wewnętrzna, nowa ulica)."""
    out = {}
    if tree is None:
        return out
    import base64
    keys = sorted(keys)
    for s in range(0, len(keys), 120):
        part = keys[s:s + 120]
        lats, lons = [], []
        for k in part:
            i, j = map(int, k.split("_"))
            for r in range(GRID):
                for c in range(GRID):
                    lats.append(i * TLAT + (r + 0.5) * TLAT / GRID)
                    lons.append(j * TLON + (c + 0.5) * TLON / GRID)
        hit = tree.query(shapely.points(lons, lats), predicate="dwithin", distance=NEAR_DEG + 0.0003)[0]
        per = GRID * GRID
        bits = {}
        for h in set(hit.tolist()):
            bits.setdefault(h // per, []).append(h % per)
        for t, idx in bits.items():
            buf = bytearray((per + 7) // 8)
            for x in idx:
                buf[x >> 3] |= 1 << (x & 7)
            out[part[t]] = base64.b64encode(bytes(buf)).decode()
    return out


class Builder(osmium.SimpleHandler):
    def __init__(self, need=()):
        super().__init__()
        self.tiles = defaultdict(lambda: {"w": [], "c": []})
        self.ways = self.cams = self.skipped = 0
        self.need, self.coords = set(need), {}
        self.todo = []   # drogi bez limitu do oznaczenia zabudowy

    def node(self, n):
        if n.id in self.need:
            self.coords[n.id] = (n.location.lat, n.location.lon)
        if n.tags.get("highway") != "speed_camera":
            return
        t = n.tags
        rec = [n.id, round(n.location.lat * 1e5), round(n.location.lon * 1e5),
               t.get("maxspeed", ""), t.get("direction", "") or t.get("camera:direction", "")]
        self.tiles[f"{math.floor(n.location.lat / TLAT)}_{math.floor(n.location.lon / TLON)}"]["c"].append(rec)
        self.cams += 1

    def way(self, w):
        t = w.tags
        hw = t.get("highway")
        if hw not in HIGHWAYS:
            return
        try:
            pts = [(n.lat, n.lon) for n in w.nodes]
        except osmium.InvalidLocationError:
            self.skipped += 1
            return
        if len(pts) < 2:
            return
        maxspeed = t.get("maxspeed") or t.get("maxspeed:forward") or t.get("maxspeed:backward") or ""
        kind = t.get("maxspeed:type") or t.get("source:maxspeed") or t.get("zone:maxspeed") or ""
        name = t.get("name") or t.get("ref") or ""
        g, plat, plon = [], None, None
        for lat, lon in pts:
            a, b = round(lat * 1e5), round(lon * 1e5)
            if plat is None:
                g += [a, b]
            else:
                g += [a - plat, b - plon]
            plat, plon = a, b
        hgv = t.get("maxspeed:hgv") or ""
        trailer = t.get("maxspeed:trailer") or ""
        motorroad = 1 if t.get("motorroad") == "yes" else 0
        rec = [w.id, maxspeed, name, hw, kind, g]
        if hgv or trailer or motorroad:          # pola dodatkowe tylko gdy są (mniejsze kafelki)
            rec += [hgv, trailer, motorroad]
        if not maxspeed and not kind and hw not in NO_GUESS and not motorroad:
            self.todo.append((rec, array("d", (p[0] for p in pts)), array("d", (p[1] for p in pts))))
        for k in tile_range([p[0] for p in pts], [p[1] for p in pts]):
            self.tiles[k]["w"].append(rec)
        self.ways += 1


def main():
    if len(sys.argv) not in (3, 4):
        sys.exit("użycie: build_tiles.py drogi.osm.pbf katalog_wyjściowy [zabudowa.osm.pbf]")
    src, out = sys.argv[1], sys.argv[2]
    built = sys.argv[3] if len(sys.argv) == 4 else None
    t0 = time.time()
    scan = SectionScan()
    scan.apply_file(src)
    b = Builder(scan.need)
    b.apply_file(src, locations=True, idx="flex_mem")
    tree = load_built_up(built)
    marked = mark_built_up(tree, b.todo)
    print(f"drogi z oszacowaną zabudową: {marked}", flush=True)
    grids = built_grids(tree, list(b.tiles.keys()))
    for k, g in grids.items():
        b.tiles[k]["b"] = g
    print(f"kafelki z siatką zabudowy: {len(grids)}", flush=True)
    b.todo = None
    os.makedirs(os.path.join(out, "t"), exist_ok=True)
    size = 0
    for k, tile in b.tiles.items():
        data = json.dumps(tile, ensure_ascii=False, separators=(",", ":"))
        with open(os.path.join(out, "t", k + ".json"), "w", encoding="utf-8") as f:
            f.write(data)
        size += len(data.encode("utf-8"))
    secs = []
    for x in scan.sections:
        a, z = b.coords.get(x["from"]), b.coords.get(x["to"])
        if not a or not z:
            continue
        secs.append([x["id"], x["maxspeed"], x["name"], round(a[0] * 1e5), round(a[1] * 1e5),
                     round(z[0] * 1e5), round(z[1] * 1e5), round(dist_m(a, z)), x["both"]])
    with open(os.path.join(out, "sections.json"), "w", encoding="utf-8") as f:
        json.dump({"s": secs}, f, ensure_ascii=False, separators=(",", ":"))
    index = {
        "v": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "f": 3, "grid": GRID, "tlat": TLAT, "tlon": TLON,
        "tiles": len(b.tiles), "ways": b.ways, "guessed": marked, "cams": b.cams, "sections": len(secs),
        "source": os.environ.get("REGION_URL", src),
        "license": "© OpenStreetMap contributors, ODbL",
    }
    with open(os.path.join(out, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)
    with open(os.path.join(out, ".nojekyll"), "w") as f:
        f.write("")
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(f"<!doctype html><meta charset=utf-8><title>Limit – dane</title>"
                f"<p>Dane limitów prędkości i fotoradarów dla aplikacji Limit.</p>"
                f"<p>Wersja: {index['v']} · kafelków: {index['tiles']} · dróg: {index['ways']} · fotoradarów: {index['cams']}</p>"
                f"<p>Dane © współtwórcy <a href='https://www.openstreetmap.org/copyright'>OpenStreetMap</a>, licencja ODbL.</p>")
    print(f"odcinkowych pomiarów: {len(secs)}")
    print(f"dróg: {b.ways}, fotoradarów: {b.cams}, pominiętych: {b.skipped}, kafelków: {len(b.tiles)}, "
          f"{size / 1e6:.1f} MB, {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
