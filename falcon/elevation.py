"""Elevation for ride analysis, computed afterwards from stored positions.

Sources, best first:
  * terrain model tiles in a folder (DEM): SRTM-style `.hgt` (pure Python) or GeoTIFF in
    EPSG:4326 lat/lon (needs numpy + tifffile). Lantmäteriet (Sweden, ground model, SWEREF 99 TM)
    must be reprojected first: `gdalwarp -t_srs EPSG:4326 in.tif out.tif`.
    Copernicus GLO-30 (tools/dem_fetch.py) is a SURFACE model: trees and buildings count as
    height – fine on open roads, biased in forest.
  * GPS altitude: noisy (vertical error 2–3× horizontal). Filtered: poor fixes dropped,
    median filter, and climbs only counted past a hysteresis threshold.
No positions are sent anywhere – tiles are read offline.
"""
from __future__ import annotations

import math
import re
import statistics
import struct
from pathlib import Path

# ---------- SWEREF 99 TM (EPSG:3006 / 5845 horizontally) ----------
# Gauss–Krüger forward projection, GRS80, central meridian 15° E, scale 0.9996,
# false easting 500 000 m (Lantmäteriet's published formulas). Accurate to millimetres.
_A, _F = 6378137.0, 1 / 298.257222101
_E2 = _F * (2 - _F)
_N = _F / (2 - _F)
_AH = _A / (1 + _N) * (1 + _N ** 2 / 4 + _N ** 4 / 64)
_B1 = _N / 2 - 2 * _N ** 2 / 3 + 5 * _N ** 3 / 16 + 41 * _N ** 4 / 180
_B2 = 13 * _N ** 2 / 48 - 3 * _N ** 3 / 5 + 557 * _N ** 4 / 1440
_B3 = 61 * _N ** 3 / 240 - 103 * _N ** 4 / 140
_B4 = 49561 * _N ** 4 / 161280


def to_sweref99tm(lat: float, lon: float) -> tuple[float, float]:
    """WGS84/SWEREF99 lat, lon (degrees) -> (northing, easting) in metres."""
    k0, lon0, fe = 0.9996, math.radians(15.0), 500000.0
    phi, lam = math.radians(lat), math.radians(lon)
    e2 = _E2
    a_ = e2
    b_ = (5 * e2 ** 2 - e2 ** 3) / 6
    c_ = (104 * e2 ** 3 - 45 * e2 ** 4) / 120
    d_ = 1237 * e2 ** 4 / 1260
    phi_s = phi - math.sin(phi) * math.cos(phi) * (a_ + b_ * math.sin(phi) ** 2 + c_ * math.sin(phi) ** 4
                                                   + d_ * math.sin(phi) ** 6)
    dl = lam - lon0
    xi = math.atan(math.tan(phi_s) / math.cos(dl))
    eta = math.atanh(math.cos(phi_s) * math.sin(dl))
    x = k0 * _AH * (xi + _B1 * math.sin(2 * xi) * math.cosh(2 * eta) + _B2 * math.sin(4 * xi) * math.cosh(4 * eta)
                    + _B3 * math.sin(6 * xi) * math.cosh(6 * eta) + _B4 * math.sin(8 * xi) * math.cosh(8 * eta))
    y = k0 * _AH * (eta + _B1 * math.cos(2 * xi) * math.sinh(2 * eta) + _B2 * math.cos(4 * xi) * math.sinh(4 * eta)
                    + _B3 * math.cos(6 * xi) * math.sinh(6 * eta) + _B4 * math.cos(8 * xi) * math.sinh(8 * eta)) + fe
    return x, y


HGT_RE = re.compile(r"([NS])(\d{2})([EW])(\d{3})", re.I)


