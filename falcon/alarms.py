"""Ride alarms and safety margin.

Safety margin = 100 % − PWM: how much motor headroom is left. When PWM reaches 100 % the
controller can no longer hold the rider up (the classic EUC "cut-out"), so margin is the
most important number while riding. A linear fit over the last PREDICT_WINDOW_S seconds
gives a PREDICT_AHEAD_S forecast so the warning comes before the limit, not at it.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

PREDICT_WINDOW_S = 3.0
PREDICT_AHEAD_S = 3.0

DEFAULTS = {
    "pwm_warn": 70.0, "pwm_alarm": 80.0,
    "speed_warn": None,                       # km/h, set by the rider
    "motor_temp_warn": 70.0, "motor_temp_alarm": 85.0,
    "board_temp_warn": 60.0, "board_temp_alarm": 70.0,
    "battery_warn_pct": 20.0, "battery_alarm_pct": 10.0,
    "cell_min_warn_v": 3.40, "cell_min_alarm_v": 3.30,
}


@dataclass
class Alarm:
    level: str        # warn | alarm
    code: str
    text: str
    value: float | None = None


def _fit_ahead(points: list[tuple[float, float]], ahead: float) -> float | None:
    if len(points) < 3:
        return None
    t0 = points[0][0]
    xs = [p[0] - t0 for p in points]
    ys = [p[1] for p in points]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return my + slope * (xs[-1] + ahead - mx)


class RideAlarms:
    def __init__(self, config: dict | None = None):
        self.cfg = {**DEFAULTS, **(config or {})}
        self.pwm_hist: deque = deque()
        self.active: list[Alarm] = []
        self.last_pwm: float | None = None
        self.predicted_pwm: float | None = None

    def update(self, snap: dict, wheel_pct: int | None, now: float | None = None) -> list[Alarm]:
        now = time.time() if now is None else now
        c = self.cfg
        p0, p7 = snap.get("p0") or {}, snap.get("p7") or {}
        out: list[Alarm] = []

        pwm = p7.get("pwm_pct")
        if pwm is not None:
            self.last_pwm = float(pwm)
            self.pwm_hist.append((now, float(pwm)))
            while self.pwm_hist and now - self.pwm_hist[0][0] > PREDICT_WINDOW_S:
                self.pwm_hist.popleft()
            self.predicted_pwm = _fit_ahead(list(self.pwm_hist), PREDICT_AHEAD_S)
            worst = max(pwm, self.predicted_pwm or 0)
            if worst >= c["pwm_alarm"]:
                out.append(Alarm("alarm", "pwm", f"PWM {pwm:.0f} % – säkerhetsmarginal {100 - pwm:.0f} %"
                                 + (f" (om 3 s: {self.predicted_pwm:.0f} %)" if self.predicted_pwm and self.predicted_pwm > pwm else "")
                                 + " – SAKTA IN", pwm))
            elif worst >= c["pwm_warn"]:
                out.append(Alarm("warn", "pwm", f"PWM {pwm:.0f} % – säkerhetsmarginal {100 - pwm:.0f} %", pwm))

        spd = p0.get("speed_kmh")
        if c.get("speed_warn") and spd is not None and spd >= c["speed_warn"]:
            out.append(Alarm("warn", "speed", f"Fart {spd:.0f} km/h (gräns {c['speed_warn']:.0f})", spd))

        for code, val, warn, alarm, label in (
                ("motor_temp", p7.get("motor_temp_c"), c["motor_temp_warn"], c["motor_temp_alarm"], "Motortemperatur"),
                ("board_temp", p0.get("board_temp_c"), c["board_temp_warn"], c["board_temp_alarm"], "Moderkortets temperatur")):
            if val is None:
                continue
            if val >= alarm:
                out.append(Alarm("alarm", code, f"{label} {val:.0f} °C", val))
            elif val >= warn:
                out.append(Alarm("warn", code, f"{label} {val:.0f} °C", val))

        if wheel_pct is not None:
            if wheel_pct <= c["battery_alarm_pct"]:
                out.append(Alarm("alarm", "battery", f"Batteri {wheel_pct} %", wheel_pct))
            elif wheel_pct <= c["battery_warn_pct"]:
                out.append(Alarm("warn", "battery", f"Batteri {wheel_pct} %", wheel_pct))

        cells = [v for s in (snap.get("cells") or {}).values() for v in s.get("cells_mv", [])]
        if cells:
            lo = min(cells) / 1000
            if lo <= c["cell_min_alarm_v"]:
                out.append(Alarm("alarm", "cell_min", f"Lägsta cell {lo:.2f} V", lo))
            elif lo <= c["cell_min_warn_v"]:
                out.append(Alarm("warn", "cell_min", f"Lägsta cell {lo:.2f} V", lo))

        self.active = out
        return out

    def report(self) -> dict:
        return {"active": [a.__dict__ for a in self.active], "config": self.cfg,
                "pwm": self.last_pwm,
                "safety_margin_pct": round(100 - self.last_pwm, 1) if self.last_pwm is not None else None,
                "predicted_pwm_3s": round(self.predicted_pwm, 1) if self.predicted_pwm is not None else None}
