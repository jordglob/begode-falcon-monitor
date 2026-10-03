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

    def export(self) -> dict:
        return {k: [f.t, f.n, f.si, f.sv, f.sii, f.siv, f.svv] for k, f in self.fits.items() if f.n}

    def restore(self, data: dict | None) -> None:
        for k, v in (data or {}).items():
            f = self.fits.setdefault(k, _Fit())
            f.t, f.n, f.si, f.sv, f.sii, f.siv, f.svv = v

    def resistance(self, key: str) -> float | None:
        f = self.fits.get(key)
        r = f.result() if f else None
        return r["mohm"] if r and r["mohm"] > 0 else None

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


class EnergyCounter:
    """Battery energy integrated on every packet (0.3 s): P = bus voltage × battery current.
    Cumulative and monotonic: energy between two moments = counter difference, exact
    regardless of how sparsely positions are stored. Consumption and regeneration apart."""

    MAX_DT = 1.0      # a longer gap is not integrated (counted as missing coverage)

    def __init__(self):
        self.wh_out = 0.0
        self.wh_regen = 0.0
        self.last_ts: float | None = None
        self._iv_pwm_max = None
        self._iv_i_sum = 0.0
        self._iv_dt = 0.0
        self._iv_start: float | None = None
        self._reset_extremes()

    def _reset_extremes(self) -> None:
        self._iv_i_max = None          # highest discharge current, A
        self._iv_p_max = None          # highest consumption power, W
        self._iv_regen_max = None      # highest regeneration power, W (positive number)
        self._iv_v_min = None          # lowest bus voltage (sag under load), V

    def add(self, ts: float, volt: float | None, amps: float | None, pwm: float | None) -> None:
        if volt is None or amps is None:
            return
        if self._iv_start is None:
            self._iv_start = ts
        if self.last_ts is not None:
            dt = ts - self.last_ts
            if 0 < dt <= self.MAX_DT:
                wh = volt * amps * dt / 3600.0
                if wh >= 0:
                    self.wh_out += wh
                else:
                    self.wh_regen += -wh
                self._iv_i_sum += amps * dt
                self._iv_dt += dt
        self.last_ts = ts
        if pwm is not None:
            self._iv_pwm_max = pwm if self._iv_pwm_max is None else max(self._iv_pwm_max, pwm)
        p = volt * amps
        self._iv_i_max = amps if self._iv_i_max is None else max(self._iv_i_max, amps)
        if p >= 0:
            self._iv_p_max = p if self._iv_p_max is None else max(self._iv_p_max, p)
        else:
            self._iv_regen_max = -p if self._iv_regen_max is None else max(self._iv_regen_max, -p)
        self._iv_v_min = volt if self._iv_v_min is None else min(self._iv_v_min, volt)

    def take_interval(self, now: float) -> dict:
        """Counters + interval stats since the previous call (stored with each GPS point)."""
        span = now - self._iv_start if self._iv_start else 0.0
        out = {"wh_out_cum": round(self.wh_out, 4), "wh_regen_cum": round(self.wh_regen, 4),
               "pwm_max": self._iv_pwm_max,
               "current_avg": round(self._iv_i_sum / self._iv_dt, 2) if self._iv_dt else None,
               "data_cov": round(min(1.0, self._iv_dt / span), 2) if span > 0 else 0.0,
               "current_max": round(self._iv_i_max, 2) if self._iv_i_max is not None else None,
               "power_max_w": round(self._iv_p_max) if self._iv_p_max is not None else None,
               "regen_max_w": round(self._iv_regen_max) if self._iv_regen_max is not None else None,
               "volt_min": round(self._iv_v_min, 2) if self._iv_v_min is not None else None}
        self._iv_pwm_max, self._iv_i_sum, self._iv_dt, self._iv_start = None, 0.0, 0.0, now
        self._reset_extremes()
        return out
