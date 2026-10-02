"""Control: off by default, local-only two-step enable, auto-off, whitelist, motion block,
write-twice-read-once through the real API with a fake wheel."""
import asyncio
import os
import re
import tempfile
from pathlib import Path

import pytest

_tmp = tempfile.mkdtemp()
os.environ.setdefault("FALCON_DB", os.path.join(_tmp, "t.db"))
os.environ.setdefault("FALCON_BACKUPS", os.path.join(_tmp, "backups"))

from falcon import control as ctl                                  # noqa: E402
from falcon.ble import WheelLink                                    # noqa: E402
from falcon.protocol import (FrameAssembler, WheelState, ascii_replies,  # noqa: E402
                             cmd_tiltback_speed)

FIX = Path(__file__).resolve().parent / "fixtures" / "falcon_pro_idle.log"


def replay_state():
    asm, st = FrameAssembler(), WheelState()
    for line in FIX.read_text().splitlines():
        if re.match(r"^\s*\d+\.\d{3} ffe1 ", line):
            for f in asm.feed(bytes.fromhex(line.split()[2])):
                st.apply(f)
    return st


# ---------- gate ----------
class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def test_gate_off_by_default_and_local_two_step():
    g = ctl.ControlGate(addresses=lambda: {"127.0.0.1", "192.168.1.5"})
    assert not g.enabled
    with pytest.raises(PermissionError):
        g.request_enable("192.168.1.77")              # phone
    tok = g.request_enable("192.168.1.5")              # the server's own LAN address
    with pytest.raises(PermissionError):
        g.confirm_enable("192.168.1.5", "wrong")
    tok = g.request_enable("127.0.0.1")
    g.confirm_enable("127.0.0.1", tok)
    assert g.enabled
    with pytest.raises(PermissionError):
        g.check_use("192.168.1.77")                    # still local-only when on


def test_gate_confirm_window_and_auto_off():
    c = Clock()
    g = ctl.ControlGate(addresses=lambda: {"127.0.0.1"}, clock=c)
    tok = g.request_enable("127.0.0.1")
    c.t += ctl.CONFIRM_WINDOW_S + 1
    with pytest.raises(PermissionError):
        g.confirm_enable("127.0.0.1", tok)
    g.confirm_enable("127.0.0.1", g.request_enable("127.0.0.1"))
    c.t += ctl.IDLE_OFF_S + 1
    assert g.status("127.0.0.1")["enabled"] is False


def test_whitelist_and_ranges():
    assert "tiltback" not in ctl.SETTINGS                   # not offered before mapping
    assert ctl.build("led_mode", 4).payload == b"WM4"
    assert ctl.build("pedal", "hard").payload == b"h"
    with pytest.raises(ValueError):
        ctl.build("led_mode", 12)
    with pytest.raises(ValueError):
        ctl.build("calibrate", None)
    assert ctl.build("beep", None).absolute is False        # toggles/actions sent once


def test_motion_block():
    st = replay_state().snapshot()
    assert ctl.motion_block(st, 0.1) is None
    assert ctl.motion_block(st, 5.0) == "inga färska data från hjulet"
    st["p0"]["speed_kmh"] = 3.0
    assert ctl.motion_block(st, 0.1) == "hjulet rullar"


def test_diff_ignores_volatile_fields():
    a = {"p4.led_mode": 3, "p0.speed_kmh": 0, "p1[0].temp_a_c": 30}
    b = {"p4.led_mode": 4, "p0.speed_kmh": 1, "p1[0].temp_a_c": 31}
    assert ctl.diff(a, b) == {"p4.led_mode": [3, 4]}


def test_ascii_reply_between_frames():
    frame = bytes.fromhex("55aa19500000003d0021fe5cfa150c8800010018") + b"\x5a" * 4
    assert ascii_replies(frame + b"GW1634003" + frame) == ["GW1634003"]
    assert ascii_replies(frame * 3) == []                   # footers 'ZZZZ' are not replies
    assert ascii_replies(b"\x5a" * 4 + b"GW1634001" + frame) == ["GW1634001"]   # cut frame


