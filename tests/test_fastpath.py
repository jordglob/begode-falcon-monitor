"""Bus from p0 on every packet; per-cell resistance by regression; current-scale calibration."""
import random

import pytest

from falcon.fastpath import BusTracker, CellRegression


def test_bus_updates_on_every_p0_and_finds_sag():
    b = BusTracker()
    for n in range(200):                       # 60 s of p0 at 0.3 s
        i = 2 + (n % 20)                       # 2..21 A
        v = 97.2 - 0.06 * i
        out = b.on_p0({"voltage_raw": round(v / 1.5 * 100), "phase_current_a": 3 * i},
                      {"battery_current_a": i}, 1000 + n * 0.3, None)
    assert out["updates"] == 200
    assert out["sag"]["ohm"] == pytest.approx(0.06, abs=0.003)
    assert out["current_source"].startswith("paket 7")


def test_bus_voltage_scaling():
    b = BusTracker()
    assert b.bus_v({"voltage_raw": 6480}) == pytest.approx(97.2)


def test_bus_waits_for_p7():
    b = BusTracker()
    assert b.on_p0({"voltage_raw": 6480}, {}, 0, None) == {}


def test_cell_regression_finds_weak_cell():
    rnd = random.Random(1)
    reg = CellRegression()
    t = 0.0
    for rnd_i in range(400):                    # ~12 min, bank round-robin like the wheel
        for string in "AB":
            for bank in range(3):
                t += 0.3
                i = rnd.uniform(0, 30)          # battery current, A
                mv = []
                for j in range(8):
                    cell = bank * 8 + j + 1
                    r = 6.0 if (string, cell) == ("B", 13) else 3.0   # mΩ
                    mv.append(round(4000 - r * i / 2 + rnd.gauss(0, 1.0)))
                reg.on_bank(t, string, bank, mv, i)
    rep = reg.report(now=t)
    assert rep["fitted"] == 48
    assert rep["cells"]["B13"]["mohm"] == pytest.approx(6.0, rel=0.1)
    assert rep["cells"]["A1"]["mohm"] == pytest.approx(3.0, rel=0.1)
    assert rep["flag"] == ["B13"]


def test_cell_regression_needs_current_variation():
    reg = CellRegression()
    for n in range(100):
        reg.on_bank(n * 1.8, "A", 0, [4000] * 8, 0.1)
    rep = reg.report(now=200)
    assert rep["fitted"] == 0 and rep["cells"]["A1"]["mv"] == 4000
    assert rep["cells"]["A1"]["age_s"] == pytest.approx(200 - 99 * 1.8, abs=0.1)
