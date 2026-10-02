"""Wheel control — OFF by default, switched on from the web page, ONLY on the computer
that runs the server (client IP = loopback or one of the host's own addresses).

Safety layers, in order:
  1. gate: off at every server start; enabling needs a two-step confirmation from a
     local client; auto-off after IDLE_OFF_S without a command; anyone may switch OFF,
  2. whitelist: only the commands in SETTINGS can be sent; protocol.is_blocked() is
     checked again at the BLE write,
  3. motion: refused while the wheel moves or when telemetry is stale,
  4. write twice -> read once (verify.Verifier); every attempt is logged with the
     full before/after diff of all decoded fields, so unmapped settings show what moved.
"""
from __future__ import annotations

import secrets
import subprocess
import time

from .protocol import (Command, cmd_ambient_mode, cmd_beeper_volume, cmd_headlight,
                       cmd_pedal_mode, cmd_power_alarm, CMD_BEEP, CMD_REQUEST_NAME,
                       CMD_REQUEST_VERSION)

IDLE_OFF_S = 600
CONFIRM_WINDOW_S = 30
MAX_SPEED_KMH = 0.5
MAX_PHASE_A = 3.0
MAX_DATA_AGE_S = 2.0


def own_addresses() -> set[str]:
    addrs = {"127.0.0.1", "::1"}
    try:
        out = subprocess.run(["ip", "-o", "addr"], capture_output=True, text=True, timeout=3).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) > 3 and parts[2] in ("inet", "inet6"):
                addrs.add(parts[3].split("/")[0])
    except Exception:
        pass
    return addrs


class ControlGate:
    def __init__(self, addresses=None, clock=time.time):
        self._addresses = addresses        # callable -> set[str]; None = own_addresses
        self._cached: set[str] = set()
        self._cached_at = 0.0
        self.clock = clock
        self.enabled = False
        self.enabled_at: float | None = None
        self.last_activity = 0.0
        self.pending_token: str | None = None
        self.pending_at = 0.0

    def is_local(self, host: str | None) -> bool:
        if not host:
            return False
        now = self.clock()
        if now - self._cached_at > 60 or not self._cached:
            self._cached = (self._addresses or own_addresses)()
            self._cached_at = now
        return host in self._cached or host.startswith("127.")

    def _expire(self) -> None:
        if self.enabled and self.clock() - self.last_activity > IDLE_OFF_S:
            self.disable("automatiskt efter inaktivitet")

    def request_enable(self, host: str) -> str:
        if not self.is_local(host):
            raise PermissionError("styrning kan bara slås på från datorn som kör servern")
        self.pending_token = secrets.token_hex(8)
        self.pending_at = self.clock()
        return self.pending_token

    def confirm_enable(self, host: str, token: str) -> None:
        if not self.is_local(host):
            raise PermissionError("styrning kan bara slås på från datorn som kör servern")
        if (not self.pending_token or token != self.pending_token
                or self.clock() - self.pending_at > CONFIRM_WINDOW_S):
            self.pending_token = None
            raise PermissionError("bekräftelsen saknas eller har gått ut – börja om")
        self.pending_token = None
        self.enabled = True
        self.enabled_at = self.last_activity = self.clock()
        self.reason_off = None

    def disable(self, reason: str = "avstängd") -> None:
        self.enabled = False
        self.enabled_at = None
        self.pending_token = None
        self.reason_off = reason

    def check_use(self, host: str) -> None:
        self._expire()
        if not self.is_local(host):
            raise PermissionError("styrning bara från datorn som kör servern")
        if not self.enabled:
            raise PermissionError("styrningen är avstängd")
        self.last_activity = self.clock()

    def status(self, host: str | None) -> dict:
        self._expire()
        left = None
        if self.enabled:
            left = max(0, round(IDLE_OFF_S - (self.clock() - self.last_activity)))
        return {"enabled": self.enabled, "local_client": self.is_local(host),
                "auto_off_in_s": left, "reason_off": getattr(self, "reason_off", None),
                "idle_off_s": IDLE_OFF_S}


