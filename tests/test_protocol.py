"""Replays a real BLE capture from a Falcon Pro (2026-10-02, wheel standing still)."""
import asyncio
import re
from pathlib import Path

import pytest

from falcon.energy import SagEstimator, energy_view
from falcon.protocol import (FrameAssembler, WheelState, cmd_pedal_mode, cmd_tiltback_speed,
                             cmd_power_alarm, is_blocked, Command)
from falcon.verify import Verifier

CAPTURE = Path(__file__).resolve().parent / "fixtures" / "falcon_pro_idle.log"


def chunks():
    for line in CAPTURE.read_text().splitlines():
        if re.match(r"^\s*\d+\.\d{3} ffe1 ", line):
            yield bytes.fromhex(line.split()[2])


@pytest.fixture(scope="module")
def replay():
    asm, st = FrameAssembler(), WheelState()
    n = 0
    for c in chunks():
        for f in asm.feed(c):
            st.apply(f)
            n += 1
    return st, n, asm


def test_all_frames_assembled(replay):
    st, n, _ = replay
    assert n == 783
    assert st.counts == {0: 156, 1: 156, 2: 79, 3: 78, 4: 157, 7: 157}
    assert st.unknown_types == {}


def test_packet0(replay):
    p0 = replay[0].p0
    assert p0["speed_kmh"] == 0
    assert 25 < p0["board_temp_c"] < 40


def test_battery_groups(replay):
    st = replay[0]
    assert sorted(st.groups) == [0, 1, 2, 3]
    b = st.bms()
    assert sorted(b) == [1, 2] and b[1]["string"] == "A" and b[2]["string"] == "B"
    assert b[1]["voltage_v"] == 97.2 and b[1]["protection"] == ["normal", "normal"]
    assert b[1]["half_voltages_v"] == [49.4, 49.3]          # half-pack voltages
    assert len(b[1]["temps_c"]) == 4 and b[1]["temp_max_c"] == 36
    assert st.battery_current() == pytest.approx(0.1)        # BMS 1 0.0 + BMS 2 0.1


def test_packet0_and_7_currents(replay):
    st = replay[0]
    assert st.p0["phase_current_a"] == pytest.approx(0.2)    # p0 word 5 = phase current /100
    assert st.p7["battery_current_a"] == pytest.approx(-0.78)
    assert st.p7["motor_temp_c"] == 30


def test_cells_two_strings_of_24(replay):
    cs = replay[0].cell_summary()
    assert cs["A"]["count"] == 24 and cs["B"]["count"] == 24
    for s in cs.values():
        assert 4000 < s["min_mv"] <= s["max_mv"] < 4250
        assert abs(s["sum_v"] - 97.2) < 2.0   # 24 cells in series ~ pack voltage


def test_packet4_settings(replay):
    p4 = replay[0].p4
    assert p4["tiltback_kmh"] == 51
    assert p4["power_off_in_s"] == 7200
    assert p4["led_mode"] == 3
    assert p4["alerts"] == [] and p4["in_miles"] is False
    assert 830_000 < p4["odometer_raw"] < 840_000


def test_alert_bits():
    from falcon.protocol import Frame, decode_p4
    payload = bytes(12) + bytes([0x60, 0x00]) + bytes(2)     # frame byte 14 = payload[12]
    d = decode_p4(Frame(4, 0x18, payload[:16]))
    assert d["alerts"] == ["övertemperatur", "fel på hallsensor"]


def test_energy_view(replay):
    st = replay[0]
    ev = energy_view(st.snapshot(), SagEstimator().estimate(), {})
    assert ev["battery"]["cell_spread_mv"] < 20
    assert ev["bus"]["sag"]["ohm"] is None  # no load in the capture


def test_assembler_resyncs_on_garbage():
    asm = FrameAssembler()
    good = bytes.fromhex("55aa19500000003d0021fe5cfa150c8800010018") + b"\x5a" * 4
    out = asm.feed(b"\x01\x55\x02" + good[:7]) + asm.feed(good[7:])
    assert len(out) == 1 and out[0].type == 0


def test_sag_estimator():
    s = SagEstimator()
    for i in range(30):
        a = 2 + i
        s.add(a, 100.0 - 0.05 * a, ts=1000 + i)
    e = s.estimate()
    assert e["ohm"] == pytest.approx(0.05, abs=1e-6)
    assert e["ocv_v"] == pytest.approx(100.0, abs=0.01)


def test_command_encodings():
    assert cmd_pedal_mode("hard").payload == b"h"
    assert cmd_tiltback_speed(40).payload == b"WY39"   # step 3 from 3, like the official app
    assert cmd_power_alarm(83).payload == b"WP80"
    for p in (b"cy", b"<", b"=", b">", b"e", b"x", b"+-", b"Wl5", b"WX1"):
        assert is_blocked(p)
    assert not is_blocked(b"h")


class FakeWheel:
    """Applies writes; produces a fresh settings frame shortly after."""

    def __init__(self, value, ignore_writes=0):
        self.value, self.gen, self.sent, self.ignore = value, 0, [], ignore_writes

    async def send(self, payload):
        self.sent.append(payload)
        if self.ignore:
            self.ignore -= 1
        else:
            self.value = payload

        async def tick():
            await asyncio.sleep(0.02)
            self.gen += 1
        asyncio.ensure_future(tick())

    def read(self):
        return self.gen, self.value


