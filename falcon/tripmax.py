"""Max (and min) values per ride, each with the moment and place it happened.

Sources: the ride's GPS points (speed, PWM, current/power/regeneration/voltage extremes
measured at packet rate and stored per interval), the 5 s telemetry inside the ride's time
window (cells, temperatures), the elevation analysis (grades) and the logged alarm events.
"""
from __future__ import annotations

from .rides import _speed


def _pos_at(points: list[dict], ts: float) -> tuple[float | None, float | None]:
    best = min(points, key=lambda p: abs(p["ts"] - ts)) if points else None
    return (best["lat"], best["lon"]) if best else (None, None)


def _ext(rows, key, fn=max, getter=None):
    vals = [(getter(r) if getter else r.get(key), r) for r in rows]
    vals = [(v, r) for v, r in vals if v is not None]
    if not vals:
        return None, None
    v, r = fn(vals, key=lambda x: x[0])
    return v, r["ts"]


def compute(points: list[dict], samples: list[dict], analysis: dict | None, events: list[dict]) -> dict:
    items = []

    def add(key, label, value, unit, ts, level=None, note=None, dec=1):
        if value is None:
            return
        lat, lon = _pos_at(points, ts) if ts else (None, None)
        items.append({"key": key, "label": label, "value": round(value, dec) if dec is not None else value,
                      "unit": unit, "ts": ts, "lat": lat, "lon": lon, "level": level, "note": note})

    v, t = _ext(points, None, max, _speed)
    add("speed", "Maxfart", v, "km/h", t)
    v, t = _ext(points, "pwm_max")
    if v is not None:
        margin = 100 - v
        add("margin", "Lägsta säkerhetsmarginal", margin, "%", t,
            "bad" if margin <= 20 else "warn" if margin <= 30 else "ok", f"högsta PWM {v:.0f} %", 0)
    v, t = _ext(points, "current_max")
    add("current", "Högsta ström", v, "A", t)
    v, t = _ext(points, "power_max_w")
    add("power", "Högsta effekt (förbrukning)", v, "W", t, dec=0)
    v, t = _ext(points, "regen_max_w")
    add("regen", "Högsta återvinning (inbromsning/nedför)", v, "W", t, dec=0)
    v, t = _ext(points, "volt_min", min)
    add("volt_min", "Lägsta spänning under last", v, "V", t, dec=2)

    v, t = _ext(samples, "cell_min_mv", min)
    if v is not None:
        add("cell_min", "Lägsta cellspänning", v / 1000, "V", t,
            "bad" if v < 3000 else "warn" if v < 3300 else "ok", dec=3)
    v, t = _ext(samples, "cell_spread_mv")
    if v is not None:
        add("cell_spread", "Största cellspridning", v, "mV", t, "bad" if v > 60 else "warn" if v > 30 else "ok", dec=0)
    for key, label, warn, bad in (("motor_temp_c", "Högsta motortemperatur", 70, 85),
                                  ("board_temp_c", "Högsta moderkortstemperatur", 60, 70),
                                  ("temp2_c", "Högsta batteritemperatur", 45, 55)):
        v, t = _ext(samples, key)
        if v is not None:
            add(key, label, v, "°C", t, "bad" if v >= bad else "warn" if v >= warn else "ok", dec=0)

    if analysis and analysis.get("ok"):
        tot = analysis["totals"]
        prof = analysis.get("profile") or []
        if prof:
            up = max(prof, key=lambda r: r[2])
            dn = min(prof, key=lambda r: r[2])
            add("grade_up", "Brantaste uppför", tot.get("steepest_up_pct"), "%", up[5])
            add("grade_down", "Brantaste nedför", tot.get("steepest_down_pct"), "%", dn[5])
            hi = max(prof, key=lambda r: r[1])
            add("elev_max", "Högsta punkt", tot.get("max_elev_m"), "m", hi[5], dec=0)

    alarms = [e for e in events if e.get("level") == "alarm" and e.get("new") not in (None, "None", "False", "normal", "")]
    kinds = sorted({e["key"] for e in alarms})
    return {"items": items, "alarms": {"count": len(alarms), "kinds": kinds,
                                       "events": [{**e, **dict(zip(("lat", "lon"), _pos_at(points, e["ts"])))}
                                                  for e in alarms[:50]]},
            "notes": [] if any(p.get("current_max") is not None for p in points) else
            ["Ström, effekt, återvinning och lägsta spänning per tur sparas från version 0.15.0 – äldre turer saknar dem."]}
