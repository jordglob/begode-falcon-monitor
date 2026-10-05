"""Pack imbalance guard ("obalansvakt").

Background: a previous pack failure on this wheel was a BMS current shunt made of
4 parallel resistors where one was never soldered. The remaining 3 carried 133 %
current each (178 % heat) until they burned, one by one. Because the BMS assumes
4 resistors, a missing one makes that group REPORT ~33 % too much current, and
every further burned resistor is a STEP (x1.33 -> x2 -> x4). That is what the
shunt checks look for, first.

Checks, in priority order (1-3 are switched off, see SHUNT_CHECKS):
  1. shunt ratio   - each BMS group's current vs the other groups (under load)
  2. shunt sum     - sum of BMS group currents vs the controller's total current
  3. pack dropout  - a group at ~0 A while the others carry the load (off); since 0.27.7 instead
                     the FAST voltage check: the two packs' voltages apart for 2.5 s
  4. string drift  - string A vs B mean cell voltage (at rest)
  5. temperature   - group temperature spread (under load)
  6. cells/banks/balancing - single-cell outliers, bank offsets, stuck balancing
  7. BMS rows       - the two current values of one BMS disagree while charging
  8. silent row     - one of the four current rows reports nothing while the others do
  9. pack current   - the two packs report currents more than 30 % apart (10 s charging, 5 min riding)
 10. group voltage  - the four groups more than 0.5 V apart at rest (Begode's own limit)

The wheel has two packs in parallel, each with its own smart BMS (BMS 1 = string A,
BMS 2 = string B, per WheelLog). Each BMS measures its pack current with its own shunt,
so the shunt checks compare BMS 1 with BMS 2, and their sum with the controller's
battery current (p7).
"""
from __future__ import annotations

import statistics
from collections import deque
import time
from dataclasses import dataclass, field

from .sampling import CurrentTrace, NewFrames, REST_S, half_of, segments

# Checks 1-3 compare the BMS rows' current field, which turned out not to follow the real
# load (2026-10-03: ~0 correlation with the controller's p7 current, false alarms on both
# strings). Off until that field is understood.
SHUNT_CHECKS = False
LOAD_A = 5.0           # controller current needed for current-sharing checks
REST_A = 1.0           # below this, voltages are compared (no load sag)
LEARN_SAMPLES = 120    # load samples needed before a learned baseline is trusted

