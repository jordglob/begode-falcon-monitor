# Projektets historik – från idé till v0.15.1

Sammanfattning av utvecklingen 2–3 oktober 2026. Detaljer per version finns i
[`CHANGELOG.md`](../CHANGELOG.md); protokollet i [`research/protocol.md`](research/protocol.md).

## Utgångspunkt
- Hjul: **Begode Falcon Pro** (24S, två parallella paket med var sin smart BMS, 1,8 kWh).
- Problem: tillverkarens app känns osäker – man vet inte om en ändrad inställning gått fram.
- Önskemål: en bättre app som **skriver två gånger och läser en gång** innan något visas, och
  som kan **upptäcka obalans mellan batteripaketen** (ett tidigare paketfel: en shunt med fyra
  parallella motstånd där ett aldrig var lött – de tre kvar överhettades och brann av).

## Hur protokollet togs fram
1. **Passiv Bluetooth-inspelning** av hjulet (bara lyssna, inga kommandon).
2. **Tillverkarens Android-app** studerades för kommandon, paketfält och firmwarekatalogen.
3. **Rättelse mot öppna källor**: WheelLog och Home Assistant-integrationen *begode* visade att
   flera fältnamn i tillverkarens app inte stämde (två BMS i stället för fyra grupper,
   batteriström i paket 7, fasström i paket 0, halva paketspänningar, larmbitar).
4. **Live-observationer** rättade resten, t.ex. att paket 4 ord 4 är en **nedräkning till
   automatisk avstängning** (7200 → 0 → hjulet stängs av) och inte en inställning.
5. Hjulets firmware lästes med kommandot `V` och matchades mot tillverkarens katalog – den
   officiella filen fungerar som återställningskopia.

Viktiga fakta: hjulet skickar en **fast omgång var 0,3 s** (varje cell förnyas var 1,8 s – går
inte att ändra över Bluetooth), tar **en anslutning åt gången**, har **ingen parkoppling** och
**inga kvittenser** på kommandon.

## Versioner
| Version | Innehåll |
|---|---|
| 0.1 | Läsande instrumentpanel: avkodning, live-data, 48 celler, historik |
| 0.2 | **Obalansvakt**: shuntkvot BMS 1 mot BMS 2 (+33 % = 1 av 4 motstånd saknas), steg-larm, bortfallet paket |
| 0.3 | **Batterihälsa**: kapacitet från laddningar, cykler, inre motstånd och självurladdning per cell, exponering |
| 0.4 | Kraftbussen vid varje paket via server-push; inre motstånd per cell med regression |
| 0.5 | Fältbetydelser rättade enligt WheelLog/HA; första publicering |
| 0.6 | Stabil Bluetooth: tidsgränser, datavakt, BlueZ-återhämtning |
| 0.7 | Alla parametrar (141 fält), version, signalstyrka, engelsk README, changelog |
| 0.8 | **Styrning** – av som standard, bara från servern, tvåstegsbekräftelse, verifierade kommandon |
| 0.9 | Inställningsbackup; nedräkningen till avstängning identifierad (0.9.1) |
| 0.10–0.11 | GPS (bärbar dators mobilmodem, rättning av GPS-veckofel) och **turer på karta** |
| 0.12 | **Fartrimlighet** hjul mot GPS, säkerhetsmarginal + larm, släpp anslutningen, laddningsgräns via Shelly, GPX/CSV, grafer, konkurrentjämförelse |
| 0.13 | **Höjd och energi**: backar, energi per backe, regression, fysikjämförelse, terrängmodeller (Lantmäteriet, Copernicus), inställningsfliken, snabbare återanslutning |
| 0.14 | **Pip-larm**: varför piper det, svart låda, "Jag hör pip nu!"-knapp, tillverkarens tolkning av larmbitarna |
| 0.15 | **Maxvärden per tur** med tid och plats; rättning av tom inställningsflik (0.15.1) |

## Principer som följts
- **Säkerhet först**: appen läser som standard. Styrning måste slås på aktivt vid serverdatorn,
  stängs av själv, vägrar när hjulet rullar, och farliga kommandon (kalibrering, utväxling,
  bromsbrytning, tiltback innan den kartlagts m.fl.) är spärrade i koden.
- **Mätt, inte gissat**: osäkra fält märks ❓, gissade värden märks "gissad", och siffror som
  beror på obekräftade strömskalor märks preliminära.
- **Integritet**: inga positioner eller hjulets adress i koden; terrängdata läses offline;
  testdata använder påhittade koordinater.
- **Testat**: 108 automatiska tester, plus torrkörningar med syntetiska turer och skärmbilder.

## Viktiga lärdomar
- En öppen webbsida med server-push kan blockera nedstängning – tidsgräns behövs.
- `bluetoothctl` kan hänga för evigt om den läser från stdin – alltid begränsa och döda.
- BlueZ kan fastna i "ansluten" utan att något fungerar – att låta BlueZ glömma enheten löser det.
- Ett lyft hjul som snurrar får inte sätta turens maxfart – jämför med GPS.
- Brusig GPS-höjd ger för många höjdmeter – jämna ut och använd terrängmodell.
- Hjulets 80 %-larm (5 pip/s) och BMS-summern i batteriet är två olika saker; BMS-pipet
  rapporteras inte över Bluetooth, bara orsakerna indirekt.

## Kvar att göra
- ~~Kartlägga tiltback~~ – **klart 3 oktober**: paket 4 ord 5 = tiltback km/h, bekräftat live (51 → 48 → 51). Fartlarmens gränser återstår.
- **Bekräfta strömskalorna** under belastning och laddning.
- **Terrängmodell** för hemområdet (kräver gratis Geotorget-konto).
- **Mobilversion** (Web Bluetooth): turer utan dator, mobilens GPS och barometer, mikrofon som
  känner igen pip.
- Laddningsgränsen behöver en Shelly-kontakt för att användas på riktigt.
