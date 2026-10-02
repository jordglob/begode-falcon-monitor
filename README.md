# begode-falcon-monitor

Unofficial monitor for the **Begode Falcon Pro** electric unicycle (and probably other Begode
wheels with a smart BMS) over Bluetooth LE, running on a Linux computer. Web UI with live data,
every decoded parameter, energy storage, a **pack imbalance guard** and **battery health /
degradation** tracking over time. The web UI itself is in Swedish.

> ⚠️ **Not affiliated with Begode.** Use at your own risk. The program is **read-only by
> default**. Wheel control exists but is **off at every start**, can only be switched on from
> the computer running the server (never from a phone or another LAN device), needs a two-step
> confirmation and switches itself off after 10 minutes. Dangerous commands (calibration, gear
> ratio, brake cut-off, tiltback etc.) are blocked in code and can never be sent.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m falcon.server          # http://<computer-ip>:8096 (LAN)
```

Without configuration the program connects to the first wheel advertising a name starting with
`GotWay`/`Begode` and then locks to it. Set `FALCON_ADDR=AA:BB:…` to pick a specific wheel.
The wheel accepts only one BLE connection at a time – close the wheel's phone app first.

| Variable | Meaning |
|---|---|
| `FALCON_ADDR` | wheel Bluetooth address (optional) |
| `FALCON_PORT` | web port, default 8096 |
| `FALCON_DB` | SQLite file, default `~/.local/share/begode-falcon/history.db` |
| `FALCON_NOMINAL_WH` | nominal capacity for comparison, default 1800 (Falcon Pro) |

## Web UI tabs

| Tab | Content |
|---|---|
| Live | speed, voltage, trip, odometer, wheel alerts, settings reported by the wheel |
| Energilager | imbalance guard, battery, cells (age + resistance per cell), BMS 1/2, bus (live via SSE, every 0.3 s), motor/electronics |
| Batterihälsa | capacity and health, energy counters, wheel gauge vs ours, sessions, per-cell resistance, self-discharge, exposure |
| Alla parametrar | **every decoded field of every packet** with unit, status (✅ ⚠️ ❓), raw bytes, update period |
| Styrning | wheel control – off by default, local computer only, write twice → read once, command log with before/after field diff |
| Logg | events and connection stability (connects, drops with reason, share of time with data) |

The header shows the app version and the signal strength (RSSI) from the last scan. Live RSSI of
an open connection needs raw HCI access (root) and is therefore not shown.

## Layout

| File | Content |
|---|---|
| `falcon/protocol.py` | frames (`55 AA … type sub 5A5A5A5A`), decoding of packets 0/1/2/3/4/7, alert bits, field metadata, command table, block list |
| `falcon/ble.py` | BLE link: bounded connect, data watchdog, BlueZ recovery, auto-reconnect; one guarded write path used only by control |
| `falcon/fastpath.py` | bus sag on every packet; per-cell internal resistance by time-stamped regression |
| `falcon/energy.py` | energy view: cells, BMS 1/2, SoC guess, sag per amp, bus health index |
| `falcon/guard.py` | **imbalance guard**: BMS 1 vs BMS 2 shunt ratio + step alarm, BMS sum vs controller battery current, pack drop-out, string A↔B, cell/bank, temperature, endless balancing, wheel alerts |
| `falcon/health.py` | **battery health**: Wh/Ah in/out, equivalent cycles, capacity = Ah ÷ ΔSoC (mostly from charges), health vs first measurement + km to 80 %, rides out of range, Wh/km, per-cell resistance, per-cell self-discharge, time at high SoC/temperature, charger drop-outs |
| `falcon/control.py` | control gate (off by default, local-only, two-step, auto-off), command whitelist, motion block, field diff |
| `falcon/verify.py` | write twice → read once → verified / mismatch / unverifiable / unknown |
| `falcon/store.py` | SQLite history |
| `falcon/server.py` | FastAPI: `/api/state`, `/api/all`, `/api/version`, `/api/control*`, `/api/energy`, `/api/bus`, `/api/stream` (SSE), `/api/cells_fast`, `/api/guard`, `/api/health`, `/api/history`, `/api/log` |
| `web/index.html` | single-page UI |
| `tests/` | replays a real BLE capture (`tests/fixtures/`) + simulated faults and fake BLE clients |

## Protocol summary

The wheel sends a fixed round **every 0.3 s**: packet 0 + 4 + 7 + one BMS row + one cell bank
(8 cells). Each cell is therefore refreshed every 1.8 s and each BMS row every 1.2 s – this is set
by the wheel's firmware and cannot be changed over BLE.

| Packet | Content |
|---|---|
| 0 | voltage (16S-scaled), speed, trip (m), **phase current** /100 A, board temperature |
| 1 | smart BMS: rows 0–1 = BMS 1 (string A), rows 2–3 = BMS 2 (string B); voltage /10, current /10 A, 4 temperatures, half-pack voltage, status bits |
| 2 / 3 | cell voltages in mV, string A / B, 3 banks × 8 |
| 4 | odometer (m), settings bits, auto power-off, LED mode, **alert bits** |
| 7 | **battery current** = −value/100 A (negative = charging), motor temperature, PWM |

Fields that are still uncertain are marked ❓ in the UI (`protocol.UNCERTAIN`).

**Thanks to** [WheelLog](https://github.com/Wheellog/Wheellog.Android) and
[jphein/begode](https://github.com/jphein/begode) – their documented interpretation of the
Begode protocol was used to correct the field meanings. No code was copied from them.

## Estimated values

- **State of charge**: mean cell voltage → typical Li-ion curve. Valid only at rest.
- **Sag per amp**: slope of bus voltage vs battery current under load – battery + wiring + bus
  together. **The protocol carries no capacitor data.**
- **Bus health index**: 100 = same sag as the first measurements; lower = worse.

## Imbalance guard – background

A real pack failure motivated the guard: the current shunt consisted of 4 parallel resistors
and one was never soldered. The remaining 3 carried 133 % current (178 % heat) until they burned
off. Because the BMS assumes 4 resistors, a pack with one missing reads ≈33 % too much current –
the guard's first check. Current checks run only under load (≥5 A), voltage checks only at rest
(<1 A).

## Battery health – getting good measurements

- Charge within BLE range of the computer with the wheel **switched on**.
- Leave the wheel on **5 min before and 5 min after** charging (rest voltage → capacity).
- Rides out of range are reconstructed from rest voltage and odometer before/after.

## Wheel control

Open the *Styrning* tab **on the computer running the server**, click *Slå på styrning …* and
confirm within 30 s. Each command is sent twice and the wheel's next report is read back; the
log shows the result and which decoded fields changed. Use it only with the wheel standing still
(it refuses otherwise) – ideally lifted.

## Blocked (never sent)

Calibration `cy`, gear ratio `< = >`, brake cut-off/power bridge `e x`, run-mode toggle `+-`,
`Wl WC WU WX WR`, unit switch `m g`, firmware update.

## Status

Works against a real wheel at rest. Control is implemented and tested against a fake wheel; it
has not yet been used on the real wheel. Current scales and a few fields will be confirmed with data
under load and while charging. No charts yet – values are shown as numbers and tables.
See `CHANGELOG.md`.

## License

MIT – see `LICENSE`.
