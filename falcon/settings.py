"""User settings (stored in the database, edited on the Inställningar tab, local-only)."""
from __future__ import annotations

from pathlib import Path

# id -> (label, kind, default, min, max, help)
FIELDS = {
    "rider_kg": ("Förarens vikt", "float", None, 20, 250, "kg – bara för jämförelsen med fysiken"),
    "gear_kg": ("Utrustning (skydd, väska …)", "float", None, 0, 60, "kg"),
    "wheel_kg": ("Hjulets vikt", "float", None, 10, 80, "kg – väg hjulet eller ange tillverkarens uppgift"),
    "elevation_source": ("Höjdkälla", "choice", "auto", None, None,
                         "auto = terrängmodell om rutor finns, annars GPS"),
    "dem_dir": ("Mapp med terrängmodell (.hgt/.tif)", "text",
                str(Path.home() / ".local/share/begode-falcon/dem"), None, None, ""),
    "climb_threshold_dem_m": ("Minsta höjdändring för backe (terrängmodell)", "float", 2.0, 0.5, 20, "m"),
    "climb_threshold_gps_m": ("Minsta höjdändring för backe (GPS)", "float", 5.0, 1, 30, "m"),
    "gps_ride_interval_s": ("GPS-punkt under tur var", "float", 2.0, 1, 30, "s"),
    "gps_idle_interval_s": ("GPS-punkt i stillastående var", "float", 5.0, 1, 300, "s"),
}
CHOICES = {"elevation_source": {"auto": "auto", "dem": "terrängmodell", "gps": "GPS-höjd"}}


def defaults() -> dict:
    return {k: v[2] for k, v in FIELDS.items()}


def validate(update: dict) -> dict:
    out = {}
    for k, v in update.items():
        if k not in FIELDS:
            raise ValueError(f"okänd inställning {k}")
        label, kind, _d, lo, hi, _h = FIELDS[k]
        if v in (None, ""):
            out[k] = None if kind == "float" else FIELDS[k][2]
            continue
        if kind == "float":
            v = float(str(v).replace(",", "."))
            if lo is not None and not lo <= v <= hi:
                raise ValueError(f"{label}: {lo}–{hi}")
        elif kind == "choice" and v not in CHOICES[k]:
            raise ValueError(f"{label}: ogiltigt val")
        out[k] = v
    return out


def total_mass(s: dict) -> float | None:
    if s.get("rider_kg") is None or s.get("wheel_kg") is None:
        return None
    return s["rider_kg"] + (s.get("gear_kg") or 0) + s["wheel_kg"]


def describe() -> list[dict]:
    return [{"id": k, "label": v[0], "kind": v[1], "default": v[2], "min": v[3], "max": v[4],
             "help": v[5], "choices": CHOICES.get(k)} for k, v in FIELDS.items()]
