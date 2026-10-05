# Överlämning: säkerhetstillägg i hjulets egen mjukvara

Underlag till den som ska arbeta med mjukvaran i en Begode Falcon Pro (firmware `GW1634001U`).
Skrivet av assistenten bakom den här övervakningsappen, på ägarens uppdrag.

**Läs det här först.** Allt nedan bygger på vad hjulet skickar över Bluetooth, inspelat under
två dagar på ett enda hjul. Hjulets firmware har inte lästs eller analyserats av mig. Jag vet
inte vilken processor som sitter på moderkortet eller i BMS:erna, hur uppdatering går till,
eller om en misslyckad uppdatering går att backa. Det måste den som gör arbetet ta reda på.

---

## 1. Uppdraget och dess gräns

Ägaren vill att hjulet självt ska göra det som övervakningsappen i dag gör utifrån:
**iaktta, jämföra, bekräfta och varna.** Det är tillägg till säkerheten.

### Får inte röras
- Balansregleringen och motorstyrningen (PWM-generering, strömreglering, vinkelreglering).
- Tiltback, fartgränser och hur hjulet reagerar på dem.
- Batteriskydden i BMS: över- och underspänning, överström, temperatur, kortslutning.
- Tidsbeteendet i reglerslingan. Ett tillägg får inte kunna fördröja den.

### Vad som efterfrågas
- Fler och bättre **mätvärden ut** över Bluetooth.
- **Bekräftelse** när en inställning ändras.
- **Jämförelser och varningar** som hjulet räknar ut själv och rapporterar som larmbitar eller pip.
- **Lagring** av de sista sekunderna före ett larm.

Ett tillägg som inte går att göra utan att röra listan ovan ska inte göras. Säg då det, i
stället för att hitta en väg runt.

---

## 2. Hur hjulet pratar i dag

Fullständiga anteckningar: [research/protocol.md](research/protocol.md). Avkodningen i kod:
`falcon/protocol.py`.

- BLE-tjänst `ffe0`, karakteristik `ffe1` (notifiering och skrivning utan svar).
- Ram: `55 AA` + 16 byte (8 × int16, big-endian) + typ + sub + `5A 5A 5A 5A`.
- En fast runda **var 0,3 s**: paket 0, 4 och 7, samt en BMS-rad (varje rad var 1,2 s) och en
  cellbank (varje bank var 1,8 s). Cirka 16 ramar per sekund.
- En klient i taget. Ingen parning, ingen autentisering, **ingen kvittens på kommandon**.

| Paket | Innehåll |
|---|---|
| 0 | spänning, fart, tripp, fasström, korttemperatur |
| 1 | BMS-rad: paketspänning, ström, temperaturer, halvpaketspänning, statusbitar |
| 2, 3 | cellspänningar för sträng A respektive B, åtta celler per ram |
| 4 | mätarställning, inställningar, larmbitar, avstängningsnedräkning |
| 7 | batteriström, motortemperatur, PWM |

Två BMS (en per parallellt paket), vardera 24 celler i serie.

---

## 3. Efterfrågade tillägg

Varje punkt har: vad som önskas, varför, var motsvarande logik finns i appen, och hur man ser
att det fungerar. Ordningen är ägarens prioritet.

### A. Bekräfta inställningar

**Önskas.** När en inställning ändras ska hjulet svara med vilken inställning som ändrades och
vilket värde som nu gäller. Alla inställningar ska dessutom gå att läsa tillbaka.

**Varför.** I dag skickas kommandon utan svar. Appen måste skriva två gånger och sedan vänta på
att värdet dyker upp i paket 4, och hjulet tillämpar vissa ändringar först efter några
sekunder. En inställning (effektlarmet) går inte att läsa tillbaka alls. En ägare som tror att
tiltback-farten är sänkt men har kvar den gamla vet inte om det.

**I appen.** `falcon/verify.py` (skriv två gånger, läs en färsk ram, rapportera avvikelse),
`falcon/control.py` (vilka kommandon som används).

**Godkänt när.** Varje ändring ger ett svar inom en sekund med inställningens id och gällande
värde, och ett avvisat kommando ger ett svar som säger det.

### B. Vakta balansen mellan paketen

