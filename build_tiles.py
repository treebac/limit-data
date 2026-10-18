#!/usr/bin/env python3
"""
Buduje kafelki z limitami prędkości i fotoradarami dla aplikacji Limit.

Wejście: wycinek OpenStreetMap (.osm.pbf), najlepiej już przefiltrowany przez
`osmium tags-filter` (patrz .github/workflows/build.yml).
Wyjście (katalog OUT):
  index.json        – wersja danych, rozmiar siatki, liczba kafelków
  t/<i>_<j>.json    – kafelek ok. 5,5 × 5,5 km: {"w": [drogi], "c": [fotoradary]}

Format drogi:     [id, maxspeed, nazwa, highway, typ_limitu, [lat0, lon0, dlat1, dlon1, ...], (maxspeed:hgv, maxspeed:trailer, motorroad 0/1)]
                  współrzędne × 1e5, od drugiego punktu zapisywane jako różnice (krócej)
Format fotoradaru:[id, lat×1e5, lon×1e5, maxspeed, kierunek]
sections.json     – odcinkowe pomiary prędkości (relacje type=enforcement + enforcement=average_speed):
                    [id, maxspeed, nazwa, lat_od, lon_od, lat_do, lon_do, długość_m (w linii prostej), dwukierunkowy 0/1]

Dane © współtwórcy OpenStreetMap, licencja ODbL.
"""
import json, math, os, sys, time
from collections import defaultdict
import osmium

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


class Builder(osmium.SimpleHandler):
    def __init__(self, need=()):
        super().__init__()
        self.tiles = defaultdict(lambda: {"w": [], "c": []})
        self.ways = self.cams = self.skipped = 0
        self.need, self.coords = set(need), {}

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
        for k in tile_range([p[0] for p in pts], [p[1] for p in pts]):
            self.tiles[k]["w"].append(rec)
        self.ways += 1


def main():
    if len(sys.argv) != 3:
        sys.exit("użycie: build_tiles.py wejście.osm.pbf katalog_wyjściowy")
    src, out = sys.argv[1], sys.argv[2]
    t0 = time.time()
    scan = SectionScan()
    scan.apply_file(src)
    b = Builder(scan.need)
    b.apply_file(src, locations=True, idx="flex_mem")
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
        "f": 2, "tlat": TLAT, "tlon": TLON,
        "tiles": len(b.tiles), "ways": b.ways, "cams": b.cams, "sections": len(secs),
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
