"""Battery health / degradation ("Batterihälsa") — the X230 power-monitor ideas,
adapted to the wheel.

Data only exists while the wheel is on and within BLE range of the X230, so:
  * charging and parking at home are observed fully (best data),
  * rides are usually NOT observed; they are reconstructed from rest voltage and
    odometer before/after ("ride gap": km and ΔSoC, no Ah).

Capacity therefore comes from sessions WITH current data (mostly charging):
    capacity_Ah = Ah moved / ΔSoC,  ΔSoC from rest voltage (OCV) before and after.
ΔSoC uses the generic Li-ion OCV table in energy.py → capacities are marked
preliminary until a learned OCV curve exists.

Assumptions shown in the UI until verified with a real charge:
  * battery current = BMS 1 + BMS 2 (one smart BMS per parallel pack, 0.1 A units,
    per WheelLog) — sign from the BMS activity bits,
  * two strings in parallel → each cell carries half the battery current.
"""
from __future__ import annotations

import json
import statistics
import time
from collections import deque
from dataclasses import dataclass, field

from .energy import soc_from_cell_v

REST_A = 0.5            # |I| below this = idle
DISCHARGE_A = 1.0
REST_S = 300            # idle this long → voltages count as rest (OCV)
SESSION_END_IDLE_S = 600
INTERRUPT_MAX_S = 120   # charge current gone and back within this = cable/charger flap
OFF_GAP_MIN_S = 6 * 3600
MIN_DSOC = 0.20         # capacity point only for ≥20 % SoC change
STEP_A = 3.0            # current step for per-cell resistance
STEP_SETTLE_S = 3.0
MAX_DT = 5.0            # longer gaps between ticks are not integrated

WHEEL_PCT_LO, WHEEL_PCT_HI = 4800, 6550   # official app: p0 voltage → %


def wheel_percent(voltage_raw: int | None) -> int | None:
    if not voltage_raw:
        return None
    if voltage_raw <= WHEEL_PCT_LO:
        return 0
    if voltage_raw >= WHEEL_PCT_HI:
        return 100
    return round((voltage_raw - WHEEL_PCT_LO) / (WHEEL_PCT_HI - WHEEL_PCT_LO) * 100)


SCHEMA = """
CREATE TABLE IF NOT EXISTS health_sessions (
  start_ts REAL, end_ts REAL, kind TEXT, soc_start REAL, soc_end REAL,
  ah REAL, wh REAL, km REAL, max_a REAL, interruptions INTEGER, temp_max REAL, extra TEXT);
CREATE TABLE IF NOT EXISTS capacity_points (
  ts REAL, odometer_km REAL, ah REAL, wh REAL, d_soc REAL, method TEXT, preliminary INTEGER);
CREATE TABLE IF NOT EXISTS cell_ir (ts REAL, cell TEXT, mohm REAL);
CREATE TABLE IF NOT EXISTS self_discharge (
  ts REAL, hours REAL, mean_mv_day REAL, worst_cell TEXT, worst_mv_day REAL, cells TEXT);
CREATE TABLE IF NOT EXISTS exposure_daily (
  day TEXT PRIMARY KEY, s_obs REAL, s_soc90 REAL, s_soc95 REAL, s_t40 REAL, s_t45 REAL, s_charge REAL);
"""


def _cells(snap: dict) -> dict[str, int]:
    """'A1'..'A24','B1'..'B24' → mV."""
    out = {}
    for s, st in (snap.get("cells") or {}).items():
        for i, mv in enumerate(st.get("cells_mv") or []):
            out[f"{s}{i + 1}"] = mv
    return out


@dataclass
class Rest:
    ts: float
    cells: dict
    mean_mv: float
    soc: float
    odometer_km: float | None


@dataclass
class Session:
    kind: str                 # "charge" | "discharge"
    start_ts: float
    rest_before: Rest | None
    ah: float = 0.0
    wh: float = 0.0
    max_a: float = 0.0
    temp_max: float = -99.0
    interruptions: int = 0
    last_active: float = 0.0
    paused_since: float | None = None


@dataclass
class Pending:
    """A finished session/gap waiting for a rest voltage afterwards."""
    kind: str
    start_ts: float
    end_ts: float
    rest_before: Rest | None
    ah: float = 0.0
    wh: float = 0.0
    km: float = 0.0
    max_a: float = 0.0
    interruptions: int = 0
    temp_max: float = -99.0


