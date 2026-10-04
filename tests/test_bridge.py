"""Data passed on by a phone or logger instead of the app's own Bluetooth."""
import re
import time
from pathlib import Path

from falcon.ble import WheelLink, BRIDGE_HOLD_S
from falcon.protocol import WheelState

CAPTURE = Path(__file__).resolve().parent / "fixtures" / "falcon_pro_idle.log"


def raw_chunks():
    return [bytes.fromhex(ln.split()[2]) for ln in CAPTURE.read_text().splitlines()
            if re.match(r"^\s*\d+\.\d{3} ffe1 ", ln)]


def test_injected_bytes_become_frames_like_bluetooth_data():
    frames, st = [], WheelState()
    link = WheelLink(None, st, frames.append)
    assert all(link.inject(c, "iphone") for c in raw_chunks())
    assert len(frames) > 50 and len(st.groups) == 4 and st.p0 and st.p7
    assert link.connected and link.bridged and link.paused is False
    info = link.info()
    assert info["bridge"] == "iphone" and info["status"] == "ansluten via iphone" and info["last_frame_age_s"] < 1


def test_bridge_that_stops_sending_is_dropped():
    link = WheelLink(None, WheelState())
    link.inject(raw_chunks()[0], "logger")
    link.bridge_ts = time.time() - BRIDGE_HOLD_S - 1          # silence
    link.expire_bridge()
    assert not link.connected and not link.bridged and link.info()["bridge"] is None
    assert any("slutade skicka" in k for k in link.drops)


def test_inject_is_refused_while_the_own_bluetooth_has_the_wheel():
    frames = []
    link = WheelLink(None, WheelState(), frames.append)
    link.connected = True                                      # own session up
    assert link.inject(raw_chunks()[0], "iphone") is False and frames == []
