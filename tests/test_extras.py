"""Alarms/safety margin, exports, charge limit, link release."""
import asyncio

import pytest

from falcon import export
from falcon.alarms import RideAlarms
from falcon.charging import ChargeController, Plug, HOLD_S
from falcon.ble import WheelLink
from falcon.protocol import WheelState


def snap(pwm=0, speed=0, mtemp=30, btemp=30, cells_v=4.0, i=0.0):
    mv = [round(cells_v * 1000)] * 24
    return {"p0": {"speed_kmh": speed, "board_temp_c": btemp}, "p7": {"pwm_pct": pwm, "motor_temp_c": mtemp},
            "cells": {"A": {"cells_mv": mv}, "B": {"cells_mv": mv}}, "battery_current_a": i}


# ---------- alarms ----------
def test_quiet_when_normal():
    assert RideAlarms().update(snap(pwm=30), 80, now=0) == []


def test_pwm_levels_and_margin():
    a = RideAlarms()
    assert a.update(snap(pwm=72), 80, now=0)[0].level == "warn"
    assert RideAlarms().update(snap(pwm=85), 80, now=0)[0].level == "alarm"
    assert a.report()["safety_margin_pct"] == 28


def test_pwm_prediction_warns_early():
    a = RideAlarms()
    out = []
    for k in range(10):                          # PWM rising 4 %/0.3 s -> 13 %/s
        out = a.update(snap(pwm=40 + 4 * k), 80, now=k * 0.3)
    assert out and out[0].code == "pwm"          # 76 % now, prediction > 80
    assert a.report()["predicted_pwm_3s"] > 80


def test_temps_battery_cells_speed():
    a = RideAlarms({"speed_warn": 40})
    codes = {x.code: x.level for x in a.update(snap(speed=45, mtemp=90, btemp=62, cells_v=3.35), 8, now=0)}
    assert codes == {"speed": "warn", "motor_temp": "alarm", "board_temp": "warn",
                     "battery": "alarm", "cell_min": "warn"}


# ---------- export ----------
TRACK = [[51.0, 0.0, 10.0, 1_790_000_000], [51.001, 0.0, 20.0, 1_790_000_002]]


def test_gpx_is_valid_xml_with_speed():
    import xml.etree.ElementTree as ET
    g = export.ride_gpx({"start": TRACK[0][3]}, TRACK, "tur & test")
    root = ET.fromstring(g)
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    pts = root.findall(".//g:trkpt", ns)
    assert len(pts) == 2 and pts[1].get("lat") == "51.001000"
    assert "<speed>5.56</speed>" in g and "tur &amp; test" in g


def test_csv_ride_and_samples():
    c = export.ride_csv(TRACK).splitlines()
    assert c[0] == "time_utc,latitude,longitude,speed_kmh,plausibility,wheel_kmh,gps_kmh" and len(c) == 3
    s = export.samples_csv([{"ts": 1_790_000_000, "voltage_v": 97.0, "extra": "x"}]).splitlines()
    assert s[0] == "time_utc,voltage_v" and s[1].endswith(",97.0")


# ---------- charge limit ----------
class FakePlug(Plug):
    def __init__(self):
        super().__init__("http://plug", "shelly2", opener=self._open)
        self.calls = []

    def _open(self, url):
        self.calls.append(url)
        return b'{"output": true}'


def test_charge_stops_at_target_after_hold():
    p = FakePlug()
    c = ChargeController(p, {"enabled": True, "target_cell_v": 4.10})
    c.tick(snap(cells_v=4.09, i=-5), True, now=0)
    c.tick(snap(cells_v=4.10, i=-5), True, now=10)
    assert not p.calls
    c.tick(snap(cells_v=4.11, i=-5), True, now=10 + HOLD_S)
    assert p.calls == ["http://plug/rpc/Switch.Set?id=0&on=false"]


def test_charge_limit_does_nothing_when_disabled_stale_or_not_charging():
    p = FakePlug()
    for cfg, fresh, i in (({"enabled": False}, True, -5), ({"enabled": True}, False, -5), ({"enabled": True}, True, 0.2)):
        c = ChargeController(p, cfg)
        c.tick(snap(cells_v=4.15, i=i), fresh, now=0)
        c.tick(snap(cells_v=4.15, i=i), fresh, now=100)
    assert p.calls == []


def test_shelly1_urls():
    p = FakePlug()
    p.kind = "shelly1"
    p.set(True)
    assert p.calls == ["http://plug/relay/0?turn=on"]


# ---------- release ----------
def test_release_keeps_link_down_then_resumes():
    link = WheelLink("AA:BB:CC:DD:EE:FF", WheelState(), backoff_min=0.01, connect_timeout=0.05)
    calls = []

    class Never:
        is_connected = False
        services = []

        async def connect(self):
            calls.append("connect")
            await asyncio.sleep(10)

        async def disconnect(self):
            pass

    async def nothing(addr):
        return False
    link.client_factory = lambda target: Never()
    link.disconnector = nothing
    link.bluez_connected = nothing

    async def go():
        link.release(0.3)
        t = asyncio.ensure_future(link.run())
        await asyncio.sleep(0.2)
        assert calls == [] and link.status.startswith("släppt")
        await asyncio.sleep(0.4)
        t.cancel()
    asyncio.run(go())
    assert calls                                    # trying again after the release