class HgtTile:
    """SRTM .hgt: square grid of big-endian int16, rows north→south, SW corner in the name."""

    def __init__(self, path: Path):
        m = HGT_RE.search(path.name)
        if not m:
            raise ValueError(f"okänt rutnamn {path.name}")
        lat = int(m.group(2)) * (1 if m.group(1).upper() == "N" else -1)
        lon = int(m.group(4)) * (1 if m.group(3).upper() == "E" else -1)
        self.path, self.lat0, self.lon0 = path, lat, lon
        size = path.stat().st_size // 2
        self.n = int(round(math.sqrt(size)))
        if self.n * self.n != size:
            raise ValueError(f"{path.name}: inte en kvadratisk .hgt")
        self.bbox = (lat, lon, lat + 1, lon + 1)
        self._data: bytes | None = None
        self.name = f"hgt {path.name}"

    def _at(self, row: int, col: int) -> float | None:
        if self._data is None:
            self._data = self.path.read_bytes()
        i = (row * self.n + col) * 2
        v = struct.unpack_from(">h", self._data, i)[0]
        return None if v == -32768 else float(v)

    def value(self, lat: float, lon: float) -> float | None:
        step = 1.0 / (self.n - 1)
        r = (self.lat0 + 1 - lat) / step
        c = (lon - self.lon0) / step
        return _bilinear(self._at, r, c, self.n, self.n)


class GeoTiffTile:
    """GeoTIFF in EPSG:4326 (lat/lon), e.g. Copernicus GLO-30 or a reprojected national model."""

    def __init__(self, path: Path):
        import tifffile
        self.path = path
        with tifffile.TiffFile(path) as t:
            page = t.pages[0]
            tags = page.tags
            scale = tags["ModelPixelScaleTag"].value
            tie = tags["ModelTiepointTag"].value
            self.w, self.h = page.imagewidth, page.imagelength
        self.dx, self.dy = scale[0], scale[1]
        self.x_left, self.y_top = tie[3], tie[4]
        # projected (metres, SWEREF 99 TM) if the origin is far outside ±180°
        self.projected = abs(self.x_left) > 360 or abs(self.y_top) > 360
        if self.projected:
            # bbox kept in (northing, easting) metres; lookups convert lat/lon first
            self.bbox_m = (self.y_top - self.h * self.dy, self.x_left, self.y_top, self.x_left + self.w * self.dx)
            self.bbox = (-90, -180, 90, 180)                 # real test in covers()
        else:
            self.bbox = (self.y_top - self.h * self.dy, self.x_left, self.y_top, self.x_left + self.w * self.dx)
        self._arr = None
        self.name = f"geotiff {path.name}" + (" (SWEREF 99 TM)" if self.projected else "")

    def covers(self, lat: float, lon: float) -> bool:
        if not self.projected:
            a, b, c, d = self.bbox
            return a <= lat <= c and b <= lon <= d
        n, e = to_sweref99tm(lat, lon)
        a, b, c, d = self.bbox_m
        return a <= n <= c and b <= e <= d

    def _at(self, row: int, col: int) -> float | None:
        if self._arr is None:
            import tifffile
            self._arr = tifffile.imread(self.path)
        v = float(self._arr[row, col])
        return None if (math.isnan(v) or v < -1000) else v

    def value(self, lat: float, lon: float) -> float | None:
        if self.projected:
            yy, xx = to_sweref99tm(lat, lon)
        else:
            yy, xx = lat, lon
        # pixel-is-area: sample at pixel centres
        r = (self.y_top - yy) / self.dy - 0.5
        c = (xx - self.x_left) / self.dx - 0.5
        return _bilinear(self._at, r, c, self.h, self.w)

    def unload(self) -> None:
        self._arr = None


def _bilinear(at, r: float, c: float, nrows: int, ncols: int) -> float | None:
    r0, c0 = int(math.floor(r)), int(math.floor(c))
    if r0 < 0 or c0 < 0 or r0 + 1 >= nrows or c0 + 1 >= ncols:
        if 0 <= round(r) < nrows and 0 <= round(c) < ncols:
            return at(int(round(r)), int(round(c)))
        return None
    fr, fc = r - r0, c - c0
    q = [at(r0, c0), at(r0, c0 + 1), at(r0 + 1, c0), at(r0 + 1, c0 + 1)]
    if any(v is None for v in q):
        vals = [v for v in q if v is not None]
        return sum(vals) / len(vals) if vals else None
    top = q[0] * (1 - fc) + q[1] * fc
    bot = q[2] * (1 - fc) + q[3] * fc
    return top * (1 - fr) + bot * fr


