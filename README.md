# begode-falcon-monitor

Inofficiell övervakning av **Begode Falcon Pro** (och troligen andra Begode-hjul med smart BMS)
över Bluetooth LE, körd på en Linux-dator. Webbgränssnitt med live-data, energilager,
**obalansvakt** för batteripacken och **batterihälsa/degradering** över tid.

> ⚠️ **Inte kopplat till Begode.** Används på egen risk. Programmet **läser bara** – det skickar
> inga kommandon till hjulet. Kommandokärnan (skriv–verifiera) finns i koden och är testad mot ett
> låtsashjul, men är inte inkopplad. Farliga kommandon (kalibrering, utväxling, bromsbrytning m.fl.)
> är spärrade i koden och kan aldrig skickas.

## Starta

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m falcon.server          # http://<datorns-ip>:8096 (LAN)
```

Utan inställning ansluter programmet till första hjulet som annonserar ett namn som börjar på
`GotWay`/`Begode` och låser sig sedan till det. Ange ett specifikt hjul med `FALCON_ADDR=AA:BB:…`.
Hjulet tar bara en Bluetooth-anslutning åt gången – stäng hjulets app i mobilen först.

| Variabel | Betydelse |
|---|---|
| `FALCON_ADDR` | hjulets Bluetooth-adress (valfri) |
| `FALCON_PORT` | webbport, standard 8096 |
| `FALCON_DB` | SQLite-fil, standard `~/.local/share/begode-falcon/history.db` |
| `FALCON_NOMINAL_WH` | nominell kapacitet för jämförelse, standard 1800 (Falcon Pro) |

## Struktur

| Fil | Innehåll |
|---|---|
| `falcon/protocol.py` | ramar (`55 AA … typ sub 5A5A5A5A`), avkodning av paket 0/1/2/3/4/7, hjulets larmbitar, kommandotabell, spärrlista |
| `falcon/verify.py` | skriv två gånger → läs en gång (färskt paket efter sista skrivningen) → verifierad / avvikelse / okänt |
| `falcon/ble.py` | BLE-anslutning med automatisk återanslutning – **ingen skrivväg** |
| `falcon/fastpath.py` | kraftbussen räknas vid varje paket (0,3 s) och skickas via server-push; inre motstånd per cell med tidsstämplad regression |
| `falcon/energy.py` | energilager: celler, BMS 1/2, laddnivå (gissad), spänningsfall/A och hälsoindex (gissat) |
| `falcon/guard.py` | **obalansvakt**: BMS 1 mot BMS 2 (shuntkvot, steg-larm), BMS-summa mot moderkortets batteriström, bortfallet paket, sträng A↔B, cell/bank, temperatur, balansering som aldrig blir klar, hjulets egna larm |
| `falcon/health.py` | **batterihälsa**: Wh/Ah in/ut, ekvivalenta cykler, kapacitet = Ah ÷ ΔSoC (främst laddningar), hälsa mot första mätningen + km till 80 %, turer utom räckhåll, Wh/km, inre motstånd per cell, självurladdning per cell, tid vid hög laddnivå/värme, glapp i laddningen |
| `falcon/store.py` | SQLite-historik |
| `falcon/server.py` | FastAPI: `/api/state`, `/api/energy`, `/api/bus`, `/api/stream` (SSE), `/api/cells_fast`, `/api/guard`, `/api/health`, `/api/history`, `/api/log` |
| `web/index.html` | flikar Live / Energilager / Batterihälsa / Logg |
| `tests/` | spelar upp en riktig BLE-inspelning (`tests/fixtures/`) + simulerade fel |

## Protokollet i korthet

Hjulet skickar en fast omgång **var 0,3 s**: paket 0 + 4 + 7 + en BMS-rad + en cellbank (8 celler).
Varje cell förnyas alltså var 1,8 s och varje BMS-rad var 1,2 s – det styrs av hjulets programvara.

| Paket | Innehåll |
|---|---|
| 0 | spänning (16S-skalad), fart, tripp (m), **fasström** /100 A, kortets temperatur |
| 1 | smart BMS: rad 0–1 = BMS 1 (sträng A), rad 2–3 = BMS 2 (sträng B); spänning /10, ström /10 A, 4 temperaturer, halva paketets spänning, statusbitar |
| 2 / 3 | cellspänningar i mV, sträng A / B, 3 banker × 8 |
| 4 | mätarställning (m), inställningsbitar, auto-avstängning, LED-läge, **larmbitar** |
| 7 | **batteriström** = −värde/100 A (negativt = laddning), motortemperatur, PWM |

Fält som fortfarande är osäkra är märkta med ❓ i gränssnittet (`protocol.UNCERTAIN`).

**Tack till** [WheelLog](https://github.com/Wheellog/Wheellog.Android) och
[jphein/begode](https://github.com/jphein/begode) – deras dokumenterade tolkning av
Begode-protokollet användes för att rätta fältbetydelserna. Ingen kod är kopierad därifrån.

## Gissade värden – vad de betyder

- **Laddnivå**: medelcellspänning → typisk Li-ion-kurva. Gäller bara när hjulet står still.
- **Spänningsfall/A**: lutningen på bussens spänning mot batteriströmmen under belastning.
  Det är batteri + kablage + buss tillsammans. **Protokollet har ingen kondensatordata.**
- **Hälsoindex**: 100 = samma spänningsfall som de första mätningarna; lägre = sämre.

## Obalansvakt – bakgrund

Ett verkligt paketfel som fick vakten att byggas: shunten (strömmätmotståndet) bestod av
4 parallella motstånd där ett aldrig var lött. De 3 kvar fick 133 % ström (178 % värme) tills de
brann av. Eftersom BMS räknar med 4 motstånd visar ett paket med ett saknat motstånd ≈33 % för hög
ström – det är vaktens första kontroll. Strömkontrollerna körs bara under belastning (≥5 A),
spänningskontrollerna bara i vila (<1 A).

## Batterihälsa – så får du bra mätningar

- Ladda inom Bluetooth-räckhåll för datorn med hjulet **påslaget**.
- Låt hjulet stå påslaget **5 min före och 5 min efter** laddningen (vilospänning → kapacitet).
- Turer utanför räckhåll räknas ut från vilospänning och mätarställning före/efter.

## Spärrat (skickas aldrig)

Kalibrering `cy`, utväxling `< = >`, bromsbrytning/kraftbrygga `e x`, körlägesbyte `+-`,
`Wl WC WU WX WR`, enhetsbyte `m g`, programuppdatering.

## Status

Fungerar mot ett riktigt hjul i vila. Strömskalorna och några fält bekräftas först med data
under belastning och laddning. Diagram saknas ännu – värdena visas som siffror och tabeller.

## Licens

MIT – se `LICENSE`.
