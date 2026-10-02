"""BLE link to the wheel: connect, subscribe, decode, auto-reconnect.

The only write path is WheelLink.write(), called exclusively by falcon/control.py
(which is off by default). Blocked commands are refused here as a last line of defence.

Robustness (every step has a hard timeout, nothing can hang forever):
  * the whole connect -> service discovery -> subscribe sequence is bounded,
  * a data watchdog drops the link when no frame arrives for STALE_S seconds
    (the wheel normally sends a notification every ~0.05 s),
  * after every failure the link is torn down on the BlueZ side too, so a
    half-open connection never keeps the wheel busy for us or the phone,
  * reconnect backoff starts at 1 s and is capped at 10 s,
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


async def bluez_disconnect(address: str) -> None:
    """Drop a (possibly half-open) BlueZ connection to the wheel."""
    try:
        p = await asyncio.create_subprocess_exec(
            "bluetoothctl", "disconnect", address,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(p.wait(), 5)
    except Exception:
        pass


async def bluez_remove(address: str) -> None:
    """Last resort: make BlueZ forget the wheel. Seen live: BlueZ kept the device as
    'Connected' while every disconnect failed with 'Disconnected (0x0e)' and every
    connect timed out; removing the device object cleared it."""
    try:
        p = await asyncio.create_subprocess_exec(
            "bluetoothctl", "remove", address,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(p.wait(), 8)
    except Exception:
        pass


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
                 connect_timeout: float = 30.0, stale_s: float = 8.0, scan_timeout: float = 15.0,
                 backoff_min: float = 1.0, backoff_max: float = 10.0, poll_s: float = 0.5,
                 client_factory=None, scanner=None, by_name_scanner=None, disconnector=None,
                 forgetter=None, forget_after: int = 2):
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

    async def _find(self):
        addr = self.address or self.address_seen      # lock to the first wheel found
        if addr:
            return await asyncio.wait_for(self.scanner(addr, self.scan_timeout), self.scan_timeout + 10)
        dev = await asyncio.wait_for(self.by_name_scanner(self.scan_timeout), self.scan_timeout + 10)
        if dev is not None:
            self.address_seen = dev.address
        return dev

    async def _session(self, client) -> str:
        """Connect + subscribe (bounded), then watch the data flow. Returns the drop reason."""
        async def setup():
            await client.connect()
            if "firmware" not in self.device_info:
                await self._read_device_info(client)
            await client.start_notify(CHAR_UUID, self._notify)
        try:
            await asyncio.wait_for(setup(), self.connect_timeout)
        except asyncio.TimeoutError:
            return f"tidsgräns vid anslutning ({self.connect_timeout:.0f} s)"
        self.connected = True
        self.client = client
        self.connected_since = time.time()
        self.connects += 1
        self.status = "ansluten"
        self._event("Bluetooth: ansluten")
        t_sub = time.time()
        while True:
            await asyncio.sleep(self.poll_s)
            if not client.is_connected:
                return "frånkopplad (hjulet avstängt eller utom räckhåll)"
            last = self.last_frame_ts if self.last_frame_ts > t_sub else t_sub
            if time.time() - last > self.stale_s:
                return f"inga data på {self.stale_s:.0f} s"

    # ---------- main loop ----------
    async def run(self) -> None:
        backoff = self.backoff_min
        while True:
            client, address = None, self.address or self.address_seen
            try:
                self.status = "söker"
                try:
                    dev = await self._find()
                except asyncio.TimeoutError:
                    dev = None
                if dev is None and address:
                    # a half-open BlueZ link stops the wheel advertising: clear it
                    await self.disconnector(address)
                if dev is None:
                    self.status = "hittar inte hjulet (avstängt, för långt bort eller anslutet till mobilen?)"
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self.backoff_max)
                    continue
                address = getattr(dev, "address", None) or address
                self.status = "ansluter"
                client = self.client_factory(dev)
                reason = await self._session(client)
                had_data = self.connected
                self._drop(reason)
                if had_data:
                    backoff = self.backoff_min      # it worked a while: retry fast
                    self.fail_streak = 0
                else:
                    self.fail_streak += 1
            except asyncio.CancelledError:
                await self._teardown(client, address)
                raise
            except Exception as e:                  # keep running whatever happens
                self._drop(f"fel: {e}")
            await self._teardown(client, address)
            if self.fail_streak >= self.forget_after and address:
                self._event(f"Bluetooth: {self.fail_streak} misslyckade anslutningar i rad – BlueZ får glömma hjulet")
                await self.forgetter(address)
                self.forgets += 1
                self.fail_streak = 0
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
                "last_frame_age_s": round(age, 1) if age is not None else None,
                "dropped_bytes": self.asm.dropped, "device_info": self.device_info,
                "stability": {
                    "connects": self.connects,
                    "bluez_forgets": self.forgets,
                    "drops": dict(self.drops),
                    "last_drop": {"ts": self.last_drop[0], "reason": self.last_drop[1]} if self.last_drop else None,
                    "connected_for_s": round(now - self.connected_since) if self.connected_since else None,
                    "data_pct": min(100.0, round(100 * self.data_s / max(now - self.started, 1), 1)),
                }}
