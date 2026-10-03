"""BLE link to the wheel: connect, subscribe, decode, auto-reconnect.

The only write path is WheelLink.write(), called exclusively by falcon/control.py
(which is off by default). Blocked commands are refused here as a last line of defence.

Robustness (every step has a hard timeout, nothing can hang forever):
  * known address: direct connect – BlueZ connects on the first advertisement it hears,
    no scan windows with gaps; scanning only for an unknown/forgotten device,
  * the whole connect -> service discovery -> subscribe sequence is bounded,
  * a data watchdog drops the link when no frame arrives for STALE_S seconds
    (the wheel normally sends a notification every ~0.05 s),
  * after every failure the link is torn down on the BlueZ side too, so a
    half-open connection never keeps the wheel busy for us or the phone,
  * backoff (1 → 10 s) only after an established connection ended – never while the
    wheel is merely out of range or off,
  * after `forget_after` failed connects in a row, BlueZ is told to forget the
    device (clears a stuck 'Connected' state that disconnect cannot fix),
  * every drop is counted with its reason.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, deque

from bleak import BleakClient, BleakScanner

from .protocol import CHAR_UUID, FrameAssembler, WheelState, is_blocked

log = logging.getLogger("falcon.ble")

NAME_PREFIXES = ("GotWay", "Begode", "BEGODE")


async def _run_bounded(cmd: list[str], timeout: float) -> None:
    """Run a helper command; kill it if it outlives the timeout (bluetoothctl can wait
    forever, e.g. for an unknown device), so no orphan process is left behind."""
    p = None
    try:
        p = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL,
                                                 stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(p.wait(), timeout)
    except BaseException:
        if p is not None and p.returncode is None:
            try:
                p.kill()
                await asyncio.wait_for(p.wait(), 2)
            except BaseException:
                pass


async def bluez_is_connected(address: str) -> bool:
    """True if BlueZ itself believes it is connected to the wheel."""
    try:
        p = await asyncio.create_subprocess_exec("bluetoothctl", "info", address,
                                                 stdin=asyncio.subprocess.DEVNULL,
                                                 stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 5)
        return b"Connected: yes" in out
    except BaseException:
        try:
            p.kill()
        except Exception:
            pass
        return False


async def bluez_disconnect(address: str) -> None:
    """Drop a (possibly half-open) BlueZ connection to the wheel."""
    await _run_bounded(["bluetoothctl", "disconnect", address], 5)


async def bluez_remove(address: str) -> None:
    """Last resort: make BlueZ forget the wheel. Seen live: BlueZ kept the device as
    'Connected' while every disconnect failed with 'Disconnected (0x0e)' and every
    connect timed out; removing the device object cleared it."""
    await _run_bounded(["bluetoothctl", "remove", address], 8)


LAST_RSSI: dict = {}     # address -> (rssi dBm, ts) from the advertisement that found it


async def _scan(match, timeout: float):
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for dev, adv in found.values():
        if match(dev, adv):
            LAST_RSSI[dev.address] = (adv.rssi, time.time())
            return dev
    return None


async def scan_by_name(timeout: float):
    return await _scan(lambda d, ad: (d.name or ad.local_name or "").startswith(NAME_PREFIXES), timeout)


async def scan_by_address(address: str, timeout: float):
    return await _scan(lambda d, ad: d.address.upper() == address.upper(), timeout)


class WheelLink:
    def __init__(self, address: str | None, state: WheelState, on_frame=None, *,
                 connect_timeout: float = 25.0, stale_s: float = 8.0, scan_timeout: float = 15.0,
                 backoff_min: float = 1.0, backoff_max: float = 10.0, poll_s: float = 0.5,
                 client_factory=None, scanner=None, by_name_scanner=None, disconnector=None,
                 forgetter=None, forget_after: int = 2, bluez_state=None, clean_interval: float = 60.0):
        self.address = address
        self.address_seen: str | None = None
        self.state = state
        self.on_frame = on_frame
        self.asm = FrameAssembler()
        self.connected = False
        self.last_frame_ts = 0.0
        self.status = "startar"
        self.device_info: dict = {}
        # tunables / injectable parts (tests)
        self.connect_timeout, self.stale_s, self.scan_timeout = connect_timeout, stale_s, scan_timeout
        self.backoff_min, self.backoff_max, self.poll_s = backoff_min, backoff_max, poll_s
        self.client_factory = client_factory or (lambda target: BleakClient(target, timeout=20))
        self.scanner = scanner or scan_by_address
        self.by_name_scanner = by_name_scanner or scan_by_name
        self.disconnector = disconnector or bluez_disconnect
        self.forgetter = forgetter or bluez_remove
        self.bluez_connected = bluez_state or bluez_is_connected
        self._needs_scan = False
        self.clean_interval = clean_interval
        self.attempt_started = 0.0
        self.connect_times: deque = deque(maxlen=20)
        self.forget_after = forget_after
        self.fail_streak = 0
        self.forgets = 0
        # stability statistics
        self.started = time.time()
        self.connects = 0
        self.drops: Counter = Counter()
        self.last_drop: tuple[float, str] | None = None
        self.connected_since: float | None = None
        self.data_s = 0.0
        self._last_notify = 0.0
        self.client = None
        self.notifs: deque = deque(maxlen=400)     # (ts, raw bytes) for text replies
        self.on_event = None          # callback(str) for the event log
        self.paused_until = 0.0       # "release": stay disconnected until this time

    # ---------- data ----------
    def _notify(self, _h, data: bytearray) -> None:
        now = time.time()
        if self._last_notify and now - self._last_notify < self.stale_s:
            self.data_s += now - self._last_notify
        self._last_notify = now
        self.notifs.append((now, bytes(data)))
        for f in self.asm.feed(bytes(data)):
            self.state.apply(f)
            self.last_frame_ts = time.time()
            if self.on_frame:
                self.on_frame(f)

    async def _read_device_info(self, c) -> None:
        names = {"00002a26": "firmware", "00002a27": "hardware", "00002a29": "manufacturer"}
        for s in c.services:
            for ch in s.characteristics:
                key = ch.uuid[:8]
                if key in names and "read" in ch.properties:
                    try:
                        v = await asyncio.wait_for(c.read_gatt_char(ch), 3)
                        self.device_info[names[key]] = v.rstrip(b"\x00").decode(errors="replace")
                    except Exception:
                        pass

    # ---------- the only write path ----------
    async def write(self, payload: bytes) -> None:
        if is_blocked(payload):
            raise PermissionError(f"spärrat kommando {payload!r}")
        c = self.client
        if not (self.connected and c is not None and c.is_connected):
            raise ConnectionError("inte ansluten till hjulet")
        await asyncio.wait_for(c.write_gatt_char(CHAR_UUID, payload, response=False), 3)

    def notifications_since(self, ts: float) -> bytes:
        return b"".join(d for t, d in self.notifs if t >= ts)

    # ---------- helpers ----------
    def _event(self, msg: str) -> None:
        log.info(msg)
        if self.on_event:
            try:
                self.on_event(msg)
            except Exception:
                pass

    def _drop(self, reason: str) -> None:
        self.drops[reason] += 1
        self.last_drop = (time.time(), reason)
        self._event(f"Bluetooth-avbrott: {reason}")

    async def _teardown(self, client, address: str | None) -> None:
        self.connected = False
        self.client = None
        self.connected_since = None
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), 5)
            except Exception:
                pass
        if address:
            await self.disconnector(address)

    async def _scan_for_wheel(self):
        """Scan (only when the address is unknown, or BlueZ has forgotten the device)."""
        addr = self.address or self.address_seen
        if addr:
            return await asyncio.wait_for(self.scanner(addr, self.scan_timeout), self.scan_timeout + 10)
        dev = await asyncio.wait_for(self.by_name_scanner(self.scan_timeout), self.scan_timeout + 10)
        if dev is not None:
            self.address_seen = dev.address
        return dev

    def release(self, seconds: float) -> None:
        self.paused_until = time.time() + seconds
        self._event(f"Bluetooth: anslutningen släpps i {seconds/60:.0f} min (hjulet fritt för mobilappen)")

    def resume(self) -> None:
        self.paused_until = 0.0
        self._event("Bluetooth: återtar anslutningen")

    @property
    def paused(self) -> bool:
        return time.time() < self.paused_until

    async def _session(self, client, connect_timeout: float) -> tuple[bool, str]:
        """Connect + subscribe (bounded), then watch the data flow.
        Returns (reached_connected, reason it ended)."""
        async def setup():
            await client.connect()
            if "firmware" not in self.device_info:
                await self._read_device_info(client)
            await client.start_notify(CHAR_UUID, self._notify)
        t0 = time.time()
        try:
            await asyncio.wait_for(setup(), connect_timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return False, f"ingen kontakt inom {time.time() - t0:.0f} s"
        except Exception as e:
            msg = str(e) or type(e).__name__
            if "not found" in msg.lower():
                return False, "okänd för BlueZ"
            return False, f"anslutning misslyckades: {msg}"
        self.connected = True
        self.client = client
        self.connected_since = time.time()
        self.connects += 1
        self.connect_times.append(round(self.connected_since - self.attempt_started, 1))
        self.status = "ansluten"
        self._event(f"Bluetooth: ansluten (efter {self.connected_since - self.attempt_started:.0f} s)")
        t_sub = time.time()
        while True:
            await asyncio.sleep(self.poll_s)
            if self.paused:
                return True, "släppt för mobilappen"
            if not client.is_connected:
                return True, "frånkopplad (hjulet avstängt eller utom räckhåll)"
            last = self.last_frame_ts if self.last_frame_ts > t_sub else t_sub
            if time.time() - last > self.stale_s:
                return True, f"inga data på {self.stale_s:.0f} s"

    # ---------- main loop ----------
    async def run(self) -> None:
        """Known address: connect DIRECTLY (BlueZ listens for the wheel's advertisement and
        connects on the first one heard – no scan windows with gaps). Unknown address or a
        device BlueZ has forgotten: scan. Waiting with backoff only after a connection
        that was established and then failed, never while the wheel is simply not around."""
        backoff = self.backoff_min
        last_clean = 0.0
        while True:
            client, address = None, self.address or self.address_seen
            if self.paused:
                left = self.paused_until - time.time()
                self.status = f"släppt – hjulet fritt för mobilappen ({left/60:.0f} min kvar)"
                await asyncio.sleep(min(2.0, max(left, 0.1)))
                continue
            self.attempt_started = self.attempt_started or time.time()
            try:
                if not self.connected and address and time.time() - last_clean > self.clean_interval:
                    # half-open BlueZ link (seen live) keeps the wheel from advertising
                    if await self.bluez_connected(address):
                        self._event("Bluetooth: BlueZ visade en halvöppen anslutning – kopplar ner den")
                        await self.disconnector(address)
                        self.fail_streak += 1
                    last_clean = time.time()
                target = address
                if target is None or self._needs_scan:
                    self.status = "söker"
                    try:
                        dev = await self._scan_for_wheel()
                    except (asyncio.TimeoutError, TimeoutError):
                        dev = None
                    if dev is None:
                        self.status = "hittar inte hjulet (avstängt, för långt bort eller anslutet till mobilen?)"
                        await asyncio.sleep(0.2)             # keep listening, no gap
                        continue
                    self._needs_scan = False
                    target, address = dev, getattr(dev, "address", None) or address
                self.status = "väntar på hjulet" if not isinstance(target, str) else "väntar på hjulet (direktanslutning)"
                client = self.client_factory(target)
                ok, reason = await self._session(client, self.connect_timeout)
                if reason == "släppt för mobilappen":
                    await self._teardown(client, address)
                    continue
                if not ok:
                    await self._teardown(client, address)
                    if reason == "okänd för BlueZ":
                        self._needs_scan = True          # repopulate BlueZ's cache by scanning
                    elif reason.startswith("anslutning misslyckades"):
                        self._drop(reason)
                        self.fail_streak += 1
                    else:                                # wheel simply not heard: keep waiting
                        self.status = "hittar inte hjulet (avstängt, för långt bort eller anslutet till mobilen?)"
                    if self.fail_streak >= self.forget_after and address:
                        self._event(f"Bluetooth: {self.fail_streak} misslyckanden i rad – BlueZ får glömma hjulet")
                        await self.forgetter(address)
                        self.forgets += 1
                        self.fail_streak = 0
                        self._needs_scan = True
                    await asyncio.sleep(0.2)
                    continue
                # it was connected and has now ended
                self._drop(reason)
                self.fail_streak = 0
                self.attempt_started = 0.0
                backoff = self.backoff_min
            except asyncio.CancelledError:
                await self._teardown(client, address)
                raise
            except Exception as e:                  # keep running whatever happens
                self._drop(f"fel: {e}")
                self.fail_streak += 1
            await self._teardown(client, address)
            self.status = f"återansluter om {backoff:.0f} s"
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.backoff_max)

    def info(self) -> dict:
        now = time.time()
        age = now - self.last_frame_ts if self.last_frame_ts else None
        addr = self.address or self.address_seen
        rssi = LAST_RSSI.get(addr) if addr else None
        return {"address": addr, "connected": self.connected,
                "rssi_dbm": rssi[0] if rssi else None,
                "rssi_age_s": round(now - rssi[1]) if rssi else None,
                "status": self.status,
                "paused_until": self.paused_until if self.paused else None,
                "last_frame_age_s": round(age, 1) if age is not None else None,
                "dropped_bytes": self.asm.dropped, "device_info": self.device_info,
                "stability": {
                    "connects": self.connects,
                    "connect_time_s": {"last": self.connect_times[-1] if self.connect_times else None,
                                       "median": sorted(self.connect_times)[len(self.connect_times) // 2]
                                       if self.connect_times else None},
                    "bluez_forgets": self.forgets,
                    "drops": dict(self.drops),
                    "last_drop": {"ts": self.last_drop[0], "reason": self.last_drop[1]} if self.last_drop else None,
                    "connected_for_s": round(now - self.connected_since) if self.connected_since else None,
                    "data_pct": min(100.0, round(100 * self.data_s / max(now - self.started, 1), 1)),
                }}
