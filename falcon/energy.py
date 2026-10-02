"""Energy-storage view: battery strings, BMS groups, and the indirect bus/capacitor
indicator (voltage sag per ampere under load).

The protocol has no capacitor data. The sag estimate is the combined internal
resistance of battery + wiring + bus as seen by the controller — a trend
indicator, not a capacitor measurement.
"""
from __future__ import annotations

import time
from collections import deque

MIN_LOAD_A = 2.0      # ignore samples below this current
MIN_SPAN_A = 3.0      # need this much current spread for a usable slope
WINDOW_S = 120


class SagEstimator:
    def __init__(self) -> None:
        self.samples: deque = deque()  # (ts, current_a, voltage_v)

    def add(self, current_a: float, voltage_v: float, ts: float | None = None) -> None:
        ts = time.time() if ts is None else ts
        self.samples.append((ts, current_a, voltage_v))
        while self.samples and ts - self.samples[0][0] > WINDOW_S:
            self.samples.popleft()

    def estimate(self) -> dict:
        pts = [(i, v) for _, i, v in self.samples if abs(i) >= MIN_LOAD_A]
        if len(pts) < 10:
            return {"ohm": None, "reason": "kräver belastning (kör en stund)", "n": len(pts)}
        cur = [p[0] for p in pts]
        if max(cur) - min(cur) < MIN_SPAN_A:
            return {"ohm": None, "reason": "för liten variation i ström", "n": len(pts)}
        n = len(pts)
        mi = sum(cur) / n
        mv = sum(p[1] for p in pts) / n
        sxx = sum((i - mi) ** 2 for i in cur)
        sxy = sum((i - mi) * (v - mv) for i, v in pts)
        slope = sxy / sxx            # dV/dI, negative under discharge
        return {"ohm": round(-slope, 4), "mv_per_a": round(-slope * 1000, 1),
                "ocv_v": round(mv - slope * mi, 2), "n": n, "reason": None}


# Typical Li-ion NMC open-circuit voltage -> state of charge (per cell, at rest).
OCV_TABLE = [(3.00, 0), (3.45, 5), (3.55, 10), (3.62, 20), (3.68, 30), (3.74, 40),
             (3.80, 50), (3.87, 60), (3.95, 70), (4.03, 80), (4.10, 90), (4.20, 100)]


def soc_from_cell_v(v: float) -> float:
    """Guess: valid only when the wheel is at rest (no load sag)."""
    if v <= OCV_TABLE[0][0]:
        return 0.0
    for (v0, s0), (v1, s1) in zip(OCV_TABLE, OCV_TABLE[1:]):
        if v <= v1:
            return round(s0 + (s1 - s0) * (v - v0) / (v1 - v0), 1)
    return 100.0


def bus_index(sag_ohm: float | None, baseline_ohm: float | None) -> float | None:
    """Guess: 100 = as good as the first measurements, lower = more sag than then."""
    if not sag_ohm or not baseline_ohm:
        return None
    return round(100.0 * baseline_ohm / sag_ohm, 1)


def energy_view(snapshot: dict, sag: dict, link_info: dict,
                baseline_ohm: float | None = None) -> dict:
    bms = snapshot.get("bms", {})
    groups = snapshot.get("groups", {})
    g0 = next(iter(groups.values()), {})
    cells = snapshot.get("cells", {})
    all_mv = [v for s in cells.values() for v in s["cells_mv"]]
    volt = g0.get("voltage_v")
    cur = snapshot.get("battery_current_a")
    mean_cell_v = (sum(all_mv) / len(all_mv) / 1000.0) if all_mv else None
    at_rest = abs(cur or 0) < 1.0
    temps = [t for b in bms.values() for t in b["temps_c"]]
    p0, p7 = snapshot.get("p0", {}), snapshot.get("p7", {})
    return {
        "battery": {
            "voltage_v": volt,
            "current_a": cur,
            "power_w": round(volt * cur, 0) if volt is not None and cur is not None else None,
            "temps_c": [min(temps), max(temps)] if temps else [None, None],
            "pwm_limit_or_alarm": g0.get("pwm_limit_or_alarm"),
            "strings": cells,
            "cell_min_mv": min(all_mv) if all_mv else None,
            "cell_max_mv": max(all_mv) if all_mv else None,
            "cell_spread_mv": (max(all_mv) - min(all_mv)) if all_mv else None,
            "bms": bms,
            "groups": groups,
            "soc_guess_pct": soc_from_cell_v(mean_cell_v) if mean_cell_v else None,
            "soc_guess_valid": at_rest,
            "mean_cell_v": round(mean_cell_v, 4) if mean_cell_v else None,
        },
        "bus": {
            "voltage_v": volt,
            "sag": sag,
            "note": "Indirekt: protokollet saknar kondensatordata. Spänningsfall per ampere = "
                    "batteri + kablage + buss. Följ trenden över tid.",
            "reference_esr": None,
            "baseline_ohm": baseline_ohm,
            "index_guess": bus_index(sag.get("ohm"), baseline_ohm),
        },
        "electronics": {
            "board_temp_c": p0.get("board_temp_c"),
            "motor_temp_c": p7.get("motor_temp_c"),
            "phase_current_a": p0.get("phase_current_a"),
            "battery_current_p7_a": p7.get("battery_current_a"),
            "pwm_pct": p7.get("pwm_pct"),
            "alerts": snapshot.get("p4", {}).get("alerts", []),
        },
        "link": link_info,
    }
