"""Download Lantmäteriet's ground elevation model (Markhöjdmodell Nedladdning, grid 1+:
1 m grid, 2.5 × 2.5 km tiles, SWEREF 99 TM, CC BY 4.0) for an area – read directly by the
app, no reprojection needed.

Free, but the download needs a Geotorget account: https://geotorget.lantmateriet.se
Put the login in ~/.config/begode-falcon/lantmateriet.env (never in the repository):
    LM_USER=...
    LM_PASS=...

    .venv/bin/python tools/lm_fetch.py --area stockholm,nacka,huddinge [--folder DIR] [--dry-run]
    .venv/bin/python tools/lm_fetch.py --bbox LAT_MIN LON_MIN LAT_MAX LON_MAX
    .venv/bin/python tools/lm_fetch.py --around-gps 10      # ~10 km² around the current GPS position

Only the area's bounding box is sent to Lantmäteriet's catalogue – never ride positions.
Attribution: © Lantmäteriet, CC BY 4.0.
"""
import argparse
import base64
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

STAC = "https://api.lantmateriet.se/stac-hojd/v1/search"
# rough bounding boxes (lat_min, lon_min, lat_max, lon_max) of the municipalities
AREAS = {
    "stockholm": (59.22, 17.76, 59.43, 18.20),
    "nacka": (59.24, 18.12, 59.37, 18.43),
    "huddinge": (59.14, 17.83, 59.28, 18.13),
    "solna": (59.34, 17.95, 59.39, 18.05),
    "sundbyberg": (59.35, 17.89, 59.39, 17.99),
    "lidingo": (59.33, 18.08, 59.40, 18.27),
    "danderyd": (59.38, 17.99, 59.43, 18.10),
}
ENV = Path.home() / ".config/begode-falcon/lantmateriet.env"


def creds():
    user, pw = os.environ.get("LM_USER"), os.environ.get("LM_PASS")
    if (not user or not pw) and ENV.exists():
        for line in ENV.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "LM_USER":
                user = v.strip()
            elif k.strip() == "LM_PASS":
                pw = v.strip()
    return user, pw


def search(bbox):
    lat_min, lon_min, lat_max, lon_max = bbox
    url = STAC + "?" + urllib.parse.urlencode({"bbox": f"{lon_min},{lat_min},{lon_max},{lat_max}", "limit": 200})
    while url:
        d = json.load(urllib.request.urlopen(url, timeout=60))
        for f in d.get("features", []):
            # grid 1+ tiles: collection mhm-NN_N, asset under /grid1m/
            for a in f.get("assets", {}).values():
                href = a.get("href", "")
                if "/grid1m/" in href and href.endswith(".tif"):
                    yield f["id"], href, a.get("file:size") or 0
        url = next((l["href"] for l in d.get("links", []) if l.get("rel") == "next"), None)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--area", help="comma separated: " + ", ".join(AREAS))
    ap.add_argument("--bbox", nargs=4, type=float)
    ap.add_argument("--around-gps", type=float, metavar="KM2",
                    help="area (km²) centred on the GPS position from the running server")
    ap.add_argument("--server", default="http://localhost:8096")
    ap.add_argument("--folder", default=str(Path.home() / ".local/share/begode-falcon/dem"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv[1:])
    boxes = []
    if a.area:
        for name in a.area.lower().split(","):
            if name.strip() not in AREAS:
                print("okänt område:", name)
                return 2
            boxes.append(AREAS[name.strip()])
    if a.bbox:
        boxes.append(tuple(a.bbox))
    if a.around_gps:
        g = json.load(urllib.request.urlopen(a.server + "/api/gps", timeout=10))
        st = g.get("state") or {}
        if not g.get("fix") or st.get("lat") is None:
            print("ingen GPS-position från servern")
            return 2
        half_km = math.sqrt(a.around_gps) / 2
        dlat = half_km / 111.195
        dlon = half_km / (111.195 * math.cos(math.radians(st["lat"])))
        boxes.append((st["lat"] - dlat, st["lon"] - dlon, st["lat"] + dlat, st["lon"] + dlon))
        print(f"{a.around_gps:g} km² runt nuvarande GPS-position")
    if not boxes:
        ap.print_help()
        return 2
    tiles = {}
    for b in boxes:
        for tid, href, size in search(b):
            tiles[href] = (tid, size)
    total = sum(s for _, s in tiles.values())
    print(f"{len(tiles)} rutor, {total / 1e9:.2f} GB")
    if a.dry_run:
        return 0
    user, pw = creds()
    if not user or not pw:
        print(f"Inloggning saknas. Skapa ett gratis konto på https://geotorget.lantmateriet.se och skriv\n"
              f"LM_USER=… och LM_PASS=… i {ENV}")
        return 3
    auth = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
    folder = Path(a.folder)
    folder.mkdir(parents=True, exist_ok=True)
    done = 0
    for href, (tid, size) in sorted(tiles.items()):
        dst = folder / ("lm_" + href.rsplit("/", 1)[1])
        if dst.exists() and (not size or dst.stat().st_size == size):
            done += 1
            continue
        req = urllib.request.Request(href, headers={"Authorization": auth})
        try:
            with urllib.request.urlopen(req, timeout=120) as r, open(dst.with_suffix(".part"), "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            dst.with_suffix(".part").rename(dst)
            done += 1
            print(f"{done}/{len(tiles)} {dst.name}")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                print("401: fel användarnamn eller lösenord (eller kontot saknar åtkomst till höjddata)")
                return 4
            print("misslyckades", dst.name, e)
    print(f"klart: {done}/{len(tiles)} rutor i {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
