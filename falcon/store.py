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
