# Elevation and energy

Computed **afterwards** for each ride (cached per ride, recomputed when settings or the
elevation source change). Nothing is sent anywhere: terrain tiles are read offline.

## Data captured during a ride
- Battery energy is integrated on **every packet (0.3 s)**: bus voltage × battery current,
  consumption and regeneration as two monotonic counters.
- Every stored GPS point (every 2 s while moving, 5 s standing) carries both counters, the
  max PWM in the interval, average current and data coverage. Energy between any two
  points = counter difference – exact regardless of the storage interval.

## Elevation sources
| Source | Quality | How |
|---|---|---|
| Lantmäteriet Markhöjdmodell grid 1+ (Sweden) | best: 1 m, **ground** (no trees/buildings) | free (CC BY 4.0) with a [Geotorget](https://geotorget.lantmateriet.se) account; `tools/lm_fetch.py --area stockholm,nacka,huddinge`; read directly in SWEREF 99 TM |
| Copernicus GLO-30 (global) | 30 m **surface** model – trees and buildings count as height | `tools/dem_fetch.py LAT_MIN LON_MIN LAT_MAX LON_MAX` |
| `.hgt` / EPSG:4326 GeoTIFF | depends on the data | put files in the DEM folder |
| GPS altitude | coarse (vertical error 2–3× horizontal) | poor fixes dropped, median filter, 150 m smoothing |

The SWEREF 99 TM projection (Gauss–Krüger, GRS80) is implemented in pure Python and matches
pyproj to < 1 mm (tested).

## Analysis
1. Distance along the ride, elevation per point, resampled every **10 m**.
2. Smoothing: 60 m (terrain model) / 150 m (GPS). Grade over a 60 m window, clipped ±30 %.
3. **Climbs and descents** by hysteresis: a turn counts only after the elevation moved back
   by the threshold (2 m terrain model / 5 m GPS – adjustable).
4. Per climb: length, Δh, average/max grade, energy out/regenerated/net, Wh per metre of
   height, average power, **lowest safety margin** (100 % − PWM).
5. Energy per road class: flat (|grade| < 1 %), uphill, downhill.
6. **Regression** over all 10 m steps:
   `Wh/km = a + b_up·grade⁺ + b_down·grade⁻ + c·v² + d/v`
   separates climbing from speed (air drag `c·v²`, standing losses `d/v`).
   `b_up/10` = Wh per metre climbed, `b_down/10` = Wh saved per metre descended.
   On synthetic rides with known physics it recovers the true values within a few percent;
   noisy GPS altitude biases them low (≈15 % with 3 m noise) – a terrain model avoids that.
7. With a **total mass** (rider + gear + wheel, Inställningar tab) the result is compared with
   physics: potential energy m·g·Δh → climbing efficiency and share of potential energy
   recovered downhill.
8. Over many rides: Wh/km per grade bin.

## Map
Colour modes: speed and elevation (sequential), grade and power (diverging: blue = downhill /
regeneration, grey = flat / zero, orange = uphill / consumption). Elevation and power
profiles below the map share a cursor with the map marker; clicking a climb zooms both.
