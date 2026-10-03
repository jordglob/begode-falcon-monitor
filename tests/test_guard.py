"""Obalansvakt: healthy pack is quiet; shunt defects, dropout and drift are found."""
import copy
import re
from pathlib import Path

import pytest

from falcon.guard import Guard, LEARN_SAMPLES, summary
from falcon.protocol import FrameAssembler, WheelState

CAPTURE = Path(__file__).resolve().parent / "fixtures" / "falcon_pro_idle.log"


@pytest.fixture(scope="module")
def real_snapshot():
    asm, st = FrameAssembler(), WheelState()
    for line in CAPTURE.read_text().splitlines():
        if re.match(r"^\s*\d+\.\d{3} ffe1 ", line):
            for f in asm.feed(bytes.fromhex(line.split()[2])):
                st.apply(f)
    return st.snapshot()


def load_snap(base, ctrl_a, bms_a, temps=None):
    """Snapshot under load: controller battery current (p7) + per-BMS currents."""
    s = copy.deepcopy(base)
    s["p7"]["battery_current_a"] = ctrl_a
    for k, a in bms_a.items():
        s["bms"][k]["current_a"] = a
        if temps:
            s["bms"][k]["temp_max_c"] = temps[k]
    s["battery_current_a"] = sum(bms_a.values())
    return s


def codes(findings, level=None):
    return {f.code for f in findings if level is None or f.level == level}


def test_real_capture_is_quiet(real_snapshot):
    f = Guard().update(real_snapshot, now=0)
    assert not codes(f, "warn") and not codes(f, "alarm")
    assert summary(f)["findings"][0]["code"] in ("ok", "learning")


def test_healthy_load_is_quiet(real_snapshot):
    g = Guard()
    for i in range(200):
        f = g.update(load_snap(real_snapshot, 40.0, {1: 20.0, 2: 20.2}), now=i)
    assert not codes(f, "warn") and not codes(f, "alarm")
    assert g.learned


def test_unsoldered_resistor_from_day_one(real_snapshot):
    # BMS 2's shunt has 3 of 4 resistors -> reads 33 % high
    g = Guard()
    for i in range(30):
        f = g.update(load_snap(real_snapshot, 40.0, {1: 20.0, 2: 26.6}), now=i)
    hit = [x for x in f if x.code == "shunt_ratio"]
    assert hit and hit[0].where == "BMS 2 (sträng B)" and 25 < hit[0].value < 40


def test_resistor_burns_off_step_alarm(real_snapshot):
    g = Guard()
    for i in range(150):     # long period at 3-of-4 (x1.33)
        g.update(load_snap(real_snapshot, 40.0, {1: 20.0, 2: 26.6}), now=i)
    for i in range(150, 165):  # one more burns: 2-of-4 (x2)
        f = g.update(load_snap(real_snapshot, 40.0, {1: 20.0, 2: 40.0}), now=i)
    assert "shunt_step" in codes(f, "alarm")


def test_pack_dropout_alarm(real_snapshot):
    g = Guard()
    for i in range(5):
        f = g.update(load_snap(real_snapshot, 40.0, {1: 40.0, 2: 0.0}), now=i)
    hit = [x for x in f if x.code == "pack_dropout"]
    assert hit and hit[0].level == "alarm" and hit[0].where == "BMS 2 (sträng B)"


def test_bms_sum_vs_controller(real_snapshot):
    g = Guard()
    for i in range(LEARN_SAMPLES + 5):
        g.update(load_snap(real_snapshot, 40.0, {1: 20.0, 2: 20.0}), now=i)
    # all groups suddenly read 30 % high relative to the controller
    f = g.update(load_snap(real_snapshot, 40.0, {1: 26.0, 2: 26.0}), now=999)
    assert "shunt_sum" in codes(f, "warn")


def test_string_drift_after_ride(real_snapshot):
    s = copy.deepcopy(real_snapshot)
    s["cells"]["B"]["cells_mv"] = [v - 45 for v in s["cells"]["B"]["cells_mv"]]
    f = Guard().update(s, now=0)
    assert "string" in codes(f, "alarm")


def test_single_cell_located(real_snapshot):
    s = copy.deepcopy(real_snapshot)
    s["cells"]["A"]["cells_mv"][20] -= 70        # bank 2, cell 5
    f = Guard().update(s, now=0)
    hit = [x for x in f if x.code == "cell"]
    assert hit[0].level == "alarm" and hit[0].where == "sträng A, bank 2, cell 5"


def test_hot_group(real_snapshot):
    g = Guard()
    f = g.update(load_snap(real_snapshot, 40.0, {1: 20.0, 2: 20.0},
                           temps={1: 31, 2: 48}), now=0)
    assert "temp" in codes(f, "alarm")


def test_stuck_balancing(real_snapshot):
    s = copy.deepcopy(real_snapshot)
    s["bms"][2]["cell_balance"] = True
    g = Guard()
    g.update(s, now=0)
    f = g.update(s, now=3 * 3600)
    assert "balance" in codes(f, "warn")


def test_baseline_roundtrip(real_snapshot):
    g = Guard()
    for i in range(LEARN_SAMPLES + 1):
        g.update(load_snap(real_snapshot, 40.0, {1: 20.0, 2: 20.0}), now=i)
    g2 = Guard(g.export_baseline())
    assert g2.learned and g2.sum_baseline == pytest.approx(1.0)


def test_wheel_alerts(real_snapshot):
    s = copy.deepcopy(real_snapshot)
    s["p4"]["alerts"] = ["fel på hallsensor"]
    f = Guard().update(s, now=0)
    assert "wheel_alert" in codes(f, "alarm")


def test_load_spread_learns_normal_and_flags_weak_cell(real_snapshot):
    from falcon.guard import LoadSpread
    ls = LoadSpread()
    t = 0.0
    for k in range(80):                                   # healthy: ~1 mV per A per string
        i = 20 + k % 30
        cells = [4000 - int(i / 2 * 2) + (k + j) % 3 for j in range(8)]
        ls.add(t, "A", 0, cells, i)
        t += 0.3
    assert ls.baseline is not None and ls.findings(t) == []
    weak = [3950] * 8
    weak[5] = 3870                                        # one cell sags 80 mV extra at 40 A
    ls.add(t, "A", 1, weak, 40.0)
    f = ls.findings(t)
    assert f and f[0][1] == "load_spread" and "bank 1, cell 6" in f[0][2]


def test_load_cell_collapse_is_alarm():
    from falcon.guard import LoadSpread
    ls = LoadSpread(1.0)
    cells = [3500] * 8
    cells[2] = 3150
    ls.add(10, "B", 2, cells, 60.0)
    kinds = {f[1]: f[0] for f in ls.findings(10)}
    assert kinds["load_cell_low"] == "alarm" and kinds["load_spread"] == "alarm"


def test_no_load_no_check():
    from falcon.guard import LoadSpread
    ls = LoadSpread()
    ls.add(0, "A", 0, [4000] * 7 + [3800], 1.0)          # standing still
    assert ls.findings(0) == []
