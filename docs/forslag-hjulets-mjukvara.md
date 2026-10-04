# Förslag: ändringar i hjulets mjukvara (moderkort och BMS)

**Status:** underlag och förslag. Inget av detta är byggt, och det kan inte byggas i den här
appen – det gäller mjukvaran i själva hjulet (Begode Falcon Pro, firmware `GW1634001U`).
Dokumentet är tänkt som diskussionsunderlag, till exempel mot tillverkaren.

**Vad det bygger på:** enbart det hjulet skickar över Bluetooth, inspelat av den här appen
(endast läsning). Hjulets firmware har inte lästs eller analyserats. Orsakerna nedan är därför
slutsatser från mätdata, inte kunskap om hur koden ser ut.

**Underlag:** 69 inspelade blackbox-filer från 2026-10-03 (ca 15 minuter rådata, 545 kompletta
BMS-cykler, 2660 cellramar), mest från en kvällstur på 20 km, samt en inspelad laddning
2026-10-04 (25 minuter, hjulet påslaget, laddarens display som referens).

---

## Sammanfattning

| # | Förslag | Varför | Styrka i underlaget |
|---|---|---|---|
| 1 | BMS ska rapportera verklig, signerad ström per paket | Fältet är för långsamt för att följa lasten | Stark |
| 2 | Alla celler ska mätas i samma ögonblick | Halvpaketen mäts var för sig och skiljer upp till 189 mV | Stark |
| 3 | Tidsstämpel eller löpnummer i varje paket | Går inte att para ihop ström och cellspänning | Stark |
| 4 | Hjulet ska självt jämföra de två paketens ström | Ett avbränt shuntmotstånd syns inte utifrån | Medel (följer av 1) |
| 5 | Snabbare eller valbar takt för celldata | Celler kommer var 1,8 s | Medel |
| 6 | BMS-summerns tillstånd ska synas över Bluetooth | Går inte att se varför hjulet piper | Medel |
| 7 | Dokumentera larmbitar och okända fält | Två tolkningar i omlopp | Medel |
| 8 | Dokumentera den automatiska avstängningen | Nedräkning startar utan känd orsak | Svag (en händelse) |
| 9 | Stabilare Bluetooth-länk | Många avbrott under färd | Svag (mottagaren kan vara orsaken) |
| 10 | Dokumentera och jämför de två strömvärdena i varje BMS | Raderna är tidvis oense, i båda paketen | Medel (en laddning) |

---

## 1. BMS-strömmen: rapportera verklig ström

**Iakttagelse.** Varje BMS skickar ett fält som tolkas som paketets ström (paket 1, fjärde
ordet, tiondels ampere). Jämfört med moderkortets strömmätning (paket 7) under samma tid:

- Korrelationen är 0,01–0,09. Med 10 s fördröjning som mest 0,20.
- När moderkortet drog 15–25 A visade alla fyra BMS-rader 0,0–0,2 A.
- Fältet är aldrig negativt, trots att moderkortets ström är negativ (återvinning) i 27 % av ramarna.
- De två raderna från samma BMS visar olika värden (t.ex. 4,7 A och 1,7 A samtidigt).
- Värdet ligger fruset i 2–7 sekunder åt gången.

**Under laddning** ser det annorlunda ut: där är strömmen stadig, och fältet visar rimliga och
stabila värden som uppdateras ungefär var sjunde sekund. Fältet verkar alltså vara en riktig men
långsam mätning – för långsam för körning, där strömmen växlar på delar av en sekund.

**Följd.** Under körning går det inte att jämföra de två paketens ström, och därmed inte att
upptäcka en shunt som mäter fel eller ett paket som inte bär ström. En app som försöker får falsklarm
(den här appen gav över 100 sådana på en tur innan kontrollen stängdes av).

**Förslag.**
- Rapportera paketets verkliga ström, med tecken (plus = urladdning, minus = laddning/återvinning).
- Samma värde på båda raderna från samma BMS, uppdaterat i varje ram.
- Om fältet är ett medelvärde: dokumentera över hur lång tid, och skicka dessutom ett ögonblicksvärde.

## 2. Cellspänningar: mät alla celler samtidigt

**Iakttagelse.** Varje BMS verkar mäta sina 24 celler som två halvpaket om 12 (cell 1–12 och
13–24), vart och ett vid sin egen tidpunkt. Skarven mellan cell 12 och 13 går mitt i den
mellersta cellramen (cell 9–16):

