"""Record what the running app sees, twice a second, for a whole ride or charge.

    python -m tools.record_state OUT.jsonl [max_hours]

One JSON line per poll: link state, controller current, the four BMS rows, all cell voltages,
speed and PWM. The app itself only stores 5 s samples and black boxes around alarms; this keeps
everything, so link gaps and BMS row values can be examined afterwards. Stop: delete OUT.jsonl.run
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path


def main(out_path: str, max_hours: float = 4.0, base: str = "http://localhost:8096") -> None:
    run = Path(out_path + ".run")
    run.write_text(str(time.time()))
    t0 = time.time()
    with open(out_path, "a", buffering=1) as out:
        while time.time() - t0 < max_hours * 3600 and run.exists():
            now = time.time()
            try:
                with urllib.request.urlopen(base + "/api/state", timeout=3) as r:
                    st = json.load(r)
                sn, link = st["snapshot"], st["link"]
                p0, p7 = sn.get("p0") or {}, sn.get("p7") or {}
                rows = {k: [v.get("current_a"), v.get("activity"), v.get("voltage_v"), v.get("temp_a_c"),
                            v.get("temp_b_c")] for k, v in (sn.get("groups") or {}).items()}
                out.write(json.dumps({
                    "t": round(now, 2), "status": link["status"], "conn": link["connected"],
                    "age": link.get("last_frame_age_s"), "rssi": link.get("rssi_dbm"),
                    "drops": sum(link["stability"]["drops"].values()), "frames": sum((sn.get("counts") or {}).values()),
                    "p7_i": p7.get("battery_current_a"), "pwm": p7.get("pwm_pct"), "speed": p0.get("speed_kmh"),
                    "bus_raw": p0.get("voltage_raw"), "app_i": sn.get("battery_current_a"), "rows": rows,
                    "cells": {s: c.get("cells_mv") for s, c in (sn.get("cells") or {}).items()}}) + "\n")
            except Exception as e:  # the recorder must outlive app restarts and timeouts
                out.write(json.dumps({"t": round(now, 2), "err": str(e)[:80]}) + "\n")
            time.sleep(0.5)
    run.unlink(missing_ok=True)


if __name__ == "__main__":
    main(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 4.0)
