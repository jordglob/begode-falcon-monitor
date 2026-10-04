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
MAX_STEP_S = 30         # points come every 2-5 s; a longer step is a hole in the data


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


def good_fix(p: dict) -> bool:
    if p.get("sats") is None and p.get("hdop") is not None:      # a phone gives accuracy, no satellite count
        return p["hdop"] <= GOOD_HDOP
    return (p.get("sats") or 0) >= GOOD_SATS and (p.get("hdop") or 99) <= GOOD_HDOP


def trusted_speed(row: dict) -> float | None:
    """Speed good enough for a record (max speed): the wheel's own speed, or GPS speed with a
    good fix. GPS speed from a poor fix can jump many km/h (seen: 44.7 km/h on 4 satellites,
    HDOP 7.6, while the wheel never passed 40) – fine for drawing the track, not for a maximum.
    Points without sats/HDOP (older data) count as good."""
    w = row.get("wheel_speed_kmh")
    if w is not None and plausibility(row) != "spin":
        return w
    if row.get("sats") is None and row.get("hdop") is None:
        return row.get("speed_kmh")
    return row.get("speed_kmh") if good_fix(row) else None


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


# ---------- joining ride parts ----------
# A ride is cut whenever nothing moves for STOP_S (a break) or the points stop coming (computer
# asleep, GPS off). Parts that obviously belong together are suggested as one ride; the user
# decides. A merge is stored as a time span, the points themselves are never changed.
MERGE_MAX_GAP_S = 90 * 60      # a break of up to 1.5 h still belongs to the same ride
MERGE_SAME_PLACE_M = 300       # next part starts where the previous one ended: a break
MERGE_MAX_KMH = 60             # gap without data: the distance must be rideable in that time


def _link(a: list[dict], b: list[dict]) -> dict | None:
    """Why part b can continue part a, or None."""
    gap_s = b[0]["ts"] - a[-1]["ts"]
    if gap_s < 0 or gap_s > MERGE_MAX_GAP_S:
        return None
    gap_m = haversine_m(a[-1]["lat"], a[-1]["lon"], b[0]["lat"], b[0]["lon"])
    mins = max(1, round(gap_s / 60))
    if gap_m <= MERGE_SAME_PLACE_M:
        return {"gap_s": round(gap_s), "gap_m": round(gap_m), "kind": "paus", "sure": gap_s <= 15 * 60,
                "text": f"paus {mins} min på samma plats"}
    if gap_m / max(gap_s, 1) * 3.6 <= MERGE_MAX_KMH:
        return {"gap_s": round(gap_s), "gap_m": round(gap_m), "kind": "lucka", "sure": False,
                "text": f"lucka {mins} min, {gap_m / 1000:.1f} km utan positioner (datorn i vila eller GPS av)"}
    return None


def suggest_merges(segs: list[list[dict]]) -> list[dict]:
    """Chains of consecutive parts that look like one ride."""
    out, chain, links = [], [], []

    def close():
        if len(chain) >= 2:
            first, last = chain[0], chain[-1]
            dist = sum(summary(r)["distance_km"] for r in chain) + sum(k["gap_m"] for k in links) / 1000
            out.append({"ids": [int(r[0]["ts"]) for r in chain], "parts": len(chain),
                        "start": first[0]["ts"], "end": last[-1]["ts"], "name": ride_name(first[0]["ts"])[0],
                        "distance_km": round(dist, 2), "links": list(links),
                        "confidence": "hög" if all(k["sure"] for k in links) else "medel",
                        "text": "; ".join(k["text"] for k in links)})

    for r in segs:
        k = _link(chain[-1], r) if chain else None
        if chain and k is None:
            close()
            chain, links = [], []
        if k:
            links.append(k)
        chain.append(r)
    close()
    return out


