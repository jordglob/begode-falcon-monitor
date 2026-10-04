"""Charge limit via a smart plug (idea from EUC World): stop charging at a chosen average
cell voltage (e.g. 4.10 V ≈ 90 %) to spare the battery – fits the exposure tracking in
battery health. Off unless a plug is configured.

Fail-safe: if the wheel's data stops, the plug is left as it is – the charger's own
end-of-charge cut-off still protects the pack, so the worst case is a full charge.

Plugs: Shelly Gen1 (`/relay/0?turn=on|off`) and Shelly Gen2/Plus (`/rpc/Switch.Set`).
"""
from __future__ import annotations

import json
import time
import urllib.request

HOLD_S = 30          # mean cell voltage must stay at/above the target this long
CHARGING_A = -0.5    # battery current below this = charging


class Plug:
    def __init__(self, url: str | None, kind: str = "shelly2", opener=None):
        self.url = (url or "").rstrip("/")
        self.kind = kind
        self.opener = opener or (lambda u: urllib.request.urlopen(u, timeout=5).read())

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def _get(self, path: str) -> bytes:
        return self.opener(self.url + path)

    def set(self, on: bool) -> None:
        if not self.configured:
            raise RuntimeError("ingen smart kontakt konfigurerad (FALCON_PLUG_URL)")
        if self.kind == "shelly1":
            self._get(f"/relay/0?turn={'on' if on else 'off'}")
        else:
            self._get(f"/rpc/Switch.Set?id=0&on={'true' if on else 'false'}")

    def state(self) -> bool | None:
        if not self.configured:
            return None
        try:
            if self.kind == "shelly1":
                return bool(json.loads(self._get("/relay/0"))["ison"])
            return bool(json.loads(self._get("/rpc/Switch.GetStatus?id=0"))["output"])
        except Exception:
            return None


class ChargeController:
    def __init__(self, plug: Plug, config: dict | None = None, on_event=None):
        c = config or {}
        self.plug = plug
        self.enabled = bool(c.get("enabled", False))
        self.target_cell_v = float(c.get("target_cell_v", 4.10))
        self.on_event = on_event or (lambda m: None)
        self.above_since: float | None = None
        self.last_action: dict | None = None
        self.charging = False
        self.mean_cell_v: float | None = None

    def config(self) -> dict:
        return {"enabled": self.enabled, "target_cell_v": self.target_cell_v}

    def tick(self, snap: dict, fresh: bool, now: float | None = None) -> None:
        now = time.time() if now is None else now
        cells = [v for s in (snap.get("cells") or {}).values() for v in s.get("cells_mv", [])]
        self.mean_cell_v = sum(cells) / len(cells) / 1000 if cells else None
        i = snap.get("battery_current_a")
        bms_says = any(b.get("activity") == "laddning" for b in (snap.get("bms") or {}).values())
        self.charging = bool(fresh and bms_says and i is not None and i < CHARGING_A)  # p7 < 0 on regen too
        if not (self.enabled and self.plug.configured and self.charging and self.mean_cell_v):
            self.above_since = None
            return
        if self.mean_cell_v >= self.target_cell_v:
            self.above_since = self.above_since or now
            if now - self.above_since >= HOLD_S:
                try:
                    self.plug.set(False)
                    self.last_action = {"ts": now, "action": "av", "mean_cell_v": round(self.mean_cell_v, 3)}
                    self.on_event(f"🔌 Laddning stoppad vid {self.mean_cell_v:.3f} V/cell "
                                  f"(gräns {self.target_cell_v:.2f} V)")
                except Exception as e:
                    self.on_event(f"🔌 Kunde inte stänga av kontakten: {e}")
                self.above_since = None
        else:
            self.above_since = None

    def report(self) -> dict:
        return {**self.config(), "plug_configured": self.plug.configured, "plug_kind": self.plug.kind,
                "charging": self.charging, "mean_cell_v": round(self.mean_cell_v, 3) if self.mean_cell_v else None,
                "above_target_for_s": round(time.time() - self.above_since) if self.above_since else None,
                "last_action": self.last_action}
