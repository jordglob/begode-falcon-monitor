# Changelog

All notable changes. Versions follow `falcon/__init__.py`.

## 0.13.0 – 2026-10-03
- **Elevation and energy per ride** (computed afterwards, cached): climbs/descents by
  hysteresis, ascent/descent, steepest grade, energy per climb and per road class, Wh per metre
  of height, lowest safety margin per climb, regression separating climbing from speed, and a
  physics comparison (climbing efficiency, share recovered downhill) when a total mass is set.
- Map colour modes **speed / elevation / grade / power**; elevation and power profiles synced
  with a map marker; climb list that zooms map and profiles; Wh/km per grade over all rides.
- **Terrain models**: Lantmäteriet Markhöjdmodell grid 1+ (1 m ground model, read directly in
  SWEREF 99 TM – projection in pure Python, matches pyproj < 1 mm), Copernicus GLO-30,
  `.hgt`; `tools/lm_fetch.py` (Geotorget account, e.g. `--area stockholm,nacka,huddinge`:
  184 tiles, 1.6 GB) and `tools/dem_fetch.py`. Links to Geotorget in the app.
- **Inställningar** tab: rider / gear / wheel weight, elevation source, DEM folder, climb
  thresholds, GPS storage intervals.
- Energy integrated on every packet (0.3 s) and stored with each GPS point (consumption and
  regeneration counters, max PWM, average current, coverage); GPS points every 2 s while moving.
- **BLE reconnection**: direct connect to a known address (BlueZ connects on the first
  advertisement heard – no scan windows with gaps); scanning only for an unknown/forgotten
  device; backoff only after an established connection ended; half-open BlueZ links detected
  with `bluetoothctl info`; time-to-connect statistics. Live: 22 s at −90 dBm (was ~4 min).
- `docs/elevation.md`.

## 0.12.0 – 2026-10-03
- **Speed plausibility**: wheel speed vs GPS speed (only where the GPS fix is good: ≥5
  satellites, HDOP ≤2.5). *Spin* = the wheel turns clearly faster than the GPS moves (lifted,
  slipping); *carried* = GPS moves while the wheel stands still. Shown live, per ride and as
  dashed overlays on the map. Spinning points no longer set a ride's max speed.
- **Safety margin** (100 % − PWM) with a 3 s PWM forecast; **alarms** for PWM, speed, motor and
  board temperature, battery and lowest cell – banner, browser beep and event log; limits
  editable from the server computer.
- **Release the link** for 15 min / 1 h so the phone app can connect; "Ta tillbaka" resumes.
- **Charge limit via smart plug** (Shelly Gen1/Gen2, `FALCON_PLUG_URL`, `FALCON_PLUG_TYPE`):
  stops charging at a chosen average cell voltage (default 4.10 V); fail-safe leaves the plug
  alone when data stops.
- **Export**: rides as GPX 1.1 and CSV (with wheel/GPS speed and plausibility), telemetry as CSV.
- **Grafer** tab: voltage, current, cell spread, cell min/max, temperatures, sag and speed over
  1 h – 30 days (uPlot, vendored, MIT), downsampled server-side; one measure per chart.
- Deep links for every tab (`#graphs`, `#health`, …).
- `bluetoothctl` helpers are killed if they outlive their timeout and never read stdin
  (an unknown device could hang them forever).
- `docs/research/`: competing-apps comparison and protocol notes.
- Dry run extended: demo rides include a spinning section, demo telemetry for the graphs.

## 0.11.0 – 2026-10-03
- **Rides on a map**: rides are cut from the stored GPS points (gap > 2 min or 3 min without
  movement ends a ride; needs ≥30 s moving above 3 km/h and ≥100 m). List with distance, time
  in motion, max and average speed; the map (Leaflet + OpenStreetMap) draws the track coloured
  by speed on a single-hue ramp (light = slow, dark = fast) with a legend, hover tooltip with
  speed and time, start/finish markers. The wheel's own speed is used where available.
- Deep links `#pos` and `#ride=<id>`.
- Dry run without wheel or GPS: `tools/demo_ride.py` writes synthetic rides (Greenwich Park);
  `FALCON_BLE=0` runs the server without Bluetooth.

## 0.10.0 – 2026-10-03
- **Position** tab (GPS): fix type, position (OpenStreetMap link), rough accuracy, satellites
  used/in view with signal strength, HDOP/PDOP, GPS speed, course, altitude, UTC time, time to
  first fix. Source: ModemManager (`mmcli --location-get`), e.g. a laptop's WWAN GPS.
- GPS week-number rollover corrected (receivers reporting dates 1024 weeks too early).
- Positions stored every 5 s together with the wheel's own speed – groundwork for rides on a
  map coloured by speed. `FALCON_GPS=0` disables GPS.

