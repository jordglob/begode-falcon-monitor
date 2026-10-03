"""Per-cell resistance history by temperature and load; regression persistence."""
import sqlite3

import pytest

from falcon.cellhistory import CellHistory
from falcon.fastpath import CellRegression
from falcon.thermal import RModel


def test_bins_by_temperature_and_load_and_flags_weak_cell():
    db = sqlite3.connect(":memory:")
    h = CellHistory(db)
    ts = 1_790_000_000
    for k in range(400):
        temp = 5.0 if k < 200 else 22.0
        r_mult = 2.0 if temp < 10 else 1.0                    # cold: double resistance
        i = 5 + (k % 40)                                      # string current 5..44 A
        for n in range(1, 25):
            r = (6.0 if n == 7 else 3.0) * r_mult             # A7 is weak
            h.add(ts + k, f"A{n}", i, 4000 - r * i, temp)
    h.flush()
    rep = h.report(RModel([(25.0, 3.0)]))
    a1 = next(c for c in rep["cells"] if c["cell"] == "A1")
    assert a1["by_temp"][20] == pytest.approx(3.0, rel=0.02) and a1["by_temp"][5] == pytest.approx(6.0, rel=0.02)
    assert a1["by_load"]["15–35 A"] == pytest.approx(rep["cells"][0]["by_load"]["15–35 A"], rel=0.5)
    assert any(f["cell"] == "A7" for f in rep["flags"])
    assert list(a1["r25_by_day"].values())[0] > 0


def test_history_survives_restart():
    db = sqlite3.connect(":memory:")
    h = CellHistory(db)
    for k in range(50):
        h.add(1_790_000_000 + k, "B3", 5 + k % 30, 4000 - 3 * (5 + k % 30), 20)
    h.flush()
    h2 = CellHistory(db)
    assert next(c for c in h2.report()["cells"] if c["cell"] == "B3")["by_temp"][20] == pytest.approx(3.0, rel=0.02)


def test_cell_regression_export_restore():
    reg = CellRegression()
    for k in range(100):
        i = k % 30
        reg.on_bank(k * 1.8, "A", 0, [4000 - round(3 * i / 2)] * 8, i)
    r = reg.resistance("A1")
    reg2 = CellRegression()
    reg2.restore(reg.export())
    assert r and reg2.resistance("A1") == pytest.approx(r)
