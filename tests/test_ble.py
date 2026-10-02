"""Connection robustness with fake BLE clients: hangs, silence and drops always recover."""
import asyncio
from pathlib import Path
import re

from falcon.ble import WheelLink
from falcon.protocol import WheelState

FIX = Path(__file__).resolve().parent / "fixtures" / "falcon_pro_idle.log"
CHUNKS = [bytes.fromhex(l.split()[2]) for l in FIX.read_text().splitlines()
          if re.match(r"^\s*\d+\.\d{3} ffe1 ", l)]


class Dev:
    address = "AA:BB:CC:DD:EE:FF"


class FakeClient:
    """behaviour: 'hang' (connect never returns), 'silent' (connects, no data),
    'drop' (sends data then disconnects), 'ok' (keeps sending)."""

    def __init__(self, behaviour, log):
        self.behaviour, self.log = behaviour, log
        self.is_connected = False
        self.services = []
        self._task = None

    async def connect(self):
        self.log.append(("connect", self.behaviour))
        if self.behaviour == "hang":
            await asyncio.sleep(3600)
        self.is_connected = True

    async def start_notify(self, _uuid, cb):
        if self.behaviour in ("drop", "ok"):
            async def feed():
                for i, c in enumerate(CHUNKS * 50):
                    if self.behaviour == "drop" and i == 60:
                        self.is_connected = False
                        return
                    cb(None, bytearray(c))
                    await asyncio.sleep(0.005)
            self._task = asyncio.ensure_future(feed())

    async def disconnect(self):
        self.log.append(("disconnect", self.behaviour))
        self.is_connected = False
        if self._task:
            self._task.cancel()


def make_link(script, log, bluez, forgot=None):
    it = iter(script)

    async def scanner(addr, timeout):
        return Dev()

    async def disconnector(addr):
        bluez.append(addr)

    return WheelLink("AA:BB:CC:DD:EE:FF", WheelState(), connect_timeout=0.2, stale_s=0.3,
                     scan_timeout=0.1, backoff_min=0.01, backoff_max=0.05, poll_s=0.02,
                     client_factory=lambda dev: FakeClient(next(it, "ok"), log),
                     scanner=scanner, disconnector=disconnector,
                     forgetter=(lambda a: _append(forgot, a)) if forgot is not None else _noop)


async def _noop(addr):
    pass


async def _append(lst, a):
    lst.append(a)


async def run_for(link, secs, snapshot=None):
    t = asyncio.ensure_future(link.run())
    await asyncio.sleep(secs)
    if snapshot is not None:
        snapshot.update(link.info())
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass


def test_recovers_from_hang_silence_and_drop():
    log, bluez = [], []
    link = make_link(["hang", "silent", "drop", "ok"], log, bluez)
    asyncio.run(run_for(link, 3.0))
    reasons = link.drops
    assert any(r.startswith("tidsgräns vid anslutning") for r in reasons)
    assert any(r.startswith("inga data") for r in reasons)
    assert any(r.startswith("frånkopplad") for r in reasons)
    assert link.connects >= 3                      # silent, drop, ok all reached 'connected'
    assert link.state.counts.get(0, 0) > 0         # data flowed in the end
    assert bluez.count("AA:BB:CC:DD:EE:FF") >= 3   # BlueZ side cleaned after each failure
    assert ("disconnect", "hang") in log           # the hung client was torn down


def test_ok_link_stays_up():
    log, bluez = [], []
    link = make_link(["ok"], log, bluez)
    info = {}
    asyncio.run(run_for(link, 1.0, info))
    assert link.connects == 1 and not link.drops
    st = info["stability"]
    assert st["connected_for_s"] is not None and st["data_pct"] > 50


def test_not_found_clears_half_open_link():
    bluez = []

    async def scanner(addr, timeout):
        return None

    async def disconnector(addr):
        bluez.append(addr)

    link = WheelLink("AA:BB:CC:DD:EE:FF", WheelState(), scan_timeout=0.05, backoff_min=0.01,
                     backoff_max=0.02, scanner=scanner, disconnector=disconnector)
    asyncio.run(run_for(link, 0.3))
    assert bluez and link.status.startswith("hittar inte")


def test_forgets_device_after_repeated_connect_timeouts():
    log, bluez, forgot = [], [], []
    link = make_link(["hang", "hang", "ok"], log, bluez, forgot)
    asyncio.run(run_for(link, 2.0))
    assert forgot == ["AA:BB:CC:DD:EE:FF"]
    assert link.connects >= 1 and link.state.counts.get(0, 0) > 0
