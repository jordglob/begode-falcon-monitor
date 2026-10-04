"""Safe speed from the PWM model."""
import pytest

from falcon import margin as M


def test_defaults_reproduce_the_measured_table():
    k = M.PwmModel().coefficients()
    assert k[3] is False
    # 2026-10-04: 30 km/h cruise at full charge left 59 %, 40 km/h with 40 A left 34 %
    assert M.margin_pct(30, 10, 100.8, M.RP_OHM, k) == pytest.approx(59, abs=1.5)
    assert M.margin_pct(40, 40, 100.8, M.RP_OHM, k) == pytest.approx(34, abs=1.5)
    assert M.safe_speed(100.8, M.RP_OHM, k, 40.0) == pytest.approx(51, abs=1.0)
    assert M.safe_speed(85.2, M.RP_OHM, k, 40.0) == pytest.approx(40.6, abs=1.0)


def test_safe_speed_falls_with_charge_and_with_load():
    r_full = M.report(100.8, None, M.PwmModel(), "x")
    r_low = M.report(86.0, None, M.PwmModel(), "x")
    assert r_full["ok"] and r_full["cruise_kmh"] > r_full["hard_kmh"] > 0
    assert r_low["hard_kmh"] < r_full["hard_kmh"] and r_low["cruise_kmh"] < r_full["cruise_kmh"]
    assert r_full["cutout_kmh"] > r_full["hard_kmh"]
    assert M.report(None, None, M.PwmModel(), "x")["ok"] is False


def test_model_learns_the_wheels_own_coefficients_and_rejects_nonsense():
    m = M.PwmModel()
    for n in range(2000):                                  # a wheel that needs 1.5 V per km/h
        v, i, bus = 10 + n % 35, (n * 7) % 40 - 5, 90 + n % 9
        m.add(v, i, bus, 100 * (1.5 * v + 0.3 * i + 2.0) / bus)
    ke, r, c, learned = m.coefficients()
    assert learned and ke == pytest.approx(1.5, abs=0.01) and r == pytest.approx(0.3, abs=0.01) and c == pytest.approx(2.0, abs=0.1)
    assert M.PwmModel(m.export()).coefficients()[0] == pytest.approx(ke)     # survives a restart
    bad = M.PwmModel()
    for n in range(2000):                                  # garbage: PWM unrelated to speed
        bad.add(10 + n % 35, 5.0, 95.0, 5.0)
    assert bad.coefficients()[3] is False
    few = M.PwmModel()
    few.add(20, 5, 95, 30)
    assert few.coefficients()[3] is False
