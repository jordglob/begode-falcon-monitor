# Förslag: ändringar i hjulets mjukvara (moderkort och BMS)

**Status:** underlag och förslag. Inget av detta är byggt, och det kan inte byggas i den här
appen – det gäller mjukvaran i själva hjulet (Begode Falcon Pro, firmware `GW1634001U`).
Dokumentet är tänkt som diskussionsunderlag, till exempel mot tillverkaren.

**Vad det bygger på:** enbart det hjulet skickar över Bluetooth, inspelat av den här appen
(endast läsning). Hjulets firmware har inte lästs eller analyserats. Orsakerna nedan är därför
slutsatser från mätdata, inte kunskap om hur koden ser ut.

**Underlag:** 69 inspelade blackbox-filer från 2026-10-03 (ca 15 minuter rådata, 545 kompletta
BMS-cykler, 2048 cellramar), mest från en kvällstur på 20 km.

---

## Sammanfattning

| # | Förslag | Varför | Styrka i underlaget |
|---|---|---|---|
| 1 | BMS ska rapportera verklig, signerad ström per paket | Fältet följer inte lasten alls | Stark |
| 2 | Alla celler i en ram ska mätas i samma ögonblick | Halvorna i en ram skiljer upp till 189 mV | Stark |
| 3 | Tidsstämpel eller löpnummer i varje paket | Går inte att para ihop ström och cellspänning | Stark |
| 4 | Hjulet ska självt jämföra de två paketens ström | Ett avbränt shuntmotstånd syns inte utifrån | Medel (följer av 1) |
| 5 | Snabbare eller valbar takt för celldata | Celler kommer var 1,8 s | Medel |
| 6 | BMS-summerns tillstånd ska synas över Bluetooth | Går inte att se varför hjulet piper | Medel |
| 7 | Dokumentera larmbitar och okända fält | Två tolkningar i omlopp | Medel |
| 8 | Dokumentera den automatiska avstängningen | Nedräkning startar utan känd orsak | Svag (en händelse) |
| 9 | Stabilare Bluetooth-länk | Många avbrott under färd | Svag (mottagaren kan vara orsaken) |

---

## 1. BMS-strömmen: rapportera verklig ström

**Iakttagelse.** Varje BMS skickar ett fält som tolkas som paketets ström (paket 1, fjärde
ordet, tiondels ampere). Jämfört med moderkortets strömmätning (paket 7) under samma tid:

- Korrelationen är 0,01–0,09. Med 10 s fördröjning som mest 0,20.
- När moderkortet drog 15–25 A visade alla fyra BMS-rader 0,0–0,2 A.
- Fältet är aldrig negativt, trots att moderkortets ström är negativ (återvinning) i 27 % av ramarna.
- De två raderna från samma BMS visar olika värden (t.ex. 4,7 A och 1,7 A samtidigt).
- Värdet ligger fruset i 2–7 sekunder åt gången.

**Följd.** Det går inte att jämföra de två paketens ström, och därmed inte att upptäcka en
shunt som mäter fel eller ett paket som inte bär ström. En app som försöker får falsklarm
(den här appen gav över 100 sådana på en tur innan kontrollen stängdes av).

**Förslag.**
- Rapportera paketets verkliga ström, med tecken (plus = urladdning, minus = laddning/återvinning).
- Samma värde på båda raderna från samma BMS, uppdaterat i varje ram.
- Om fältet i själva verket är något annat (t.ex. ett medelvärde eller en laddström): dokumentera vad.

## 2. Cellspänningar: mät alla celler i en ram samtidigt

**Iakttagelse.** En cellram innehåller åtta celler. De fyra första och de fyra sista verkar
mätas vid olika tidpunkter:

- Skillnaden mellan halvornas medelvärde: median 2 mV, 95:e percentilen 54 mV, högst 189 mV.
- Inom en halva (samma ögonblick): median 5 mV, högst 49 mV.
- Exempel: `3863 3867 3855 3824 | 4043 4042 4039 4042` mV i en och samma ram, vid ca 5 A.

Olika banker om åtta celler kommer dessutom i olika ramar, 0,3 s isär eller mer.

**Följd.** "Cellspridning under last" går inte att beräkna. En spridning på 200 mV ser ut
som en döende cell men är bara två mätningar vid olika belastning. Den verkliga spridningen
mellan samtidigt mätta celler var liten – alla 48 celler låg inom −4,9 till +1,6 mV från sin
grupp vid minst 10 A.

