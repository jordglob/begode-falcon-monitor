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


# ---------- the two current rows of one BMS ----------
def _charging(base, rows1, rows2):
    s = copy.deepcopy(base)
    for k, rows in ((1, rows1), (2, rows2)):
        s["bms"][k]["activity"] = "laddning"
        s["bms"][k]["row_currents_a"] = list(rows)
    s["battery_current_a"] = -7.4
    return s


def test_bms_rows_disagreeing_while_charging_is_reported(real_snapshot):
    g = Guard()
    for i in range(29):
        f = g.update(_charging(real_snapshot, (3.6, 3.6), (5.6, 3.85)), now=i)
    assert "bms_rows" not in codes(f)                      # not yet: rows update seconds apart at plug-in
    f = g.update(_charging(real_snapshot, (3.6, 3.6), (5.6, 3.85)), now=30)
    hit = [x for x in f if x.code == "bms_rows"]
    assert len(hit) == 1 and hit[0].where == "BMS 2 (sträng B)" and 40 < hit[0].value < 50
    assert hit[0].level == "info" and "shunt" not in hit[0].text      # seen on both packs: cause unknown


def test_bms_rows_agreeing_or_not_charging_is_quiet(real_snapshot):
    g = Guard()
    for i in range(60):
        f = g.update(_charging(real_snapshot, (3.6, 3.6), (3.9, 3.85)), now=i)
    assert "bms_rows" not in codes(f)
    s = _charging(real_snapshot, (3.6, 3.6), (5.6, 3.85))
    s["bms"][2]["activity"] = "urladdning"                 # riding: the field does not follow the load
    for i in range(60):
        f = g.update(s, now=100 + i)
    assert "bms_rows" not in codes(f)


# ---------- a current row that never reports anything ----------
def _rows(base, currents):
    s = copy.deepcopy(base)
    for k, a in zip(sorted(s["groups"]), currents):
        s["groups"][k]["current_a"] = a
    return s


def test_silent_current_row_is_an_alarm(real_snapshot):
    """The earlier pack failure: one of the four rows never reported any amperes."""
    g = Guard()
    f = []
    for t in range(200):                                   # charging: three rows at ~3.7 A, one dead
        f = g.update(_rows(real_snapshot, [3.7, 3.6, 0.0, 3.7]), now=float(t))
    hit = [x for x in f if x.code == "row_silent"]
    assert len(hit) == 1 and hit[0].level == "alarm" and hit[0].where == "LB – BMS 2 (sträng B), rad 1"
    assert "0.0 A" in hit[0].text and "3.7 A" in hit[0].text


def test_silent_row_needs_time_and_current_in_the_others(real_snapshot):
    g = Guard()
    for t in range(100):                                   # under two minutes: not yet
        f = g.update(_rows(real_snapshot, [3.7, 3.6, 0.0, 3.7]), now=float(t))
    assert "row_silent" not in codes(f)
    g = Guard()
    for t in range(300):                                   # nobody carries current: nothing to compare
        f = g.update(_rows(real_snapshot, [0.1, 0.0, 0.0, 0.2]), now=float(t))
    assert "row_silent" not in codes(f)
    g = Guard()
    for t in range(300):                                   # healthy spread seen on real rides (lowest 0.36 of the others)
        f = g.update(_rows(real_snapshot, [1.1, 3.0, 3.2, 3.0]), now=float(t))
    assert "row_silent" not in codes(f)


# ---------- the two packs report different currents ----------
def _packs(base, a, b, activity):
    s = copy.deepcopy(base)
    for k, cur in ((1, a), (2, b)):
        s["bms"][k]["current_a"], s["bms"][k]["activity"] = cur, activity
    s["battery_current_a"] = -(a + b) if activity == "laddning" else a + b
    return s


def test_pack_imbalance_while_charging_alarms_within_ten_seconds(real_snapshot):
    g = Guard()
    for t in range(8):
        f = g.update(_packs(real_snapshot, 3.6, 5.2, "laddning"), now=float(t))
    assert "pack_current" not in codes(f)                  # the window is not full yet
    for t in range(8, 12):
        f = g.update(_packs(real_snapshot, 3.6, 5.2, "laddning"), now=float(t))
    hit = [x for x in f if x.code == "pack_current"]
    assert len(hit) == 1 and hit[0].level == "alarm" and "BMS 2" in hit[0].text.split("mer ström")[0]
    assert "under laddning" in hit[0].text and 30 < hit[0].value < 45


def test_pack_imbalance_while_riding_needs_five_minutes(real_snapshot):
    g = Guard()
    for t in range(60):                                    # a minute of a big difference: normal while riding
        f = g.update(_packs(real_snapshot, 2.0, 6.0, "urladdning"), now=float(t))
    assert "pack_current" not in codes(f)
    for t in range(60, 330):
        f = g.update(_packs(real_snapshot, 2.0, 6.0, "urladdning"), now=float(t))
    assert "pack_current" in codes(f, "alarm")


