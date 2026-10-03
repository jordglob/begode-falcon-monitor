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


def write_ride(db, t0, laps, vmax):
    pts = interp(LOOP, 60) * laps
    t = t0
    for i, (lat, lon) in enumerate(pts):
        # speed profile: accelerate, cruise with waves, brake at the end
        x = i / len(pts)
        v = vmax * min(1, x * 8, (1 - x) * 8) * (0.75 + 0.25 * math.sin(i / 25))
        db.execute("INSERT OR REPLACE INTO gps_samples VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (t, lat, lon, 20.0, v * 0.97, 0, 9, 0.9, "3D", v if i % 4 else None))
        t += 2.0
    return t


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/claude-falcon-demo.db"
    db = Store(__import__("pathlib").Path(path)).db
    now = time.time()
    end = write_ride(db, now - 3 * 3600, laps=2, vmax=38)
    write_ride(db, end + 1800, laps=1, vmax=25)      # second ride after a 30 min break
    db.commit()
    print("demo-turer skrivna till", path)
