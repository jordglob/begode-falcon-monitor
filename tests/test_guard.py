"""Obalansvakt: healthy pack is quiet; shunt defects, dropout and drift are found."""
import copy
import re
from pathlib import Path

import pytest

from falcon.guard import Guard, LEARN_SAMPLES, summary
from falcon.protocol import Frame, FrameAssembler, WheelState

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


@pytest.fixture
def shunt_on(monkeypatch):
    monkeypatch.setattr("falcon.guard.SHUNT_CHECKS", True)


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


def test_unsoldered_resistor_from_day_one(real_snapshot, shunt_on):
    # BMS 2's shunt has 3 of 4 resistors -> reads 33 % high
    g = Guard()
    for i in range(30):
        f = g.update(load_snap(real_snapshot, 40.0, {1: 20.0, 2: 26.6}), now=i)
    hit = [x for x in f if x.code == "shunt_ratio"]
    assert hit and hit[0].where == "BMS 2 (sträng B)" and 25 < hit[0].value < 40


def test_resistor_burns_off_step_alarm(real_snapshot, shunt_on):
    g = Guard()
    for i in range(150):     # long period at 3-of-4 (x1.33)
        g.update(load_snap(real_snapshot, 40.0, {1: 20.0, 2: 26.6}), now=i)
    for i in range(150, 165):  # one more burns: 2-of-4 (x2)
        f = g.update(load_snap(real_snapshot, 40.0, {1: 20.0, 2: 40.0}), now=i)
    assert "shunt_step" in codes(f, "alarm")


def test_shunt_checks_are_off_by_default(real_snapshot):
    g = Guard()
    for i in range(30):      # the BMS current field does not follow the load -> no verdicts from it
        f = g.update(load_snap(real_snapshot, 40.0, {1: 0.1, 2: 26.6}), now=i)
    assert not {x.code for x in f} & {"shunt_ratio", "shunt_step", "shunt_sum", "pack_dropout"}


def test_pack_dropout_alarm(real_snapshot, shunt_on):
    g = Guard()
    for i in range(5):
        f = g.update(load_snap(real_snapshot, 40.0, {1: 40.0, 2: 0.0}), now=i)
    hit = [x for x in f if x.code == "pack_dropout"]
    assert hit and hit[0].level == "alarm" and hit[0].where == "BMS 2 (sträng B)"


def test_bms_sum_vs_controller(real_snapshot, shunt_on):
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


def test_baseline_roundtrip(real_snapshot, shunt_on):
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


def _steady(ls, t, amps, seconds=4.5, string="A", bank=0, mv=4000):
    """Feed new cell frames at a steady current long enough for the gate to open."""
    n = 0
    while n * 0.3 < seconds:
        ls.add(t, string, bank, [mv - n % 2] * 8, amps)
        t += 0.3
        n += 1
    return t


