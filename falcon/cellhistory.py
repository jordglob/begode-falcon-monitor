"""Per-cell internal-resistance history by temperature and load.

A cell's resistance depends on temperature (much higher when cold), on load (current level and
how long it lasts) and on age. Mixing them hides what matters, so every cell sample is filed
by DAY × TEMPERATURE BIN (5 °C) × LOAD BIN (string current) and a regression V = OCV − R·I
is kept per bin. Comparing the same bins over weeks shows ageing; comparing bins shows how a
cell behaves cold or under hard load; a cell that departs from its neighbours in the same bin
is suspicious. Stored in SQLite (survives restarts).
"""
from __future__ import annotations

import math
import time

LOAD_BINS = [(0, 15, "0–15 A"), (15, 35, "15–35 A"), (35, 999, "35+ A")]   # per string
MIN_N = 15
MIN_I_STD = 1.0

SCHEMA = """CREATE TABLE IF NOT EXISTS cell_ir_bins (day TEXT, cell TEXT, tbin INTEGER, lbin TEXT,
  n REAL, si REAL, sv REAL, sii REAL, siv REAL, PRIMARY KEY (day, cell, tbin, lbin))"""


def load_bin(i_string: float) -> str:
    a = abs(i_string)
    return next(lbl for lo, hi, lbl in LOAD_BINS if lo <= a < hi)


def fit(n, si, sv, sii, siv) -> float | None:
    if n < MIN_N:
        return None
    var = sii / n - (si / n) ** 2
    if var < MIN_I_STD ** 2:
        return None
    slope = (siv / n - si / n * sv / n) / var          # mV per A (string)
    r = -slope
    return round(r, 3) if 0 < r < 100 else None


class CellHistory:
    def __init__(self, db):
        self.db = db
        self.db.execute(SCHEMA)
        self.db.commit()
        self.acc: dict = {}
        self.dirty: set = set()

    def add(self, ts: float, cell: str, i_string: float | None, mv: float, temp_c: float | None) -> None:
        if i_string is None or temp_c is None:
            return
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        key = (day, cell, int(math.floor(temp_c / 5) * 5), load_bin(i_string))
        a = self.acc.get(key)
        if a is None:
            row = self.db.execute("SELECT n, si, sv, sii, siv FROM cell_ir_bins WHERE day=? AND cell=? AND tbin=? "
                                  "AND lbin=?", key).fetchone()
            a = self.acc[key] = list(row) if row else [0.0, 0.0, 0.0, 0.0, 0.0]
        i = abs(i_string)
        a[0] += 1; a[1] += i; a[2] += mv; a[3] += i * i; a[4] += i * mv
        self.dirty.add(key)

    def flush(self) -> int:
        for key in self.dirty:
            self.db.execute("INSERT OR REPLACE INTO cell_ir_bins VALUES (?,?,?,?,?,?,?,?,?)", (*key, *self.acc[key]))
        n = len(self.dirty)
        self.dirty.clear()
        if n:
            self.db.commit()
        return n

    def report(self, model=None, days: int = 365) -> dict:
        """Per cell: resistance per temperature bin (all loads), per load bin, and a daily
        trend normalised to 25 °C (needs the R(T) model)."""
        since = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
        rows = self.db.execute("SELECT day, cell, tbin, lbin, n, si, sv, sii, siv FROM cell_ir_bins WHERE day >= ?",
                               (since,)).fetchall()
        by_t: dict = {}
        by_l: dict = {}
        trend: dict = {}
        for day, cell, tbin, lbin, *acc in rows:
            for d, k in ((by_t, (cell, tbin)), (by_l, (cell, lbin)), (trend, (cell, day, tbin))):
                s = d.setdefault(k, [0.0] * 5)
                for j in range(5):
                    s[j] += acc[j]
        cells = sorted({c for c, _ in by_t} | {c for c, _ in by_l}, key=lambda c: (c[0], int(c[1:])))
        tbins = sorted({t for _, t in by_t})
        out = {"cells": [], "tbins": tbins, "lbins": [l for _, _, l in LOAD_BINS]}
        for c in cells:
            rt = {t: fit(*by_t[(c, t)]) for t in tbins if (c, t) in by_t}
            rl = {l: fit(*by_l[(c, l)]) for l in out["lbins"] if (c, l) in by_l}
            tr = {}
            if model is not None:
                for (cc, day, tbin), acc in trend.items():
                    if cc != c:
                        continue
                    r = fit(*acc)
                    if r:
                        tr.setdefault(day, []).append(r * model.scale(tbin + 2.5, 25.0))
            out["cells"].append({"cell": c, "by_temp": rt, "by_load": rl,
                                 "r25_by_day": {d: round(sum(v) / len(v), 3) for d, v in sorted(tr.items())}})
        # cells departing from the median in the same temperature bin
        flags = []
        for t in tbins:
            vals = [(x["cell"], x["by_temp"].get(t)) for x in out["cells"] if x["by_temp"].get(t)]
            if len(vals) >= 8:
                med = sorted(v for _, v in vals)[len(vals) // 2]
                flags += [{"cell": c, "tbin": t, "r": v, "median": med} for c, v in vals if v > 1.5 * med]
        out["flags"] = flags
        return out
