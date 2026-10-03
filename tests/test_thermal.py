"""Battery temperature: R(T) model, available power, warm-up, hot spot, charging guard."""
import math

import pytest

from falcon.thermal import RModel, Thermal, charge_temp_ok, V_MIN_MV


def snap(temps1, temps2, cur=10.0):
    return {"bms": {1: {"temps_c": temps1, "temp_max_c": max(temps1), "current_a": cur / 2},
                    2: {"temps_c": temps2, "temp_max_c": max(temps2), "current_a": cur / 2}}}


def test_default_curve_doubles_at_zero():
    m = RModel([(25.0, 3.0)])
    assert m.r_at(25) == pytest.approx(3.0, rel=1e-6)
    assert m.r_at(0) == pytest.approx(6.0, rel=1e-3) and not m.fitted


def test_fit_recovers_arrhenius():
    b = 3500.0
    pts = [(t, 3.0 * math.exp(b * (1 / (t + 273.15) - 1 / 298.15))) for t in range(0, 31)]
    m = RModel(pts)
    assert m.fitted and m.b == pytest.approx(b, rel=0.01) and m.r_at(25) == pytest.approx(3.0, rel=0.01)


def test_available_power_and_cold_drop():
    th = Thermal(RModel([(25.0, 3.0)]))
    warm = th.evaluate(snap([25] * 4, [25] * 4), 4000, 3.0, 25.0, 1000, 80, now=0)
    i = (4000 - V_MIN_MV) / 3.0
    assert warm["pmax_w"] == pytest.approx(24 * 3.2 * i * 2, rel=0.01)
    assert warm["prec_w"] == pytest.approx(0.7 * warm["pmax_w"], rel=0.01)
    cold = Thermal(RModel([(25.0, 3.0)])).evaluate(snap([0] * 4, [0] * 4), 4000, 3.0, 25.0, 1000, 80, now=0)
    assert cold["pmax_w"] == pytest.approx(warm["pmax_w"] / 2, rel=0.02)
    assert any("Kallt" in n[1] or "kallt" in n[1] for n in cold["notes"])


def test_limiter_and_levels():
    th = Thermal(RModel([(25.0, 3.0)]))
    r = th.evaluate(snap([25] * 4, [25] * 4), 4000, 3.0, 25.0, 0.8 * 40960, 60, now=0)
    assert r["share_pct"] >= 70 and r["level"] == "warn" and r["limiter"] == "batteriet"


def test_hotspot_between_packs():
    r = Thermal(RModel([(25.0, 3.0)])).evaluate(snap([30, 31, 30, 31], [38, 39, 38, 39]), 4000, 3.0, 30, 500, 80, now=0)
    assert r["hotspot"] and "BMS 2" in r["hotspot"]


def test_warmup_eta():
    th = Thermal(RModel([(25.0, 3.0)]))
    for k in range(20):                                    # 5 °C rising 0.5 °C/min
        th.evaluate(snap([5 + k * 0.05] * 4, [5 + k * 0.05] * 4), None, None, None, None, None, now=k * 6)
    r = th.evaluate(snap([6] * 4, [6] * 4), None, None, None, None, None, now=126)
    assert r["warmup_min"] is not None and 10 <= r["warmup_min"] <= 25


def test_charge_temperature_guard():
    assert charge_temp_ok(2)[0] is False and charge_temp_ok(50)[0] is False and charge_temp_ok(20) == (True, None)
