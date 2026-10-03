"""Dry run: write synthetic rides (no real location) into a database, for testing the
'Position & turer' tab without a wheel or GPS.

    .venv/bin/python tools/demo_ride.py /tmp/demo.db
    FALCON_DB=/tmp/demo.db FALCON_BLE=0 FALCON_GPS=0 FALCON_PORT=8097 \\
        FALCON_BACKUPS=/tmp/demo-backups .venv/bin/python -m falcon.server
"""
import math
import random
import sqlite3
import sys
import time

from falcon.store import Store

# a loop around Greenwich Park, London (public place, synthetic data). The park rises
# ~40 m from the river side (north) to the Observatory hill (south) – the demo uses a
# synthetic hill of that size so climbs, regeneration and grades can be checked.
LOOP = [(51.4769, -0.0005), (51.4790, 0.0005), (51.4815, 0.0030), (51.4805, 0.0075),
        (51.4775, 0.0090), (51.4745, 0.0060), (51.4735, 0.0010), (51.4750, -0.0020),
        (51.4769, -0.0005)]


def interp(path, n):
    out = []
    for (a, b), (c, d) in zip(path, path[1:]):
        for i in range(n):
            t = i / n
            out.append((a + (c - a) * t, b + (d - b) * t))
    return out


MASS = 120.0          # kg, rider + gear + wheel, for the synthetic power


def hill(lat):
    """Synthetic elevation: 10 m at the north edge rising to 50 m at the south."""
    return 10 + 40 * max(0.0, min(1.0, (51.4815 - lat) / (51.4815 - 51.4735)))


def _walk(path, laps):
    """Loop polyline repeated `laps` times as (lat, lon, cumulative metres)."""
    from falcon.rides import haversine_m
    pts, d = [], 0.0
    seq = path * laps
    for i, (lat, lon) in enumerate(seq):
        if i:
            d += haversine_m(seq[i - 1][0], seq[i - 1][1], lat, lon)
        pts.append((lat, lon, d))
    return pts


def _at(walk, dist):
    for (a, b, d0), (c, e, d1) in zip(walk, walk[1:]):
        if d0 <= dist <= d1:
            f = (dist - d0) / (d1 - d0) if d1 > d0 else 0
            return a + (c - a) * f, b + (e - b) * f
    return walk[-1][0], walk[-1][1]


def write_ride(db, t0, laps, vmax, spin=None):
    """Physically consistent synthetic ride: positions follow the speed, battery power =
    rolling + air + m·g·v·grade (η 0.8 uphill, 60 % regeneration downhill)."""
    walk = _walk(LOOP, laps)
    total = walk[-1][2]
    t, d, i = t0, 0.0, 0
    wh_out = wh_reg = 0.0
    lat, lon = _at(walk, 0)
    while d < total:
        x = d / total
        v = vmax * min(1, 0.05 + x * 8, (1 - x) * 8 + 0.05) * (0.8 + 0.2 * math.sin(i / 20))   # km/h
        vm = v / 3.6
        step = vm * 2.0
        lat2, lon2 = _at(walk, min(total, d + step))
        dh = hill(lat2) - hill(lat)
        grade = dh / step if step > 0 else 0.0
        p_flat = 60 + 18 * vm + 0.35 * vm ** 3
        p_grade = MASS * 9.81 * vm * grade
        p = p_flat + (p_grade / 0.8 if p_grade > 0 else p_grade * 0.6)
        if p >= 0:
            wh_out += p * 2.0 / 3600
        else:
            wh_reg += -p * 2.0 / 3600
        wheel = v if i % 4 else None
        gps_v = v * 0.98
        if spin and spin[0] <= i < spin[1]:              # wheel lifted / slipping
            wheel, gps_v = 45.0, 4.0
        db.execute("INSERT OR REPLACE INTO gps_samples (ts, lat, lon, alt_m, speed_kmh, course_deg, sats, hdop, "
                   "fix_type, wheel_speed_kmh, wh_out_cum, wh_regen_cum, pwm_max, current_avg, data_cov) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (t, lat2, lon2, hill(lat2) + random.gauss(0, 3), gps_v, 0, 9, 0.9, "3D", wheel,
                    round(wh_out, 4), round(wh_reg, 4), min(95, 15 + 1.2 * v + max(0, grade) * 300), p / 92, 1.0))
        lat, lon, d, t, i = lat2, lon2, d + step, t + 2.0, i + 1
    return t


def write_samples(db, t0, hours):
    """Synthetic telemetry every 5 s: discharge, ride currents, temperatures, cells."""
    n = int(hours * 3600 / 5)
    for i in range(n):
        x = i / n
        riding = 0.3 < x < 0.6
        cur = (12 + 8 * math.sin(i / 7)) if riding else 0.3
        v = 98.4 - 8 * x - 0.06 * cur
        cmin, cmax = round(v / 24 * 1000 - 4 - (6 if riding else 0)), round(v / 24 * 1000 + 3)
        db.execute("INSERT OR REPLACE INTO samples (ts, voltage_v, current_a, speed_kmh, cell_min_mv, "
                   "cell_max_mv, cell_spread_mv, temp1_c, temp2_c, board_temp_c, motor_temp_c, sag_mohm) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (t0 + i * 5, round(v, 2), round(cur, 1), round(cur * 2.2, 1) if riding else 0,
                    cmin, cmax, cmax - cmin, 24 + 8 * x, 26 + 10 * x, 30 + (12 if riding else 0) * x,
                    25 + (25 if riding else 0) * x, 60 + 3 * math.sin(i / 50) if riding else None))


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/claude-falcon-demo.db"
    random.seed(7)
    db = Store(__import__("pathlib").Path(path)).db
    now = time.time()
    end = write_ride(db, now - 3 * 3600, laps=2, vmax=38, spin=(300, 330))
    write_ride(db, end + 1800, laps=1, vmax=25)      # second ride after a 30 min break
    write_samples(db, now - 6 * 3600, 6)
    db.commit()
    print("demo-turer skrivna till", path)