def run(coro):
    return asyncio.run(coro)


def test_verify_writes_twice_reads_once():
    w = FakeWheel(b"s")
    v = Verifier(w.send, w.read, lambda: False, frame_timeout=0.5, gap=0.01, settle_timeout=0.3)
    r = run(v.apply(cmd_pedal_mode("hard"), expected=b"h"))
    assert r.status == "verified" and r.sends == 2 and w.sent == [b"h", b"h"]


def test_verify_survives_one_lost_write():
    w = FakeWheel(b"s", ignore_writes=1)
    v = Verifier(w.send, w.read, lambda: False, frame_timeout=0.5, gap=0.01, settle_timeout=0.3)
    assert run(v.apply(cmd_pedal_mode("hard"), expected=b"h")).status == "verified"


def test_verify_reports_mismatch():
    w = FakeWheel(b"s", ignore_writes=2)
    v = Verifier(w.send, w.read, lambda: False, frame_timeout=0.5, gap=0.01, settle_timeout=0.3)
    r = run(v.apply(cmd_pedal_mode("hard"), expected=b"h"))
    assert r.status == "mismatch" and r.after == b"s"


def test_verify_refuses_when_moving_and_blocked():
    w = FakeWheel(b"s")
    v = Verifier(w.send, w.read, lambda: True)
    assert run(v.apply(cmd_pedal_mode("hard"), b"h")).status == "refused"
    v = Verifier(w.send, w.read, lambda: False)
    assert run(v.apply(Command("cal", b"cy", True), None)).status == "refused"
    assert w.sent == []


def test_toggle_sent_once():
    w = FakeWheel(0)
    v = Verifier(w.send, w.read, lambda: False, gap=0.01)
    r = run(v.apply(Command("beep", b"b", False), None))
    assert r.sends == 1 and r.status == "unverifiable"


def test_verify_unknown_when_no_fresh_frame():
    class Silent(FakeWheel):
        async def send(self, payload):
            self.sent.append(payload)
    w = Silent(b"s")
    v = Verifier(w.send, w.read, lambda: False, frame_timeout=0.2, gap=0.01)
    assert run(v.apply(cmd_pedal_mode("hard"), b"h")).status == "unknown"


def test_soc_guess_and_bus_index(replay):
    from falcon.energy import soc_from_cell_v, bus_index
    assert soc_from_cell_v(4.20) == 100 and soc_from_cell_v(2.9) == 0
    assert 88 < soc_from_cell_v(4.11) < 93
    ev = energy_view(replay[0].snapshot(), SagEstimator().estimate(), {})
    assert ev["battery"]["soc_guess_valid"] and 85 < ev["battery"]["soc_guess_pct"] < 95
    assert bus_index(0.10, 0.08) == 80.0 and bus_index(None, 0.08) is None


def test_raw_store_covers_every_packet_row(replay):
    raw = replay[0].raw
    assert set(raw) == {"0.24", "1.0", "1.1", "1.2", "1.3", "2.0", "2.1", "2.2",
                        "3.0", "3.1", "3.2", "4.24", "7.24"}
    assert raw["1.0"]["count"] == 39 and len(raw["0.24"]["u16"]) == 8


def test_field_info_covers_all_decoded_fields(replay):
    from falcon.protocol import FIELD_INFO
    st = replay[0]
    for t, d in ((0, st.p0), (4, st.p4), (7, st.p7), (1, st.groups[0])):
        for name in d:
            if (t, name) in ((1, "group"), (1, "bms"), (1, "half")):
                continue
            assert f"p{t}.{name}" in FIELD_INFO, f"p{t}.{name}"


def test_web_page_has_unique_ids_and_tab_sections():
    import re
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text()
    ids = re.findall(r'\bid="([^"$]+)"', html)
    dup = sorted({i for i in ids if ids.count(i) > 1})
    assert dup == [], dup
    for tab in re.findall(r'<button data-t="([a-z]+)"', html):
        assert f'<section id="{tab}"' in html, tab        # every tab button has its own section


def test_settings_reachable_from_header():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text()
    head = html.split("<nav>")[0]
    assert 'onclick="togglePrefs()"' in head and '<section id="prefs" class="overlay">' in html


def test_verify_waits_for_a_slow_wheel():
    """Seen live: the wheel showed the new value only after a while."""
    class Slow(FakeWheel):
        async def send(self, payload):
            self.sent.append(payload)

            async def later():
                await asyncio.sleep(0.4)                 # applies late
                self.value = payload
            async def frames():
                for _ in range(20):
                    await asyncio.sleep(0.05)
                    self.gen += 1
            asyncio.ensure_future(later())
            asyncio.ensure_future(frames())
    w = Slow(b"s")
    v = Verifier(w.send, w.read, lambda: None, frame_timeout=0.5, gap=0.01, settle_timeout=2.0)
    r = run(v.apply(cmd_pedal_mode("hard"), expected=b"h"))
    assert r.status == "verified" and "bekräftat efter" in r.detail


def test_refusal_reason_is_shown():
    w = FakeWheel(b"s")
    v = Verifier(w.send, w.read, lambda: "motorn arbetar (fasström)")
    r = run(v.apply(cmd_pedal_mode("hard"), b"h"))
    assert r.status == "refused" and r.detail == "motorn arbetar (fasström)"
