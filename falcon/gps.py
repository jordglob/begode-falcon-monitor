"""GPS position (step 1: status + stats; goal: rides on a map coloured by speed).

Source: ModemManager (`mmcli -m <n> --location-get`), e.g. the GPS in a laptop's WWAN
module. NMEA parsing is source-independent so gpsd/serial can be added later.

Known receiver quirk handled here: GPS week-number rollover. Old receivers (seen on a
Lenovo/Ericsson H5321 gw) report dates exactly 1024 weeks too early (2026 -> 2007);
time of day is correct. Dates before 2019 are moved forward by 1024 weeks.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import re
import shutil
import time
from collections import deque

ROLLOVER = dt.timedelta(weeks=1024)
KNOT_KMH = 1.852


def nmea_ok(line: str) -> bool:
    m = re.match(r"^\$([^*]+)\*([0-9A-Fa-f]{2})\s*$", line.strip())
    if not m:
        return False
    c = 0
    for ch in m.group(1):
        c ^= ord(ch)
    return c == int(m.group(2), 16)


def _deg(v: str, hemi: str) -> float | None:
    if not v:
        return None
    d = int(float(v) / 100)
    deg = d + (float(v) - d * 100) / 60
    return -deg if hemi in ("S", "W") else deg


def _f(v: str) -> float | None:
    try:
        return float(v)
    except ValueError:
        return None


def fix_date(ddmmyy: str, hhmmss: str) -> tuple[dt.datetime | None, bool]:
    """UTC datetime from RMC fields; second value True if a rollover was corrected."""
    if len(ddmmyy) != 6 or len(hhmmss) < 6:
        return None, False
    try:
        t = dt.datetime.strptime(ddmmyy + hhmmss[:6], "%d%m%y%H%M%S").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None, False
    corrected = False
    while t.year < 2019:
        t += ROLLOVER
        corrected = True
    return t, corrected


class Nmea:
    """Keeps the latest state from GGA / RMC / GSA / GSV sentences."""

    def __init__(self):
        self.state: dict = {}
        self._gsv: dict = {}
        self.sats: list[dict] = []

    def feed(self, line: str) -> None:
        line = line.strip()
        if not line.startswith("$") or not nmea_ok(line):
            return
        f = line.split("*")[0].split(",")
        kind = f[0][3:]
        s = self.state
        if kind == "GGA" and len(f) >= 10:
            s["fix_quality"] = int(f[6] or 0)
            s["sats_used"] = int(f[7] or 0)
            s["hdop"] = _f(f[8])
            s["alt_m"] = _f(f[9])
            if s["fix_quality"] > 0:
                s["lat"], s["lon"] = _deg(f[2], f[3]), _deg(f[4], f[5])
        elif kind == "RMC" and len(f) >= 10:
            s["valid"] = f[2] == "A"
            if s["valid"]:
                s["lat"], s["lon"] = _deg(f[3], f[4]), _deg(f[5], f[6])
                kn = _f(f[7])
                s["speed_kmh"] = round(kn * KNOT_KMH, 2) if kn is not None else None
                s["course_deg"] = _f(f[8])
            t, corr = fix_date(f[9], f[1])
            if t:
                s["utc"] = t.isoformat()
                s["rollover_corrected"] = corr
                s["raw_date"] = f[9]
        elif kind == "GSA" and len(f) >= 18:
            s["fix_type"] = {"1": "ingen", "2": "2D", "3": "3D"}.get(f[2], "?")
            s["pdop"], s["hdop"], s["vdop"] = _f(f[15]), _f(f[16]), _f(f[17])
        elif kind == "GSV" and len(f) >= 4:
            total, num = int(f[1] or 1), int(f[2] or 1)
            if num == 1:
                self._gsv = {}
            for i in range(4, len(f) - 3, 4):
                if f[i]:
                    self._gsv[f[i]] = {"prn": int(f[i]), "elev": _f(f[i + 1]), "azim": _f(f[i + 2]),
                                       "snr": _f(f[i + 3])}
            if num == total:
                self.sats = sorted(self._gsv.values(), key=lambda x: -(x["snr"] or 0))
                s["sats_in_view"] = int(f[3] or 0)

    @property
    def has_fix(self) -> bool:
        return bool(self.state.get("valid")) or (self.state.get("fix_quality") or 0) > 0


def find_mm_modem() -> str | None:
    """First ModemManager modem index, or None."""
    if not shutil.which("mmcli"):
        return None
    import subprocess
    try:
        out = subprocess.run(["mmcli", "-L"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    m = re.search(r"/Modem/(\d+)", out)
    return m.group(1) if m else None


class GpsReader:
    def __init__(self, modem: str | None = None, interval_s: float = 2.0):
        self.modem = modem
        self.interval_s = interval_s
        self.nmea = Nmea()
        self.status = "startar"
        self.started = time.time()
        self.first_fix_s: float | None = None
        self.polls = 0
        self.fix_polls = 0
        self.last_ts: float | None = None
        self.track: deque = deque(maxlen=2000)     # recent fixes (ts, lat, lon, speed)
        self.on_fix = None                          # callback(dict) for storage

    async def _get(self) -> str:
        p = await asyncio.create_subprocess_exec(
            "mmcli", "-m", self.modem, "--location-get",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 10)
        return out.decode(errors="replace")

    async def run(self) -> None:
        while True:
            try:
                if self.modem is None:
                    self.modem = find_mm_modem()
                    if self.modem is None:
                        self.status = "ingen GPS hittad (ModemManager)"
                        await asyncio.sleep(30)
                        continue
                text = await self._get()
                for line in re.findall(r"\$G[A-Z]{4},[^\s|]*", text):
                    self.nmea.feed(line)
                self.polls += 1
                self.last_ts = time.time()
                if "$G" not in text:
                    self.status = "GPS avstängd i modemet (mmcli --location-enable-gps-nmea)"
                elif self.nmea.has_fix:
                    self.fix_polls += 1
                    self.status = "position"
                    if self.first_fix_s is None:
                        self.first_fix_s = round(time.time() - self.started)
                    s = self.nmea.state
                    self.track.append((self.last_ts, s.get("lat"), s.get("lon"), s.get("speed_kmh")))
                    if self.on_fix:
                        self.on_fix(dict(s))
                else:
                    self.status = "söker satelliter"
            except Exception as e:
                self.status = f"fel: {e}"
            await asyncio.sleep(self.interval_s)

    def report(self) -> dict:
        s = dict(self.nmea.state)
        hdop = s.get("hdop")
        return {
            "status": self.status, "source": f"ModemManager modem {self.modem}" if self.modem else None,
            "fix": self.nmea.has_fix, "state": s,
            "accuracy_m_guess": round(hdop * 5) if hdop else None,   # rough: HDOP x ~5 m
            "satellites": self.nmea.sats,
            "stats": {"polls": self.polls, "fix_share_pct": round(100 * self.fix_polls / self.polls, 1)
                      if self.polls else None,
                      "first_fix_s": self.first_fix_s, "running_s": round(time.time() - self.started),
                      "track_points": len(self.track)},
        }