def test_load_spread_learns_normal_and_flags_weak_cell(real_snapshot):
    from falcon.guard import LoadSpread
    ls = LoadSpread()
    t = 0.0
    for k in range(90):                                   # healthy: a few mV inside each half-pack
        cells = [3970 + (k // 3 + j) % 3 for j in range(8)]
        ls.add(t, "A", k % 3, cells, 30.0)
        t += 0.3
    assert ls.baseline is not None and ls.findings(t) == []
    weak = [3950] * 8
    weak[5] = 3870                                        # one cell sags 80 mV extra at 30 A
    ls.add(t, "A", 1, weak, 30.0)
    f = ls.findings(t)
    assert f and f[0][1] == "load_spread" and "bank 1, cell 6" in f[0][2]


def test_load_cell_collapse_is_alarm():
    from falcon.guard import LoadSpread
    ls = LoadSpread(1.0)
    t = _steady(ls, 0.0, 60.0, string="B", bank=0, mv=3500)
    cells = [3500] * 8
    cells[2] = 3150
    ls.add(t, "B", 2, cells, 60.0)
    kinds = {f[1]: f[0] for f in ls.findings(t)}
    assert kinds["load_cell_low"] == "alarm" and kinds["load_spread"] == "alarm"


def test_low_cell_alarm_needs_no_steady_current():
    from falcon.guard import LoadSpread
    ls = LoadSpread(1.0)
    cells = [3500] * 8
    cells[2] = 3150
    ls.add(10, "B", 2, cells, 60.0)                       # first frame ever, load just hit
    kinds = {f[1]: f[0] for f in ls.findings(10)}
    assert kinds == {"load_cell_low": "alarm"}


def test_no_load_no_check():
    from falcon.guard import LoadSpread
    ls = LoadSpread()
    ls.add(0, "A", 0, [4000] * 7 + [3800], 1.0)          # standing still
    assert ls.findings(0) == []


def test_half_pack_seam_is_not_a_cell_fault():
    """Real frame 2026-10-03: cells 9-12 measured under load, 13-16 a moment later at rest."""
    from falcon.guard import LoadSpread
    ls = LoadSpread()
    t = _steady(ls, 0.0, 8.0)
    ls.add(t, "B", 1, [3863, 3867, 3855, 3824, 4043, 4042, 4039, 4042], 8.0)
    assert ls.findings(t) == []                           # 219 mV across the seam, 43 mV inside a half


def test_changing_current_and_repeated_frames_are_skipped():
    from falcon.guard import LoadSpread
    ls = LoadSpread(1.0)
    t = 0.0
    for k in range(20):                                   # current swings 10..40 A: instant unknown
        ls.add(t, "A", 0, [3900 - k] * 7 + [3700], 10.0 + 30.0 * (k % 2))
        t += 0.3
    assert ls.findings(t) == [] and ls.used == 0
    ls2 = LoadSpread(1.0)
    t = _steady(ls2, 0.0, 30.0, bank=1)
    used = ls2.used
    for _ in range(5):                                    # the same frame again and again = no new measurement
        ls2.add(t, "A", 0, [3900] * 7 + [3700], 30.0)
        t += 0.3
    assert ls2.used == used + 1


def _pack_round(ls, t0, currents, weak=None, weak_half=None, r_mohm=3.0, jitter=0):
    """Six banks sampled 0.3 s apart, like the wheel."""
    t = t0
    for n, (s, b) in enumerate([(s, b) for s in "AB" for b in range(3)]):
        i = currents[n % len(currents)]
        cells = []
        for j in range(8):
            no = b * 8 + j + 1
            mv = 4000 - r_mohm * i / 2 + jitter
            if f"{s}{no}" == weak:
                mv -= 70
            if weak_half == f"{s}{1 if no <= 12 else 2}":
                mv -= 45
            cells.append(round(mv))
        ls.add(t, s, b, cells, i, r_lookup=lambda k: r_mohm)
        t += 0.3
    return t


def _pack_rounds(ls, currents, **kw):
    t = 0.0
    for n in range(4):                                    # the gate needs a few seconds of steady current
        t = _pack_round(ls, t, currents, jitter=n % 2, **kw)
    return t


def test_compensated_whole_pack_ignores_load_differences():
    from falcon.guard import LoadSpread
    ls = LoadSpread(1.5)
    t = _pack_rounds(ls, [10, 18, 26, 34, 42, 50])        # very different currents per bank -> not compared
    assert [f for f in ls.findings(t) if f[0] in ("warn", "alarm")] == []


def test_compensated_healthy_pack_is_quiet():
    from falcon.guard import LoadSpread
    ls = LoadSpread(1.5)
    t = _pack_rounds(ls, [30])
    assert len(ls.comp) == 48 and ls.comp_findings(t) == []


def test_compensated_finds_weak_cell_across_banks():
    from falcon.guard import LoadSpread
    ls = LoadSpread(1.5)
    t = _pack_rounds(ls, [30], weak="B17")
    f = ls.comp_findings(t)
    assert f and f[0][1] == "load_comp_cell" and "cell 17" in f[0][2] and f[0][0] == "warn"


def test_compensated_finds_weak_half_pack():
    from falcon.guard import LoadSpread
    ls = LoadSpread(1.5)
    t = _pack_rounds(ls, [30], weak_half="A1")
    assert any(f[1] == "load_comp_bank" and "cell 1–12" in f[2] for f in ls.comp_findings(t))


def test_rest_checks_wait_until_the_pack_has_rested(real_snapshot):
    """Right after load the stored cell values are still from the load -> no verdict yet."""
    s = copy.deepcopy(real_snapshot)
    s["cells"]["A"]["cells_mv"][3] -= 90
    s["battery_current_a"] = 0.2
    g = Guard()
    g.load_spread.trace.add(0.0, 30.0)
    for k in range(1, 10):
        g.load_spread.trace.add(k * 0.3, 0.2)
    assert "cell" not in codes(g.update(s, now=3.0))       # rested 2.7 s
    for k in range(10, 40):
        g.load_spread.trace.add(k * 0.3, 0.2)
    assert "cell" in codes(g.update(s, now=11.7), "alarm")


# ---------- regression: real frames that used to raise false alarms ----------
ARTIFACTS = Path(__file__).resolve().parent / "fixtures" / "ride_artifacts.txt"
CELL_CODES = {"cell", "bank", "string", "load_spread", "load_comp_cell", "load_comp_bank", "load_cell_low",
              "shunt_ratio", "shunt_step", "shunt_sum", "pack_dropout"}


def _artifact_frames():
    rows = [ln.split() for ln in ARTIFACTS.read_text().splitlines() if ln and not ln.startswith("#")]
    return [(float(t), int(typ), int(sub), hx) for t, typ, sub, hx in rows]


def test_recorded_ride_artifacts_raise_no_cell_or_shunt_findings():
    from tools.replay_blackbox import replay
    frames = _artifact_frames()
    spreads = [max(c) - min(c) for c in (list(Frame(t, s, bytes.fromhex(h)).u16()) for _, t, s, h in frames
                                         if t in (2, 3))]
    assert max(spreads) >= 180                            # the recording really contains the big seam steps
    found, guard = replay(frames)
    assert {code for _, code in found} & CELL_CODES == set()


def test_same_recording_with_a_truly_weak_cell_is_found():
    """Cell B5 pulled 120 mV down in every frame: lower than its half-pack mates measured at
    the same instant -> must still be reported, at rest and under steady load."""
    from tools.replay_blackbox import replay
    frames = []
    for ts, typ, sub, hx in _artifact_frames():
        if typ == 3 and sub == 0:
            b = bytearray(bytes.fromhex(hx))
            mv = int.from_bytes(b[8:10], "big") - 120
            b[8:10] = mv.to_bytes(2, "big")
            hx = b.hex()
        frames.append((ts, typ, sub, hx))
    found, guard = replay(frames)
    codes_found = {code for _, code in found}
    assert "cell" in codes_found or "load_spread" in codes_found
