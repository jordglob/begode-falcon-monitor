"""Ride analysis afterwards: elevation profile, climbs, and energy against elevation.

Pipeline: positions -> distance along the ride -> elevation (terrain model or filtered GPS)
-> resampled every STEP_M metres -> smoothed -> grade -> climbs/descents by hysteresis ->
energy per step from the packet-rate energy counters stored with each position.

Energy per class: flat (|grade| < FLAT_PCT), up, down. "Extra climbing energy" is what the
climbs cost above the flat-road consumption over the same distance. With a total mass the
result is compared with physics: potential energy m·g·Δh.
"""
from __future__ import annotations

import bisect
import math

from .elevation import gps_altitudes, hysteresis_climbs
from .rides import haversine_m

ANALYSIS_VERSION = 1
STEP_M = 10.0
SMOOTH_M = {"dem": 60.0, "gps": 150.0}
GRADE_WINDOW_M = 60.0
FLAT_PCT = 1.0
MAX_GRADE = 30.0
G = 9.81


def _interp(xs: list[float], ys: list[float | None], x: float) -> float | None:
    """Linear interpolation over valid (non-None) samples; None outside or across gaps."""
    i = bisect.bisect_left(xs, x)
    if i < len(xs) and xs[i] == x:
        return ys[i]
    if i == 0 or i >= len(xs):
        return None
    a, b = ys[i - 1], ys[i]
    if a is None or b is None:
        return None
    f = (x - xs[i - 1]) / (xs[i] - xs[i - 1]) if xs[i] != xs[i - 1] else 0.0
    return a + (b - a) * f


def _smooth(vals: list[float], half: int) -> list[float]:
    if half <= 0:
        return vals[:]
    out = []
    pre = [0.0]
    for v in vals:
        pre.append(pre[-1] + v)
    n = len(vals)
    for i in range(n):
        a, b = max(0, i - half), min(n, i + half + 1)
        out.append((pre[b] - pre[a]) / (b - a))
    return out


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting (small normal equations, no numpy)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[piv][c]) < 1e-12:
            return None
        m[c], m[piv] = m[piv], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] for i in range(n)]


def grade_regression(rows: list[tuple[float, float, float, float]]) -> dict | None:
    """rows: (km, wh_net, grade %, speed km/h). Fits
        Wh/km = a + b_up·g⁺ + b_down·g⁻ + c·v² + d/v
    weighted by distance (d/v = standing losses spread over fewer km at low speed, c·v² = air).
    b/10 = Wh per metre of climb (up) / saved per metre of descent."""
    data = [(km, wh / km, g, v) for km, wh, g, v in rows if km > 0 and v is not None and v > 2]
    if len(data) < 30 or not any(g > 1 for _, _, g, _ in data) or not any(g < -1 for _, _, g, _ in data):
        return None
    vs = [v for _, _, _, v in data]
    mv = sum(vs) / len(vs)
    speed_terms = (sum((v - mv) ** 2 for v in vs) / len(vs)) ** 0.5 > 1.5   # else collinear
    k = 5 if speed_terms else 3
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for w, y, g, v in data:
        x = [1.0, max(g, 0.0), min(g, 0.0), v * v, 1.0 / v][:k]
        for i in range(k):
            xty[i] += w * x[i] * y
            for j in range(k):
                xtx[i][j] += w * x[i] * x[j]
    sol = _solve(xtx, xty)
    if not sol:
        return None
    a, bu, bd = sol[:3]
    c, d = (sol[3], sol[4]) if speed_terms else (None, None)
    return {"base_wh_per_km": round(a, 1), "up_wh_per_m": round(bu / 10, 3),
            "down_wh_per_m": round(bd / 10, 3), "air_coef": round(c, 4) if c is not None else None,
            "standing_coef": round(d, 1) if d is not None else None, "speed_terms": speed_terms,
            "n": len(data)}


def choose_source(points: list[dict], setting: str, dem) -> tuple[str, list[float | None]]:
    dem_vals = [dem.value(p["lat"], p["lon"]) for p in points] if dem is not None and dem.tiles else []
    covered = sum(v is not None for v in dem_vals) / len(points) if dem_vals else 0.0
    if setting == "dem" or (setting == "auto" and covered >= 0.8):
        if covered > 0:
            return "dem", dem_vals
    return "gps", gps_altitudes(points)