**Förslag.**
- Frys alla 48 cellspänningar i samma ögonblick och skicka sedan ut dem ram för ram.
- Om det inte går: skicka med strömmen i mätögonblicket för varje grupp, så att mottagaren kan kompensera.

## 3. Tidsstämpel eller löpnummer i varje paket

**Iakttagelse.** Paketen saknar tidsuppgift. Mottagaren vet bara när paketet kom fram över
Bluetooth, inte när värdet mättes. Punkt 1 och 2 visar att mätögonblick och sändögonblick kan
skilja flera sekunder.

**Förslag.** Ett löpnummer eller en millisekundräknare per mätning (inte per sändning). Det
räcker med 16 bitar. Då kan mottagaren para ihop ström och spänning rätt och se tappade paket.

## 4. Låt hjulet självt vakta shuntarna

**Bakgrund.** Det här hjulet har tidigare haft ett batterifel där ett av fyra parallella
shuntmotstånd aldrig var lött. De tre kvarvarande bar 133 % ström var (178 % värme) tills de
brann av ett efter ett. Eftersom BMS räknar med fyra motstånd visar det då för hög ström.

**Följd av punkt 1.** Felet går inte att upptäcka utifrån med dagens data.

**Förslag.** Hjulet har redan det som behövs: moderkortets ström och två BMS-strömmar. Låt
firmware jämföra dem och larma när

- ett paket visar klart mer eller mindre än det andra under last, eller
- summan av paketens ström avviker från moderkortets.

Rapportera resultatet som en egen larmbit.

## 5. Snabbare eller valbar takt för celldata

**Iakttagelse.** Hjulet skickar en fast runda var 0,3 s: paket 0, 4 och 7 samt en BMS-rad och
en cellbank. Varje cellbank kommer alltså var 1,8 s och varje BMS-rad var 1,2 s. Inget känt
kommando ändrar takten.

**Följd.** Spänningsdippar på under en sekund – de som avgör om en cell når sin undre gräns –
syns inte per cell.

**Förslag.** Ett läge där celldata skickas tätare (hela batteriet minst en gång per sekund),
eller att lägsta cellspänning sedan förra ramen skickas med.

## 6. BMS-summerns tillstånd över Bluetooth

**Iakttagelse.** Hjulet har två summrar: moderkortets och BMS:ens. BMS-summern (cell under
ca 2,8 V, övertemperatur, BMS-fel) syns inte i data, bara indirekt via cellspänningar och
skyddsstatus. Se [beeps.md](beeps.md).

**Förslag.** En bit per summer som säger att den ljuder, och en kod för orsaken.

## 7. Dokumentera larmbitar och okända fält

**Iakttagelse.**
- Larmbyten i paket 4 har två tolkningar i omlopp (tillverkarens app och WheelLog) som säger
  olika saker för samma bit, t.ex. "MOS-transistor bränd" mot "fartlarm 2".
- Ett fält i BMS-raden är antingen en effektgräns eller en varningsnivå.
- Ett räckviddstal i paket 0 har okänd skala.
- Pedallägets bitar är inte säkert tolkade.

**Förslag.** En publicerad beskrivning av paketens fält och larmbitar för aktuell firmware.

## 8. Dokumentera den automatiska avstängningen

**Iakttagelse.** Ett fält i paket 4 är en nedräkning (7200 s → 0 → hjulet stänger av). Vid ett
tillfälle startade nedräkningen utan att orsaken gick att se i data.

**Förslag.** Dokumentera vad som startar och nollställer nedräkningen.

## 9. Stabilare Bluetooth-länk

**Iakttagelse.** Under kvällsturen: 36 anslutningar och 34 avbrott. Bara 14 % av turens
GPS-punkter fick data från hjulet.

**Osäkerhet.** Mottagaren var en bärbar dator i en väska, med en adapter som samtidigt
användes av annan utrustning. Avbrotten kan mycket väl bero på mottagaren. Punkten tas med
för att luckorna begränsar hur mycket av underlaget ovan som finns.

**Förslag.** Inget konkret innan det är provat med en bättre mottagare.

---

## Vad underlaget inte visar

- Underlaget är snett: blackbox-filer sparas kring larm, och larmen kom mest vid låg last.
  Över 15 A finns bara ett tiotal mätpunkter för BMS-strömmen.
- Laddning är inte inspelad. BMS-strömmen kan vara riktig under laddning.
- Allt gäller ett enda hjul med en firmwareversion.
- Att cellramens halvor mäts vid olika tidpunkter är den enklaste förklaringen till mönstret,
  men den är inte bekräftad mot hjulets kod.