## 0.9.1 – 2026-10-03
- Packet 4 word 4 is a **countdown to auto power-off** (seen live: 7200 → 0, then the wheel
  switched itself off), not a setting. Renamed `power_off_in_s`; the Live tab shows
  "Stänger av om … min" with ⚠️ below 10 minutes.
- Settings backup and the control field diff ignore the countdown (it caused one spurious
  "setting changed" backup per minute).

## 0.9.0 – 2026-10-02
- **Settings backup**: dated JSON snapshots (decoded settings, wheel firmware, BLE module,
  odometer, every raw packet row) in `~/.local/share/begode-falcon/backups/` (`FALCON_BACKUPS`).
  Saved manually from the *Styrning* tab, automatically once a day and whenever a setting differs
  from the latest backup (e.g. changed in the phone app). Download, "difference vs now" and
  restore of verifiable settings (LED mode) through the normal control path.
- Text replies to `V`/`N` no longer keep footer remnants (`ZZZZ`) of a cut frame.
- Wheel firmware (from `V`) is stored and survives restarts.
- SQLite in WAL mode and no write transaction left open between ticks (a 60 s open
  transaction blocked other writers).

## 0.8.0 – 2026-10-02
- **Wheel control (off by default)**: new *Styrning* tab. Can only be switched on – and used –
  from the computer running the server (client IP = loopback or the host's own address); other
  devices can only watch. Two-step confirmation, off at every server start, auto-off after
  10 min without a command, anyone can switch it off.
- Commands: beep, LED/ambient mode (✅ verified via packet 4), pedal mode, headlight, beeper
  volume, battery alarm (⚠️ sent twice, not yet verifiable), read requests `V`/`N` (text reply
  shown; `V` fills in the wheel firmware). Tiltback is deliberately not offered until mapped.
- Refused while the wheel moves, the motor works or telemetry is older than 2 s.
- Every command logged (SQLite `control_log` + event log) with result and the before/after diff
  of all decoded fields – unmapped settings show which field moved.
- The BLE layer has a single guarded write path that refuses blocked commands.

## 0.7.0 – 2026-10-02
- **All parameters** tab: every decoded field of every packet with description, unit, status
  (✅ / ⚠️ / ❓), raw bytes, 16-bit words, update period and age (`/api/all`).
- App version and BLE module firmware shown (`/api/version`); wheel firmware needs the `V`
  request and is not read yet.
- Signal strength (RSSI) from the last scan shown in the header.
- README in English; this changelog.

## 0.6.0 – 2026-10-02
- **Stable BLE link**: hard 30 s bound on connect + service discovery + subscribe; 8 s data
  watchdog forces a reconnect; BlueZ-side disconnect after every failure; BlueZ is told to forget
  the wheel after 2 failed connects in a row (fixes a stuck "Connected" state where every
  disconnect failed with `Disconnected (0x0e)`); backoff 1 → 10 s.
- Connection stability on the Logg tab: connects, drops with reason, share of time with data.
- Server stops within 2 s on Ctrl+C even with open SSE streams.

## 0.5.0 – 2026-10-02
- **Field meanings corrected** after comparison with WheelLog and the Home Assistant begode
  integration: two smart BMS units (rows 0–1 / 2–3) instead of four groups; packet 7 carries the
  battery current, packet 0 the phase current; half-pack voltages; wheel alert bits decoded.
- Imbalance guard compares BMS 1 with BMS 2; battery health no longer double-counts current.
- Wheel auto-discovery by name, then locked to the first wheel found. First public release.

## 0.4.0 – 2026-10-02
- Bus voltage/sag/health index recomputed on every packet (0.3 s) and pushed to the page via SSE.
- Per-cell internal resistance by time-stamped regression; per-cell value age shown.

## 0.3.0 – 2026-10-02
- **Battery health**: capacity from charges (Ah ÷ ΔSoC from rest voltage), health vs first
  measurement, km to 80 %, rides out of range, Wh/km, per-cell resistance at current steps,
  per-cell self-discharge, exposure (high SoC / temperature), charger drop-outs, wheel gauge
  check. Nominal capacity 1800 Wh.

## 0.2.0 – 2026-10-02
- **Imbalance guard**: shunt ratio and step alarm (unsoldered/burned shunt resistor), sum vs
  controller, pack drop-out, string/cell/bank, temperature, endless balancing.

## 0.1.0 – 2026-10-02
- Read-only BLE dashboard: frame decoding, live data, energy view with 48 cell voltages,
  SoC guess, sag estimate, SQLite history; write-and-verify core (not wired in) with blocked
  dangerous commands.
