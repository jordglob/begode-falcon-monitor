"""Battery temperature vs available power, warm-up, hot spots and charging temperature.

Internal resistance rises steeply in the cold. R(T) is LEARNED from this pack's own data
(per-cell resistance from the regression × the BMS temperature sensors), fitted as
    ln R = a + b / T_kelvin            (Arrhenius)
Until enough data spans a temperature range, a cautious default is used: R doubles from
25 °C to 0 °C.

Available power (weakest cell decides):
    I_string_max = (V0 − V_MIN) / R(T)          V0 = weakest cell without load
    P_max        = 24 · V_MIN · I_string_max · 2 strings
Recommended peak = REC_SHARE · P_max (margin for a sudden jolt).
"""
from __future__ import annotations

import math
import time
from collections import deque

V_MIN_MV = 3200            # lowest cell voltage under load we plan for (above BMS cut-off)
REC_SHARE = 0.70
SERIES, STRINGS = 24, 2
DEFAULT_B = math.log(2) / (1 / 273.15 - 1 / 298.15)    # R(0 °C) = 2 × R(25 °C)
FIT_MIN_POINTS, FIT_MIN_SPAN_C = 20, 5.0
COLD_C, VERY_COLD_C = 10.0, 0.0
HOT_C, VERY_HOT_C = 45.0, 55.0
CHARGE_MIN_C, CHARGE_MAX_C = 5.0, 45.0
HOTSPOT_WARN_C = 6.0


def _k(c: float) -> float:
    return c + 273.15


