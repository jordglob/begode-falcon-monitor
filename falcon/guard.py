"""Pack imbalance guard ("obalansvakt").

Background: a previous pack failure on this wheel was a BMS current shunt made of
4 parallel resistors where one was never soldered. The remaining 3 carried 133 %
current each (178 % heat) until they burned, one by one. Because the BMS assumes
4 resistors, a missing one makes that group REPORT ~33 % too much current, and
every further burned resistor is a STEP (x1.33 -> x2 -> x4). That is what the
shunt checks look for, first.

Checks, in priority order:
  1. shunt ratio   - each BMS group's current vs the other groups (under load)
  2. shunt sum     - sum of BMS group currents vs the controller's total current
  3. pack dropout  - a group at ~0 A while the others carry the load
  4. string drift  - string A vs B mean cell voltage (at rest)
  5. temperature   - group temperature spread (under load)
  6. cells/banks/balancing - single-cell outliers, bank offsets, stuck balancing

The wheel has two packs in parallel, each with its own smart BMS (BMS 1 = string A,
BMS 2 = string B, per WheelLog). Each BMS measures its pack current with its own shunt,
so the shunt checks compare BMS 1 with BMS 2, and their sum with the controller's
battery current (p7).
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

LOAD_A = 5.0           # controller current needed for current-sharing checks
REST_A = 1.0           # below this, voltages are compared (no load sag)
LEARN_SAMPLES = 120    # load samples needed before a learned baseline is trusted

# thresholds (start values; adjustable)
SHUNT_WARN = 0.20      # group reads >20 % above the others / its own baseline
SHUNT_STEP = 0.25      # sudden jump of >25 % in a group's ratio = alarm
SUM_WARN = 0.20        # BMS sum differs >20 % from controller current
DROPOUT_FRAC = 0.10    # group below 10 % of the others' mean under load
CELL_WARN_MV, CELL_ALARM_MV = 30, 60
STRING_WARN_MV, STRING_ALARM_MV = 20, 40
BANK_WARN_MV = 15
TEMP_WARN_C, TEMP_ALARM_C = 8, 15
BALANCE_WARN_S = 2 * 3600

LEVELS = {"ok": 0, "info": 1, "warn": 2, "alarm": 3}


@dataclass
class Finding:
    level: str
    code: str
    where: str
    text: str
    value: float | None = None


class Ema:
    def __init__(self, alpha: float):
        self.alpha, self.v = alpha, None

    def add(self, x: float) -> float:
        self.v = x if self.v is None else self.v + self.alpha * (x - self.v)
        return self.v


@dataclass
class GroupTrack:
    fast: Ema = field(default_factory=lambda: Ema(0.3))
    slow: Ema = field(default_factory=lambda: Ema(0.02))
    learn: list = field(default_factory=list)
    baseline: float | None = None
    step_count: int = 0
    dropout_count: int = 0
    balance_since: float | None = None


class Guard:
    def __init__(self, baseline: dict | None = None):
        self.groups: dict[int, GroupTrack] = {}
        self.sum_learn: list = []
        self.sum_baseline: float | None = None
        self.load_samples = 0
        self.findings: list[Finding] = []
        if baseline:
            self.load_baseline(baseline)

    # ---------- baseline persistence ----------
    def export_baseline(self) -> dict:
        return {"groups": {str(k): g.baseline for k, g in self.groups.items() if g.baseline},
                "sum": self.sum_baseline, "load_samples": self.load_samples}

    def load_baseline(self, b: dict) -> None:
        for k, v in (b.get("groups") or {}).items():
            self.groups.setdefault(int(k), GroupTrack()).baseline = v
        self.sum_baseline = b.get("sum")
        self.load_samples = b.get("load_samples", 0)

    @property
    def learned(self) -> bool:
        return self.load_samples >= LEARN_SAMPLES

    # ---------- main entry ----------
    def update(self, snap: dict, now: float | None = None) -> list[Finding]:
        now = time.time() if now is None else now
        out: list[Finding] = []
        bms = {int(k): v for k, v in (snap.get("bms") or {}).items()}
        pack_i = snap.get("battery_current_a")
        p7_i = (snap.get("p7") or {}).get("battery_current_a")
        out += self._alert_checks((snap.get("p4") or {}).get("alerts") or [])
        if bms and pack_i is not None:
            if abs(pack_i) >= LOAD_A and len(bms) >= 2:
                self.load_samples += 1
                out += self._shunt_checks(bms, p7_i)
                out += self._temp_checks(bms)
            if abs(pack_i) < REST_A:
                out += self._voltage_checks(snap.get("cells") or {})
            out += self._balance_checks(bms, now)
        if not any(LEVELS[f.level] >= LEVELS["warn"] for f in out):
            out.insert(0, Finding("ok", "ok", "", "Packen i balans"))
        if not self.learned:
            out.append(Finding("info", "learning", "",
                               f"Lär sig strömfördelningen mellan BMS 1 och BMS 2: {self.load_samples}/"
                               f"{LEARN_SAMPLES} mätningar under belastning (kör med X230 inom räckhåll)."))
        self.findings = out
        return out

    # ---------- wheel's own alert bits (p4 byte 14) ----------
    def _alert_checks(self, alerts: list[str]) -> list[Finding]:
        severe = {"fel på hallsensor", "övertemperatur", "överspänning", "låg spänning"}
        return [Finding("alarm" if a in severe else "info" if a == "transportläge" else "warn",
                        "wheel_alert", "hjulet", f"Hjulet larmar: {a}") for a in alerts]

    # ---------- 1-3: shunt / current sharing ----------
    def _shunt_checks(self, groups: dict, ctrl_i: float | None) -> list[Finding]:
        out = []
        cur = {k: abs(g.get("current_a") or 0.0) for k, g in groups.items()}
        for k, ik in cur.items():
            others = [v for j, v in cur.items() if j != k]
            mo = statistics.mean(others) if others else 0.0
            t = self.groups.setdefault(k, GroupTrack())
            # 3. dropout: this group carries ~nothing while the others carry the load
            if mo > 1.0 and ik < DROPOUT_FRAC * mo:
                t.dropout_count += 1
                if t.dropout_count >= 3:
                    out.append(Finding("alarm", "pack_dropout", _name(k),
                                       f"{_name(k)} bär ingen ström ({ik:.1f} A) medan det andra paketet bär "
                                       f"{mo:.1f} A — paketet kan vara bortkopplat (avbränd shunt "
                                       f"eller bruten förbindelse). Kör inte hårt.", ik))
                continue
            t.dropout_count = 0
            if mo <= 0.5:
                continue
            ratio = ik / mo
            fast, slow = t.fast.add(ratio), t.slow.add(ratio)
            if t.baseline is None:
                t.learn.append(ratio)
                if len(t.learn) >= LEARN_SAMPLES:
                    t.baseline = statistics.median(t.learn)
            # 1a. reads clearly more than the other groups (absolute, catches a defect
            #     present from day one) or more than its own learned normal (drift)
            dev_abs = fast - 1.0
            dev_base = fast / t.baseline - 1.0 if t.baseline else 0.0
            dev = max(dev_abs, dev_base)
            if dev > SHUNT_WARN:
                ref = "det andra paketet" if dev_abs >= dev_base else "sitt normalläge"
                out.append(Finding("warn", "shunt_ratio", _name(k),
                                   f"{_name(k)} visar {dev*100:.0f} % mer ström än {ref} — passar med "
                                   f"ett olött eller avbränt shuntmotstånd (1 av 4 saknas ≈ +33 %).",
                                   round(dev * 100, 1)))
            # 1b. step: a sudden jump = another resistor just went
            if slow and fast / slow - 1.0 > SHUNT_STEP:
                t.step_count += 1
                if t.step_count >= 5:
                    out.append(Finding("alarm", "shunt_step", _name(k),
                                       f"Plötsligt steg i {_name(k)}s strömmätning "
                                       f"(+{(fast/slow-1)*100:.0f} %) — ännu ett shuntmotstånd kan ha "
                                       f"brunnit av. Sluta köra hårt och kontrollera paketet.",
                                       round((fast / slow - 1) * 100, 1)))
            else:
                t.step_count = 0
        # 2. sum of BMS currents vs controller current
        total = sum(cur.values())
        if ctrl_i is not None and abs(ctrl_i) > LOAD_A and total > 0:
            r = total / abs(ctrl_i)
            if self.sum_baseline is None:
                self.sum_learn.append(r)
                if len(self.sum_learn) >= LEARN_SAMPLES:
                    self.sum_baseline = statistics.median(self.sum_learn)
            elif abs(r / self.sum_baseline - 1.0) > SUM_WARN:
                out.append(Finding("warn", "shunt_sum", "BMS ↔ moderkort",
                                   f"BMS 1 + BMS 2 ({total:.1f} A) stämmer inte med moderkortets "
                                   f"ström ({abs(ctrl_i):.1f} A) — {(r/self.sum_baseline-1)*100:+.0f} % mot "
                                   f"normalläget. En shunt kan mäta fel.", round(r, 3)))
        return out

    # ---------- 4 + 6: voltages at rest ----------
    def _voltage_checks(self, cells: dict) -> list[Finding]:
        out = []
        means = {}
        for s, st in cells.items():
            mv = st.get("cells_mv") or []
            if len(mv) < 8:
                continue
            med = statistics.median(mv)
            means[s] = statistics.mean(mv)
            for i, v in enumerate(mv):
                d = v - med
                lvl = "alarm" if abs(d) > CELL_ALARM_MV else "warn" if abs(d) > CELL_WARN_MV else None
                if lvl:
                    out.append(Finding(lvl, "cell", f"sträng {s}, bank {i//8}, cell {i%8+1}",
                                       f"Sträng {s}, bank {i//8}, cell {i%8+1} (nr {i+1}): {d:+.0f} mV "
                                       f"mot strängens median.", d))
            for b in range(len(mv) // 8):
                bm = statistics.mean(mv[b * 8:(b + 1) * 8])
                if abs(bm - means[s]) > BANK_WARN_MV:
                    out.append(Finding("warn", "bank", f"sträng {s}, bank {b}",
                                       f"Sträng {s}, bank {b}: medel {bm - means[s]:+.0f} mV mot strängen.",
                                       bm - means[s]))
        if "A" in means and "B" in means:
            d = means["A"] - means["B"]
            lvl = "alarm" if abs(d) > STRING_ALARM_MV else "warn" if abs(d) > STRING_WARN_MV else None
            if lvl:
                hi = "A" if d > 0 else "B"
                out.append(Finding(lvl, "string", "sträng A ↔ B",
                                   f"Sträng A och B skiljer {abs(d):.0f} mV per cell i vila. Sträng {hi} ligger "
                                   f"högre — kan betyda att den inte laddats ur lika mycket, dvs. bar mindre "
                                   f"ström (bortkopplat paket eller högre motstånd).", d))
        return out

    # ---------- 5: temperatures under load ----------
    def _temp_checks(self, bms: dict) -> list[Finding]:
        temps = {k: b["temp_max_c"] for k, b in bms.items() if b.get("temp_max_c") is not None}
        if len(temps) < 2:
            return []
        hot = max(temps, key=temps.get)
        spread = temps[hot] - min(temps.values())
        lvl = "alarm" if spread > TEMP_ALARM_C else "warn" if spread > TEMP_WARN_C else None
        if not lvl:
            return []
        return [Finding(lvl, "temp", _name(hot),
                        f"{_name(hot)} är {spread:.0f} °C varmare än det andra paketet under belastning — "
                        f"paketet jobbar hårdare.", spread)]

    # ---------- 6: balancing that never finishes ----------
    def _balance_checks(self, bms: dict, now: float) -> list[Finding]:
        out = []
        for k, b in bms.items():
            t = self.groups.setdefault(k, GroupTrack())
            if b.get("cell_balance"):
                if t.balance_since is None:
                    t.balance_since = now
                dur = now - t.balance_since
                if dur > BALANCE_WARN_S:
                    out.append(Finding("warn", "balance", _name(k),
                                       f"{_name(k)} har balanserat i {dur/3600:.1f} h utan uppehåll — "
                                       f"kan betyda ett trasigt balanseringsmotstånd.", dur))
            else:
                t.balance_since = None
        return out


def _name(k: int) -> str:
    return f"BMS {k} (sträng {'A' if k == 1 else 'B'})"


def summary(findings: list[Finding]) -> dict:
    worst = max((LEVELS[f.level] for f in findings), default=0)
    level = next(k for k, v in LEVELS.items() if v == worst)
    return {"level": level,
            "findings": [f.__dict__ for f in sorted(findings, key=lambda f: -LEVELS[f.level])]}
