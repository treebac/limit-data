# limit-data

Dane limitów prędkości i fotoradarów dla aplikacji **Limit**, budowane codziennie z OpenStreetMap
i publikowane na GitHub Pages: `https://treebac.github.io/limit-data/`.

- `index.json` – wersja danych i parametry siatki
- `t/<i>_<j>.json` – kafelek ok. 5,5 × 5,5 km (`i = floor(lat / 0.05)`, `j = floor(lon / 0.08)`)

Automat: `.github/workflows/build.yml` (codziennie i ręcznie z zakładki *Actions*).
Skrypt: `build_tiles.py` (pyosmium).

## Jednorazowa konfiguracja
Settings → Pages → *Build and deployment* → Source: **GitHub Actions**.

Dane © współtwórcy [OpenStreetMap](https://www.openstreetmap.org/copyright), licencja ODbL.