class Health:
    def __init__(self, db, nominal_wh: float | None = None):
        self.db = db
        self.db.executescript(SCHEMA)
        self.nominal_wh = nominal_wh
        c = self._kv("health_counters") or {}
        self.wh_out, self.ah_out = c.get("wh_out", 0.0), c.get("ah_out", 0.0)
        self.wh_in, self.ah_in = c.get("wh_in", 0.0), c.get("ah_in", 0.0)
        self.observed_s = c.get("observed_s", 0.0)
        self.last_ts: float | None = None
        self.last_connected = False
        self.idle_since: float | None = None
        self.rest: Rest | None = None
        self.session: Session | None = None
        self.pending: Pending | None = None
        self.last_seen: dict | None = None      # last state before a disconnect
        self.hist: deque = deque(maxlen=40)     # (ts, I, cells) for resistance steps
        self.step_wait: tuple | None = None
        self.now_view: dict = {}

    # ---------- small db helpers ----------
    def _kv(self, k):
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else None

    def _set_kv(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v)))

    def save(self):
        self._set_kv("health_counters", {"wh_out": self.wh_out, "ah_out": self.ah_out,
                                         "wh_in": self.wh_in, "ah_in": self.ah_in,
                                         "observed_s": self.observed_s})
        self.db.commit()

    # ---------- main entry, 1 Hz ----------
    def tick(self, ts: float, snap: dict | None, connected: bool) -> None:
        if not connected or not snap or not snap.get("groups"):
            if self.last_connected:
                self._on_disconnect()
            self.last_connected = False
            self.last_ts = None
            return
        groups = list(snap["groups"].values())
        bms = list((snap.get("bms") or {}).values())
        current = snap.get("battery_current_a") or 0.0   # BMS 1 + BMS 2, + = discharge
        volt = groups[0].get("voltage_v") or 0.0
        temp = max((b["temp_max_c"] for b in bms if b.get("temp_max_c") is not None), default=None)
        cells = _cells(snap)
        odo = (snap.get("p4") or {}).get("odometer_raw")
        odo_km = odo / 1000.0 if odo else None
        mean_mv = statistics.mean(cells.values()) if cells else None
        soc = soc_from_cell_v(mean_mv / 1000.0) if mean_mv else None

        if not self.last_connected:
            self._on_reconnect(ts, odo_km)
        self.last_connected = True

        dt = 0.0
        if self.last_ts is not None:
            dt = min(max(ts - self.last_ts, 0.0), MAX_DT)
        self.last_ts = ts
        self.observed_s += dt

        # energy counters
        ah = abs(current) * dt / 3600.0
        if current > REST_A:
            self.ah_out += ah; self.wh_out += ah * volt
        elif current < -REST_A:
            self.ah_in += ah; self.wh_in += ah * volt

        mode = "charge" if current < -REST_A else "discharge" if current > DISCHARGE_A else "idle"
        if mode != "idle" and self.pending and self.session is None and cells:
            # load starts before a rest voltage was reached (e.g. charger plugged in right
            # after a ride): close the pending item with the present, non-rest voltage
            self._finalize_pending(Rest(ts, dict(cells), mean_mv, soc, odo_km), after_is_rest=False)
        self._sessions(ts, mode, current, volt, dt, temp)
        self._rest(ts, mode, cells, mean_mv, soc, odo_km)
        self._exposure(ts, dt, soc, temp, mode)
        self._resistance(ts, current, cells)
        self.last_seen = {"ts": ts, "odo_km": odo_km, "rest": self.rest}
        self.now_view = {"current_a": round(current, 2), "voltage_v": volt, "mode": mode,
                         "soc_ocv": soc, "soc_valid": self.idle_since is not None and
                         ts - self.idle_since >= REST_S,
                         "wheel_pct": wheel_percent((snap.get("p0") or {}).get("voltage_raw")),
                         "range_raw": (snap.get("p0") or {}).get("range_raw"),
                         "odometer_km": odo_km, "temp_max": temp}

    # ---------- sessions (charge / in-range discharge) ----------
    def _sessions(self, ts, mode, current, volt, dt, temp):
        s = self.session
        if mode in ("charge", "discharge"):
            if s and s.kind != mode:
                self._close_session(ts)
                s = None
            if s is None:
                rb = self.rest if self.rest and ts - self.rest.ts < 1800 else None
                s = self.session = Session(mode, ts, rb)
            if s.paused_since is not None:
                if ts - s.paused_since <= INTERRUPT_MAX_S and mode == "charge":
                    s.interruptions += 1
                s.paused_since = None
            a = abs(current)
            s.ah += a * dt / 3600.0
            s.wh += a * dt / 3600.0 * volt
            s.max_a = max(s.max_a, a)
            if temp is not None:
                s.temp_max = max(s.temp_max, temp)
            s.last_active = ts
        elif s:
            if s.paused_since is None:
                s.paused_since = ts
            if ts - s.last_active > SESSION_END_IDLE_S:
                self._close_session(ts)

    def _close_session(self, ts):
        s, self.session = self.session, None
        if not s or s.ah < 0.05:
            return
        self.pending = Pending(s.kind, s.start_ts, s.last_active or ts, s.rest_before,
                               ah=s.ah, wh=s.wh, max_a=s.max_a,
                               interruptions=s.interruptions, temp_max=s.temp_max)

    # ---------- rest voltage (OCV) ----------
    def _rest(self, ts, mode, cells, mean_mv, soc, odo_km):
        if mode != "idle":
            self.idle_since = None
            return
        if self.idle_since is None:
            self.idle_since = ts
        if ts - self.idle_since < REST_S or not cells:
            return
        self.rest = Rest(ts, dict(cells), mean_mv, soc, odo_km)
        if self.pending and self.session is None:
            self._finalize_pending(self.rest)

    def _finalize_pending(self, after: Rest, after_is_rest: bool = True):
        p, self.pending = self.pending, None
        if p.kind == "off_gap" and p.rest_before and after_is_rest:
            self._selfdischarge(p.rest_before, after)
        before = p.rest_before
        soc0 = before.soc if before else None
        soc1 = after.soc
        km = p.km
        if p.kind == "ride_gap" and before and before.odometer_km and after.odometer_km:
            km = after.odometer_km - before.odometer_km
        self.db.execute("INSERT INTO health_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (p.start_ts, p.end_ts, p.kind, soc0, soc1, p.ah, p.wh, km, p.max_a,
                         p.interruptions, p.temp_max if p.temp_max > -99 else None,
                         json.dumps({"after_rest": after_is_rest})))
        if soc0 is not None and p.ah > 0 and after_is_rest:
            d = abs(soc1 - soc0) / 100.0
            if d >= MIN_DSOC:
                self.db.execute("INSERT INTO capacity_points VALUES (?,?,?,?,?,?,?)",
                                (after.ts, after.odometer_km, p.ah / d, p.wh / d, d, p.kind, 1))
        self.db.commit()

    # ---------- disconnect / reconnect: rides and parking ----------
    def _on_disconnect(self):
        if self.session:
            self._close_session(self.last_ts or time.time())

    def _on_reconnect(self, ts, odo_km):
        seen = self.last_seen
        self.idle_since = None
        if not seen:
            return
        before = seen.get("rest")
        if seen.get("odo_km") and odo_km and odo_km - seen["odo_km"] > 0.05:
            # a ride we did not see
            self.pending = Pending("ride_gap", seen["ts"], ts, before,
                                   km=odo_km - seen["odo_km"])
        elif ts - seen["ts"] >= OFF_GAP_MIN_S and before:
            self.pending = Pending("off_gap", seen["ts"], ts, before)

    # ---------- self-discharge (needs rest before and after an off gap) ----------
    def _selfdischarge(self, before: Rest, after: Rest):
        hours = (after.ts - before.ts) / 3600.0
        if hours < OFF_GAP_MIN_S / 3600.0:
            return
        per = {c: (before.cells[c] - after.cells[c]) / hours * 24
               for c in before.cells if c in after.cells}
        if not per:
            return
        worst = max(per, key=per.get)
        self.db.execute("INSERT INTO self_discharge VALUES (?,?,?,?,?,?)",
                        (after.ts, hours, statistics.mean(per.values()), worst, per[worst],
                         json.dumps({k: round(v, 2) for k, v in per.items()})))

    # ---------- exposure (time at high SoC / temperature) ----------
    def _exposure(self, ts, dt, soc, temp, mode):
        if dt <= 0:
            return
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        row = self.db.execute("SELECT * FROM exposure_daily WHERE day=?", (day,)).fetchone()
        v = list(row[1:]) if row else [0.0] * 6
        v[0] += dt
        if soc is not None and soc >= 90: v[1] += dt
        if soc is not None and soc >= 95: v[2] += dt
        if temp is not None and temp >= 40: v[3] += dt
        if temp is not None and temp >= 45: v[4] += dt
        if mode == "charge": v[5] += dt
        self.db.execute("INSERT OR REPLACE INTO exposure_daily VALUES (?,?,?,?,?,?,?)", (day, *v))
        self.db.commit()          # never keep a write transaction open between ticks

    # ---------- per-cell resistance from current steps ----------
    def _resistance(self, ts, current, cells):
        self.hist.append((ts, current, cells))
        if self.step_wait:
            t_step, before = self.step_wait
            if ts - t_step >= STEP_SETTLE_S:
                self.step_wait = None
                b_ts, b_i, b_cells = before
                d_i = (current - b_i) / 2.0            # two strings in parallel
                if abs(d_i) * 2 >= STEP_A:
                    for c, mv in cells.items():
                        if c in b_cells:
                            r = (b_cells[c] - mv) / d_i      # mV/A = mΩ
                            if 0 < r < 200:
                                self.db.execute("INSERT INTO cell_ir VALUES (?,?,?)", (ts, c, r))
            return
        old = [h for h in self.hist if ts - h[0] >= STEP_SETTLE_S]
        if old and abs(current - old[-1][1]) >= STEP_A:
            self.step_wait = (ts, old[-1])

    # ---------- report ----------
    def report(self) -> dict:
        db = self.db
        caps = [dict(zip(("ts", "odometer_km", "ah", "wh", "d_soc", "method", "preliminary"), r))
                for r in db.execute("SELECT * FROM capacity_points ORDER BY ts")]
        baseline = caps[0]["wh"] if caps else None
        latest = statistics.median([c["wh"] for c in caps[-3:]]) if caps else None
        health = round(100 * latest / baseline, 1) if baseline else None
        forecast = None
        pts = [(c["odometer_km"], c["wh"]) for c in caps if c["odometer_km"]]
        if len(pts) >= 3 and baseline:
            xs, ys = zip(*pts)
            mx, my = statistics.mean(xs), statistics.mean(ys)
            sxx = sum((x - mx) ** 2 for x in xs)
            if sxx > 0:
                slope = sum((x - mx) * (y - my) for x, y in pts) / sxx
                if slope < 0:
                    forecast = round(mx + (0.8 * baseline - my) / slope)
        cap_ah = statistics.median([c["ah"] for c in caps[-3:]]) if caps else None
        sessions = [dict(zip(("start_ts", "end_ts", "kind", "soc_start", "soc_end", "ah", "wh", "km",
                              "max_a", "interruptions", "temp_max"), r[:11]))
                    for r in db.execute("SELECT * FROM health_sessions ORDER BY start_ts DESC LIMIT 30")]
        rides = [s for s in sessions if s["kind"] == "ride_gap" and s["km"] and s["soc_start"] is not None
                 and s["soc_end"] is not None]
        wh_km = None
        if rides and latest:
            vals = [(s["soc_start"] - s["soc_end"]) / 100 * latest / s["km"] for s in rides if s["km"] > 0.5]
            wh_km = round(statistics.median(vals), 1) if vals else None
        ir = {}
        for c, m in db.execute("SELECT cell, mohm FROM cell_ir WHERE ts > "
                               "(SELECT max(ts) FROM cell_ir) - 30 * 86400"):
            ir.setdefault(c, []).append(m)
        ir_med = {c: round(statistics.median(v), 2) for c, v in ir.items()}
        ir_flag = []
        if len(ir_med) >= 8:
            m = statistics.median(ir_med.values())
            ir_flag = sorted([c for c, v in ir_med.items() if v > 1.5 * m], key=lambda c: -ir_med[c])
        sd = [dict(zip(("ts", "hours", "mean_mv_day", "worst_cell", "worst_mv_day"), r[:5]))
              for r in db.execute("SELECT * FROM self_discharge ORDER BY ts DESC LIMIT 10")]
        exp = [dict(zip(("day", "s_obs", "s_soc90", "s_soc95", "s_t40", "s_t45", "s_charge"), r))
               for r in db.execute("SELECT * FROM exposure_daily ORDER BY day DESC LIMIT 30")]
        now = self.now_view
        gauge = None
        if now.get("soc_valid") and now.get("wheel_pct") is not None and now.get("soc_ocv") is not None:
            gauge = round(now["wheel_pct"] - now["soc_ocv"], 1)
        range_ours = None
        if wh_km and latest and now.get("soc_ocv") is not None:
            range_ours = round(now["soc_ocv"] / 100 * latest / wh_km)
        return {
            "now": now,
            "counters": {"wh_out": round(self.wh_out, 1), "ah_out": round(self.ah_out, 2),
                         "wh_in": round(self.wh_in, 1), "ah_in": round(self.ah_in, 2),
                         "observed_h": round(self.observed_s / 3600, 2),
                         "eq_cycles": round(self.ah_in / cap_ah, 2) if cap_ah else None},
            "capacity": {"points": caps[-20:], "baseline_wh": baseline, "latest_wh": latest,
                         "health_pct": health, "km_to_80pct": forecast,
                         "nominal_wh": self.nominal_wh},
            "gauge": {"wheel_minus_ours_pct": gauge, "range_wheel_raw": now.get("range_raw"),
                      "range_ours_km": range_ours, "wh_per_km": wh_km},
            "sessions": sessions,
            "cell_ir_mohm": ir_med, "cell_ir_flag": ir_flag,
            "self_discharge": sd,
            "exposure": exp,
            "pending": self.pending.kind if self.pending else None,
            "assumptions": ["Batteriström = BMS 1 + BMS 2 (ett BMS per parallellt paket, 0,1 A enligt WheelLog; verifieras vid första laddning)",
                            "Två strängar parallellt → varje cell bär halva strömmen",
                            "Laddnivå från vilospänning med generisk Li-ion-kurva → kapacitet preliminär"],
        }
