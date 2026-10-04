"""Safe speed right now: how fast before the safety margin (100 - PWM) is used up.

The controller reaches a speed by putting a share (PWM) of the battery voltage on the motor.
What the motor needs is, to a good approximation, linear in speed and current:

    motor voltage = KE * speed + R * current + C          PWM = motor voltage / bus voltage

Fitted on this wheel 2026-10-03/04 (6367 points, 88-98 V): KE 1.20 V per km/h, R 0.37 V per A,
C 1.7 V, R^2 0.974, +-2 PWM points; the fit from one day predicted the other, lower-voltage
day with -0.5 points mean error. The bus voltage itself drops with load (pack + wiring
resistance) and with the state of charge, so the same speed costs more margin on a lower
battery. Below ~88 V this is the model, not a measurement.
"""
from __future__ import annotations

KE_V_PER_KMH = 1.202
R_V_PER_A = 0.371
C_V = 1.68
RP_OHM = 0.089                 # voltage sag per ampere at the bus, measured 2026-10-04 at ~20 C
CRUISE_A_PER_KMH2 = 0.0105     # steady riding on the flat: ~10 A at 30 km/h, ~17 A at 40 km/h
HARD_LOAD_A = 40.0             # acceleration, a climb or a bump at speed
MARGIN_PCT = 20.0              # 80 % PWM: where the wheel starts its fast beeping
MIN_FIT_N = 1000


class PwmModel:
    """The three coefficients, re-learned from the wheel's own packets (least squares on running
    sums). Until enough moving samples exist, or if the fit looks implausible, the measured
    defaults above are used."""

    def __init__(self, sums: list | None = None):
        self.s = list(sums) if sums and len(sums) == 10 else [0.0] * 10

    def add(self, speed_kmh, current_a, bus_v, pwm_pct) -> None:
        if None in (speed_kmh, current_a, bus_v, pwm_pct) or speed_kmh < 5 or bus_v < 40:
            return
        x1, x2, y = float(speed_kmh), float(current_a), pwm_pct * bus_v / 100.0
        s = self.s
        s[0] += 1; s[1] += x1; s[2] += x2; s[3] += x1 * x1; s[4] += x1 * x2
        s[5] += x2 * x2; s[6] += y; s[7] += x1 * y; s[8] += x2 * y; s[9] += y * y

    def export(self) -> list:
        return list(self.s)

    def coefficients(self) -> tuple[float, float, float, bool]:
        """(KE, R, C, learned)"""
        n, s1, s2, s11, s12, s22, sy, s1y, s2y, _ = self.s
        if n >= MIN_FIT_N:
            a = [[s11, s12, s1, s1y], [s12, s22, s2, s2y], [s1, s2, n, sy]]
            try:
                for i in range(3):
                    p = max(range(i, 3), key=lambda k: abs(a[k][i]))
                    a[i], a[p] = a[p], a[i]
                    for k in range(i + 1, 3):
                        f = a[k][i] / a[i][i]
                        a[k] = [u - f * v for u, v in zip(a[k], a[i])]
                c = a[2][3] / a[2][2]
                r = (a[1][3] - a[1][2] * c) / a[1][1]
                ke = (a[0][3] - a[0][1] * r - a[0][2] * c) / a[0][0]
                if 0.6 <= ke <= 2.5 and 0.0 <= r <= 1.5 and -10.0 <= c <= 15.0:
                    return ke, r, c, True
            except ZeroDivisionError:
                pass
        return KE_V_PER_KMH, R_V_PER_A, C_V, False


def cruise_current(speed_kmh: float) -> float:
    return CRUISE_A_PER_KMH2 * speed_kmh * speed_kmh


def margin_pct(speed_kmh: float, load_a: float, v_rest: float, rp_ohm: float, k: tuple) -> float:
    ke, r, c = k[:3]
    v = v_rest - rp_ohm * load_a
    return 100.0 - 100.0 * (ke * speed_kmh + r * load_a + c) / v if v > 1 else 0.0


def safe_speed(v_rest: float, rp_ohm: float, k: tuple, load_a: float | None, margin: float = MARGIN_PCT) -> float:
    """Highest speed that still leaves `margin`. load_a None = the current of steady riding at
    that speed (grows with speed), otherwise a fixed load."""
    lo, hi = 0.0, 150.0
    for _ in range(40):
        mid = (lo + hi) / 2
        i = cruise_current(mid) if load_a is None else load_a
        if margin_pct(mid, i, v_rest, rp_ohm, k) >= margin:
            lo = mid
        else:
            hi = mid
    return lo


def report(v_rest: float | None, rp_ohm: float | None, model: PwmModel, source: str) -> dict:
    if not v_rest:
        return {"ok": False, "reason": "ingen spänning känd ännu"}
    rp = rp_ohm if rp_ohm and 0.02 <= rp_ohm <= 0.4 else RP_OHM
    k = model.coefficients()
    return {"ok": True, "source": source, "v_rest": round(v_rest, 1), "rp_mohm": round(rp * 1000),
            "margin_pct": MARGIN_PCT, "hard_load_a": HARD_LOAD_A,
            "cruise_kmh": round(safe_speed(v_rest, rp, k, None), 0),
            "hard_kmh": round(safe_speed(v_rest, rp, k, HARD_LOAD_A), 0),
            "cutout_kmh": round(safe_speed(v_rest, rp, k, HARD_LOAD_A, 0.0), 0),
            "model": "inlärd" if k[3] else "uppmätt 2026-10-04", "n": int(model.s[0])}