**Önskas.** Hjulet jämför själv sina två paket och varnar:

| Kontroll | Gräns i appen | När |
|---|---|---|
| Medelcell sträng A mot sträng B | varning 20 mV, larm 40 mV | i vila |
| Enskild cell mot sin strängs median | varning 30 mV, larm 60 mV | i vila |
| Cell mot celler mätta i samma ögonblick | varning 80 mV, larm 150 mV | under last |
| Lägsta cell | larm vid 3,2 V | under last |
| Temperaturskillnad mellan paketen | varning 8 °C, larm 15 °C | under last |
| Balansering som aldrig blir klar | varning efter 2 h | alltid |
| Strömdelning mellan paketen | se punkt C | under last |

| En grupp fryst: samma spänning och ingen ström | 8 s, medan de andra rör sig ≥0,3 V och bär ≥1 A | under körning |
| En grupps ström tyst | 10 s när de övriga visar ≥8 A; annars 3 min medel | körning och laddning |
| De fyra gruppspänningarna | varning 0,5 V, larm 1,0 V (0,5 V är Begodes eget mått) | i vila |

**Varför.** Det verkliga felet på det här hjulet (juni 2026) såg ut så här i Begodes egen app:
gruppen RF "rapporterar hela tiden samma spänning och 0 ström" medan LF, LB och RB visade
4,6–8,6 A. Gruppspänningarna låg 3,0 V isär (46,6 / 47,8 / 47,3 / 44,8 V). Alla statusrader var
gröna och hjulet larmade aldrig. Följden blev pulserande pip i en lång uppförsbacke och sedan
ett tvärt stopp över ett gupp. I den utbytta gruppen fanns brända komponenter. Gruppens
mätkort hade alltså slutat mäta, och dess celler var obevakade.

De fyra grupperna (12 celler var) heter LF, RF, LB, RB i Begodes app och kommer som BMS-rad
0–3 i den ordningen; rad 0–1 är den främre strängen, rad 2–3 den bakre.

**I appen.** `falcon/guard.py`, med mätreglerna i `falcon/sampling.py`.

**Godkänt när.** En konstruerad obalans (till exempel ett paket urkopplat på bänk, eller
simulerade värden) ger varning, och ett friskt hjul ger ingen varning under en hel tur.

### C. Mätvärden som går att lita på

Punkt B går inte att göra väl utifrån, och delvis inte inifrån heller, utan detta:

1. **Cellerna mäts i samma ögonblick.** I dag mäts varje halvpaket om 12 celler för sig. Under
   växlande last skiljer halvorna upp till 189 mV utan att någon cell är dålig, medan celler
   inom samma mätning ligger inom några millivolt. Antingen fryses alla 48 samtidigt, eller så
   skickas strömmen i mätögonblicket med.
2. **Verklig ström per paket, med tecken.** BMS-radernas strömfält följer inte lasten under
   körning (visar 0,0–0,2 A när moderkortet drar 15–25 A, aldrig negativt) men ger stadiga
   värden under laddning. De två raderna i samma BMS är tidvis oense, i båda paketen, alltid
   med rad 1 högst. Vad raderna betyder är okänt.
3. **Löpnummer eller tidsstämpel per mätning.** Ungefär varannan cellram är en upprepning av
   den förra. Mottagaren kan inte se det annat än genom att jämföra värdena.

Bakgrund och siffror: [forslag-hjulets-mjukvara.md](forslag-hjulets-mjukvara.md).

### D. Marginal och säker fart

**Önskas.** Hjulet rapporterar sin marginal (100 − PWM) och farten där 20 % återstår vid
aktuell batterispänning, och varnar tidigare när batteriet är lågt. **Som varning och
mätvärde – inte som ändrad tiltback eller ändrad reglering.**

**Varför.** Samma fart kostar mer marginal på ett lägre batteri. Uppmätt på det här hjulet:

    motorspänning ≈ 1,20 V per km/h + 0,37 V per A + 1,7 V        PWM = motorspänning / busspänning

Passningen gäller ±2 PWM-enheter över 88–98 V och förutsade 5 800 senare mätpunkter med
+0,3 ± 2,3 enheter. Vid 40–50 km/h var marginalen i snitt 41 % vid 92–96 V och 30 % vid
84–88 V. Under cirka 88 V är sambandet en beräkning.