class RModel:
    def __init__(self, points: list | None = None):
        self.points: deque = deque(points or [], maxlen=2000)     # (temp °C, R mΩ)
        self.a: float | None = None
        self.b = DEFAULT_B
        self.fitted = False
        self.refit()

    def add(self, temp_c: float, r_mohm: float) -> None:
        if r_mohm and r_mohm > 0 and -30 < temp_c < 80:
            self.points.append((round(temp_c, 1), round(r_mohm, 3)))
            if len(self.points) % 10 == 0:
                self.refit()

    def refit(self) -> None:
        pts = list(self.points)
        if not pts:
            return
        ts = [p[0] for p in pts]
        if len(pts) >= FIT_MIN_POINTS and max(ts) - min(ts) >= FIT_MIN_SPAN_C:
            xs = [1 / _k(t) for t in ts]
            ys = [math.log(r) for _, r in pts]
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            sxx = sum((x - mx) ** 2 for x in xs)
            if sxx > 0:
                b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
                if 500 < b < 10000:                    # physically sensible
                    self.b, self.a, self.fitted = b, my - b * mx, True
                    return
        # default slope through the median of the measured points
        rs = sorted(r for _, r in pts)
        tm = sorted(ts)[len(ts) // 2]
        self.a = math.log(rs[len(rs) // 2]) - self.b / _k(tm)
        self.fitted = False

    def r_at(self, temp_c: float) -> float | None:
        if self.a is None:
            return None
        return math.exp(self.a + self.b / _k(temp_c))

    def scale(self, from_c: float, to_c: float) -> float:
        return math.exp(self.b * (1 / _k(to_c) - 1 / _k(from_c)))

    def curve(self) -> list[list[float]]:
        return [[t, round(self.r_at(t), 3)] for t in range(-10, 51, 5)] if self.a is not None else []


def battery_temp(snap: dict) -> float | None:
    temps = [t for b in (snap.get("bms") or {}).values() for t in (b.get("temps_c") or []) if t is not None]
    return sum(temps) / len(temps) if temps else None


class Thermal:
    def __init__(self, model: RModel | None = None):
        self.model = model or RModel()
        self.temp_hist: deque = deque(maxlen=720)          # (ts, °C) – 1 h at 5 s
        self.last: dict = {}

    def learn(self, temp_c: float | None, cell_r: dict[str, float]) -> None:
        """One point per call: median measured cell resistance at the current temperature."""
        rs = sorted(r for r in cell_r.values() if r)
        if temp_c is None or len(rs) < 8:
            return
        self.model.add(temp_c, rs[len(rs) // 2])

    def evaluate(self, snap: dict, v0_weakest_mv: float | None, r_weakest_mohm: float | None,
                 r_measured_at_c: float | None, p_now_w: float | None, pwm_margin: float | None,
                 now: float | None = None) -> dict:
        now = time.time() if now is None else now
        t = battery_temp(snap)
        if t is not None:
            self.temp_hist.append((now, t))
        out = {"temp_c": round(t, 1) if t is not None else None, "notes": [], "level": "ok"}
        # resistance of the weakest cell at the current temperature
        r = None
        if r_weakest_mohm and r_measured_at_c is not None and t is not None:
            r = r_weakest_mohm * self.model.scale(r_measured_at_c, t)
        elif t is not None:
            r = self.model.r_at(t)
        out["r_now_mohm"] = round(r, 2) if r else None
        out["r25_mohm"] = round(r * self.model.scale(t, 25.0), 2) if r and t is not None else None
        if r and v0_weakest_mv and v0_weakest_mv > V_MIN_MV:
            i_string = (v0_weakest_mv - V_MIN_MV) / r           # mV / mΩ = A
            pmax = SERIES * (V_MIN_MV / 1000) * i_string * STRINGS
            out.update(pmax_w=round(pmax), prec_w=round(pmax * REC_SHARE), i_max_a=round(i_string * STRINGS))
            if t is not None:
                warm = SERIES * (V_MIN_MV / 1000) * (v0_weakest_mv - V_MIN_MV) / (r * self.model.scale(t, 25.0)) * STRINGS
                out["pmax_25c_w"] = round(warm)
        if p_now_w is not None and out.get("pmax_w"):
            out["share_pct"] = round(100 * max(p_now_w, 0) / out["pmax_w"])
            batt_margin = 100 - out["share_pct"]
            out["limiter"] = ("batteriet" if pwm_margin is None or batt_margin < pwm_margin else "motorn (PWM)")
            if out["share_pct"] >= 100:
                out["level"] = "alarm"
            elif out["share_pct"] >= 100 * REC_SHARE:
                out["level"] = "warn"
        # temperature notes
        if t is not None:
            if t < VERY_COLD_C:
                out["notes"].append(("alarm", f"Mycket kallt batteri ({t:.0f} °C): inre motståndet är flera gånger "
                                              f"det normala – kör mjukt, undvik hårda accelerationer."))
            elif t < COLD_C:
                out["notes"].append(("warn", f"Kallt batteri ({t:.0f} °C): mindre effekt tillgänglig de första "
                                             f"minuterna – kör mjukt tills det värmts upp."))
            elif t > VERY_HOT_C:
                out["notes"].append(("alarm", f"Mycket varmt batteri ({t:.0f} °C) – låt det svalna, åldras snabbt."))
            elif t > HOT_C:
                out["notes"].append(("warn", f"Varmt batteri ({t:.0f} °C) – snabbare åldrande, ladda inte förrän det svalnat."))
            warm_eta = self.warmup_eta(now, 15.0)
            if warm_eta is not None:
                out["warmup_min"] = warm_eta
        out["hotspot"] = self.hotspot(snap)
        if out["hotspot"]:
            out["notes"].append(("warn", out["hotspot"]))
        if any(n[0] == "alarm" for n in out["notes"]) and out["level"] != "alarm":
            out["level"] = "alarm"
        elif out["notes"] and out["level"] == "ok":
            out["level"] = "warn"
        self.last = out
        return out

    def warmup_eta(self, now: float, target_c: float) -> int | None:
        """Minutes until the battery reaches target_c at the current warming rate."""
        pts = [p for p in self.temp_hist if now - p[0] <= 600]
        if len(pts) < 10 or pts[-1][1] >= target_c:
            return None
        rate = (pts[-1][1] - pts[0][1]) / ((pts[-1][0] - pts[0][0]) / 60)      # °C per minute
        if rate <= 0.02:
            return None
        return round((target_c - pts[-1][1]) / rate)

    @staticmethod
    def hotspot(snap: dict) -> str | None:
        """One BMS (or one sensor) clearly warmer than the rest – e.g. an overheating shunt
        or a bad connection."""
        bms = snap.get("bms") or {}
        maxes = {k: b.get("temp_max_c") for k, b in bms.items() if b.get("temp_max_c") is not None}
        if len(maxes) == 2:
            hot = max(maxes, key=maxes.get)
            diff = maxes[hot] - min(maxes.values())
            if diff >= HOTSPOT_WARN_C:
                return (f"Varmpunkt: BMS {hot} är {diff:.0f} °C varmare än det andra paketet – kan vara en "
                        f"överhettad shunt eller dålig förbindelse.")
        for k, b in bms.items():
            ts = [x for x in (b.get("temps_c") or []) if x is not None]
            if len(ts) >= 3:
                s = sorted(ts)
                if s[-1] - s[len(s) // 2] >= HOTSPOT_WARN_C + 2:
                    return f"Varmpunkt i BMS {k}: en givare visar {s[-1]:.0f} °C mot {s[len(s) // 2]:.0f} °C för de andra."
        return None


def charge_temp_ok(temp_c: float | None) -> tuple[bool, str | None]:
    if temp_c is None:
        return True, None
    if temp_c < CHARGE_MIN_C:
        return False, f"Batteriet är {temp_c:.0f} °C – ladda inte under {CHARGE_MIN_C:.0f} °C (risk för litiumplätering)."
    if temp_c > CHARGE_MAX_C:
        return False, f"Batteriet är {temp_c:.0f} °C – ladda inte över {CHARGE_MAX_C:.0f} °C, låt det svalna."
    return True, None
