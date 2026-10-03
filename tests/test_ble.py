"""Connection robustness with fake BLE clients: direct connect, hangs, silence, drops,
half-open BlueZ links and forgotten devices always recover – without gaps in listening."""
import asyncio
import re
from pathlib import Path

from falcon.ble import WheelLink
from falcon.protocol import WheelState

FIX = Path(__file__).resolve().parent / "fixtures" / "falcon_pro_idle.log"
CHUNKS = [bytes.fromhex(l.split()[2]) for l in FIX.read_text().splitlines()
          if re.match(r"^\s*\d+\.\d{3} ffe1 ", l)]
ADDR = "AA:BB:CC:DD:EE:FF"


class Dev:
    address = ADDR


class FakeClient:
    """'hang' (wheel not heard), 'silent' (connects, no data), 'drop' (data then gone),
    'ok' (keeps sending), 'unknown' (BlueZ forgot the device)."""

    def __init__(self, behaviour, log):
        self.behaviour, self.log = behaviour, log
        self.is_connected = False
        self.services = []
        self._task = None

    async def connect(self):
        self.log.append(("connect", self.behaviour))
        if self.behaviour == "hang":
            await asyncio.sleep(3600)
        if self.behaviour == "unknown":
            raise Exception(f"Device with address {ADDR} was not found.")
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


def make_link(script, log, *, bluez_connected=False, forgot=None, scans=None, address=ADDR):
    it = iter(script)

    async def scanner(addr, timeout):
        if scans is not None:
            scans.append(addr)
        return Dev()

    async def disconnector(addr):
        log.append(("bluez-disconnect", addr))

    async def forgetter(addr):
        if forgot is not None:
            forgot.append(addr)

    async def state(addr):
        return bluez_connected

    return WheelLink(address, WheelState(), connect_timeout=0.2, stale_s=0.3, scan_timeout=0.1,
                     backoff_min=0.01, backoff_max=0.05, poll_s=0.02, clean_interval=0.05,
                     client_factory=lambda target: FakeClient(next(it, "ok"), log),
                     scanner=scanner, by_name_scanner=lambda t: scanner(None, t),
                     disconnector=disconnector, forgetter=forgetter, bluez_state=state)


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


def test_not_heard_is_not_a_failure_and_listening_continues():
    log = []
    link = make_link(["hang"] * 50, log)
    asyncio.run(run_for(link, 1.0))
    attempts = sum(1 for e in log if e == ("connect", "hang"))
    assert attempts >= 3                          # back-to-back attempts, no long gaps
    assert not link.drops and link.status.startswith(("hittar inte", "väntar på hjulet"))


def test_recovers_from_silence_and_drop():
    log = []
    link = make_link(["hang", "silent", "drop", "ok"], log)
    asyncio.run(run_for(link, 3.0))
    assert any(r.startswith("inga data") for r in link.drops)
    assert any(r.startswith("frånkopplad") for r in link.drops)
    assert link.connects >= 3 and link.state.counts.get(0, 0) > 0
    assert ("disconnect", "hang") in log          # a timed-out attempt is torn down


def test_direct_connect_without_scanning():
    log, scans = [], []
    link = make_link(["ok"], log, scans=scans)
    info = {}
    asyncio.run(run_for(link, 1.0, info))
    assert scans == [] and link.connects == 1 and not link.drops
    st = info["stability"]
    assert st["connected_for_s"] is not None and st["data_pct"] > 50
    assert st["connect_time_s"]["last"] is not None


def test_forgotten_device_triggers_scan_then_connects():
    log, scans = [], []
    link = make_link(["unknown", "ok"], log, scans=scans)
    asyncio.run(run_for(link, 1.0))
    assert scans and link.connects == 1


def test_half_open_bluez_link_is_cleaned_then_forgotten():
    log, forgot = [], []
    link = make_link(["hang"] * 100, log, bluez_connected=True, forgot=forgot)
    asyncio.run(run_for(link, 1.0))
    assert ("bluez-disconnect", ADDR) in log
    assert forgot == [ADDR] or len(forgot) >= 1


def test_unknown_address_scans_by_name():
    log, scans = [], []
    link = make_link(["ok"], log, scans=scans, address=None)
    asyncio.run(run_for(link, 0.8))
    assert scans and link.address_seen == ADDR and link.connects == 1
