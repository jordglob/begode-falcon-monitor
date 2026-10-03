"""Export rides (GPX 1.1, CSV) and stored telemetry (CSV)."""
from __future__ import annotations

import csv
import datetime as dt
import io
from xml.sax.saxutils import escape


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ride_gpx(summary: dict, track: list[list], name: str = "Falcon-tur") -> str:
    """track rows: [lat, lon, speed_kmh, ts]. Speed in m/s in a Garmin-style extension."""
    pts = []
    for lat, lon, v, ts, *_ in track:
        ext = (f"<extensions><speed>{v / 3.6:.2f}</speed></extensions>" if v is not None else "")
        pts.append(f'      <trkpt lat="{lat:.6f}" lon="{lon:.6f}"><time>{_iso(ts)}</time>{ext}</trkpt>')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<gpx version="1.1" creator="begode-falcon-monitor" xmlns="http://www.topografix.com/GPX/1/1">\n'
            f'  <metadata><name>{escape(name)}</name><time>{_iso(summary["start"])}</time></metadata>\n'
            f'  <trk><name>{escape(name)}</name><trkseg>\n' + "\n".join(pts) + "\n  </trkseg></trk>\n</gpx>\n")


def ride_csv(track: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time_utc", "latitude", "longitude", "speed_kmh", "plausibility", "wheel_kmh", "gps_kmh"])
    for lat, lon, v, ts, *rest in track:
        flag, wk, gk = (rest + [None, None, None])[:3]
        w.writerow([_iso(ts), f"{lat:.6f}", f"{lon:.6f}", "" if v is None else v, flag or "",
                    "" if wk is None else wk, "" if gk is None else gk])
    return buf.getvalue()


def samples_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    if not rows:
        return "time_utc\n"
    cols = [c for c in rows[0] if c != "extra"]
    w = csv.writer(buf)
    w.writerow(["time_utc"] + [c for c in cols if c != "ts"])
    for r in rows:
        w.writerow([_iso(r["ts"])] + ["" if r[c] is None else r[c] for c in cols if c != "ts"])
    return buf.getvalue()