class Dem:
    """All tiles in a folder; picks the tile covering each point."""

    def __init__(self, folder: str | Path | None):
        self.tiles = []
        self.errors = []
        self._lru: list = []
        self.folder = Path(folder) if folder else None
        if not self.folder or not self.folder.is_dir():
            return
        for p in sorted(self.folder.iterdir()):
            try:
                if p.suffix.lower() == ".hgt":
                    self.tiles.append(HgtTile(p))
                elif p.suffix.lower() in (".tif", ".tiff"):
                    self.tiles.append(GeoTiffTile(p))
            except Exception as e:                       # bad file or tifffile missing
                self.errors.append(f"{p.name}: {e}")

    MAX_LOADED = 8

    def value(self, lat: float, lon: float) -> float | None:
        for t in self.tiles:
            hit = t.covers(lat, lon) if hasattr(t, "covers") else (
                t.bbox[0] <= lat <= t.bbox[2] and t.bbox[1] <= lon <= t.bbox[3])
            if hit:
                if t not in self._lru:
                    self._lru.append(t)
                    if len(self._lru) > self.MAX_LOADED:
                        old = self._lru.pop(0)
                        if hasattr(old, "unload"):
                            old.unload()
                v = t.value(lat, lon)
                if v is not None:
                    return v
        return None

    def describe(self) -> dict:
        return {"folder": str(self.folder) if self.folder else None, "tiles": [t.name for t in self.tiles],
                "errors": self.errors}


# ---------- GPS altitude filtering ----------
GOOD_SATS, GOOD_HDOP = 5, 2.5


def gps_altitudes(points: list[dict], window: int = 5) -> list[float | None]:
    """Median-filtered GPS altitude; points with a poor fix are interpolated over."""
    raw = [p.get("alt_m") if (p.get("sats") or 0) >= GOOD_SATS and (p.get("hdop") or 99) <= GOOD_HDOP
           else None for p in points]
    idx = [i for i, v in enumerate(raw) if v is not None]
    if not idx:
        return [None] * len(points)
    filled = []
    for i in range(len(raw)):
        if raw[i] is not None:
            filled.append(raw[i])
            continue
        lo = max((j for j in idx if j < i), default=None)
        hi = min((j for j in idx if j > i), default=None)
        if lo is None:
            filled.append(raw[hi])
        elif hi is None:
            filled.append(raw[lo])
        else:
            f = (i - lo) / (hi - lo)
            filled.append(raw[lo] + (raw[hi] - raw[lo]) * f)
    h = window // 2
    return [statistics.median(filled[max(0, i - h): i + h + 1]) for i in range(len(filled))]


def hysteresis_climbs(dist_m: list[float], elev: list[float], threshold: float) -> list[dict]:
    """Alternating climbs/descents. A turn is accepted only once the elevation has moved
    `threshold` metres back from the running extreme, so noise below the threshold is ignored."""
    segs, state, start = [], None, 0
    i_min = i_max = 0
    for i, e in enumerate(elev):
        if state is None:
            if e < elev[i_min]:
                i_min = i
            if e > elev[i_max]:
                i_max = i
            if e - elev[i_min] >= threshold:
                state, start, i_max = "up", i_min, i
            elif elev[i_max] - e >= threshold:
                state, start, i_min = "down", i_max, i
        elif state == "up":
            if e > elev[i_max]:
                i_max = i
            elif elev[i_max] - e >= threshold:
                segs.append(("up", start, i_max))
                state, start, i_min = "down", i_max, i
        else:
            if e < elev[i_min]:
                i_min = i
            elif e - elev[i_min] >= threshold:
                segs.append(("down", start, i_min))
                state, start, i_max = "up", i_min, i
    if state == "up" and elev[i_max] - elev[start] >= threshold:
        segs.append(("up", start, i_max))
    elif state == "down" and elev[start] - elev[i_min] >= threshold:
        segs.append(("down", start, i_min))
    return [{"dir": d, "i0": a, "i1": b, "dh_m": round(elev[b] - elev[a], 1),
             "length_m": round(dist_m[b] - dist_m[a], 1)} for d, a, b in segs]
