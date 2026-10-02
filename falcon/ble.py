"""Read-only BLE link to the wheel: connect, subscribe, decode, auto-reconnect.

Step 2: this module has no write path at all. Commands arrive in step 4.
"""
from __future__ import annotations

import asyncio
import logging
import time

from bleak import BleakClient, BleakScanner

from .protocol import CHAR_UUID, FrameAssembler, WheelState

log = logging.getLogger("falcon.ble")

NAME_PREFIXES = ("GotWay", "Begode", "BEGODE")


class WheelLink:
    def __init__(self, address: str | None, state: WheelState, on_frame=None):
        self.address = address
        self.address_seen: str | None = None
        self.state = state
        self.on_frame = on_frame
        self.asm = FrameAssembler()
        self.connected = False
        self.last_frame_ts = 0.0
        self.status = "startar"
        self.rssi = None
        self.device_info: dict = {}

    def _notify(self, _h, data: bytearray) -> None:
        for f in self.asm.feed(bytes(data)):
            self.state.apply(f)
            self.last_frame_ts = time.time()
            if self.on_frame:
                self.on_frame(f)

    async def _read_device_info(self, c: BleakClient) -> None:
        names = {"00002a26": "firmware", "00002a27": "hardware", "00002a29": "manufacturer"}
        for s in c.services:
            for ch in s.characteristics:
                key = ch.uuid[:8]
                if key in names and "read" in ch.properties:
                    try:
                        v = await c.read_gatt_char(ch)
                        self.device_info[names[key]] = v.rstrip(b"\x00").decode(errors="replace")
                    except Exception:
                        pass

    async def run(self) -> None:
        backoff = 2
        while True:
            try:
                self.status = "söker"
                # hard outer timeout: a BlueZ discovery session can hang forever
                try:
                    addr = self.address or self.address_seen   # lock to the first wheel found
                    if addr:
                        find = BleakScanner.find_device_by_address(addr, timeout=15)
                    else:   # no address configured: first wheel advertising a Begode name
                        find = BleakScanner.find_device_by_filter(
                            lambda d, ad: (d.name or ad.local_name or "").startswith(NAME_PREFIXES),
                            timeout=15)
                    dev = await asyncio.wait_for(find, 25)
                except asyncio.TimeoutError:
                    dev = None
                if dev is not None and not self.address_seen:
                    self.address_seen = dev.address
                if dev is None:
                    self.status = "hittar inte hjulet (avstängt, för långt bort eller anslutet till mobilen?)"
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue
                self.status = "ansluter"
                async with BleakClient(dev, timeout=20) as c:
                    self.connected = True
                    self.status = "ansluten"
                    backoff = 2
                    await self._read_device_info(c)
                    await c.start_notify(CHAR_UUID, self._notify)
                    while c.is_connected:
                        await asyncio.sleep(1)
                        if self.last_frame_ts and time.time() - self.last_frame_ts > 10:
                            self.status = "ansluten men inga data på 10 s"
                self.connected = False
                self.status = "frånkopplad"
            except Exception as e:  # keep running whatever happens
                self.connected = False
                self.status = f"fel: {e}"
                log.warning("ble error: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def info(self) -> dict:
        age = time.time() - self.last_frame_ts if self.last_frame_ts else None
        return {"address": self.address or self.address_seen, "connected": self.connected, "status": self.status,
                "last_frame_age_s": round(age, 1) if age is not None else None,
                "dropped_bytes": self.asm.dropped, "device_info": self.device_info}
