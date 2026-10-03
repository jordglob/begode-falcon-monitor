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
    is_moving: returns a reason (str) or True if changes must be refused, else None/False

    Seen live: the wheel can take SECONDS before its reports show a new value. So after the
    two writes every fresh frame is read until the value matches or `settle_timeout` passes;
    only then is it a mismatch (and the server keeps watching for a late change).
    """

    def __init__(self, send: Callable[[bytes], Awaitable[None]],
                 read: Callable[[], tuple[int, object]],
                 is_moving: Callable[[], object],
                 frame_timeout: float = 2.0, gap: float = 0.15, settle_timeout: float = 5.0):
        self.send = send
        self.read = read
        self.is_moving = is_moving
        self.frame_timeout = frame_timeout
        self.gap = gap
        self.settle_timeout = settle_timeout

    async def apply(self, cmd: Command, expected: object = None) -> VerifyResult:
        r = VerifyResult(cmd.name, "unknown", ts=time.time())
        if is_blocked(cmd.payload):
            r.status, r.detail = "refused", "spärrat kommando"
            return r
        why = self.is_moving()
        if why:
            r.status, r.detail = "refused", why if isinstance(why, str) else "hjulet rullar"
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
        # read every frame produced after the last write until it matches or time runs out
        t_sent = time.monotonic()
        gen0, _ = self.read()
        seen_fresh = False
        last = None
        while True:
            now = time.monotonic()
            gen, val = self.read()
            if gen > gen0:
                seen_fresh, last, gen0 = True, val, gen
                if val == expected:
                    r.after = val
                    r.status = "verified"
                    r.detail = f"bekräftat efter {now - t_sent:.1f} s"
                    return r
            if not seen_fresh and now - t_sent > self.frame_timeout:
                r.status, r.detail = "unknown", "inget färskt paket — läs om"
                return r
            if now - t_sent > self.settle_timeout:
                r.after = last
                r.status = "mismatch"
                r.detail = (f"hjulet visar {last}, väntat {expected} efter {self.settle_timeout:.0f} s"
                            " – bevakas vidare")
                return r
            await asyncio.sleep(0.05)