# thresholds (start values; adjustable)
SHUNT_WARN = 0.20      # group reads >20 % above the others / its own baseline
SHUNT_STEP = 0.25      # sudden jump of >25 % in a group's ratio = alarm
SUM_WARN = 0.20        # BMS sum differs >20 % from controller current
DROPOUT_FRAC = 0.10    # group below 10 % of the others' mean under load
# the two current rows of one BMS while charging: steady current, so the slow field is valid
ROW_MIN_A = 1.0        # both rows must show at least this
ROW_WARN = 0.20        # rows of one BMS differ more than this
ROW_HOLD = 30          # ... for this many updates in a row (rows update seconds apart at plug-in)
# a current row that stays silent while the other three report current. This is how the earlier
# pack failure on this wheel first showed: one of the four rows never reported any amperes.
# The rows are slow and not simultaneous, so they are compared as 3-minute means; on healthy
# rides and charges the lowest row was never below 0.36 of the others at these settings.
# imbalance between the two packs: their reported currents differ by more than PACK_DIFF.
# While charging the current is steady and 10 s is enough (healthy charge: at most 24 %).
# While riding the rows are slow and not simultaneous: over 10 s healthy packs differed by more
# than 30 % a third of the time (117 false alarms in one ride); over 5 minutes at most 21 %.
# FAST pack dropout, from the packs' own voltages. The two packs are in parallel, so each BMS
# must report the same pack voltage. If one pack is cut off (burnt lead or shunt) it stays at
# its resting voltage while the other carries all the current and sags twice as much: about
# 2.2 V apart at 10 A and 4.4 V at 20 A on this wheel. On a healthy 100-minute ride the two
# never stayed even 0.5 V apart in the same direction for 2.5 s (single readings differ by up
# to 2.8 V because the rows are not simultaneous, hence the hold time).
DROP_V = 1.0            # volts apart ...
DROP_HOLD_S = 2.5       # ... in the same direction for this long (about two BMS row cycles)
DROP_FRESH_S = 2.0      # both packs' voltages must be this recent
DROP_KEEP_S = 10.0      # the alarm stays up this long after the last reading that showed it
# FAST dead current row: one of the four rows stays at next to nothing while the other three
# show real current. In the owner's pack failure the broken pack showed 0.1 A, at most 0.3 A.
# A row arrives every 1.2 s but its current value only changes about every 5 s, so 10 s is two
# updates. Healthy rows also sit near zero at times while riding, so the others must be high:
# on a healthy ride the rule gave 0 false alarms at 8 A / 10 s (2 at 5 A / 10 s, 2 at 8 A / 5 s).
# While charging the current is steady and every row carries it, so 2 A is enough there.
# FROZEN GROUP - the signature of the owner's real fault (June 2026, Begode app): the RF group
# 'all the time reports the same voltage and 0 current' while the other three groups move.
# Its measuring board had burnt components; nothing in the wheel flagged it. One group's
# voltage standing completely still AND showing no current, while the other three groups'
# voltages move and they carry current, never happened in 5086 healthy rows (hard riding
# included); with the fault injected under load it was found in a median of 6-8 s.
# The four group voltages at rest. Begode support: "the voltage difference among the four
# batteries is ultimately controlled at 0.5" V. With the June 2026 fault they were 3.0 V apart
# long before the wheel cut out. Healthy at rest: 0.2 V. Only judged after a real rest, because
# the groups are measured at different moments and differ by volts under load.
GROUP_V_WARN, GROUP_V_ALARM = 0.5, 1.0
GROUP_REST_S = 15.0     # rested this long before the group voltages are compared
GROUP_HOLD_S = 10.0     # ... and the spread has been there this long
FROZEN_HOLD_S = 8.0     # the group's voltage has not changed for this long ...
FROZEN_OTHERS_V = 0.3   # ... while each of the other three moved at least this much
FROZEN_OTHERS_A = 1.0   # ... and they show at least this current on average
LOWROW_A = 0.5          # the row shows at most this ...
LOWROW_OTHERS_A = 8.0   # ... while the other three average at least this (riding)
LOWROW_OTHERS_CHARGE_A = 2.0   # ... or this (charging)
LOWROW_HOLD_S = 10.0    # ... for this long
PACK_DIFF = 0.30
PACK_WIN_CHARGE_S = 10
PACK_WIN_RIDE_S = 300
PACK_MIN_A = 1.5       # mean of the two packs needed before a difference means anything
SILENT_WINDOW_S = 180
SILENT_OTHERS_A = 1.5  # mean of the other three rows needed before a silent row means anything
SILENT_FRAC = 0.15     # the row's mean below this share of the others' (healthy minimum: 0.36) ...
SILENT_MAX_A = 0.40    # ... and below this in absolute terms (the owner's broken pack showed 0.1-0.3 A)
CELL_WARN_MV, CELL_ALARM_MV = 30, 60
STRING_WARN_MV, STRING_ALARM_MV = 20, 40
BANK_WARN_MV = 15
TEMP_WARN_C, TEMP_ALARM_C = 8, 15
BALANCE_WARN_S = 2 * 3600

LEVELS = {"ok": 0, "info": 1, "warn": 2, "alarm": 3}

# cell spread UNDER LOAD – measured inside one half-pack (12 cells measured at one instant, see
# sampling.py). The seam between cell 12 and 13 runs through bank 1, and banks arrive in
# different frames, so anything wider than a half-pack mixes different load moments.
LOAD_SPREAD_MIN_A = 5.0        # pack current needed
LOAD_SPREAD_WARN_MV = 80       # absolute limits per half-pack part
LOAD_SPREAD_ALARM_MV = 150
LOAD_CELL_ALARM_MV = 3200      # a cell this low under load can collapse -> cut-out
LOAD_LEARN = 60                # samples to learn the normal mV-per-A spread
LOAD_EXCESS_WARN_MV = 40       # above the learned normal for the same current


COMP_MAX_AGE_S = 6.0          # a cell value older than this is not used
COMP_MIN_CELLS = 40           # of 48
COMP_CELL_WARN_MV, COMP_CELL_ALARM_MV = 40, 80
COMP_BANK_WARN_MV = 30


def _cell_name(string: str, no: int) -> str:
    return f"sträng {string}, bank {(no - 1) // 8}, cell {(no - 1) % 8 + 1} (nr {no})"