def test_healthy_packs_and_low_current_are_quiet(real_snapshot):
    g = Guard()
    for t in range(400):                                   # 21 % apart: the most seen on a healthy ride
        f = g.update(_packs(real_snapshot, 3.0, 3.7, "urladdning"), now=float(t))
    assert "pack_current" not in codes(f)
    g = Guard()
    for t in range(400):                                   # next to no current: a ratio means nothing
        f = g.update(_packs(real_snapshot, 0.1, 0.4, "urladdning"), now=float(t))
    assert "pack_current" not in codes(f)


# ---------- fast pack dropout from the packs' voltages ----------
def _rows_v(g, t0, seconds, v1, v2):
    """BMS rows arrive every 0.3 s in the order 0,1,2,3 (rows 0-1 = BMS 1, 2-3 = BMS 2)."""
    t, k = t0, 0
    while t < t0 + seconds:
        g.on_bms_row(t, 1 if k % 4 < 2 else 2, v1 if k % 4 < 2 else v2)
        t += 0.3
        k += 1
    return t


def test_pack_cut_off_under_load_alarms_within_seconds(real_snapshot):
    g = Guard()
    t = _rows_v(g, 0.0, 20, 95.0, 95.1)                     # healthy
    assert "pack_dropout" not in codes(g.update(real_snapshot, now=t))
    t1 = _rows_v(g, t, 2.0, 91.0, 95.4)                     # pack 2 cut off at 20 A: 4.4 V apart
    assert "pack_dropout" not in codes(g.update(real_snapshot, now=t1))     # not yet two row cycles
    t2 = _rows_v(g, t1, 2.0, 91.0, 95.4)
    hit = [x for x in g.update(real_snapshot, now=t2) if x.code == "pack_dropout"]
    assert len(hit) == 1 and hit[0].level == "alarm" and t2 - t < 5.0
    assert "BMS 1 (sträng A) 91.0 V" in hit[0].text and "BMS 2 (sträng B) 95.4 V" in hit[0].text
    t3 = _rows_v(g, t2, 20, 95.0, 95.0)                     # healthy again: the alarm clears
    assert "pack_dropout" not in codes(g.update(real_snapshot, now=t3))


def test_single_readings_far_apart_are_not_a_dropout(real_snapshot):
    """Rows are not simultaneous: 2.8 V apart in one reading was seen on a healthy ride."""
    g = Guard()
    t = 0.0
    for k in range(200):
        d = 2.8 if k % 10 == 0 else 0.1                    # a spike now and then, in changing directions
        g.on_bms_row(t, 1, 95.0 + (d if k % 20 == 0 else 0)); t += 0.3
        g.on_bms_row(t, 2, 95.0 + (d if k % 20 == 10 else 0)); t += 0.3
    assert "pack_dropout" not in codes(g.update(real_snapshot, now=t))
    g.on_bms_row(t, 1, 91.0)                               # stale partner: BMS 2 not heard for a while
    g.on_bms_row(t + 5, 1, 91.0)
    g.on_bms_row(t + 9, 1, 91.0)
    assert "pack_dropout" not in codes(g.update(real_snapshot, now=t + 9))


# ---------- fast dead current row ----------
def _rows_i(g, t0, seconds, amps, activity="urladdning"):
    t, k = t0, 0
    while t < t0 + seconds:
        g.on_bms_row(t, 1 if k % 4 < 2 else 2, 95.0, k % 4, amps[k % 4], activity)
        t += 0.3
        k += 1
    return t


def test_dead_current_row_under_load_alarms_in_about_ten_seconds(real_snapshot):
    """The owner's case: the broken pack showed 0.1-0.3 A while the others showed about 10 A."""
    g = Guard()
    t = _rows_i(g, 0.0, 8.0, [10.2, 0.1, 9.8, 10.5])
    assert "row_dead" not in codes(g.update(real_snapshot, now=t))
    t = _rows_i(g, t, 4.0, [10.2, 0.3, 9.8, 10.5])
    hit = [x for x in g.update(real_snapshot, now=t) if x.code == "row_dead"]
    assert len(hit) == 1 and hit[0].level == "alarm" and hit[0].where == "RF – BMS 1 (sträng A), rad 2" and t < 13


def test_dead_row_while_charging_needs_less_current_in_the_others(real_snapshot):
    g = Guard()
    t = _rows_i(g, 0.0, 12.0, [3.7, 3.6, 0.1, 3.7], "laddning")
    assert "row_dead" in codes(g.update(real_snapshot, now=t), "alarm")