**I appen.** `falcon/margin.py`.

**Godkänt när.** Varningen kommer vid lägre fart när batteriet är lägre, och reglerslingans
beteende är oförändrat (se avsnitt 4).

### E. Förklara larmen

**Önskas.** En kod för varför hjulet piper, inklusive BMS:ens egen summer, och dokumenterade
larmbitar.

**Varför.** Larmbyten i paket 4 har två tolkningar i omlopp som säger olika saker om samma
bit. BMS-summern syns inte alls över Bluetooth. Se [beeps.md](beeps.md).

### F. Svart låda i hjulet

**Önskas.** De sista 30 sekunderna av mätvärden sparas vid larm, skydd som löser ut eller
avstängning under fart, och går att hämta över Bluetooth efteråt.

**Varför.** Efter en avkastning finns i dag ingen uppgift om vad som hände, om inte en extern
logger råkade vara ansluten.

**I appen.** `falcon/beeps.py` gör detta utifrån (30 s före, 15 s efter).

### G. Mindre saker
- Dokumentera vad som startar och nollställer den automatiska avstängningens nedräkning (paket 4).
- Snabbare takt för celldata, eller lägsta cellvärde sedan förra ramen.

---

## 4. Hur man visar att det som håller ägaren uppe är orört

Appen kan användas som oberoende kontroll. Verktyg: `python -m tools.record_state UT.jsonl`
spelar in allt två gånger per sekund; `python -m tools.replay_blackbox` kör inspelade ramar
genom vakten.

**Före varje ändring, och efter:**

1. **Upplyft hjul.** Samma sekvens: stillastående, långsam uppvarvning, jämn fart, inbromsning.
2. **Samma korta sträcka** i låg fart, med skyddsutrustning.
3. Jämför:

| Mätetal | Värde före | Tolerans |
|---|---|---|
| Rundans längd | 0,3 s | oförändrad |
| Ramar per sekund | ca 16 | oförändrad, inga nya luckor |
| Motorspänning per km/h | 1,20 V | ±3 % |
| Motorspänning per ampere | 0,37 V | ±10 % |
| Konstant term | 1,7 V | ±1 V |
| PWM vid samma fart och spänning | enligt sambandet | ±2 enheter |
| Larmbitar vid samma händelser | som före | oförändrade |
| Befintliga pakets innehåll | som före | oförändrat, om inte ändringen avser just det |

Avviker något av de tre motortalen har ändringen påverkat motorstyrningen. Då ska den backas,
inte förklaras.

---

## 5. Arbetsregler

- **Väg tillbaka först.** Den officiella firmwarefilen för den här versionen finns sparad hos
  ägaren. Verifiera att den går att lägga tillbaka innan något annat skrivs till hjulet.
- **En ändring i taget**, kontrollerad enligt avsnitt 4 innan nästa.
- **Bänk före väg.** Upplyft hjul, sedan låg fart, sedan vanlig körning.
- **Nya paket hellre än ändrade.** Lägg tillägg i nya pakettyper så att befintliga appar inte
  misstolkar gamla fält.
- **Batteriskydden ändras aldrig**, inte ens för att "förbättra" dem.
- **Vid osäkerhet om ett tillägg rör reglerslingan: gör det inte.**

---

## 6. Vad som är osäkert i underlaget

- Ett hjul, två dagars data, en inspelad laddning.
- BMS-radernas strömfält är inte förstått.
- Moderkortets strömskala är inte kontrollerad mot ett instrument. Ett oberoende stöd finns:
  uppmätt energi per höjdmeter (0,32 Wh) ligger nära teorin (0,305 Wh).
- Under cirka 55 % laddning finns inga mätningar av marginalen.
- Gränsvärdena i avsnitt 3 B är appens startvärden, inte utprovade över tid.
- Appens egna fel under de här dagarna (falska larm av mätartefakter, en lagringsslinga som
  dog) är rättade, men visar att även övervakning kan ha fel. Samma ödmjukhet bör gälla
  tilläggen i hjulet: en falsk varning är irriterande, en uteblivet riktig är farlig.
