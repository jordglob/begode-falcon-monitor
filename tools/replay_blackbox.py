"""Replay recorded frames through the guard the way the server feeds it.

    python -m tools.replay_blackbox ~/.local/share/begode-falcon/blackbox/*.json

Prints the findings (warn/alarm) per code. Used to check guard changes against real rides.
"""
from __future__ import annotations

import collections
import json
import sys

from falcon.fastpath import CellRegression
from falcon.guard import Guard
from falcon.protocol import Frame, WheelState
from falcon.sampling import NewFrames


def replay(frames, guard: Guard | None = None) -> tuple[collections.Counter, Guard]:
    """frames: iterable of (ts, type, sub, hex) sorted by ts -> Counter[(level, code)]."""
    state, guard, reg, seen = WheelState(), guard or Guard(), CellRegression(), NewFrames()
    found: collections.Counter = collections.Counter()
    last = None
    for ts, typ, sub, hx in frames:
        f = Frame(typ, sub, bytes.fromhex(hx))
        state.apply(f)
        if typ == 7:
            guard.load_spread.trace.add(ts, state.p7.get("battery_current_a"))
        elif typ in (2, 3):
            string, cells = "A" if typ == 2 else "B", list(f.u16())
            guard.load_spread.add(ts, string, sub, cells, state.battery_current(), r_lookup=reg.resistance)
            i = guard.load_spread.trace.steady(ts) if seen.is_new((string, sub), cells) else None
            reg.on_bank(ts, string, sub, cells, i)
        if (last is None or ts - last >= 1.0) and len(state.groups) == 4 and state.p7:
            last = ts
            for x in guard.update(state.snapshot(), now=ts):
                if x.level in ("warn", "alarm"):
                    found[(x.level, x.code)] += 1
    return found, guard


def load(paths) -> list[tuple]:
    seen, out = set(), []
    for p in paths:
        d = json.load(open(p))
        for f in d["frames"]:
            k = (round(d["ts"] + f["t"], 2), f["type"], f["sub"])
            if k not in seen:
                seen.add(k)
                out.append((*k, f["hex"]))
    return sorted(out)


if __name__ == "__main__":
    found, g = replay(load(sys.argv[1:]))
    for (level, code), n in sorted(found.items(), key=lambda x: -x[1]):
        print(f"{n:5d}  {level:5s} {code}")
    ls = g.load_spread
    print(f"cell frames used {ls.used}, skipped (current not steady) {ls.skipped}, baseline {ls.baseline}")
