"""Settings backup: snapshot content, change detection, restore through the control path."""
import json
import re
from pathlib import Path

import pytest

from falcon.backup import Backups
from falcon.protocol import FrameAssembler, WheelState

FIX = Path(__file__).resolve().parent / "fixtures" / "falcon_pro_idle.log"


def replay():
    asm, st = FrameAssembler(), WheelState()
    for line in FIX.read_text().splitlines():
        if re.match(r"^\s*\d+\.\d{3} ffe1 ", line):
            for f in asm.feed(bytes.fromhex(line.split()[2])):
                st.apply(f)
    return st


def test_backup_contains_everything(tmp_path):
    st = replay()
    b = Backups(tmp_path)
    doc = b.make(st.snapshot(), st.raw, {"wheel_firmware": "GW1634001", "firmware": "V1.9"},
                 "0.9.0", now=1_790_000_000)
    saved = json.loads((tmp_path / doc["name"]).read_text())
    assert saved["wheel_firmware"] == "GW1634001" and saved["odometer_m"] == 836866
    assert saved["settings"]["p4.led_mode"] == 3 and saved["restorable"] == {"p4.led_mode": 3, "p4.tiltback_kmh": 51}
    assert len(saved["raw_rows"]) == 13
    assert b.list()[0]["name"] == doc["name"]


def test_changes_vs_now_ignores_odometer(tmp_path):
    st = replay()
    b = Backups(tmp_path)
    doc = b.make(st.snapshot(), st.raw, {}, "x", now=1)
    snap = st.snapshot()
    snap["p4"] = {**snap["p4"], "odometer_raw": 999999, "led_mode": 1}
    assert Backups.changes_vs_now(doc, snap) == {"p4.led_mode": [3, 1]}


def test_load_rejects_path_tricks(tmp_path):
    with pytest.raises(ValueError):
        Backups(tmp_path).load("../../etc/passwd")


def test_power_off_countdown_is_not_a_setting_change(tmp_path):
    st = replay()
    doc = Backups(tmp_path).make(st.snapshot(), st.raw, {}, "x", now=1)
    snap = st.snapshot()
    snap["p4"] = {**snap["p4"], "power_off_in_s": 27}       # seen live: 7200 -> 0 -> wheel off
    assert Backups.changes_vs_now(doc, snap) == {}
