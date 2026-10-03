# Begode Falcon Pro BLE protocol – research notes

Measured on a Falcon Pro (wheel firmware GW1634001, BLE module "Link" V1.9 2024-04-24),
cross-checked with WheelLog (GotwayAdapter) and the Home Assistant begode integration.
Status: ✅ plausible/confirmed · ⚠️ scale from one source, not confirmed under load · ❓ unclear.

## Transport
- Service `0000ffe0-…`, characteristic `0000ffe1-…` (notify + write without response), MTU 23.
- Frame: `55 AA` + 16 bytes (8 × int16 big-endian) + type + sub + `5A 5A 5A 5A` (24 bytes).
- Fixed round **every 0.3 s**: packet 0 + 4 + 7 + one BMS row (1.2 s cycle) + one cell bank
  (1.8 s cycle). Set by firmware; no known command changes it.
- One BLE client at a time. No pairing, no authentication, no command acknowledgement.

## Packet 0 – live data (0.3 s)
| Word | Field | Scale | Status |
|---|---|---|---|
| 1 | voltage, 16S-scaled | /100 V, ×1.5 for 24S | ✅ |
| 2 | speed | /100 m/s ×3.6 | ⚠️ |
| 3 | (range per official app) | raw | ❓ |
| 4 | trip | m | ✅ |
| 5 | **phase current** | /100 A | ⚠️ |
| 6 | board temperature (MPU6050) | /340 + 36.53 °C | ✅ |
| 7 | PWM on custom firmware only | raw | ❓ |
| 8 | flags | raw | ❓ |

## Packet 1 – smart BMS (sub 0–1 = BMS 1 / string A, sub 2–3 = BMS 2 / string B; 1.2 s per row)
| Word | Field | Scale | Status |
|---|---|---|---|
| 1 | PWM limit or battery alarm | % | ❓ |
| 3 | pack voltage | /10 V | ✅ |
| 4 | BMS current (per BMS, repeated on both rows) | /10 A | ⚠️ |
| 5, 6 | temperatures (1/2 on even rows, 3/4 on odd rows) | °C | ✅ |
| 7 | half-pack voltage | /10 V | ✅ |
| 8 | status bits: activity (14–15), temp state (12–13), volt state (10–11), cell balance (7), protection (4–6), group balance (3), MOS (0–1) | bits | ✅ (MOS ❓) |

## Packets 2 / 3 – cell voltages (string A / B), sub = bank 0–2, 8 cells each, mV — ✅ (2 × 24S)

## Packet 4 – odometer, settings, alerts (0.3 s)
| Word/byte | Field | Status |
|---|---|---|
| words 1–2 | odometer, m | ✅ |
| word 3 | settings bits: pedal mode (13–14), speed alarms (10–11), roll angle (7–8), miles (0) | ⚠️ |
| word 4 | **countdown to auto power-off**, s (7200 → 0 → wheel switches off) | ✅ seen live |
| word 5 | tiltback speed km/h (WheelLog) or pedal sensitivity (official app) – 51 | ❓ mapping pending |
| byte 13 | LED / ambient mode | ✅ (verified with `WM`) |
| byte 14 | alert bits: high power, speed 2, speed 1, low voltage, over-voltage, over-temperature, hall sensor error, transport mode | ✅ |
| byte 15 | light mode (2 bits) | ⚠️ |
| word 8 | vehicle id | ❓ |

## Packet 7 – motor (0.3 s)
| Word | Field | Scale | Status |
|---|---|---|---|
| 1 | **battery current** (negative = charging) | −raw/100 A | ⚠️ |
| 3 | motor temperature | °C | ✅ |
| 4 | PWM | % | ⚠️ |

## Commands (ASCII, no checksum, no ack)
| Command | Meaning | In this app |
|---|---|---|
| `V`, `N` | read firmware / name (text reply between frames) | ✅ (`V` → `GW1634001`) |
| `b` | beep | ✅ |
| `WM0`–`WM9` | LED / ambient mode | ✅ verified |
| `h` `f` `s` | pedal mode hard / medium / soft | ⚠️ unverifiable yet |
| `Q` `E` `T` | headlight on / off / flash | ⚠️ |
| `WB1`–`WB9` | beeper volume | ⚠️ |
| `WP50`–`WP90` | battery alarm (5 % steps) | ⚠️ |
| `WY03`–`WY90` | tiltback speed (3 km/h steps) | ❌ blocked until mapped |
| `cy`, `< = >`, `e x`, `+-`, `Wl WC WU WX WR`, `m g` | calibration, gear ratio, brake/power bridge, mode toggle, current limit etc., units | 🔒 never sent |

## Firmware catalog
The official app reads `https://one-api.begode.com/api/version/one/version?firmwareTypeCode=GW16210`
(FALCON). Files are served unencrypted from `/imgs/temp/`. Falcon Pro mainboard GW1634001 =
`ETMAXPWM_1775115198177.bin` (2026-03-20); Falcon Pro BMS = `BMS_1779937578647.bin` (2026-05-28).
Firmware files are Begode's and are not part of this repository.
