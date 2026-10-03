# Why is the wheel beeping? (Pip-larm tab)

## Two buzzers
| Buzzer | Triggers | Visible over BLE? |
|---|---|---|
| **Mainboard** | speed alarms (2 / 3 beeps per s), 80 % power/PWM alarm (5 beeps per s, cannot be disabled), low voltage (1–3 beeps every 2 s depending on speed), hall sensor fault (2 beeps every 0.5 s), board temperature (2 short beeps every 2 s), over-voltage (3 short beeps every 2 s), fall / extreme low voltage at power-on (1 beep per s, 5 times) | mostly: speed, PWM, voltage, temperature, alert byte 14 of packet 4 |
| **BMS (inside the battery)** | any cell below ~2.8 V (keeps beeping, ~20 mA, charge within ~48 h), over-temperature, BMS faults (e.g. broken balance lead) | **not directly** – only indirectly through cell voltages, BMS voltage/temperature state, protection state and MOS |

## Alert byte 14 (packet 4) – two interpretations
| Bit | Official Begode app | WheelLog |
|---|---|---|
| 0x01 | power fault | high power (80 % alarm) |
| 0x02 | **MOS transistor burned** | speed alarm 2 |
| 0x04 | gyro fault | speed alarm 1 |
| 0x08 | low voltage | low voltage |
| 0x10 | over-voltage | over-voltage |
| 0x20 | over-temperature | over-temperature |
| 0x40 | hall sensor fault | hall sensor fault |
| 0x80 | locked | transport mode |

The official app is newer and written for these wheels, so its meaning is shown first and
treated as an alarm.

## What the tab does
- Logs every change at packet rate (0.3 s): all eight alert bits, per BMS: protection,
  voltage state, temperature state, MOS; lowest cell band (< 3.3 / 3.0 / 2.8 V);
  a pack carrying no current under load; PWM zone (≥ 70 / ≥ 80 %); raw flags of packet 0.
- **Black box**: a rolling buffer of every raw frame; on any alarm-level change, or when the
  rider presses **"Jag hör pip nu!"** (works from the phone too), 30 s before and 15 s after
  are written to `~/.local/share/begode-falcon/blackbox/` together with the events.
- Instructions for what to do when the wheel beeps, the known patterns and the bit table.

## Link to the shunt failure
When a shunt resistor burns off or a pack's BMS trips, that pack stops delivering current.
The mainboard suddenly has half the battery, the voltage sags under load and PWM shoots up –
BMS beeps and the mainboard's 80 % alarm can sound together just before the wheel cannot hold
the rider any more. The tab (and the imbalance guard) watch exactly these signals.

## Sources
- EUC Forum – GotWay alarm beep codes: https://forum.electricunicycle.org/topic/4258-gotway-alarm-beep-codes/
- Alien Rides – Begode / Extreme Bull battery buzzing: https://alienrides.com/pages/begode-extreme-bull-battery-buzzing-beeping
- EUC Alarm app (listens to beeps with the microphone): https://forum.electricunicycle.org/topic/25726-euc-alarm-app-hear-your-gotwaybegode-wheel-beeps/
- Official Begode app (fault labels for alert byte 14), WheelLog GotwayAdapter.
