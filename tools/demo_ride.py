"""Dry run: write synthetic rides (no real location) into a database, for testing the
'Position & turer' tab without a wheel or GPS.

    .venv/bin/python tools/demo_ride.py /tmp/demo.db
    FALCON_DB=/tmp/demo.db FALCON_BLE=0 FALCON_GPS=0 FALCON_PORT=8097 \\
        FALCON_BACKUPS=/tmp/demo-backups .venv/bin/python -m falcon.server
"""
import math
import sqlite3
import sys
import time

from falcon.store import Store

# a loop around Greenwich Park, London (public place, synthetic data)
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


def write_ride(db, t0, laps, vmax, spin=None):
    pts = interp(LOOP, 60) * laps
    t = t0
    for i, (lat, lon) in enumerate(pts):
        # speed profile: accelerate, cruise with waves, brake at the end
        x = i / len(pts)
        v = vmax * min(1, x * 8, (1 - x) * 8) * (0.75 + 0.25 * math.sin(i / 25))
        wheel = v if i % 4 else None
        gps_v = v * 0.97
        if spin and spin[0] <= i < spin[1]:          # wheel lifted / slipping: spins, GPS slow
            wheel, gps_v = 45.0, 4.0
        db.execute("INSERT OR REPLACE INTO gps_samples VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (t, lat, lon, 20.0, gps_v, 0, 9, 0.9, "3D", wheel))
        t += 2.0
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
    db = Store(__import__("pathlib").Path(path)).db
    now = time.time()
    end = write_ride(db, now - 3 * 3600, laps=2, vmax=38, spin=(300, 330))
    write_ride(db, end + 1800, laps=1, vmax=25)      # second ride after a 30 min break
    write_samples(db, now - 6 * 3600, 6)
    db.commit()
    print("demo-turer skrivna till", path)
