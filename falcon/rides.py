"""Rides from stored GPS points: segmentation, distance, speed statistics.

A ride is a run of GPS points with no gap longer than GAP_S, that contains real movement
(speed above MOVING_KMH for at least MIN_MOVING_S) and ends after STOP_S without movement.
Speed per point: the wheel's own speed when it was connected, otherwise GPS speed.
"""
from __future__ import annotations

import math

GAP_S = 120
STOP_S = 180
MOVING_KMH = 3.0
MIN_MOVING_S = 30
MIN_DISTANCE_M = 100


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _speed(row: dict) -> float | None:
    w = row.get("wheel_speed_kmh")
    return w if w is not None else row.get("speed_kmh")


def segment(points: list[dict]) -> list[list[dict]]:
    """points sorted by ts, each with ts/lat/lon/speed_kmh/wheel_speed_kmh."""
    rides, cur, last_move = [], [], None
    for p in points:
        if p.get("lat") is None:
            continue
        if cur and (p["ts"] - cur[-1]["ts"] > GAP_S
                    or (last_move is not None and p["ts"] - last_move > STOP_S)):
            rides.append(cur)
            cur, last_move = [], None
        cur.append(p)
        if (_speed(p) or 0) >= MOVING_KMH:
            last_move = p["ts"]
    if cur:
        rides.append(cur)
    out = []
    for r in rides:
        # trim standing still at the start and end
        mv = [i for i, p in enumerate(r) if (_speed(p) or 0) >= MOVING_KMH]
        if not mv:
            continue
        r = r[max(mv[0] - 1, 0): mv[-1] + 2]
        moving_s = sum(b["ts"] - a["ts"] for a, b in zip(r, r[1:]) if (_speed(b) or 0) >= MOVING_KMH)
        if moving_s >= MIN_MOVING_S and summary(r)["distance_km"] * 1000 >= MIN_DISTANCE_M:
            out.append(r)
    return out


def summary(r: list[dict]) -> dict:
    dist = sum(haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) for a, b in zip(r, r[1:]))
    speeds = [s for s in (_speed(p) for p in r) if s is not None]
    moving_s = sum(b["ts"] - a["ts"] for a, b in zip(r, r[1:]) if (_speed(b) or 0) >= MOVING_KMH)
    dur = r[-1]["ts"] - r[0]["ts"]
    return {
        "id": int(r[0]["ts"]),
        "start": r[0]["ts"], "end": r[-1]["ts"], "duration_s": round(dur),
        "moving_s": round(moving_s),
        "distance_km": round(dist / 1000, 2),
        "max_kmh": round(max(speeds), 1) if speeds else None,
        "avg_moving_kmh": round(dist / 1000 / (moving_s / 3600), 1) if moving_s > 0 else None,
        "points": len(r),
        "wheel_speed_share_pct": round(100 * sum(1 for p in r if p.get("wheel_speed_kmh") is not None) / len(r)),
    }


def track(r: list[dict]) -> list[list]:
    """[lat, lon, speed_kmh, ts] per point for the map."""
    return [[round(p["lat"], 6), round(p["lon"], 6),
             round(_speed(p), 1) if _speed(p) is not None else None, p["ts"]] for p in r]


def load_points(db, since: float = 0) -> list[dict]:
    cols = ("ts", "lat", "lon", "speed_kmh", "wheel_speed_kmh")
    rows = db.execute("SELECT ts, lat, lon, speed_kmh, wheel_speed_kmh FROM gps_samples "
                      "WHERE ts >= ? ORDER BY ts", (since,))
    return [dict(zip(cols, r)) for r in rows]
