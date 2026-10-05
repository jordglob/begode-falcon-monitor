# Logger på hjulet: appen på ett litet Linux-kort

Målet: slippa ta med en bärbar dator. Ett litet kort sitter på hjulet, kör samma app och har
radion några centimeter från hjulets Bluetooth-modul. Telefonen är skärm och ger kortet nät
och – om man vill – position.

**Status:** förberett och provat på en vanlig Linux-dator i en ren Python-miljö. Inte provat på
ett riktigt kort ännu. Bluetooth-stabiliteten på andra kort än Raspberry Pi är okänd.

## Vad som behövs

| Del | Kommentar |
|---|---|
| Kort | Raspberry Pi Zero 2 W, Radxa Zero 3W eller Orange Pi Zero 2W (64-bitars, minst 512 MB) |
| Minneskort | 16 GB eller mer, gärna en "endurance"-modell |
| Ström | 5 V: liten powerbank, eller hjulets USB-uttag om det finns |
| Hölje | Vattentätt, fäst så att kortet inte skakar loss |
| GPS | Telefonens (inget att köpa) eller en USB-GPS |
| Telefon | Internetdelning påslagen; den visar också sidan |

## Installation

1. **Skriv systemet till minneskortet** (Raspberry Pi OS Lite 64-bit, eller Debian/Armbian för
   de andra korten). Ställ in användare, värdnamn och hemmets WiFi redan i skrivprogrammet, och
   slå på SSH.
2. **Logga in** över SSH och hämta appen:
   ```
   sudo apt-get install -y git
   git clone https://github.com/jordglob/begode-falcon-monitor.git
   cd begode-falcon-monitor
   bash deploy/install.sh
   ```
   Skriptet installerar Python-miljön och Bluetooth, lägger in appen som en tjänst som startar
   vid uppstart (även utan inloggning) och kontrollerar att den svarar. Det går att köra igen.
3. **Lägg till telefonens internetdelning** som ett andra nät, så att kortet hittar telefonen
   ute:
   ```
   sudo nmcli device wifi connect "TELEFONENS NAMN" password "LÖSENORDET"
   sudo nmcli connection modify "TELEFONENS NAMN" connection.autoconnect yes connection.autoconnect-priority 5
   ```
   Hemmanätet bör ha högre prioritet (t.ex. 10) så att kortet väljer det hemma.

## Användning

- **Slå på strömmen till kortet.** Efter ungefär en halv minut söker appen efter hjulet.
- **Telefonen:** slå på Internetdelning och öppna `https://falcon.local:8443`. Det är samma
  adress hemma och ute: appen annonserar namnet `falcon.local` för den adress kortet har just
  nu. Godkänn certifikatvarningen första gången (certifikatet är kortets eget).
- **Position från telefonen:** gå till Position & turer och tryck "Använd den här enhetens
  GPS". Sidan måste vara öppen och skärmen tänd för att positionen ska skickas.
- Vanlig http utan telefonens GPS finns på `http://falcon.local:8096`.
- **USB-GPS:** hittas automatiskt om den syns under `/dev/serial/by-id`. Annars:
  `FALCON_GPS_DEV=/dev/ttyACM0` (och `FALCON_GPS_BAUD=9600` för en serieansluten modul) i
  tjänstefilen.
- **Hemma** når du kortet på hemmanätet med samma adress och ser turerna där.

## Inställningar (miljövariabler i tjänstefilen)

| Variabel | Betydelse | Standard |
|---|---|---|
| `FALCON_MDNS_NAME` | Namnet som annonseras (`NAMN.local`); tomt = av | falcon |
| `FALCON_PORT` | Sidans port | 8096 |
| `FALCON_HTTPS_PORT` | Https-dörren (krävs för telefonens GPS); 0 = av | 8443 |
| `FALCON_GPS` | `0` stänger av kortets egen GPS-läsning | auto |
| `FALCON_GPS_DEV`, `FALCON_GPS_BAUD` | Serie- eller USB-GPS | hittas automatiskt, 9600 |
| `FALCON_ADDR` | Lås till ett visst hjul (Bluetooth-adress) | första hjulet som hittas |
| `FALCON_INJECT_TOKEN` | Kräv lösenord för data som skickas in utifrån | ingen |

## Att tänka på

- **Strömavbrott.** Databasen tål att strömmen bryts, men minneskort kan ta skada av det. Stäng
  helst av kortet ordnat (`sudo poweroff`), eller använd ett kort avsett för ständig skrivning.
- **En anslutning i taget.** Hjulet tar bara en Bluetooth-anslutning. Är DarknessBot eller
  Begode-appen ansluten i telefonen kommer kortet inte åt hjulet.
- **Två appar, två databaser.** Kortet och en dator hemma delar inte historik automatiskt.
- **Höjdmodell och tester** installeras inte av skriptet. Vill du ha dem:
  `.venv/bin/pip install -r requirements.txt`.

## Data utifrån (för den som vill bygga vidare)

- `POST /api/gps/push` – position: `{"lat": …, "lon": …, "speed_kmh": …, "alt_m": …, "accuracy_m": …}`.
- `POST /api/inject` – hjulets råa Bluetooth-data från något annat som håller anslutningen:
  `{"data": ["<hex>", …], "source": "namn"}`. Appens egen Bluetooth står då av.