def merged(segs: list[list[dict]], points: list[dict], spans: list) -> list[list[dict]]:
    """Apply stored merges. spans = [[start_ts, end_ts], ...]; every part that starts inside a
    span becomes one ride made of ALL stored points from the first start to the last end, so
    the breaks and whatever was logged in between are part of the track."""
    if not spans:
        return segs
    out, used = [], set()
    for i, r in enumerate(segs):
        if i in used:
            continue
        span = next((s for s in spans if s[0] <= r[0]["ts"] <= s[1]), None)
        group = [j for j in range(i, len(segs)) if span and span[0] <= segs[j][0]["ts"] <= span[1]]
        if len(group) < 2:
            out.append(r)
            continue
        used.update(group)
        t0, t1 = segs[group[0]][0]["ts"], segs[group[-1]][-1]["ts"]
        out.append([p for p in points if t0 <= p["ts"] <= t1 and p.get("lat") is not None])
    return out


def add_span(spans: list, t0: float, t1: float) -> list:
    """Add a merge span, fusing it with any span it overlaps."""
    keep = []
    for a, b in spans:
        if a <= t1 and t0 <= b:
            t0, t1 = min(t0, a), max(t1, b)
        else:
            keep.append([a, b])
    return sorted(keep + [[t0, t1]])


MONTHS = ["jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]


def ride_name(ts: float) -> tuple[str, str]:
    """('Tur 3 okt 2026 kl. 14:39', '2026-10-03-1439') – shown name and link slug."""
    import time as _t
    t = _t.localtime(ts)
    return (f"Tur {t.tm_mday} {MONTHS[t.tm_mon - 1]} {t.tm_year} kl. {t.tm_hour:02d}:{t.tm_min:02d}",
            _t.strftime("%Y-%m-%d-%H%M", t))


def summary(r: list[dict]) -> dict:
    steps = [(b["ts"] - a["ts"], haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]), (_speed(b) or 0) >= MOVING_KMH)
             for a, b in zip(r, r[1:])]
    dist = sum(d for _, d, _ in steps)
    # a step longer than MAX_STEP_S has no points in it (computer asleep, joined parts): its
    # distance is real travel, but it is neither moving time nor part of the average speed
    gap_m = sum(d for dt, d, _ in steps if dt > MAX_STEP_S)
    speeds = [s for s in (trusted_speed(p) for p in r) if s is not None]
    moving_s = sum(dt for dt, _, mv in steps if mv and dt <= MAX_STEP_S)
    dur = r[-1]["ts"] - r[0]["ts"]
    name, slug = ride_name(r[0]["ts"])
    return {
        "id": int(r[0]["ts"]), "name": name, "slug": slug,
        "start": r[0]["ts"], "end": r[-1]["ts"], "duration_s": round(dur),
        "moving_s": round(moving_s),
        "distance_km": round(dist / 1000, 2),
        "max_kmh": round(max(speeds), 1) if speeds else None,
        "avg_moving_kmh": round((dist - gap_m) / 1000 / (moving_s / 3600), 1) if moving_s > 0 else None,
        "gap_km": round(gap_m / 1000, 2),
        "points": len(r),
        "wheel_speed_share_pct": round(100 * sum(1 for p in r if p.get("wheel_speed_kmh") is not None) / len(r)),
        **_plaus_summary(r),
        "power_max_w": max((p["power_max_w"] for p in r if p.get("power_max_w") is not None), default=None),
        "min_margin_pct": (100 - max(p["pwm_max"] for p in r if p.get("pwm_max") is not None))
        if any(p.get("pwm_max") is not None for p in r) else None,
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
    have = {r[1] for r in db.execute("PRAGMA table_info(gps_samples)")}
    cols = [c for c in ("ts", "lat", "lon", "speed_kmh", "wheel_speed_kmh", "sats", "hdop", "alt_m",
                        "wh_out_cum", "wh_regen_cum", "pwm_max", "current_avg", "data_cov",
                        "current_max", "power_max_w", "regen_max_w", "volt_min") if c in have]
    rows = db.execute(f"SELECT {', '.join(cols)} FROM gps_samples "
                      "WHERE ts >= ? ORDER BY ts", (since,))
    return [dict(zip(cols, r)) for r in rows]
