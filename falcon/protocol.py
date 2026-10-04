"""Begode Falcon Pro BLE protocol: frame assembly, decoding, command table.

Field names and scale factors follow the official Begode app (com.begode.app 0.1.23,
com.euc.android.protocol.*). Frame layout as captured from a Falcon Pro:

    55 AA | 16 bytes payload (8 x int16 big-endian) | type | sub | 5A 5A 5A 5A

`sub` is 0x18 for single packets, or a group/cell-bank index (types 1, 2, 3).
"""
from __future__ import annotations

import statistics
import struct
import time
from dataclasses import dataclass, field

HEADER = b"\x55\xaa"
FOOTER = b"\x5a\x5a\x5a\x5a"
FRAME_LEN = 24

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"


@dataclass(frozen=True)
class Frame:
    type: int
    sub: int
    payload: bytes  # 16 bytes

    def s16(self) -> tuple[int, ...]:
        return struct.unpack(">8h", self.payload)

    def u16(self) -> tuple[int, ...]:
        return struct.unpack(">8H", self.payload)


class FrameAssembler:
    """Turns the BLE notification byte stream (20-byte chunks) into frames."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.dropped = 0

    def feed(self, data: bytes) -> list[Frame]:
        self.buf += data
        out: list[Frame] = []
        while True:
            i = self.buf.find(HEADER)
            if i < 0:
                # keep a trailing 0x55 that may start the next header
                keep = 1 if self.buf[-1:] == b"\x55" else 0
                self.dropped += len(self.buf) - keep
                del self.buf[: len(self.buf) - keep]
                break
            if i:
                self.dropped += i
                del self.buf[:i]
            if len(self.buf) < FRAME_LEN:
                break
            f = bytes(self.buf[:FRAME_LEN])
            if f[20:24] == FOOTER:
                out.append(Frame(f[18], f[19], f[2:18]))
                del self.buf[:FRAME_LEN]
            else:
                self.dropped += 1
                del self.buf[:1]
        return out


def ascii_replies(data: bytes, min_len: int = 3) -> list[str]:
    """Text answers (e.g. to 'V'/'N') hidden between binary frames: remove every
    complete frame first (their 5A5A5A5A footers read as 'ZZZZ'), then keep printable runs."""
    rest = bytearray()
    i = 0
    while i < len(data):
        if data[i:i + 2] == HEADER and data[i + 20:i + 24] == FOOTER:
            i += FRAME_LEN
            continue
        rest.append(data[i])
        i += 1
    out, cur = [], bytearray()
    for b in rest:
        if 32 <= b < 127:
            cur.append(b)
        else:
            if len(cur) >= min_len:
                out.append(cur.decode())
            cur = bytearray()
    if len(cur) >= min_len:
        out.append(cur.decode())
    # a reply that starts right after a frame cut by the capture window keeps the footer
    # remnant 'Z'/'ZZ'.. (0x5A) in front of it
    return [r.lstrip("Z") for r in out if len(r.lstrip("Z")) >= min_len]


# ---------- decoders ----------
# Field meanings cross-checked against WheelLog (GotwayAdapter) and the Home Assistant
# "begode" integration (verified live on a Falcon). Where those differ from the official
# Begode app's names, WheelLog/HA win and the field is marked in UNCERTAIN.

UNCERTAIN = {
    "p0.word3_raw": "räckvidd enligt Begode-appen, läses inte av WheelLog",
    "p1.group": ("BMS-rad (0–3)", "", "ok"),
    "p1.bms": ("BMS (1 = sträng A, 2 = sträng B)", "", "ok"),
    "p1.half": ("Paketets halva (1/2)", "", "ok"),
    "p1.pwm_limit_or_alarm": "PWM-gräns eller batterivarning (källorna säger olika)",
    "p1.mos": "MOS-bitarna visar 'av' trots ström — betydelse oklar",
}

# field -> (description, unit, status)  status: ok = plausible/confirmed,
# scale = scale from one source, not confirmed under load, unknown = meaning unclear
FIELD_INFO = {
    "p0.voltage_raw": ("Spänning, rå (16S-skalad, ×1,5 = verklig)", "0,01 V", "ok"),
    "p0.speed_kmh": ("Fart", "km/h", "scale"),
    "p0.word3_raw": ("Ord 3 (räckvidd enligt Begode-appen)", "", "unknown"),
    "p0.trip_m": ("Tripp", "m", "ok"),
    "p0.phase_current_a": ("Fasström (motor)", "A", "scale"),
    "p0.board_temp_c": ("Moderkortets temperatur (MPU6050)", "°C", "ok"),
    "p0.word7_raw": ("Ord 7 (PWM bara på egen firmware)", "", "unknown"),
    "p0.flags_raw": ("Flaggor", "", "unknown"),
    "p1.group": ("BMS-rad (0–3)", "", "ok"),
    "p1.bms": ("BMS (1 = sträng A, 2 = sträng B)", "", "ok"),
    "p1.half": ("Paketets halva (1/2)", "", "ok"),
    "p1.pwm_limit_or_alarm": ("PWM-gräns eller batterivarning", "%", "unknown"),
    "p1.voltage_v": ("Batterispänning (BMS)", "V", "ok"),
    "p1.current_a": ("Ström genom detta BMS (shunt)", "A", "scale"),
    "p1.temp_a_c": ("Temperatur (givare 1 eller 3)", "°C", "ok"),
    "p1.temp_b_c": ("Temperatur (givare 2 eller 4)", "°C", "ok"),
    "p1.half_voltage_v": ("Halva paketets spänning", "V", "ok"),
    "p1.activity": ("Aktivitet", "", "ok"),
    "p1.temp_state": ("Temperaturstatus", "", "ok"),
    "p1.volt_state": ("Spänningsstatus", "", "ok"),
    "p1.protection": ("Skydd", "", "ok"),
    "p1.mos": ("MOS", "", "unknown"),
    "p1.cell_balance": ("Cellbalansering", "", "ok"),
    "p1.group_balance": ("Gruppbalansering", "", "ok"),
    "p1.info_raw": ("Statusord, rått", "", "ok"),
    "p4.odometer_raw": ("Mätarställning", "m", "ok"),
    "p4.settings_raw": ("Inställningsord, rått", "", "ok"),
    "p4.pedal_mode_bits": ("Pedalläge (bitar 13–14)", "", "unknown"),
    "p4.speed_alarm_mode": ("Fartlarmläge (bitar 10–11)", "", "scale"),
    "p4.roll_angle_mode": ("Lutningsvinkel-läge (bitar 7–8)", "", "scale"),
    "p4.in_miles": ("Miles", "", "ok"),
    "p4.power_off_in_s": ("Tid kvar till auto-avstängning (nedräkning, 7200 = full)", "s", "ok"),
    "p4.tiltback_kmh": ("Tiltback-fart (bekräftad live med WY48/WY51)", "km/h", "ok"),
    "p4.led_mode": ("LED-/stämningsljusläge", "", "ok"),
    "p4.alert_raw": ("Larmbyte, rått", "", "ok"),
    "p4.alerts": ("Hjulets larm", "", "ok"),
    "p4.light_mode": ("Ljusläge", "", "scale"),
    "p4.vehicle_id_raw": ("Fordons-ID", "", "unknown"),
    "p7.battery_current_a": ("Batteriström (negativ = laddning)", "A", "scale"),
    "p7.advanced_config_raw": ("Avancerad konfiguration", "", "unknown"),
    "p7.motor_temp_c": ("Motortemperatur", "°C", "ok"),
    "p7.pwm_pct": ("PWM", "%", "scale"),
}

# Begode app's meaning first (newer, written for these wheels), WheelLog's second
ALERT_BITS = [(0x01, "strömfel / hög effekt"), (0x02, "MOS bränd / fartlarm 2"), (0x04, "gyrofel / fartlarm 1"),
              (0x08, "låg spänning"), (0x10, "överspänning"), (0x20, "övertemperatur"),
              (0x40, "fel på hallsensor"), (0x80, "låst / transportläge")]


def decode_p0(f: Frame) -> dict:
    v, spd, w3, trip, phase, temp, w7, w8 = f.s16()
    return {
        "voltage_raw": v & 0xFFFF,
        "speed_kmh": round(abs(spd / 100.0 * 3.6), 2),
        "word3_raw": w3,
        "trip_m": trip & 0xFFFF,
        "phase_current_a": abs(phase) / 100.0,
        "board_temp_c": round(temp / 340.0 + 36.53, 1),
        "word7_raw": w7 & 0xFFFF,      # PWM only on custom firmware; junk on stock
        "flags_raw": w8 & 0xFFFF,
    }


_ACTIVITY = {0: "vila", 1: "urladdning", 3: "laddning"}
_HEALTH = {0: "över", 1: "under", 3: "normal"}
_MOS = {0: "på", 1: "av", 3: "fel"}
_PROT = {0: "normal", 1: "kommunikationsfel", 2: "batteri frånkopplat",
         4: "cellspänning onormal", 5: "laddning onormal"}


def decode_p1(f: Frame) -> dict:
    """Smart BMS rows: sub 0,1 = BMS 1 (cells in type 2 = string A); sub 2,3 = BMS 2
    (type 3 = string B). Even sub carries temps 1/2 + half-pack voltage 1, odd sub
    temps 3/4 + half-pack voltage 2. Current is per BMS (repeated on both rows)."""
    w1, _w2, volt10, cur10, ta, tb, half10, info = f.s16()
    info &= 0xFFFF
    return {
        "group": f.sub,
        "bms": 1 if f.sub < 2 else 2,
        "half": 1 if f.sub % 2 == 0 else 2,
        "pwm_limit_or_alarm": w1,
        "voltage_v": volt10 / 10.0,
        "current_a": cur10 / 10.0,
        "temp_a_c": ta,
        "temp_b_c": tb,
        "half_voltage_v": half10 / 10.0,
        "activity": _ACTIVITY.get((info >> 14) & 3, "okänd"),
        "temp_state": _HEALTH.get((info >> 12) & 3, "okänd"),
        "volt_state": _HEALTH.get((info >> 10) & 3, "okänd"),
        "protection": _PROT.get((info >> 4) & 7, "okänd"),
        "mos": _MOS.get(info & 3, "okänd"),
        "cell_balance": bool((info >> 7) & 1),
        "group_balance": bool((info >> 3) & 1),
        "info_raw": info,
    }


def decode_cells(f: Frame) -> dict:
    """Types 2 (BMS 1, string A) and 3 (BMS 2, string B): 8 cells in mV, sub = bank 0..2."""
    return {"string": "A" if f.type == 2 else "B", "bank": f.sub,
            "cells_mv": list(f.u16())}


def decode_p4(f: Frame) -> dict:
    hi, lo, settings, apo, w5, w6, w7, vid = f.u16()
    p = f.payload
    alert = p[12]                          # frame byte 14
    return {
        "odometer_raw": (hi << 16) | lo,
        "settings_raw": settings,
        "pedal_mode_bits": (settings >> 13) & 3,
        "speed_alarm_mode": (settings >> 10) & 3,
        "roll_angle_mode": (settings >> 7) & 3,
        "in_miles": bool(settings & 1),
        "power_off_in_s": apo,          # countdown, not a setting (seen 7200 -> 0 -> off)
        "tiltback_kmh": w5,
        "led_mode": p[11],                 # frame byte 13 (ambient light)
        "alert_raw": alert,
        "alerts": [name for bit, name in ALERT_BITS if alert & bit],
        "light_mode": p[13] & 3,           # frame byte 15
        "vehicle_id_raw": vid,
    }


def decode_p7(f: Frame) -> dict:
    batt, adv, mtemp, pwm, *_ = f.s16()
    return {
        "battery_current_a": -batt / 100.0,   # negative = charging
        "advanced_config_raw": adv & 0xFFFF,
        "motor_temp_c": mtemp,
        "pwm_pct": abs(pwm),
    }


@dataclass
class WheelState:
    """Latest decoded values. Cells are kept per string/bank."""
    p0: dict = field(default_factory=dict)
    p4: dict = field(default_factory=dict)
    p7: dict = field(default_factory=dict)
    groups: dict = field(default_factory=dict)   # BMS row (sub 0..3) -> p1 dict
    cells: dict = field(default_factory=dict)    # "A0".."B2" -> [mV x8]
    counts: dict = field(default_factory=dict)   # frame type -> count
    unknown_types: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)      # "type.sub" -> last raw frame + count
    started: float = field(default_factory=time.time)

    def apply(self, f: Frame) -> None:
        self.counts[f.type] = self.counts.get(f.type, 0) + 1
        k = f"{f.type}.{f.sub}"
        prev = self.raw.get(k, {})
        self.raw[k] = {"ts": time.time(), "hex": f.payload.hex(), "u16": list(f.u16()),
                       "s16": list(f.s16()), "count": prev.get("count", 0) + 1,
                       "first_ts": prev.get("first_ts", time.time())}
        if f.type == 0:
            self.p0 = decode_p0(f)
        elif f.type == 1:
            self.groups[f.sub] = decode_p1(f)
        elif f.type in (2, 3):
            d = decode_cells(f)
            self.cells[f"{d['string']}{d['bank']}"] = d["cells_mv"]
        elif f.type == 4:
            self.p4 = decode_p4(f)
        elif f.type == 7:
            self.p7 = decode_p7(f)
        else:
            self.unknown_types[f.type] = f.payload.hex()

    def bms(self) -> dict:
        """Two BMS units (one per parallel pack), merged from their two rows."""
        out = {}
        # Each BMS sends a current on both of its rows, and they do not always agree: during one
        # charge (2026-10-04, charger showing 7 A) BMS 2 read 5.6/3.85 A and, later, BMS 1 read
        # 5.8/2.9 A - always the first row high. What the rows mean is not known, so use the
        # row closest to what the other rows say (3.6 + 3.85 = 7.4 A, close to the charger).
        mid = statistics.median(abs(g["current_a"]) for g in self.groups.values()) if self.groups else 0.0
        for n, string in ((1, "A"), (2, "B")):
            rows = sorted((g for g in self.groups.values() if g["bms"] == n), key=lambda g: g["half"])
            if not rows:
                continue
            even = next((g for g in rows if g["half"] == 1), rows[0])
            best = min(rows, key=lambda g: abs(abs(g["current_a"]) - mid))
            temps = [t for g in sorted(rows, key=lambda g: g["half"])
                     for t in (g["temp_a_c"], g["temp_b_c"])]
            out[n] = {
                "bms": n, "string": string,
                "current_a": best["current_a"],
                "row_currents_a": [g["current_a"] for g in rows],
                "voltage_v": even["voltage_v"],
                "half_voltages_v": [g["half_voltage_v"] for g in sorted(rows, key=lambda g: g["half"])],
                "temps_c": temps,
                "temp_max_c": max(temps) if temps else None,
                "activity": even["activity"],
                "protection": [g["protection"] for g in rows],
                "mos": [g["mos"] for g in rows],
                "cell_balance": any(g["cell_balance"] for g in rows),
                "group_balance": any(g["group_balance"] for g in rows),
                "temp_state": [g["temp_state"] for g in rows],
                "volt_state": [g["volt_state"] for g in rows],
            }
        return out

    def bms_charging(self) -> bool:
        return any(x["activity"] == "laddning" for x in self.bms().values())

    def battery_current(self) -> float | None:
        """Pack current (+ = discharge): the controller's measurement (p7).

        The BMS rows' current field does not follow the load: on the 2026-10-03 ride it had
        ~0 correlation with p7 and read 0.0-0.2 A while the controller drew 15-25 A. It is
        kept only while a BMS reports charging (not yet checked against p7) or without p7."""
        b = self.bms()
        bms_sum = sum(abs(x["current_a"]) for x in b.values()) if b else None
        if bms_sum is not None and self.bms_charging():
            return -bms_sum
        i = self.p7.get("battery_current_a")
        return i if i is not None else bms_sum

    def cell_summary(self) -> dict:
        out = {}
        for s in ("A", "B"):
            mv = [v for k in sorted(self.cells) if k[0] == s for v in self.cells[k]]
            if mv:
                out[s] = {"cells_mv": mv, "count": len(mv), "min_mv": min(mv),
                          "max_mv": max(mv), "spread_mv": max(mv) - min(mv),
                          "sum_v": round(sum(mv) / 1000.0, 2)}
        return out

    def snapshot(self) -> dict:
        return {"p0": self.p0, "p4": self.p4, "p7": self.p7,
                "groups": self.groups, "bms": self.bms(),
                "battery_current_a": self.battery_current(),
                "cells": self.cell_summary(),
                "counts": self.counts, "unknown_types": self.unknown_types}


# ---------- commands ----------
# Encodings from com.euc.android.protocol.WheelCommandBuilder.
# `absolute` commands set a value and are safe to send twice;
# toggles must be sent exactly once. Nothing in BLOCKED is ever sent.

@dataclass(frozen=True)
class Command:
    name: str
    payload: bytes
    absolute: bool


def cmd_pedal_mode(mode: str) -> Command:
    return Command(f"pedal_mode={mode}", {"hard": b"h", "medium": b"f", "soft": b"s"}[mode], True)


def cmd_tiltback_speed(kmh: int) -> Command:
    v = max(3, min(90, kmh)); v = 3 + ((v - 3) // 3) * 3
    return Command(f"tiltback={v}", b"WY%02d" % v, True)


def cmd_beeper_volume(level: int) -> Command:
    v = max(1, min(9, level))
    return Command(f"beeper_volume={v}", b"WB%d" % v, True)


def cmd_power_alarm(pct: int) -> Command:
    v = max(50, min(90, pct)); v = 50 + ((v - 50) // 5) * 5
    return Command(f"power_alarm={v}", b"WP%02d" % v, True)


def cmd_ambient_mode(mode: int) -> Command:
    v = max(0, min(9, mode))
    return Command(f"ambient_mode={v}", b"WM%d" % v, True)


def cmd_headlight(state: str) -> Command:
    return Command(f"headlight={state}", {"on": b"Q", "off": b"E", "flash": b"T"}[state], True)


CMD_BEEP = Command("beep", b"b", False)
CMD_REQUEST_VERSION = Command("request_version", b"V", False)
CMD_REQUEST_NAME = Command("request_name", b"N", False)

# Never sent by this app: calibration, gear ratio, brake cutoff / power bridge
# (same byte 'e'/'x' for two functions), run-mode toggle, tilt-shutdown gear
# (same '<' '=' '>' as gear ratio), current limit, firmware upgrade.
BLOCKED_BYTES = {b"cy", b"<", b"=", b">", b"e", b"x", b"+-", b"m", b"g"}
BLOCKED_PREFIXES = (b"Wl", b"WC", b"WU", b"WX", b"WR")


def is_blocked(payload: bytes) -> bool:
    return payload in BLOCKED_BYTES or payload.startswith(BLOCKED_PREFIXES)
