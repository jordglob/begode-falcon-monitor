"""Beep watch: state changes logged, black boxes written around events and marks."""
import copy
import json
import re
from pathlib import Path

from falcon.beeps import BeepWatch, POST_S
from falcon.protocol import FrameAssembler, WheelState

FIX = Path(__file__).resolve().parent / "fixtures" / "falcon_pro_idle.log"


def base():
    asm, st = FrameAssembler(), WheelState()
    for line in FIX.read_text().splitlines():
        if re.match(r"^\s*\d+\.\d{3} ffe1 ", line):
            for f in asm.feed(bytes.fromhex(line.split()[2])):
                st.apply(f)
    return st.snapshot()


def test_quiet_when_nothing_changes(tmp_path):
    w = BeepWatch(tmp_path)
    s = base()
    w.check(0, s, 0.1)
    assert w.check(1, s, 0.1) == []
    assert w.report()["active"] == []


def test_alert_bit_logged_with_both_meanings_and_blackbox(tmp_path):
    msgs = []
    w = BeepWatch(tmp_path, on_event=msgs.append)
    s = base()
    w.check(0, s, 0.1)
    for t in range(0, 40):
        w.frame(t * 1.0, 0, 24, "00" * 16)
    s2 = copy.deepcopy(s)
    s2["p4"]["alert_raw"] = 0x02
    ev = w.check(40, s2, 0.1)
    assert ev and "MOS-transistor bränd" in ev[0]["text"] and "fartlarm 2" in ev[0]["text"]
    assert any("larmbit 0x02" in m for m in msgs)
    w.frame(40 + POST_S + 1, 0, 24, "00" * 16)          # after-window passes -> box written
    boxes = list(tmp_path.glob("blackbox-*.json"))
    assert len(boxes) == 1
    d = json.loads(boxes[0].read_text())
    assert d["events"] and min(f["t"] for f in d["frames"]) <= -25


def test_pack_without_current_and_low_cell(tmp_path, monkeypatch):
    monkeypatch.setattr("falcon.guard.SHUNT_CHECKS", True)
    w = BeepWatch(tmp_path)
    s = base()
    w.check(0, s, 0.1)
    s2 = copy.deepcopy(s)
    s2["bms"][1]["current_a"], s2["bms"][2]["current_a"] = 0.0, 40.0
    s2["cells"]["A"]["cells_mv"][3] = 2750
    keys = {e["key"]: e["new"] for e in w.check(1, s2, 40.0)}
    assert keys["paket som inte bär ström"] == "BMS 1"
    assert keys["lägsta cell"].startswith("under 2,8 V")


def test_mark_creates_box(tmp_path):
    w = BeepWatch(tmp_path)
    w.frame(100, 0, 24, "00" * 16)
    w.mark(100, "5 pip/s")
    w.frame(100 + POST_S + 1, 0, 24, "00" * 16)
    d = json.loads(next(tmp_path.glob("blackbox-*.json")).read_text())
    assert d["reason"].startswith("användaren hörde pip") and "5 pip/s" in d["reason"]


def test_api_mark_from_any_device(monkeypatch, tmp_path):
    import os
    os.environ.setdefault("FALCON_DB", str(tmp_path / "t.db"))
    from fastapi.testclient import TestClient
    import falcon.server as srv
    monkeypatch.setattr(srv, "beepwatch", BeepWatch(tmp_path / "bb"))
    c = TestClient(srv.app)
    r = c.post("/api/beeps/mark", json={"note": "från batteriet"}).json()
    assert r["ok"] and r["note"] == "från batteriet"
    rep = c.get("/api/beeps").json()
    assert rep["marks"] and rep["patterns"] and len(rep["alert_bits"]) == 8
