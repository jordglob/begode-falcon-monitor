"""Why is the wheel beeping? – state-change logging at packet rate + a black box.

Two buzzers can beep on a Begode wheel:
  * the MAINBOARD buzzer: speed alarms, 80 % power/PWM alarm, low/over-voltage, hall
    sensor, board temperature, fall-down … (patterns documented by the community);
  * the BMS buzzer inside the battery: independent of the mainboard and the app – it sounds
    on cell under-voltage (~2.8 V), over-temperature and BMS faults and is NOT reported over
    Bluetooth as such. What can be seen is the BMS status (protection, voltage/temperature
    state, MOS) and the cell voltages, which is what this module watches.

Every change of a watched value is logged with a timestamp. A rolling 60 s buffer of raw
frames is kept; on any alarm-level change, or when the rider presses "Jag hör pip nu!",
the buffer (30 s before + 15 s after) is written to a black-box file for analysis.
"""
from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path

from . import guard

# packet 4, frame byte 14 – two interpretations exist; the official app's is newer
ALERT_BITS = [
    (0x01, "strömfel / hög effekt", "hög effekt (80 %-larm)", "alarm"),
    (0x02, "MOS-transistor bränd", "fartlarm 2", "alarm"),
    (0x04, "gyrofel", "fartlarm 1", "alarm"),
    (0x08, "låg spänning", "låg spänning", "alarm"),
    (0x10, "överspänning", "överspänning", "alarm"),
    (0x20, "övertemperatur", "övertemperatur", "alarm"),
    (0x40, "fel på hallsensor", "fel på hallsensor", "alarm"),
    (0x80, "låst", "transportläge", "info"),
]

# known beep patterns (community documentation of the mainboard buzzer + BMS buzzer facts)
PATTERNS = [
    ("Mainboard", "2 pip/s", "Fartlarm 1", "fart över larmgräns 1", "ja – fart i paket 0"),
    ("Mainboard", "3 pip/s", "Fartlarm 2", "fart över larmgräns 2", "ja – fart i paket 0"),
    ("Mainboard", "5 pip/s, går inte att stänga av", "80 %-larm (effekt/PWM)", "motorn nära gränsen – SAKTA IN",
     "ja – PWM i paket 7, larmbit 0x01"),
    ("Mainboard", "1 pip var 2:a s (under 7 km/h)", "Låg spänning", "batteriet nästan tomt", "ja – spänning, bit 0x08"),
    ("Mainboard", "2 pip var 2:a s (7–14 km/h)", "Låg spänning", "batteriet nästan tomt", "ja – spänning, bit 0x08"),
    ("Mainboard", "3 pip var 2:a s (över 14 km/h)", "Låg spänning", "batteriet nästan tomt", "ja – spänning, bit 0x08"),
    ("Mainboard", "2 pip var 0,5 s", "Fel på hallsensor", "motorns lägesgivare – kör inte", "ja – bit 0x40"),
    ("Mainboard", "2 korta pip var 2:a s", "Hög temperatur på moderkortet", "låt svalna", "ja – temperatur, bit 0x20"),
    ("Mainboard", "3 korta pip var 2:a s", "Överspänning", "t.ex. full laddning + inbromsning", "ja – spänning, bit 0x10"),
    ("Mainboard", "1 pip/s, 5 gånger", "Fall / extremt låg spänning vid start", "", "delvis"),
    ("BMS (i batteriet)", "ihållande pip som inte slutar", "Cell under ~2,8 V", "akut – ladda inom 48 h",
     "delvis – cellspänningar och BMS spänningsstatus"),
    ("BMS (i batteriet)", "pip från batteriet", "Över-temperatur / BMS-fel / avbrott i balanseringstråd",
     "kontrollera paketet", "delvis – BMS temperatur-/skyddsstatus, cell som avviker"),
    ("BMS (i batteriet)", "pip + ett paket slutar ge ström", "Skydd har löst ut (t.ex. överström, avbränd shunt)",
     "FARLIGT – andra paketet bär allt", "ja – BMS-ström 0 A, MOS/skydd, obalansvakten"),
]

PRE_S, POST_S = 30.0, 15.0