def analyze(points: list[dict], settings: dict, dem=None, mass_kg: float | None = None) -> dict:
    pts = [p for p in points if p.get("lat") is not None]
    if len(pts) < 3:
        return {"ok": False, "reason": "för få punkter"}
    # distance along the ride
    dist = [0.0]
    for a, b in zip(pts, pts[1:]):
        dist.append(dist[-1] + haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]))
    # make distance strictly increasing for interpolation
    for i in range(1, len(dist)):
        if dist[i] <= dist[i - 1]:
            dist[i] = dist[i - 1] + 1e-6
    source, elev_raw = choose_source(pts, settings.get("elevation_source", "auto"), dem)
    if all(v is None for v in elev_raw):
        return {"ok": False, "reason": "ingen höjd (GPS-höjd saknas och ingen terrängmodell)"}
    ts = [p["ts"] for p in pts]
    wo = [p.get("wh_out_cum") for p in pts]
    wr = [p.get("wh_regen_cum") for p in pts]

    # resample every STEP_M metres
    total = dist[-1]
    n = int(total // STEP_M) + 1
    s = [k * STEP_M for k in range(n)]
    e_s = [_interp(dist, elev_raw, x) for x in s]
    # fill elevation gaps (DEM holes) by nearest valid
    last = next((v for v in e_s if v is not None), 0.0)
    for k in range(n):
        if e_s[k] is None:
            e_s[k] = last
        last = e_s[k]
    half = int(SMOOTH_M[source] / STEP_M / 2)
    e_sm = _smooth(e_s, half)
    t_s = [_interp(dist, ts, x) for x in s]
    wo_s = [_interp(dist, wo, x) for x in s]
    wr_s = [_interp(dist, wr, x) for x in s]

    # grade over GRADE_WINDOW_M
    gw = max(1, int(GRADE_WINDOW_M / STEP_M / 2))
    grade = []
    for k in range(n):
        a, b = max(0, k - gw), min(n - 1, k + gw)
        g = 100.0 * (e_sm[b] - e_sm[a]) / ((b - a) * STEP_M) if b > a else 0.0
        grade.append(max(-MAX_GRADE, min(MAX_GRADE, g)))

    # climbs / descents
    thr = settings.get(f"climb_threshold_{source}_m") or (2.0 if source == "dem" else 5.0)
    segs = hysteresis_climbs(s, e_sm, thr)

    def energy(k0: int, k1: int) -> tuple[float | None, float | None]:
        if None in (wo_s[k0], wo_s[k1], wr_s[k0], wr_s[k1]):
            return None, None
        return wo_s[k1] - wo_s[k0], wr_s[k1] - wr_s[k0]

    pwm_by_ts = [(p["ts"], p.get("pwm_max")) for p in pts if p.get("pwm_max") is not None]

    climbs = []
    for c in segs:
        k0, k1 = c["i0"], c["i1"]
        out, reg = energy(k0, k1)
        dur = (t_s[k1] - t_s[k0]) if t_s[k0] is not None and t_s[k1] is not None else None
        net = (out - reg) if out is not None else None
        pw = [v for t, v in pwm_by_ts if t_s[k0] is not None and t_s[k1] is not None and t_s[k0] <= t <= t_s[k1]]
        climbs.append({
            "dir": c["dir"], "start_km": round(s[k0] / 1000, 3), "end_km": round(s[k1] / 1000, 3),
            "length_m": c["length_m"], "dh_m": c["dh_m"],
            "avg_grade_pct": round(100 * c["dh_m"] / c["length_m"], 1) if c["length_m"] else None,
            "max_grade_pct": round((max if c["dir"] == "up" else min)(grade[k0:k1 + 1]), 1),
            "duration_s": round(dur) if dur else None,
            "wh_out": round(out, 2) if out is not None else None,
            "wh_regen": round(reg, 2) if reg is not None else None,
            "wh_net": round(net, 2) if net is not None else None,
            "wh_per_m": round(net / abs(c["dh_m"]), 3) if net is not None and c["dh_m"] else None,
            "avg_power_w": round(net * 3600 / dur) if net is not None and dur else None,
            "min_margin_pct": round(100 - max(pw)) if pw else None,
            "t0": t_s[k0], "t1": t_s[k1],
        })

    # energy per class (flat / up / down) step by step
    cls = {"flat": [0.0, 0.0, 0.0, 0.0], "up": [0.0, 0.0, 0.0, 0.0], "down": [0.0, 0.0, 0.0, 0.0]}
    covered_m = 0.0
    for k in range(1, n):
        g = grade[k]
        c = "flat" if abs(g) < FLAT_PCT else ("up" if g > 0 else "down")
        out, reg = energy(k - 1, k)
        if out is None:
            continue
        cls[c][0] += STEP_M
        cls[c][1] += out
        cls[c][2] += reg
        cls[c][3] += out - reg
        covered_m += STEP_M
    per_class = {k: {"km": round(v[0] / 1000, 3), "wh_out": round(v[1], 2), "wh_regen": round(v[2], 2),
                     "wh_net": round(v[3], 2), "wh_per_km": round(v[3] / (v[0] / 1000), 1) if v[0] > 50 else None}
                 for k, v in cls.items()}

    ascent = sum(c["dh_m"] for c in climbs if c["dir"] == "up")
    descent = -sum(c["dh_m"] for c in climbs if c["dir"] == "down")
    out_all, reg_all = energy(0, n - 1)
    flat_whkm = per_class["flat"]["wh_per_km"]
    up = per_class["up"]
    extra_up = (up["wh_net"] - flat_whkm * up["km"]) if flat_whkm is not None and up["km"] else None
    reg_down = per_class["down"]["wh_regen"]
    physics = None
    if mass_kg:
        epot_up = mass_kg * G * ascent / 3600
        epot_down = mass_kg * G * descent / 3600
        physics = {
            "mass_kg": mass_kg,
            "potential_up_wh": round(epot_up, 1), "potential_down_wh": round(epot_down, 1),
            "theory_wh_per_m": round(mass_kg * G / 3600, 3),
            "climb_efficiency_pct": round(100 * epot_up / extra_up) if extra_up and extra_up > 0 and epot_up else None,
            "regen_recovered_pct": round(100 * reg_down / epot_down) if epot_down else None,
        }

    totals = {
        "distance_km": round(total / 1000, 2), "ascent_m": round(ascent, 1), "descent_m": round(descent, 1),
        "max_elev_m": round(max(e_sm), 1), "min_elev_m": round(min(e_sm), 1),
        "steepest_up_pct": round(max(grade), 1), "steepest_down_pct": round(min(grade), 1),
        "wh_out": round(out_all, 1) if out_all is not None else None,
        "wh_regen": round(reg_all, 1) if reg_all is not None else None,
        "wh_net": round(out_all - reg_all, 1) if out_all is not None else None,
        "wh_per_km": round((out_all - reg_all) / (total / 1000), 1) if out_all is not None and total > 0 else None,
        "energy_coverage_pct": round(100 * covered_m / total) if total else 0,
        "extra_climb_wh": round(extra_up, 1) if extra_up is not None else None,
        "climb_wh_per_m": round(extra_up / ascent, 3) if extra_up is not None and ascent > 0 else None,
        "regen_down_wh": round(reg_down, 1),
        "regen_per_m": round(reg_down / descent, 3) if descent > 0 else None,
    }

    # profile for the UI (≤ 600 points) and per-position values for the map
    stride = max(1, n // 600)
    prof = []
    for k in range(0, n, stride):
        k2 = min(n - 1, k + stride)
        out, reg = energy(k, k2)
        dt = (t_s[k2] - t_s[k]) if t_s[k] is not None and t_s[k2] is not None else None
        pw = round((out - reg) * 3600 / dt) if out is not None and dt else None
        spd = round(stride * STEP_M / dt * 3.6, 1) if dt else None
        prof.append([round(s[k] / 1000, 3), round(e_sm[k], 1), round(grade[k], 1), pw, spd, t_s[k]])
    per_point = []
    for i, p in enumerate(pts):
        k = min(n - 1, int(round(dist[i] / STEP_M)))
        pw = None
        if i > 0 and wo[i] is not None and wo[i - 1] is not None and wr[i] is not None and wr[i - 1] is not None:
            dt = ts[i] - ts[i - 1]
            if dt > 0:
                pw = round(((wo[i] - wo[i - 1]) - (wr[i] - wr[i - 1])) * 3600 / dt)
        per_point.append([round(e_sm[k], 1), round(grade[k], 1), pw])

    # regression over the profile steps (separates climbing from speed effects)
    rows = []
    for r0, r1 in zip(prof, prof[1:]):
        if r0[3] is not None and r0[5] is not None and r1[5] is not None and r0[4]:
            km = r1[0] - r0[0]
            rows.append((km, r0[3] * (r1[5] - r0[5]) / 3600, r0[2], r0[4]))
    reg = grade_regression(rows)
    if reg and physics:
        th = physics["theory_wh_per_m"]
        if reg["up_wh_per_m"] > 0:
            physics["climb_efficiency_regression_pct"] = round(100 * th / reg["up_wh_per_m"])
        if reg["down_wh_per_m"] > 0:
            physics["descent_recovered_regression_pct"] = round(100 * reg["down_wh_per_m"] / th)

    return {"ok": True, "version": ANALYSIS_VERSION, "regression": reg, "source": source, "step_m": STEP_M,
            "threshold_m": thr, "totals": totals, "per_class": per_class, "climbs": climbs,
            "physics": physics, "profile": prof, "per_point": per_point}


def grade_energy_bins(analyses: list[dict], width: float = 2.0, limit: float = 10.0) -> list[dict]:
    """Wh/km per grade bin over many rides (from each ride's profile)."""
    bins: dict[float, list[float]] = {}
    for a in analyses:
        prof = a.get("profile") or []
        for r0, r1 in zip(prof, prof[1:]):
            km = r1[0] - r0[0]
            if km <= 0 or r0[3] is None or r0[5] is None or r1[5] is None:
                continue
            g = max(-limit, min(limit, r0[2]))
            b = round(math.floor(g / width) * width + width / 2, 1)
            wh = r0[3] * (r1[5] - r0[5]) / 3600
            acc = bins.setdefault(b, [0.0, 0.0, 0])
            acc[0] += km
            acc[1] += wh
            acc[2] += 1
    return [{"grade_pct": b, "km": round(v[0], 2), "wh_per_km": round(v[1] / v[0], 1) if v[0] > 0.05 else None,
             "n": v[2]} for b, v in sorted(bins.items())]