- Ramar med över 60 mV spridning: i 121 av 123 ligger steget exakt mellan cell 12 och 13.
- Steget över skarven: median 2 mV, 95:e percentilen 54 mV, högst 189 mV.
- Cell 9–12 mot cell 1–8 (samma halvpaket, olika ramar): median 1,4 mV skillnad.
- Cell 9–12 mot cell 17–24 (olika halvpaket): median 14 mV, 90:e percentilen 62 mV.
- Exempel: `3863 3867 3855 3824 | 4043 4042 4039 4042` mV i en och samma ram, vid ca 5 A.
- Ungefär varannan cellram är en exakt upprepning av den förra – BMS hade inte mätt på nytt.

**Följd.** "Cellspridning under last" över hela batteriet går inte att beräkna. En spridning på
200 mV ser ut som en döende cell men är bara två mätningar vid olika belastning. Den verkliga
spridningen mellan samtidigt mätta celler var liten.

**Förslag.**
- Frys alla 48 cellspänningar i samma ögonblick och skicka sedan ut dem ram för ram.
- Om det inte går: skicka med strömmen i mätögonblicket för varje halvpaket, så att mottagaren kan kompensera.
- Markera i ramen om värdena är en ny mätning eller en upprepning.

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

## 10. Jämför de två strömvärdena i varje BMS

**Iakttagelse.** Varje BMS skickar sin ström på två rader. Under en laddning, med laddarens
display på 7 A, var raderna tidvis oense – i båda paketen:

| Tid in i laddningen | BMS 1 rad 1 / rad 2 | BMS 2 rad 1 / rad 2 |
|---|---|---|
| 0–25 min (stadig ström) | 3,6 / 3,6 A | 5,6 / 3,85 A |
| ca 43 min | 5,8 / 2,9 A | – |
| ca 60 min | 4,9 / 1,9 A | – |
| ca 65 min (avtagande ström) | 1,5 / 1,4 A | 2,1–2,4 / 1,4–1,6 A |

- Under de första 25 minuterna var BMS 1:s rader överens, medan BMS 2:s rad 1 låg 41–52 % över
  rad 2 med konstant kvot (rad 1 ≈ 1,44 × rad 2).
- Senare under samma laddning visade BMS 1:s rad 1 ungefär dubbelt så mycket som rad 2 i
  cirka 17 minuter. Den delen är bara känd från appens larmlogg, inte från en inspelning.
- Det är alltid rad 1 som visar mer, aldrig rad 2.
- De värden som är överens ger cirka 7,4 A totalt under den stadiga delen, vilket stämmer med
  laddaren. Med rad 1 från varje BMS blir summan 9,2 A.
- Moderkortet ser inte laddströmmen alls (det visar hjulets egen förbrukning), så BMS är den
  enda källan under laddning.

**Följd.** Det går inte att säga vilket paket som laddas med hur mycket, och inte heller om
något är fel. Eftersom avvikelsen dyker upp i båda paketen är det troligare att raderna inte
visar samma mätning, eller att rapporteringen ändras under laddningens gång, än att en enskild
strömmätning är trasig – men underlaget räcker inte för att avgöra det. Hjulet självt reagerar
inte på skillnaden.

**Förslag.**
- Dokumentera vad de två raderna visar: samma mätning två gånger, eller två olika.
- Om de ska vara lika: låt BMS jämföra dem och larma när de skiljer mer än en rimlig tolerans.

---

## Vad underlaget inte visar

- Underlaget är snett: blackbox-filer sparas kring larm, och larmen kom mest vid låg last.
  Över 15 A finns bara ett tiotal mätpunkter för BMS-strömmen.
- Bara en laddning är inspelad, och bara dess första 25 minuter i detalj; referensen är
  laddarens display (hela ampere).
- En tidigare version av det här dokumentet pekade ut BMS 2 som avvikande. Senare data från
  samma laddning visade samma sak i BMS 1.
- Moderkortets strömskala är inte bekräftad mot en oberoende mätning.
- Allt gäller ett enda hjul med en firmwareversion.
- Att halvpaketen mäts vid olika tidpunkter är den enklaste förklaringen till mönstret,
  men den är inte bekräftad mot hjulets kod.
- En tidigare version av det här dokumentet beskrev skarven som "fyra och fyra celler i en
  ram". Fler data visade att enheten är halvpaket om 12 celler.
