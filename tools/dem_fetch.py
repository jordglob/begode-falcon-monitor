"""Download Copernicus GLO-30 terrain tiles (1° × 1°, ~30 MB each) for an area, once, so
elevation can be looked up offline. Positions are never sent anywhere – only the tile
names for the bounding box are requested.

    .venv/bin/python tools/dem_fetch.py LAT_MIN LON_MIN LAT_MAX LON_MAX [FOLDER]
    e.g. Stockholm county:  tools/dem_fetch.py 58.7 17.2 60.3 19.4

Copernicus GLO-30 is a SURFACE model (trees and buildings included). For Sweden the
Lantmäteriet ground model is better: reproject it to EPSG:4326 (gdalwarp -t_srs EPSG:4326)
and put the GeoTIFF in the same folder.
Attribution: © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018, provided
under COPERNICUS by the European Union and ESA; all rights reserved.
"""
import math
import sys
import urllib.request
from pathlib import Path

BASE = "https://copernicus-dem-30m.s3.amazonaws.com"


def tile_name(lat: int, lon: int) -> str:
    ns, ew = ("N" if lat >= 0 else "S"), ("E" if lon >= 0 else "W")
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"


def tiles(lat_min, lon_min, lat_max, lon_max):
    for lat in range(math.floor(lat_min), math.floor(lat_max) + 1):
        for lon in range(math.floor(lon_min), math.floor(lon_max) + 1):
            yield lat, lon


def main(argv):
    if len(argv) < 5:
        print(__doc__)
        return 2
    lat_min, lon_min, lat_max, lon_max = map(float, argv[1:5])
    folder = Path(argv[5] if len(argv) > 5 else Path.home() / ".local/share/begode-falcon/dem")
    folder.mkdir(parents=True, exist_ok=True)
    todo = list(tiles(lat_min, lon_min, lat_max, lon_max))
    print(f"{len(todo)} rutor till {folder}")
    for lat, lon in todo:
        name = tile_name(lat, lon)
        dst = folder / f"{name}.tif"
        if dst.exists():
            print("finns redan", dst.name)
            continue
        url = f"{BASE}/{name}/{name}.tif"
        try:
            tmp = dst.with_suffix(".part")
            urllib.request.urlretrieve(url, tmp)
            tmp.rename(dst)
            print("hämtad", dst.name, f"{dst.stat().st_size / 1e6:.0f} MB")
        except Exception as e:                    # ocean tiles do not exist
            print("saknas", name, e)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
