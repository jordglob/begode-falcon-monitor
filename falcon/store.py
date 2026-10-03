"""SQLite history, one row per interval, for long-term trends (cell spread, sag)."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  ts REAL PRIMARY KEY,
  voltage_v REAL, current_a REAL, speed_kmh REAL,
  cell_min_mv INTEGER, cell_max_mv INTEGER, cell_spread_mv INTEGER,
  temp1_c REAL, temp2_c REAL, board_temp_c REAL, motor_temp_c REAL,
  sag_mohm REAL, odometer_raw INTEGER, extra TEXT
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS gps_samples (ts REAL PRIMARY KEY, lat REAL, lon REAL, alt_m REAL,
  speed_kmh REAL, course_deg REAL, sats INTEGER, hdop REAL, fix_type TEXT, wheel_speed_kmh REAL);
CREATE TABLE IF NOT EXISTS control_log (ts REAL, host TEXT, setting TEXT, value TEXT,
  payload TEXT, status TEXT, sends INTEGER, before TEXT, after TEXT, detail TEXT, changes TEXT, reply TEXT);
CREATE TABLE IF NOT EXISTS guard_events (ts REAL, level TEXT, code TEXT, where_ TEXT, text TEXT);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")      # readers never block the writer
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the first release (SQLite ADD COLUMN)."""
        have = {r[1] for r in self.db.execute("PRAGMA table_info(gps_samples)")}
        for col, typ in (("wh_out_cum", "REAL"), ("wh_regen_cum", "REAL"), ("pwm_max", "REAL"),
                         ("current_avg", "REAL"), ("data_cov", "REAL"), ("current_max", "REAL"),
                         ("power_max_w", "REAL"), ("regen_max_w", "REAL"), ("volt_min", "REAL")):
            if col not in have:
                self.db.execute(f"ALTER TABLE gps_samples ADD COLUMN {col} {typ}")
        self.db.execute("CREATE TABLE IF NOT EXISTS thermal_samples (ts REAL PRIMARY KEY, temp_c REAL, "
                        "r_now_mohm REAL, r25_mohm REAL, pmax_w REAL, prec_w REAL, p_now_w REAL, v0_mv REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS beep_events (ts REAL, key TEXT, old TEXT, new TEXT, "
                        "level TEXT, text TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS ride_analysis (ride_id INTEGER, version INTEGER, "
                        "source TEXT, created REAL, result TEXT, PRIMARY KEY (ride_id, version, source))")
        self.db.commit()

    def add(self, ev: dict, p0: dict, p4: dict) -> None:
        b, e = ev["battery"], ev["electronics"]
        sag = ev["bus"]["sag"].get("ohm")
        self.db.execute(
            "INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), b["voltage_v"], b["current_a"], p0.get("speed_kmh"),
             b["cell_min_mv"], b["cell_max_mv"], b["cell_spread_mv"],
             b["temps_c"][0], b["temps_c"][1], e["board_temp_c"], e["motor_temp_c"],
             sag * 1000 if sag is not None else None, p4.get("odometer_raw"),
             json.dumps({"groups": len(b["groups"])})))
        self.db.commit()

    def history(self, hours: float = 24, limit: int = 2000) -> list[dict]:
        cur = self.db.execute(
            "SELECT * FROM samples WHERE ts > ? ORDER BY ts LIMIT ?",
            (time.time() - hours * 3600, limit))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def baseline_sag_ohm(self) -> float | None:
        """Median of the first 20 sag estimates ever stored = 'as new' reference."""
        rows = [r[0] for r in self.db.execute(
            "SELECT sag_mohm FROM samples WHERE sag_mohm IS NOT NULL ORDER BY ts LIMIT 20")]
        if len(rows) < 5:
            return None
        rows.sort()
        return rows[len(rows) // 2] / 1000.0

    def get_json(self, k: str):
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else None

    def set_json(self, k: str, v) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v)))
        self.db.commit()

    def add_guard_event(self, level: str, code: str, where: str, text: str) -> None:
        self.db.execute("INSERT INTO guard_events VALUES (?,?,?,?,?)",
                        (time.time(), level, code, where, text))
        self.db.commit()

    def guard_events(self, limit: int = 100) -> list[dict]:
        cur = self.db.execute("SELECT ts, level, code, where_, text FROM guard_events "
                              "ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(zip(("ts", "level", "code", "where", "text"), r)) for r in cur.fetchall()]

    def add_control(self, row: dict) -> None:
        self.db.execute("INSERT INTO control_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        tuple(json.dumps(row[k]) if isinstance(row.get(k), (dict, list)) else row.get(k)
                              for k in ("ts", "host", "setting", "value", "payload", "status", "sends",
                                        "before", "after", "detail", "changes", "reply")))
        self.db.commit()

    def control_log(self, limit: int = 30) -> list[dict]:
        cols = ("ts", "host", "setting", "value", "payload", "status", "sends", "before", "after",
                "detail", "changes", "reply")
        rows = self.db.execute("SELECT * FROM control_log ORDER BY ts DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            for k in ("changes", "reply"):
                try:
                    d[k] = json.loads(d[k]) if d[k] else None
                except Exception:
                    pass
            out.append(d)
        return out

    def add_gps(self, s: dict, wheel_speed: float | None, energy: dict | None = None,
                ts: float | None = None) -> None:
        e = energy or {}
        self.db.execute(
            "INSERT OR REPLACE INTO gps_samples (ts, lat, lon, alt_m, speed_kmh, course_deg, sats, hdop, "
            "fix_type, wheel_speed_kmh, wh_out_cum, wh_regen_cum, pwm_max, current_avg, data_cov, "
            "current_max, power_max_w, regen_max_w, volt_min) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts or time.time(), s.get("lat"), s.get("lon"), s.get("alt_m"), s.get("speed_kmh"),
             s.get("course_deg"), s.get("sats_used"), s.get("hdop"), s.get("fix_type"), wheel_speed,
             e.get("wh_out_cum"), e.get("wh_regen_cum"), e.get("pwm_max"), e.get("current_avg"),
             e.get("data_cov"), e.get("current_max"), e.get("power_max_w"), e.get("regen_max_w"),
             e.get("volt_min")))
        self.db.commit()

    def gps_count(self) -> int:
        return self.db.execute("SELECT count(*) FROM gps_samples").fetchone()[0]

    def history_bucketed(self, hours: float, points: int = 1500) -> list[dict]:
        """Averages per time bucket so any range returns ~`points` rows (5 s minimum)."""
        bucket = max(5.0, hours * 3600 / points)
        cols = ["voltage_v", "current_a", "speed_kmh", "cell_min_mv", "cell_max_mv", "cell_spread_mv",
                "temp1_c", "temp2_c", "board_temp_c", "motor_temp_c", "sag_mohm"]
        agg = ", ".join(f"avg({c})" if c != "speed_kmh" else "max(speed_kmh)" for c in cols)
        cur = self.db.execute(
            f"SELECT avg(ts), {agg} FROM samples WHERE ts > ? GROUP BY CAST(ts / ? AS INTEGER) ORDER BY 1",
            (time.time() - hours * 3600, bucket))
        return [dict(zip(["ts"] + cols, r)) for r in cur.fetchall()]

    def add_beep_event(self, e: dict) -> None:
        self.db.execute("INSERT INTO beep_events VALUES (?,?,?,?,?,?)",
                        (e["ts"], e["key"], str(e["old"]), str(e["new"]), e["level"], e["text"]))
        self.db.commit()

    def beep_events_between(self, t0: float, t1: float) -> list[dict]:
        cur = self.db.execute("SELECT ts, key, old, new, level, text FROM beep_events WHERE ts BETWEEN ? AND ? "
                              "ORDER BY ts", (t0, t1))
        return [dict(zip(("ts", "key", "old", "new", "level", "text"), r)) for r in cur.fetchall()]

    def samples_between(self, t0: float, t1: float) -> list[dict]:
        cur = self.db.execute("SELECT * FROM samples WHERE ts BETWEEN ? AND ? ORDER BY ts", (t0, t1))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def add_thermal(self, ts: float, t: dict, p_now: float | None, v0: float | None) -> None:
        self.db.execute("INSERT OR REPLACE INTO thermal_samples VALUES (?,?,?,?,?,?,?,?)",
                        (ts, t.get("temp_c"), t.get("r_now_mohm"), t.get("r25_mohm"), t.get("pmax_w"),
                         t.get("prec_w"), p_now, v0))
        self.db.commit()

    def thermal_history(self, hours: float, points: int = 1500) -> list[dict]:
        bucket = max(5.0, hours * 3600 / points)
        cols = ["temp_c", "r_now_mohm", "r25_mohm", "pmax_w", "prec_w", "p_now_w", "v0_mv"]
        agg = ", ".join(f"max({c})" if c == "p_now_w" else f"avg({c})" for c in cols)
        cur = self.db.execute(f"SELECT avg(ts), {agg} FROM thermal_samples WHERE ts > ? "
                              "GROUP BY CAST(ts / ? AS INTEGER) ORDER BY 1", (time.time() - hours * 3600, bucket))
        return [dict(zip(["ts"] + cols, r)) for r in cur.fetchall()]
