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


# speed plausibility: wheel speed vs GPS speed (only where the GPS fix is good)
GOOD_SATS, GOOD_HDOP = 5, 2.5
SPIN_MIN_KMH, SPIN_FACTOR, SPIN_MARGIN_KMH = 8.0, 1.3, 5.0
CARRIED_GPS_KMH, CARRIED_WHEEL_KMH = 10.0, 2.0


def plausibility(p: dict) -> str | None:
    """'spin'    wheel turns clearly faster than the GPS moves (wheel off the ground,
                 slipping on ice/gravel, or lifted while the motor runs),
       'carried' GPS moves but the wheel stands still (carried, in a car, data stale),
       'ok'      both agree, None = cannot judge (no wheel speed or poor GPS fix)."""
    w, g = p.get("wheel_speed_kmh"), p.get("speed_kmh")
    if w is None or g is None:
        return None
    if (p.get("sats") or 0) < GOOD_SATS or (p.get("hdop") or 99) > GOOD_HDOP:
        return None
    if w >= SPIN_MIN_KMH and w > g * SPIN_FACTOR + SPIN_MARGIN_KMH:
        return "spin"
    if g >= CARRIED_GPS_KMH and w < CARRIED_WHEEL_KMH:
        return "carried"
    return "ok"


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _speed(row: dict) -> float | None:
    """Wheel speed where it is plausible; GPS speed where the wheel spins faster than the
    GPS moves (a lifted or slipping wheel must not set the ride's max speed)."""
    w = row.get("wheel_speed_kmh")
    if w is None or plausibility(row) == "spin":
        return row.get("speed_kmh")
    return w


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
        **_plaus_summary(r),
    }


def _plaus_summary(r: list[dict]) -> dict:
    flags = [(b["ts"] - a["ts"], plausibility(b)) for a, b in zip(r, r[1:])]
    checked = sum(dt for dt, f in flags if f is not None)
    return {"spin_s": round(sum(dt for dt, f in flags if f == "spin")),
            "carried_s": round(sum(dt for dt, f in flags if f == "carried")),
            "checked_share_pct": round(100 * checked / max(r[-1]["ts"] - r[0]["ts"], 1))}


def track(r: list[dict]) -> list[list]:
    """[lat, lon, speed_kmh, ts, plausibility, wheel_kmh, gps_kmh] per point for the map."""
    rnd = lambda v: round(v, 1) if v is not None else None
    return [[round(p["lat"], 6), round(p["lon"], 6), rnd(_speed(p)), p["ts"], plausibility(p),
             rnd(p.get("wheel_speed_kmh")), rnd(p.get("speed_kmh"))] for p in r]


def load_points(db, since: float = 0) -> list[dict]:
    cols = ("ts", "lat", "lon", "speed_kmh", "wheel_speed_kmh", "sats", "hdop")
    rows = db.execute("SELECT ts, lat, lon, speed_kmh, wheel_speed_kmh, sats, hdop FROM gps_samples "
                      "WHERE ts >= ? ORDER BY ts", (since,))
    return [dict(zip(cols, r)) for r in rows]
