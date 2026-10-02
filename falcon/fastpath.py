"""Per-packet analysis — as fast as the wheel actually sends.

The wheel sends a fixed round every 0.3 s: p0 + p4 + p7 + one BMS group + one cell
bank (8 cells). So:
  * bus (controller voltage/current, p0)  -> every 0.3 s  -> BusTracker
  * each cell                              -> every 1.8 s  -> CellRegression

BusTracker: voltage sag per ampere from p0 (the controller's own measurement,
closest to the bus capacitors), recomputed on every p0 packet.

CellRegression: every cell value is time-stamped on arrival and paired with the
battery current at that instant. Per cell, V = OCV - R * I_string is fitted over
many samples with exponential forgetting -> internal resistance per cell, without
needing current steps. Two strings in parallel -> I_string = I / 2.

Current: battery current from p7 (-raw/100 A, negative = charging), per WheelLog and
the Home Assistant begode integration. p0 word 5 is PHASE current, not used here.
"""
from __future__ import annotations

import math
import statistics
import time
from collections import deque

from .energy import SagEstimator, bus_index

HALF_LIFE_S = 600.0
MIN_I_STD = 1.5        # A of current spread needed before a cell fit counts
MIN_N = 20


class BusTracker:
    """Bus voltage from p0 + battery current from p7 (both every 0.3 s)."""

    def __init__(self):
        self.sag = SagEstimator()
        self.last: dict = {}
        self.updates = 0

    def bus_v(self, p0: dict) -> float | None:
        raw = p0.get("voltage_raw")
        return raw / 100.0 * 1.5 if raw else None          # 16S-scaled -> 24S

    def current(self, p7: dict) -> float | None:
        return p7.get("battery_current_a")

    def on_p0(self, p0: dict, p7: dict, ts: float, baseline_ohm: float | None) -> dict:
        v, i = self.bus_v(p0), self.current(p7)
        if v is None or i is None:
            return self.last
        self.sag.add(i, v, ts)
        est = self.sag.estimate()
        self.updates += 1
        self.last = {"ts": ts, "bus_v": round(v, 2), "current_a": round(i, 2),
                     "phase_current_a": p0.get("phase_current_a"),
                     "sag": est, "index_guess": bus_index(est.get("ohm"), baseline_ohm),
                     "current_source": "paket 7 (batteriström)", "updates": self.updates}
        return self.last


class _Fit:
    __slots__ = ("t", "n", "si", "sv", "sii", "siv", "svv", "last_ts", "last_mv")

    def __init__(self):
        self.t = None
        self.n = self.si = self.sv = self.sii = self.siv = self.svv = 0.0
        self.last_ts = None
        self.last_mv = None

    def add(self, ts: float, i: float, v: float) -> None:
        if self.t is not None:
            w = math.pow(0.5, max(ts - self.t, 0.0) / HALF_LIFE_S)
            self.n *= w; self.si *= w; self.sv *= w
            self.sii *= w; self.siv *= w; self.svv *= w
        self.t = ts
        self.n += 1; self.si += i; self.sv += v
        self.sii += i * i; self.siv += i * v; self.svv += v * v

    def result(self) -> dict | None:
        if self.n < MIN_N:
            return None
        var_i = self.sii / self.n - (self.si / self.n) ** 2
        if var_i < MIN_I_STD ** 2:
            return None
        slope = (self.siv / self.n - self.si / self.n * self.sv / self.n) / var_i   # mV per A (battery)
        return {"mohm": round(-2.0 * slope, 2), "n": round(self.n, 1),
                "i_std": round(math.sqrt(var_i), 2)}


class CellRegression:
    def __init__(self):
        self.fits: dict[str, _Fit] = {}

    def on_bank(self, ts: float, string: str, bank: int, cells_mv: list[int],
                current_a: float | None) -> None:
        for j, mv in enumerate(cells_mv):
            key = f"{string}{bank * 8 + j + 1}"
            f = self.fits.setdefault(key, _Fit())
            f.last_ts, f.last_mv = ts, mv
            if current_a is not None:
                f.add(ts, current_a, mv)

    def report(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        cells = {}
        for k, f in self.fits.items():
            r = f.result() or {}
            cells[k] = {"mv": f.last_mv, "age_s": round(now - f.last_ts, 1) if f.last_ts else None, **r}
        vals = [c["mohm"] for c in cells.values() if c.get("mohm") is not None]
        flag = []
        if len(vals) >= 8:
            med = statistics.median(vals)
            flag = sorted([k for k, c in cells.items() if c.get("mohm") and c["mohm"] > 1.5 * med],
                          key=lambda k: -cells[k]["mohm"])
        return {"cells": cells, "flag": flag, "fitted": len(vals)}