def test_ble_write_refuses_blocked_and_disconnected():
    link = WheelLink("AA:BB:CC:DD:EE:FF", WheelState())
    with pytest.raises(PermissionError):
        asyncio.run(link.write(b"cy"))
    with pytest.raises(PermissionError):
        asyncio.run(link.write(cmd_tiltback_speed(40).payload.replace(b"WY", b"WX")))
    with pytest.raises(ConnectionError):
        asyncio.run(link.write(b"WM4"))


# ---------- end to end through the API, fake wheel ----------
class FakeLink:
    def __init__(self, state):
        self.state, self.sent = state, []
        self.connected, self.last_frame_ts, self.device_info = True, 0.0, {}

    async def write(self, payload):
        self.sent.append(payload)
        if payload.startswith(b"WM"):                          # wheel applies it
            self.state.p4 = {**self.state.p4, "led_mode": int(payload[2:])}

        async def fresh():
            await asyncio.sleep(0.05)
            self.state.counts[4] = self.state.counts.get(4, 0) + 1
        asyncio.ensure_future(fresh())

    def notifications_since(self, ts):
        return b""

    def info(self):
        return {}


@pytest.fixture()
def api(monkeypatch):
    from fastapi.testclient import TestClient
    import falcon.server as srv
    st = replay_state()
    fl = FakeLink(st)
    monkeypatch.setattr(srv, "state", st)
    monkeypatch.setattr(srv, "link", fl)
    monkeypatch.setattr(srv, "gate", ctl.ControlGate(addresses=lambda: {"testclient"}))
    monkeypatch.setattr(srv.time, "time", __import__("time").time)

    def touch():
        fl.last_frame_ts = __import__("time").time()
    return TestClient(srv.app), fl, touch, srv


def test_api_off_by_default(api):
    c, fl, touch, srv = api
    touch()
    r = c.post("/api/control/send", json={"setting": "led_mode", "value": 4})
    assert r.status_code == 403 and fl.sent == []


def test_api_enable_send_verify(api):
    c, fl, touch, srv = api
    tok = c.post("/api/control/enable").json()["token"]
    assert c.post("/api/control/confirm", json={"token": tok}).json()["ok"]
    touch()
    r = c.post("/api/control/send", json={"setting": "led_mode", "value": 4}).json()
    assert r["status"] == "verified" and fl.sent == [b"WM4", b"WM4"]
    assert r["changes"] == {"p4.led_mode": [3, 4]}
    log = c.get("/api/control").json()["log"]
    assert log[0]["setting"] == "led_mode" and log[0]["status"] == "verified"


def test_api_refuses_when_not_local(api, monkeypatch):
    c, fl, touch, srv = api
    monkeypatch.setattr(srv, "gate", ctl.ControlGate(addresses=lambda: {"127.0.0.1"}))
    assert c.post("/api/control/enable").status_code == 403


def test_api_refuses_when_moving(api):
    c, fl, touch, srv = api
    c.post("/api/control/confirm", json={"token": c.post("/api/control/enable").json()["token"]})
    srv.state.p0 = {**srv.state.p0, "speed_kmh": 5.0}
    touch()
    r = c.post("/api/control/send", json={"setting": "pedal", "value": "hard"}).json()
    assert r["status"] == "refused" and fl.sent == []


def test_api_backup_and_restore(api):
    c, fl, touch, srv = api
    assert c.post("/api/backup").json()["ok"]                       # led_mode 3 saved
    name = c.get("/api/backup").json()["items"][0]["name"]
    srv.state.p4 = {**srv.state.p4, "led_mode": 1}                    # changed elsewhere
    assert c.get("/api/backup").json()["items"][0]["differs_now"] == {"p4.led_mode": [3, 1]}
    assert c.post("/api/backup/restore", json={"name": name}).status_code == 403   # control off
    c.post("/api/control/confirm", json={"token": c.post("/api/control/enable").json()["token"]})
    touch()
    r = c.post("/api/backup/restore", json={"name": name}).json()
    assert r["results"] == [{"field": "p4.led_mode", "status": "verified", "value": 3}]
    assert fl.sent == [b"WM3", b"WM3"]