class BeepWatch:
    def __init__(self, folder: Path, on_event=None, on_record=None):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.on_event = on_event or (lambda m: None)
        self.on_record = on_record or (lambda e: None)
        self.state: dict = {}
        self.events: deque = deque(maxlen=500)
        self.frames: deque = deque()                 # (ts, type, sub, hex) last 60 s
        self.pending: list[dict] = []                # black boxes waiting for their "after" part
        self.marks: deque = deque(maxlen=50)

    # ---------- feeding ----------
    def frame(self, ts: float, ftype: int, sub: int, payload_hex: str) -> None:
        self.frames.append((ts, ftype, sub, payload_hex))
        while self.frames and ts - self.frames[0][0] > PRE_S + POST_S + 15:
            self.frames.popleft()
        done = [p for p in self.pending if ts >= p["until"]]
        for p in done:
            self.pending.remove(p)
            self._write_box(p)

    def check(self, ts: float, snap: dict, battery_current: float | None) -> list[dict]:
        """Compare watched values with the previous ones; log every change."""
        watched = self._watched(snap, battery_current)
        out = []
        for key, (val, level, text) in watched.items():
            old = self.state.get(key)
            if old is None and key not in self.state:
                self.state[key] = val
                if level == "alarm" and val not in (None, False, "normal", "på", 0):
                    out.append(self._event(ts, key, None, val, level, text))
                continue
            if val != old:
                self.state[key] = val
                lvl = level if val not in (None, False, "normal", 0) else "info"
                out.append(self._event(ts, key, old, val, lvl, text))
        return out

    def _watched(self, snap: dict, battery_current: float | None) -> dict:
        w = {}
        p4 = snap.get("p4") or {}
        raw = p4.get("alert_raw")
        if raw is not None:
            for bit, app_lbl, wl_lbl, level in ALERT_BITS:
                on = bool(raw & bit)
                w[f"larmbit 0x{bit:02x}"] = (on, level, f"{app_lbl} (Begode-appen) / {wl_lbl} (WheelLog)")
        for k, b in (snap.get("bms") or {}).items():
            name = f"BMS {k}"
            w[f"{name} skydd"] = (",".join(sorted(set(b.get("protection") or []))), "alarm", "BMS-skydd")
            w[f"{name} spänningsstatus"] = (",".join(sorted(set(b.get("volt_state") or []))), "alarm",
                                            "BMS spänningsstatus (över/under/normal)")
            w[f"{name} temperaturstatus"] = (",".join(sorted(set(b.get("temp_state") or []))), "alarm",
                                             "BMS temperaturstatus")
            w[f"{name} MOS"] = (",".join(sorted(set(b.get("mos") or []))), "warn", "BMS MOS-brytare")
        cells = [v for s in (snap.get("cells") or {}).values() for v in s.get("cells_mv", [])]
        if cells:
            lo = min(cells)
            band = "under 2,8 V – BMS-summern" if lo < 2800 else "under 3,0 V" if lo < 3000 else \
                "under 3,3 V" if lo < 3300 else "normal"
            w["lägsta cell"] = (band, "alarm", "lägsta cellspänning")
        bms = snap.get("bms") or {}
        if guard.SHUNT_CHECKS and len(bms) == 2 and battery_current is not None and abs(battery_current) > 5:
            cur = {k: abs(b.get("current_a") or 0) for k, b in bms.items()}
            drop = [k for k, v in cur.items() if v < 0.1 * max(cur.values())]
            w["paket som inte bär ström"] = (f"BMS {drop[0]}" if drop else None, "alarm",
                                            "ett paket bär ingen ström under belastning")
        pwm = (snap.get("p7") or {}).get("pwm_pct")
        if pwm is not None:
            w["PWM-zon"] = ("≥80 % (5 pip/s)" if pwm >= 80 else "≥70 %" if pwm >= 70 else "normal",
                            "alarm", "effektlarm, mainboard piper 5 ggr/s vid 80 %")
        flags = (snap.get("p0") or {}).get("flags_raw")
        if flags is not None:
            w["paket 0 flaggor (okänd betydelse)"] = (flags, "info", "rå flaggor – loggas för att kunna kopplas till pip")
        return w

    def _event(self, ts, key, old, new, level, text) -> dict:
        e = {"ts": ts, "key": key, "old": old, "new": new, "level": level, "text": text}
        self.events.appendleft(e)
        try:
            self.on_record(e)
        except Exception:
            pass
        if level == "alarm" and new not in (None, False, "normal"):
            self.on_event(f"🔔 {key}: {old} → {new} ({text})")
            self.capture(ts, f"{key}: {old} → {new}")
        return e

    # ---------- black box ----------
    def capture(self, ts: float, reason: str) -> None:
        if any(abs(p["ts"] - ts) < 5 for p in self.pending):
            return                                    # one box per burst
        self.pending.append({"ts": ts, "until": ts + POST_S, "reason": reason})

    def mark(self, ts: float, note: str = "") -> dict:
        m = {"ts": ts, "note": note}
        self.marks.appendleft(m)
        self.capture(ts, "användaren hörde pip" + (f": {note}" if note else ""))
        self.on_event("👂 Pip markerat" + (f": {note}" if note else ""))
        return m

    def _write_box(self, p: dict) -> None:
        t0, t1 = p["ts"] - PRE_S, p["ts"] + POST_S
        frames = [f for f in self.frames if t0 <= f[0] <= t1]
        evs = [e for e in self.events if t0 <= e["ts"] <= t1]
        name = time.strftime("blackbox-%Y%m%d-%H%M%S.json", time.localtime(p["ts"]))
        (self.folder / name).write_text(json.dumps({
            "reason": p["reason"], "ts": p["ts"], "window_s": [PRE_S, POST_S],
            "events": sorted(evs, key=lambda e: e["ts"]),
            "frames": [{"t": round(f[0] - p["ts"], 3), "type": f[1], "sub": f[2], "hex": f[3]} for f in frames],
        }, ensure_ascii=False))

    def boxes(self, limit: int = 30) -> list[dict]:
        out = []
        for f in sorted(self.folder.glob("blackbox-*.json"), reverse=True)[:limit]:
            try:
                d = json.loads(f.read_text())
                out.append({"name": f.name, "ts": d["ts"], "reason": d["reason"], "events": len(d["events"]),
                            "frames": len(d["frames"])})
            except Exception:
                pass
        return out

    def report(self) -> dict:
        active = [{"key": k, "value": v} for k, v in self.state.items()
                  if v not in (None, False, "normal", "") and not k.startswith("paket 0")
                  and not (k.endswith("MOS") and v in ("av", "på"))]
        return {"events": list(self.events)[:200], "active": active, "marks": list(self.marks),
                "boxes": self.boxes(), "patterns": PATTERNS,
                "alert_bits": [{"bit": f"0x{b:02x}", "app": a, "wheellog": w} for b, a, w, _ in ALERT_BITS]}