class LoadSpread:
    """Cell spread under load, only between cells measured at the same instant and only from
    new measurements taken at a steady current; learns this pack's normal spread per ampere
    and names the cell that sags most."""

    def __init__(self, baseline_mv_per_a: float | None = None):
        self.recent: list[dict] = []
        self.lows: list[dict] = []
        self.learn: list[float] = []
        self.baseline = baseline_mv_per_a
        self.worst_cells: dict[str, int] = {}
        self.comp: dict[str, tuple] = {}            # "A13" -> (ts, compensated mV, raw mV, half key)
        self.trace = CurrentTrace()
        self.frames = NewFrames()
        self.used = self.skipped = 0

    def add(self, ts: float, string: str, bank: int, cells_mv: list[int], pack_current: float | None,
            r_lookup=None) -> None:
        if pack_current is None:
            return
        self.trace.add(ts, pack_current)
        new = self.frames.is_new((string, bank), cells_mv)
        if not new or len(cells_mv) < 2:
            return
        parts = segments(bank, cells_mv)
        # lowest cell: a real reading whenever it was taken, so no steadiness needed
        if (self.trace.peak(ts) or 0.0) >= LOAD_SPREAD_MIN_A:
            j = min(range(len(cells_mv)), key=lambda k: cells_mv[k])
            self.lows.append({"ts": ts, "low_mv": cells_mv[j], "cell": _cell_name(string, bank * 8 + j + 1)})
            self.lows = [r for r in self.lows if ts - r["ts"] <= 10]
        i = self.trace.steady(ts)
        if i is None or abs(i) < LOAD_SPREAD_MIN_A:
            self.skipped += 1
            return
        self.used += 1
        # whole-pack comparison: every cell referred to "no load" with its own resistance. Valid
        # because the current was steady, so it does not matter when exactly the cell was measured
        if r_lookup is not None:
            keys = [f"{string}{bank * 8 + j + 1}" for j in range(len(cells_mv))]
            rs = [r_lookup(k) for k in keys]
            known = sorted(r for r in rs if r)
            r_fill = known[len(known) // 2] if known else None
            if r_fill is not None:
                for j, (k, mv, r) in enumerate(zip(keys, cells_mv, rs)):
                    self.comp[k] = (ts, mv + (r or r_fill) * i / 2, mv,
                                    f"{string}{half_of(bank * 8 + j + 1)}")
        i_string = abs(i) / 2
        for _half, first, seg in parts:
            if len(seg) < 2:
                continue
            lo = min(range(len(seg)), key=lambda k: seg[k])
            spread = max(seg) - seg[lo]
            cell = _cell_name(string, first + lo)
            self.recent.append({"ts": ts, "spread_mv": spread, "i_string": i_string, "low_mv": seg[lo],
                                "cell": cell})
            if self.baseline is None:
                self.learn.append(spread / i_string)
                if len(self.learn) >= LOAD_LEARN:
                    self.learn.sort()
                    self.baseline = self.learn[len(self.learn) // 2]
            if spread > LOAD_SPREAD_WARN_MV / 2:
                self.worst_cells[cell] = self.worst_cells.get(cell, 0) + 1
        self.recent = [r for r in self.recent if ts - r["ts"] <= 10]

    def comp_findings(self, now: float) -> list:
        fresh = {k: v for k, v in self.comp.items() if now - v[0] <= COMP_MAX_AGE_S}
        if len(fresh) < COMP_MIN_CELLS:
            return []
        vals = sorted(v[1] for v in fresh.values())
        med = vals[len(vals) // 2]
        out = []
        low_k = min(fresh, key=lambda k: fresh[k][1])
        dev = fresh[low_k][1] - med
        name = f"sträng {low_k[0]}, cell {low_k[1:]}"
        if dev <= -COMP_CELL_WARN_MV:
            out.append(("alarm" if dev <= -COMP_CELL_ALARM_MV else "warn", "load_comp_cell", name,
                        f"Under jämn last, jämfört med alla {len(fresh)} celler (kompenserat för ström och "
                        f"cellens motstånd): {name} ligger {-dev:.0f} mV under mitten – svag cell eller dålig "
                        f"förbindelse.", round(dev)))
        halves: dict[str, list[float]] = {}
        for v in fresh.values():
            halves.setdefault(v[3], []).append(v[1])
        for h, hv in halves.items():
            hv.sort()
            hdev = hv[len(hv) // 2] - med
            if hdev <= -COMP_BANK_WARN_MV:
                rng = "1–12" if h[1:] == "1" else "13–24"
                out.append(("warn", "load_comp_bank", f"sträng {h[0]}, cell {rng}",
                            f"Under jämn last ligger hela sträng {h[0]} cell {rng} {-hdev:.0f} mV under resten "
                            f"av paketet (kompenserat) – syns inte inom halvpaketet.", round(hdev)))
        return out

    def findings(self, now: float) -> list:
        comp = self.comp_findings(now)
        out = []
        lows = [r for r in self.lows if now - r["ts"] <= 10]
        if lows:
            low = min(lows, key=lambda r: r["low_mv"])
            if low["low_mv"] <= LOAD_CELL_ALARM_MV:
                out.append(("alarm", "load_cell_low", low["cell"],
                            f"Under last: {low['cell']} föll till {low['low_mv'] / 1000:.2f} V – risk att "
                            f"paketet stänger av. Sakta in.", low["low_mv"]))
        rec = [r for r in self.recent if now - r["ts"] <= 10]
        if not rec:
            return out + comp
        w = max(rec, key=lambda r: r["spread_mv"])
        lvl = "alarm" if w["spread_mv"] >= LOAD_SPREAD_ALARM_MV else "warn" if w["spread_mv"] >= LOAD_SPREAD_WARN_MV else None
        if lvl is None and self.baseline is not None:
            expected = self.baseline * w["i_string"]
            if w["spread_mv"] > expected + LOAD_EXCESS_WARN_MV:
                lvl = "warn"
        if lvl:
            norm = f", normalt ≈ {self.baseline * w['i_string']:.0f} mV vid samma ström" if self.baseline else ""
            out.append((lvl, "load_spread", w["cell"],
                        f"Cellspridning under jämn last {w['spread_mv']} mV vid {w['i_string'] * 2:.0f} A{norm} "
                        f"(celler mätta i samma ögonblick) – lägst: {w['cell']}.", w["spread_mv"]))
        return out + comp


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
    row_count: int = 0


class Guard:
    def __init__(self, baseline: dict | None = None):
        self.row_hist: dict[str, deque] = {}       # current per BMS row, for the silent-row check
        self.pack_hist: deque = deque()            # (ts, BMS 1 A, BMS 2 A, charging) for the pack-imbalance check
        self.pack_v: dict[int, tuple] = {}         # latest (ts, pack voltage) per BMS, for the fast dropout check
        self.drop_since: float | None = None
        self.drop_sign = 0
        self.drop_seen: tuple | None = None
        self.row_now: dict[int, tuple] = {}        # latest (ts, A, activity) per BMS row, for the fast dead-row check
        self.low_since: dict[int, float] = {}
        self.low_seen: dict[int, tuple] = {}
        self.grp_hist: dict[int, deque] = {}       # (ts, group voltage, A) per BMS row, for the frozen-group check
        self.grp_last: float | None = None
        self.grp_v_since: float | None = None
        self.groups: dict[int, GroupTrack] = {}
        self.sum_learn: list = []
        self.sum_baseline: float | None = None
        self.load_samples = 0
        self.findings: list[Finding] = []
        self.load_spread = LoadSpread((baseline or {}).get("load_spread_mv_per_a"))
        if baseline:
            self.load_baseline(baseline)

    # ---------- baseline persistence ----------
    def export_baseline(self) -> dict:
        return {"load_spread_mv_per_a": self.load_spread.baseline,
                "groups": {str(k): g.baseline for k, g in self.groups.items() if g.baseline},
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
        out += [Finding(*f) for f in self.load_spread.findings(now)]
        if bms and pack_i is not None:
            if abs(pack_i) >= LOAD_A and len(bms) >= 2:
                self.load_samples += 1
                if SHUNT_CHECKS:
                    out += self._shunt_checks(bms, p7_i)
                out += self._temp_checks(bms)
            rest = self.load_spread.trace.rest_s(now)        # None = no packet-rate history (tests, start)
            if abs(pack_i) < REST_A and (rest is None or rest >= REST_S):
                out += self._voltage_checks(snap.get("cells") or {})
            out += self._row_checks(bms)
            out += self._balance_checks(bms, now)
        out += self._dropout_finding(now)
        out += self._group_voltage_checks(snap.get("groups") or {}, now)
        out += self._silent_row_checks(snap.get("groups") or {}, now)
        out += self._pack_current_checks(bms, now)
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
        severe = {"fel på hallsensor", "övertemperatur", "överspänning", "låg spänning",
                  "MOS bränd / fartlarm 2", "gyrofel / fartlarm 1", "strömfel / hög effekt"}
        return [Finding("alarm" if a in severe else "info" if a.startswith("låst") else "warn",
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

    # ---------- 3 (fast): a pack cut off, seen in the packs' voltages ----------
    def on_bms_row(self, ts: float, bms: int, voltage_v: float | None, row: int | None = None,
                   current_a: float | None = None, activity: str | None = None,
                   group_v: float | None = None) -> None:
        """Call for every BMS row as it arrives (packet rate)."""
        if row is not None and current_a is not None:
            self._low_row(ts, int(row), abs(current_a), activity)
            if group_v is not None and 30.0 < group_v < 60.0:
                self._frozen_group(ts, int(row), group_v, abs(current_a))
        if voltage_v is None or bms not in (1, 2):
            return
        self.pack_v[bms] = (ts, voltage_v)
        if len(self.pack_v) < 2 or any(ts - t > DROP_FRESH_S for t, _ in self.pack_v.values()):
            self.drop_since = None
            return
        d = self.pack_v[1][1] - self.pack_v[2][1]
        sign = 1 if d > 0 else -1
        if abs(d) < DROP_V or (self.drop_since is not None and sign != self.drop_sign):
            self.drop_since = None
            if abs(d) < DROP_V:
                return
        if self.drop_since is None:
            self.drop_since, self.drop_sign = ts, sign
        if ts - self.drop_since >= DROP_HOLD_S:
            lo, hi = (2, 1) if d > 0 else (1, 2)
            self.drop_seen = (ts, Finding(
                "alarm", "pack_dropout", "BMS 1 ↔ BMS 2",
                f"Paketen har olika spänning: {_name(hi)} {self.pack_v[hi][1]:.1f} V, {_name(lo)} "
                f"{self.pack_v[lo][1]:.1f} V ({abs(d):.1f} V isär i {ts - self.drop_since:.0f} s). Parallella paket "
                f"ska ha samma spänning. Det lägre bär lasten ensamt – det andra kan vara bortkopplat. "
                f"Sakta in och stanna.", round(abs(d), 1)))

    def _frozen_group(self, ts: float, row: int, group_v: float, amps: float) -> None:
        if self.grp_last is not None and ts - self.grp_last > 5.0:        # a hole in the data: start over
            self.grp_hist.clear()
        self.grp_last = ts
        h = self.grp_hist.setdefault(row, deque())
        h.append((ts, group_v, amps))
        while h and ts - h[0][0] > FROZEN_HOLD_S + 1.5:
            h.popleft()
        if len(self.grp_hist) < 4:
            return
        win = {k: [x for x in v if ts - x[0] <= FROZEN_HOLD_S] for k, v in self.grp_hist.items()}
        for k, hk in win.items():
            if len(hk) < FROZEN_HOLD_S / 1.2 * 0.7 or ts - self.grp_hist[k][0][0] < FROZEN_HOLD_S:
                continue
            if max(x[1] for x in hk) != min(x[1] for x in hk) or max(x[2] for x in hk) > LOWROW_A:
                continue
            others = [v for j, v in win.items() if j != k]
            if any(len(o) < 3 or max(x[1] for x in o) - min(x[1] for x in o) < FROZEN_OTHERS_V for o in others):
                continue
            mean_a = statistics.mean(x[2] for o in others for x in o)
            if mean_a < FROZEN_OTHERS_A:
                continue
            where = _group(k)
            self.low_seen[100 + k] = (ts, Finding(
                "alarm", "group_frozen", where,
                f"{where} rapporterar samma spänning ({hk[-1][1]:.1f} V) och ingen ström sedan minst "
                f"{FROZEN_HOLD_S:.0f} s, medan de tre andra grupperna rör sig och visar {mean_a:.1f} A i snitt. "
                f"Så såg det tidigare batterifelet ut: gruppens mätkort har slutat mäta, och dess celler "
                f"är då oskyddade. Sakta in och stanna.", round(hk[-1][1], 1)))

    def _low_row(self, ts: float, row: int, amps: float, activity: str | None) -> None:
        self.row_now[row] = (ts, amps, activity)
        if len(self.row_now) < 4 or any(ts - t > 3.0 for t, _, _ in self.row_now.values()):
            self.low_since.clear()
            return
        charging = all(a == "laddning" for _, _, a in self.row_now.values())
        need = LOWROW_OTHERS_CHARGE_A if charging else LOWROW_OTHERS_A
        for r, (_, a, _) in self.row_now.items():
            others = statistics.mean(v for j, (_, v, _) in self.row_now.items() if j != r)
            if a <= LOWROW_A and others >= need:
                since = self.low_since.setdefault(r, ts)
                if ts - since >= LOWROW_HOLD_S:
                    where = _group(r)
                    self.low_seen[r] = (ts, Finding(
                        "alarm", "row_dead", where,
                        f"{where} visar {a:.1f} A medan de tre övriga visar {others:.1f} A i snitt, sedan "
                        f"{ts - since:.0f} s. Ett paket som inte rapporterar ström var tecknet vid det "
                        f"tidigare batterifelet. Sakta in och kontrollera paketet.", round(a, 2)))
            else:
                self.low_since.pop(r, None)

    def _dropout_finding(self, now: float) -> list[Finding]:
        out = [self.drop_seen[1]] if self.drop_seen and now - self.drop_seen[0] <= DROP_KEEP_S else []
        return out + [f for t, f in self.low_seen.values() if now - t <= DROP_KEEP_S]

    # ---------- 10: the four group voltages at rest ----------
    def _group_voltage_checks(self, groups: dict, now: float) -> list[Finding]:
        rest = self.load_spread.trace.rest_s(now)
        v = {int(k): g.get("half_voltage_v") for k, g in groups.items()}
        v = {k: x for k, x in v.items() if x is not None and 30.0 < x < 60.0}
        if rest is None or rest < GROUP_REST_S or len(v) < 4:
            self.grp_v_since = None
            return []
        spread = max(v.values()) - min(v.values())
        if spread <= GROUP_V_WARN + 1e-9:
            self.grp_v_since = None
            return []
        self.grp_v_since = self.grp_v_since or now
        if now - self.grp_v_since < GROUP_HOLD_S:
            return []
        lo, hi = min(v, key=v.get), max(v, key=v.get)
        vals = ", ".join(f"{GROUPS[k]} {v[k]:.1f}" for k in sorted(v))
        return [Finding("alarm" if spread > GROUP_V_ALARM else "warn", "group_voltage", f"{GROUPS[lo]} ↔ {GROUPS[hi]}",
                        f"De fyra batterigruppernas spänning skiljer {spread:.1f} V i vila ({vals} V). Begode anger "
                        f"högst 0,5 V. {GROUPS[lo]} ligger lägst och {GROUPS[hi]} högst – en grupp som inte hänger "
                        f"med laddas eller mäts inte som de andra.", round(spread, 2))]

    # ---------- 9: the two packs report clearly different currents ----------
    def _pack_current_checks(self, bms: dict, now: float) -> list[Finding]:
        if len(bms) < 2 or any(b.get("current_a") is None for b in bms.values()):
            return []
        a, b = abs(bms[1]["current_a"]), abs(bms[2]["current_a"])
        charging = all(x.get("activity") == "laddning" for x in bms.values())
        h = self.pack_hist
        if not h or now > h[-1][0]:
            h.append((now, a, b, charging))
        while h and now - h[0][0] > PACK_WIN_RIDE_S:
            h.popleft()
        win = PACK_WIN_CHARGE_S if charging else PACK_WIN_RIDE_S
        part = [x for x in h if now - x[0] <= win]
        if charging and not all(x[3] for x in part):          # just plugged in: wait for a full window
            return []
        if len(part) < win * 0.6 or now - part[0][0] < win * 0.75:
            return []
        ma, mb = statistics.mean(x[1] for x in part), statistics.mean(x[2] for x in part)
        mean = (ma + mb) / 2
        if mean < PACK_MIN_A or abs(ma - mb) / mean <= PACK_DIFF:
            return []
        hi, lo = (1, 2) if ma > mb else (2, 1)
        span = f"{win} s" if win < 120 else f"{win // 60} min"
        return [Finding("alarm", "pack_current", "BMS 1 ↔ BMS 2",
                        f"Obalans mellan paketen: {_name(hi)} visar {abs(ma - mb) / min(ma, mb) * 100:.0f} % mer ström "
                        f"än {_name(lo)} ({max(ma, mb):.1f} A mot {min(ma, mb):.1f} A i snitt de senaste {span}"
                        f"{', under laddning' if charging else ''}). Paketen ska dela strömmen lika.",
                        round(abs(ma - mb) / mean * 100, 1))]

    # ---------- 8: a current row that reports nothing while the others carry current ----------
    def _silent_row_checks(self, groups: dict, now: float) -> list[Finding]:
        rows = {str(k): g for k, g in groups.items() if g.get("current_a") is not None}
        if len(rows) < 3:
            return []
        for k, g in rows.items():
            h = self.row_hist.setdefault(k, deque())
            if not h or now > h[-1][0]:
                h.append((now, abs(g["current_a"])))
            while h and now - h[0][0] > SILENT_WINDOW_S:
                h.popleft()
        full = {k: h for k, h in self.row_hist.items() if k in rows and len(h) >= SILENT_WINDOW_S * 0.5
                and now - h[0][0] >= SILENT_WINDOW_S * 0.75}
        if len(full) < 3:
            return []
        mean = {k: sum(v for _, v in h) / len(h) for k, h in full.items()}
        out = []
        for k, m in mean.items():
            others = statistics.mean(v for j, v in mean.items() if j != k)
            if others >= SILENT_OTHERS_A and m <= SILENT_MAX_A and m < SILENT_FRAC * others:
                g = rows[k]
                where = _group(int(k))
                out.append(Finding("alarm", "row_silent", where,
                                   f"{where} rapporterar ingen ström ({m:.1f} A i snitt de senaste "
                                   f"{SILENT_WINDOW_S // 60} minuterna) medan de övriga visar {others:.1f} A. "
                                   f"En strömmätning som tystnat var det första tecknet vid det tidigare "
                                   f"batterifelet. Kontrollera paketet innan du kör hårt.", round(m, 2)))
        return out

    # ---------- 7: the two current rows of one BMS, while charging ----------
    def _row_checks(self, bms: dict) -> list[Finding]:
        out = []
        for k, b in bms.items():
            t = self.groups.setdefault(k, GroupTrack())
            rows = [abs(c) for c in (b.get("row_currents_a") or []) if c is not None]
            ok = b.get("activity") == "laddning" and len(rows) == 2 and min(rows) >= ROW_MIN_A
            dev = max(rows) / min(rows) - 1.0 if ok else 0.0
            if dev <= ROW_WARN:
                t.row_count = 0
                continue
            t.row_count += 1
            if t.row_count >= ROW_HOLD:
                hi = rows.index(max(rows)) + 1
                # information, not a warning: seen on BOTH packs during one normal charge
                # (2026-10-04), always row 1 high, so it is more likely how the BMS reports than
                # a broken current sensor. What the rows mean is unknown.
                out.append(Finding("info", "bms_rows", _name(k),
                                   f"{_name(k)}: de två strömvärdena skiljer {dev * 100:.0f} % under laddning "
                                   f"({rows[0]:.1f} A och {rows[1]:.1f} A) – {GROUPS[(k - 1) * 2 + hi - 1]} visar mer. Det har setts "
                                   f"på båda paketen och orsaken är okänd; det behöver inte vara ett fel. "
                                   f"Appen räknar på det värde som stämmer med det andra paketet.",
                                   round(dev * 100, 1)))
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


# The wheel has four battery groups of 12 cells. The Begode app calls them LF, RF, LB, RB
# (left/right, front/back); the BMS rows 0-3 arrive in that order: rows 0-1 = string A = the front
# pair, rows 2-3 = string B = the back pair. (From the app's own battery page compared with the
# cell table "Battery1F / Battery2B"; not confirmed side by side yet.)
GROUPS = ("LF", "RF", "LB", "RB")


def _group(row: int) -> str:
    return f"{GROUPS[row]} – {_name(1 if row < 2 else 2)}, rad {row % 2 + 1}"


def _name(k: int) -> str:
    return f"BMS {k} (sträng {'A' if k == 1 else 'B'})"


def summary(findings: list[Finding]) -> dict:
    worst = max((LEVELS[f.level] for f in findings), default=0)
    level = next(k for k, v in LEVELS.items() if v == worst)
    return {"level": level,
            "findings": [f.__dict__ for f in sorted(findings, key=lambda f: -LEVELS[f.level])]}