def test_rows_near_zero_while_the_others_are_low_too_is_normal(real_snapshot):
    g = Guard()
    t = _rows_i(g, 0.0, 30.0, [2.5, 0.1, 3.0, 2.8])        # riding: the others are not high enough to judge
    t = _rows_i(g, t, 6.0, [12.0, 0.2, 11.0, 12.5])        # high, but shorter than the hold time
    t = _rows_i(g, t, 30.0, [12.0, 4.0, 11.0, 12.5])       # the row wakes up
    assert "row_dead" not in codes(g.update(real_snapshot, now=t))


def test_silent_row_catches_the_owners_case_of_a_few_tenths_of_an_ampere(real_snapshot):
    g = Guard()
    for t in range(200):                                   # riding: the broken pack shows 0.1-0.3 A, the others ~3 A
        f = g.update(_rows(real_snapshot, [2.6, 3.1, [0.1, 0.2, 0.3][t % 3], 2.9]), now=float(t))
    hit = [x for x in f if x.code == "row_silent"]
    assert len(hit) == 1 and hit[0].where == "LB – BMS 2 (sträng B), rad 1"


# ---------- frozen group: the real fault of June 2026 ----------
def _groups(g, t0, seconds, fn):
    """fn(k, t) -> (group voltage, amperes) for row k at time t; rows arrive every 0.3 s."""
    t, n = t0, 0
    while t < t0 + seconds:
        k = n % 4
        v, a = fn(k, t)
        g.on_bms_row(t, 1 if k < 2 else 2, 95.0, k, a, "urladdning", v)
        t += 0.3
        n += 1
    return t


def test_frozen_group_with_no_current_alarms_while_riding(real_snapshot):
    """RF 'all the time reports the same voltage and 0 current' while the others sag and carry load."""
    import math
    g = Guard()
    healthy = lambda k, t: (round(47.0 - 0.8 * math.sin(t / 2 + k), 1), 4.0 + k)
    t = _groups(g, 0.0, 20, healthy)
    assert "group_frozen" not in codes(g.update(real_snapshot, now=t))
    broken = lambda k, t: (47.8, 0.0) if k == 1 else healthy(k, t)
    t2 = _groups(g, t, 14, broken)
    hit = [x for x in g.update(real_snapshot, now=t2) if x.code == "group_frozen"]
    assert len(hit) == 1 and hit[0].level == "alarm" and hit[0].where == "RF – BMS 1 (sträng A), rad 2"
    assert "47.8 V" in hit[0].text


def test_everything_standing_still_at_rest_is_not_a_frozen_group(real_snapshot):
    g = Guard()
    t = _groups(g, 0.0, 40, lambda k, t: (47.2, 0.0))       # parked: nothing moves, nothing flows
    assert "group_frozen" not in codes(g.update(real_snapshot, now=t))
    g = Guard()
    import math
    t = _groups(g, 0.0, 40, lambda k, t: (47.8, 3.0) if k == 1 else (round(47.0 - 0.8 * math.sin(t / 2 + k), 1), 5.0))
    assert "group_frozen" not in codes(g.update(real_snapshot, now=t))     # still voltage but it carries current


# ---------- the four group voltages at rest ----------
def _rested(g, seconds=40):
    for k in range(int(seconds / 0.3)):
        g.load_spread.trace.add(k * 0.3, 0.2)
    return seconds


def _groupv(base, volts):
    s = copy.deepcopy(base)
    for k, v in zip(sorted(s["groups"]), volts):
        s["groups"][k]["half_voltage_v"] = v
    s["battery_current_a"] = 0.2
    return s


def test_group_voltages_apart_at_rest_like_june_2026(real_snapshot):
    g = Guard()
    t = _rested(g)
    f = g.update(_groupv(real_snapshot, [46.6, 47.8, 47.3, 44.8]), now=t)
    assert "group_voltage" not in codes(f)                 # must have been there for a while
    for k in range(1, 40):
        g.load_spread.trace.add(t + k * 0.3, 0.2)
    f = g.update(_groupv(real_snapshot, [46.6, 47.8, 47.3, 44.8]), now=t + 11.5)
    hit = [x for x in f if x.code == "group_voltage"]
    assert len(hit) == 1 and hit[0].level == "alarm" and hit[0].where == "RB ↔ RF" and hit[0].value == 3.0
    assert "0,5 V" in hit[0].text


def test_group_voltages_within_begodes_limit_or_under_load_are_quiet(real_snapshot):
    g = Guard()
    t = _rested(g)
    for dt in (0, 12):
        f = g.update(_groupv(real_snapshot, [47.2, 47.1, 47.3, 47.6]), now=t + dt)     # 0.5 V: on the limit
    assert "group_voltage" not in codes(f)
    g = Guard()
    for k in range(200):                                   # riding: groups differ by volts, measured at different moments
        g.load_spread.trace.add(k * 0.3, 20.0)
    f = g.update(_groupv(real_snapshot, [46.0, 47.8, 47.3, 45.0]), now=60.0)
    assert "group_voltage" not in codes(f)
