# Competing EUC apps – feature comparison

Research done 2026-10-03 from public sources (app store pages, project READMEs, release notes).
Closed-source apps were **not** decompiled; their columns only reflect what they publish.
The official Begode app column also reflects its own public behaviour as observed over BLE.

**Legend:** ✅ yes · ⚠️ partial / conditional · ❌ no · ? not stated in the sources

## Overview
| | Begode (official) | DarknessBot | EUC World | WheelLog | EUC Planet | RideFlux | begode (Home Assistant) | **begode-falcon-monitor** |
|---|---|---|---|---|---|---|---|---|
| Platform | Android, iOS | iOS, Android, Watch, Mac | Android | Android | Android 10+ | Android 9+ | Home Assistant | Linux computer + browser |
| Source | closed | closed | closed | open (GPL-3) | open (MIT) | open | open (AGPL-3) | open (MIT) |
| Price | free | free + IAP, Premium $34.99 | free + Premium | free | free / tip | free | free | free |
| Brands | Begode | 18+ brands, 150+ models | all major | 5 brands | ~5 brands | Begode verified, others experimental | Begode | Begode (Falcon Pro tested) |

## Telemetry and battery
| Feature | Begode | DarknessBot | EUC World | WheelLog | EUC Planet | RideFlux | HA | **this** |
|---|---|---|---|---|---|---|---|---|
| Speed, voltage, current, temperature, PWM | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Every raw parameter with status | ❌ | ❌ | ❌ | ❌ | ❌ | ⚠️ | ❌ | ✅ |
| Smart BMS per-cell view | ✅ | ⚠️ | ✅ | ✅ | ✅ imbalance | ✅ dual BMS | ⚠️ min/max/spread | ✅ 48 cells, value age |
| Shunt / pack-to-pack imbalance guard | ❌ | ❌ | ❌ | ❌ | ⚠️ imbalance | ❌ | ❌ | ✅ |
| Battery health: capacity, degradation, cycles | ❌ | ❌ | ? | ❌ | ⚠️ charge curve + prediction | ❌ | ❌ | ✅ |
| Per-cell internal resistance | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |
| Per-cell self-discharge | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |
| Pitch / roll | ⚠️ phone sensors | ✅ where the wheel sends it | ? | ? | ? | ❌ | ❌ | ❌ (Begode does not send it) |
| Update cadence | ? | ? | ? | ? | adjustable | ? | ? | every packet (0.3 s) via SSE |

## Alarms, control, firmware
| Feature | Begode | DarknessBot | EUC World | WheelLog | EUC Planet | RideFlux | HA | **this** |
|---|---|---|---|---|---|---|---|---|
| Alarms (speed, PWM, temp, battery) | ⚠️ wheel's own | ✅ IAP | ✅ | ✅ | ✅ predictive 3 s | ✅ | via automations | ✅ incl. 3 s PWM prediction |
| Safety margin (PWM headroom) | ❌ | ❌ | ✅ | ❌ | ✅ | ✅ PWM alarm | ❌ | ✅ |
| Voice | ❌ | ? | ✅ | ✅ | ✅ + voice commands | ❌ | ❌ | ❌ |
| Lights, horn, modes | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ (off by default) |
| Tiltback / speed limit | ✅ | ✅ | ✅ | ✅ | ✅ "Legal Mode" | ✅ | ❌ | ❌ blocked until mapped |
| Verified writes (write twice, read back) | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |
| Control local-only, off by default | – | – | – | – | – | – | – | ✅ |
| Firmware update | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ (reads version + catalog) |
| Remote lock | ? | ✅ | ? | ? | ✅ | ❌ | ❌ | ❌ |
| Settings backup | ❌ | ? | ? | ❌ | ⚠️ Dropbox sync | ❌ | ⚠️ HA history | ✅ |

## Rides, maps, social
| Feature | Begode | DarknessBot | EUC World | WheelLog | EUC Planet | RideFlux | HA | **this** |
|---|---|---|---|---|---|---|---|---|
| Ride logging with GPS | ⚠️ blackbox | ✅ | ✅ | ✅ | ✅ auto | ✅ auto | ❌ | ✅ (computer must ride along) |
| Rides on a map | ? | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ coloured by speed |
| Wheel-vs-GPS speed plausibility | ❌ | ? | ? | ❌ | ? | ❌ | ❌ | ✅ spin / carried on the map |
| Export | ❌ | ✅ CSV | ? | ✅ CSV, GPX | ✅ CSV (DarknessBot-compatible) | ✅ CSV, GPX | ❌ | ✅ GPX, CSV |
| History graphs | ❌ | ✅ up to a year | ✅ | ? | ✅ | ✅ | ✅ | ✅ |
| Video with telemetry overlay | ❌ | ✅ IAP | ✅ Premium | ❌ | ✅ overlay studio | ❌ | ❌ | ❌ |
| Turn-by-turn navigation | ❌ | ❌ | ❌ | ❌ | ✅ + charging stations | ❌ | ❌ | ❌ |
| Live friends / rankings | ✅ nearby, ranking | ✅ friends, world ranking | ✅ cloud | ❌ | ✅ encrypted sharing | ❌ | ❌ | ❌ |

## Accessories and integration
| Feature | Begode | DarknessBot | EUC World | WheelLog | EUC Planet | RideFlux | HA | **this** |
|---|---|---|---|---|---|---|---|---|
| Smartwatch | ❌ | ✅ Apple Watch | ✅ Wear OS, Garmin, Galaxy | ✅ Garmin, Wear OS, Mi Band | ✅ Wear OS, Garmin, Amazfit | ❌ | ❌ | ❌ |
| HUD / AR glasses | ❌ | ❌ | ✅ ActiveLook | ❌ | ✅ HUD | ✅ Rokid | ❌ | ❌ |
| Charge limit via smart plug | ❌ | ❌ | ✅ Shelly, avg-cell limit | ❌ | ⚠️ | ❌ | ✅ automations | ✅ Shelly Gen1/Gen2 |
| Home automation / API | ❌ | ⚠️ Siri, Health | ✅ BLE server | ❌ | ❌ | ❌ | ✅ | ⚠️ REST + SSE |
| Runs on a computer / at home | ❌ | ⚠️ Mac | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |
| "Release link" for the phone app | – | – | – | – | – | – | ✅ | ✅ |
| No tracking / ads | ❌ | ? | ✅ | ✅ | ✅ | ✅ no internet permission | ✅ | ✅ local only |

## Sources
- EUC Planet – https://github.com/eried/eucplanet
- EUC World – https://euc.world/app and release notes https://euc.world/downloads/EUC%20World%20Release%20Notes.pdf
- DarknessBot – https://apps.apple.com/app/id1108403878, https://play.google.com/store/apps/details?id=com.darknessproduction.darknessbot
- WheelLog – https://github.com/Wheellog/Wheellog.Android
- RideFlux – https://github.com/zero2005x/RideFlux
- begode (Home Assistant) – https://github.com/jphein/begode
- eucguide on DarknessBot – https://eucguide.com/euc-app-tips-tricks-darknessbot-for-ios-and-the-apple-watch-companion-app/