def motion_block(snap: dict, data_age_s: float | None) -> str | None:
    """Reason to refuse a command, or None."""
    if data_age_s is None or data_age_s > MAX_DATA_AGE_S:
        return "inga färska data från hjulet"
    p0 = snap.get("p0") or {}
    if (p0.get("speed_kmh") or 0) > MAX_SPEED_KMH:
        return "hjulet rullar"
    if (p0.get("phase_current_a") or 0) > MAX_PHASE_A:
        return "motorn arbetar (fasström)"
    return None


# setting id -> how to build the command, how to read it back (None = not verifiable yet)
def _p4(field):
    return lambda st: (st.counts.get(4, 0), (st.p4 or {}).get(field))


SETTINGS = {
    "beep":      {"label": "Pip", "kind": "action", "build": lambda v: CMD_BEEP, "read": None},
    "led_mode":  {"label": "Stämningsljus (LED-läge)", "kind": "int", "min": 0, "max": 9,
                  "build": lambda v: cmd_ambient_mode(int(v)), "read": _p4("led_mode"),
                  "expected": lambda v: int(v)},
    "pedal":     {"label": "Pedalläge", "kind": "choice",
                  "choices": {"hard": "hårt", "medium": "medel", "soft": "mjukt"},
                  "build": lambda v: cmd_pedal_mode(v), "read": None},
    "light":     {"label": "Strålkastare", "kind": "choice",
                  "choices": {"on": "på", "off": "av", "flash": "blink"},
                  "build": lambda v: cmd_headlight(v), "read": None},
    "beeper":    {"label": "Pipvolym", "kind": "int", "min": 1, "max": 9,
                  "build": lambda v: cmd_beeper_volume(int(v)), "read": None},
    "power_alarm": {"label": "Batterivarning", "kind": "int", "min": 50, "max": 90, "step": 5,
                    "build": lambda v: cmd_power_alarm(int(v)), "read": None},
    "req_version": {"label": "Läs hjulets firmware (V)", "kind": "action",
                    "build": lambda v: CMD_REQUEST_VERSION, "read": None, "ascii_reply": True},
    "req_name":  {"label": "Läs hjulets namn (N)", "kind": "action",
                  "build": lambda v: CMD_REQUEST_NAME, "read": None, "ascii_reply": True},
}
# Tiltback speed (WY..) is deliberately NOT offered until step 3 has mapped where the wheel
# reports it — a wrong tiltback value changes how the wheel behaves at speed.


def build(setting: str, value) -> Command:
    s = SETTINGS.get(setting)
    if not s:
        raise ValueError("okänd inställning")
    if s["kind"] == "int":
        v = int(value)
        if not s["min"] <= v <= s["max"]:
            raise ValueError(f"värdet måste vara {s['min']}–{s['max']}")
    if s["kind"] == "choice" and value not in s["choices"]:
        raise ValueError("ogiltigt val")
    return s["build"](value)


def flat(snap: dict) -> dict:
    """All decoded scalar fields as 'p4.led_mode' -> value (for before/after diffs)."""
    out = {}
    for t in ("p0", "p4", "p7"):
        for k, v in (snap.get(t) or {}).items():
            out[f"{t}.{k}"] = v
    for g, d in (snap.get("groups") or {}).items():
        for k, v in d.items():
            out[f"p1[{g}].{k}"] = v
    return out


VOLATILE = ("speed_kmh", "phase_current_a", "battery_current_a", "board_temp_c", "trip_m",
            "voltage_raw", "word7_raw", "current_a", "voltage_v", "half_voltage_v", "temp_",
            "motor_temp_c", "info_raw", "activity", "cell_balance", "pwm_pct", "flags_raw")


def diff(before: dict, after: dict) -> dict:
    """Changed fields, ignoring values that move by themselves."""
    return {k: [before.get(k), after.get(k)] for k in sorted(set(before) | set(after))
            if before.get(k) != after.get(k) and not any(v in k for v in VOLATILE)}
