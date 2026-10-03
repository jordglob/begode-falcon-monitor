"""Settings backup: a dated JSON snapshot of everything the wheel reports about itself.

Saving is read-only (allowed from any device). A backup contains the decoded settings,
identity (wheel firmware, BLE module), odometer and every raw packet row, so it stays
useful even if field meanings are corrected later. Restoring only writes settings that
are both writable and verifiable (RESTORABLE) and goes through the normal control path.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

# backup field -> control setting id; only settings the wheel reports back (verified)
RESTORABLE = {"p4.led_mode": "led_mode"}

# reported in packet 4 but not settings: they change by themselves
NOT_SETTINGS = ("p4.odometer_raw", "p4.alert_raw", "p4.alerts", "p4.power_off_in_s")

NAME_RE = re.compile(r"^settings-\d{8}-\d{6}\.json$")


class Backups:
    def __init__(self, folder: Path):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)

    def make(self, snapshot: dict, raw: dict, device_info: dict, app_version: str,
             reason: str = "manuell", now: float | None = None) -> dict:
        now = time.time() if now is None else now
        p4 = snapshot.get("p4") or {}
        settings = {f"p4.{k}": v for k, v in p4.items()}
        for g, d in (snapshot.get("groups") or {}).items():
            settings[f"p1[{g}].pwm_limit_or_alarm"] = d.get("pwm_limit_or_alarm")
        doc = {
            "kind": "begode-falcon-settings",
            "created": now,
            "created_local": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "reason": reason,
            "app_version": app_version,
            "wheel_firmware": device_info.get("wheel_firmware"),
            "ble_module": {k: device_info.get(k) for k in ("firmware", "hardware", "manufacturer")},
            "odometer_m": p4.get("odometer_raw"),
            "settings": settings,
            "restorable": {k: settings.get(k) for k in RESTORABLE if settings.get(k) is not None},
            "raw_rows": {k: v["hex"] for k, v in raw.items()},
        }
        name = time.strftime("settings-%Y%m%d-%H%M%S.json", time.localtime(now))
        (self.folder / name).write_text(json.dumps(doc, indent=1, ensure_ascii=False))
        return {"name": name, **doc}

    def list(self) -> list[dict]:
        out = []
        for f in sorted(self.folder.glob("settings-*.json"), reverse=True):
            try:
                d = json.loads(f.read_text())
            except Exception:
                continue
            out.append({"name": f.name, "created": d.get("created"), "reason": d.get("reason"),
                        "wheel_firmware": d.get("wheel_firmware"), "odometer_m": d.get("odometer_m"),
                        "restorable": d.get("restorable", {})})
        return out

    def load(self, name: str) -> dict:
        if not NAME_RE.match(name or ""):
            raise ValueError("ogiltigt namn")
        f = self.folder / name
        if not f.exists():
            raise ValueError("finns inte")
        return json.loads(f.read_text())

    def latest_age_s(self, now: float | None = None) -> float | None:
        items = self.list()
        if not items or not items[0].get("created"):
            return None
        return (time.time() if now is None else now) - items[0]["created"]

    @staticmethod
    def changes_vs_now(doc: dict, snapshot: dict) -> dict:
        """Settings that differ between a backup and the wheel right now."""
        now = {f"p4.{k}": v for k, v in (snapshot.get("p4") or {}).items()}
        return {k: [v, now.get(k)] for k, v in doc.get("settings", {}).items()
                if k.startswith("p4.") and k in now and now.get(k) != v
                and k not in NOT_SETTINGS}
