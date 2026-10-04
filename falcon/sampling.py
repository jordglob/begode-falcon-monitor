"""When was a cell value measured? Rules for comparing cell voltages under load.

Found in the raw frames of 2026-10-03 (2660 cell frames):

* Each BMS measures its 24 cells as two half-packs of 12 (cells 1-12 and 13-24), each half at
  its own instant. Inside a half the cells agree within a few mV; across the seam between cell
  12 and 13 they differed by up to 189 mV while the load was changing. The seam runs through
  the middle of cell bank 1 (cells 9-16), so "spread inside one bank" mixes two instants.
* A cell frame is often a repeat of the previous one (the BMS had not measured again), and
  nothing in the frame says when the measurement was taken.

So a cell value is only compared with another one, or paired with a current, when
  1. both belong to the same half-pack, or the current has been steady long enough that the
     unknown measuring instant does not matter, and
  2. the frame is a new measurement, not a repeat.
"""
from __future__ import annotations

from collections import deque

HALF = 12            # cells per half-pack = one measuring instant
STEADY_S = 4.0       # the current must have been steady this long ...
STEADY_A = 4.0       # ... within this range (max - min), about 15 mV per cell on this pack
REST_A = 1.0         # below this the pack is resting
REST_S = 8.0         # resting this long before voltages are compared across the whole pack
KEEP_S = 30.0
GAP_S = 2.0          # packets come every 0.3 s; a longer hole means the link was down


# findings and alarms that were measuring artifacts before the rules above were applied
ARTIFACT_CODES = {"cell", "bank", "string", "load_spread", "load_comp_cell", "load_comp_bank",
                  "shunt_ratio", "shunt_step", "shunt_sum", "pack_dropout"}
ARTIFACT_BEEP_KEYS = {"paket som inte bär ström"}


def pre_fix(ts: float | None, fix_ts: float | None) -> bool:
    """Recorded before the corrected version was running on this installation."""
    return bool(ts is not None and fix_ts and ts < fix_ts)


def half_of(cell_no: int) -> int:
    """1 or 2 for a cell number 1-24 within its string."""
    return 1 if cell_no <= HALF else 2


def segments(bank: int, cells_mv: list[int]) -> list[tuple[int, int, list[int]]]:
    """Split one cell frame (bank 0-2, 8 cells) at the half-pack seam.
    -> [(half, first cell number, values)], each part measured at one instant."""
    first = bank * 8 + 1
    cut = HALF - first + 1                       # cells of this frame that belong to half 1
    if cut <= 0:
        return [(2, first, list(cells_mv))]
    if cut >= len(cells_mv):
        return [(1, first, list(cells_mv))]
    return [(1, first, list(cells_mv[:cut])), (2, first + cut, list(cells_mv[cut:]))]


def simultaneous_spread(cells_mv: list[int]) -> int | None:
    """Largest max-min inside any half-pack of one string (24 cells)."""
    parts = [cells_mv[i:i + HALF] for i in range(0, len(cells_mv), HALF)]
    parts = [p for p in parts if len(p) >= 2]
    return max(max(p) - min(p) for p in parts) if parts else None


class CurrentTrace:
    """Recent pack current (controller, packet rate) -> is it steady, is the pack resting?"""

    def __init__(self):
        self.h: deque = deque()

    def add(self, ts: float, amps: float | None) -> None:
        if amps is None:
            return
        if self.h and ts <= self.h[-1][0]:
            return
        self.h.append((ts, amps))
        while self.h and ts - self.h[0][0] > KEEP_S:
            self.h.popleft()

    def _window(self, ts: float, span: float) -> list[float] | None:
        """Values of the last `span` seconds, or None when the history does not cover them."""
        vals = [i for t, i in self.h if ts - span <= t <= ts]
        if len(vals) < 3 or not self.h or ts - self.h[0][0] < span * 0.75:
            return None
        newest = max(t for t, _ in self.h if t <= ts)
        return vals if ts - newest <= 1.5 else None

    def steady(self, ts: float) -> float | None:
        """Mean current when it stayed within STEADY_A for STEADY_S, else None."""
        vals = self._window(ts, STEADY_S)
        if vals is None or max(vals) - min(vals) > STEADY_A:
            return None
        return sum(vals) / len(vals)

    def peak(self, ts: float) -> float | None:
        """Highest absolute current of the last STEADY_S seconds (None without history)."""
        vals = [abs(i) for t, i in self.h if ts - STEADY_S <= t <= ts]
        return max(vals) if vals else None

    def rest_s(self, ts: float) -> float | None:
        """Seconds the pack has rested without interruption up to ts; None without history."""
        if not self.h:
            return None
        start, nxt = None, ts
        for t, i in reversed(self.h):
            if t > ts:
                continue
            if abs(i) >= REST_A or nxt - t > GAP_S:      # a hole in the data is not known rest
                break
            start = nxt = t
        return 0.0 if start is None else ts - start


class NewFrames:
    """True when a cell frame carries a new measurement (differs from the previous frame of the
    same bank). A repeat says nothing new and would be paired with the wrong moment."""

    def __init__(self):
        self.last: dict = {}

    def is_new(self, key, cells_mv: list[int]) -> bool:
        cells = tuple(cells_mv)
        new = self.last.get(key) != cells
        self.last[key] = cells
        return new
