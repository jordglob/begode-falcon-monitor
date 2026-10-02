"""Write-and-verify: a setting is only reported as changed when the wheel itself
reports the new value back in a fresh frame.

Not wired into the live service yet (step 4); covered by tests with a fake wheel.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from .protocol import Command, is_blocked


@dataclass
class VerifyResult:
    command: str
    status: str            # "verified" | "mismatch" | "unverifiable" | "refused" | "unknown"
    before: object = None
    after: object = None
    sends: int = 0
    detail: str = ""
    ts: float = 0.0


class Verifier:
    """
    send:      coroutine that writes raw bytes to the wheel
    read:      returns (generation, value) — generation increments on every fresh
               settings frame, so we never accept a stale value
    is_moving: returns True if the wheel is rolling (changes are refused)
    """

    def __init__(self, send: Callable[[bytes], Awaitable[None]],
                 read: Callable[[], tuple[int, object]],
                 is_moving: Callable[[], bool],
                 frame_timeout: float = 2.0, gap: float = 0.15):
        self.send = send
        self.read = read
        self.is_moving = is_moving
        self.frame_timeout = frame_timeout
        self.gap = gap

    async def _fresh_value(self, after_gen: int) -> Optional[object]:
        deadline = time.monotonic() + self.frame_timeout
        while time.monotonic() < deadline:
            gen, val = self.read()
            if gen > after_gen:
                return val
            await asyncio.sleep(0.05)
        return None

    async def apply(self, cmd: Command, expected: object = None) -> VerifyResult:
        r = VerifyResult(cmd.name, "unknown", ts=time.time())
        if is_blocked(cmd.payload):
            r.status, r.detail = "refused", "spärrat kommando"
            return r
        if self.is_moving():
            r.status, r.detail = "refused", "hjulet rullar"
            return r
        _, r.before = self.read()
        # write twice (absolute commands only; a toggle sent twice undoes itself)
        for i in range(2 if cmd.absolute else 1):
            if i:
                await asyncio.sleep(self.gap)
            await self.send(cmd.payload)
            r.sends += 1
        if expected is None:
            r.status, r.detail = "unverifiable", "skickat, hjulet rapporterar inte värdet"
            return r
        # read once: only a frame produced after the last write counts
        gen_after_write, _ = self.read()
        val = await self._fresh_value(gen_after_write)
        if val is None:
            r.status, r.detail = "unknown", "inget färskt paket — läs om"
            return r
        r.after = val
        if val == expected:
            r.status = "verified"
        else:
            r.status, r.detail = "mismatch", f"hjulet visar {val}, väntat {expected}"
        return r
