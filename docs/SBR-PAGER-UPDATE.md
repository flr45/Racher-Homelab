# SBR Pager · opdatering og Cudy LT300

Opdateringen bygger videre på `feature/sms-whatsapp-gateway` og bevarer
SIM800C USB-seriel, OpenWA-session, stationsvalg, Test opt-in og datavolumener.
Den er forberedt på branchen `codex/sms-whatsapp-review-cudy`.
Den bliver ikke automatisk installeret på racherserver.

## Opdater med dit nuværende USB-modem

Kør på **racherserver**, ikke racher-pi2:

```bash
cd /opt/SBR-Pager-Gateway
git status --short
git fetch origin codex/sms-whatsapp-review-cudy
git switch codex/sms-whatsapp-review-cudy
bash scripts/update-sbr-pager.sh
```

Hvis checkout afvises på grund af lokale kodeændringer, skal de først afklares.
Undlad `git reset --hard`. Den eksisterende `.env` er ignoreret af Git og beholdes.
Hvis `.env` ikke indeholder `SMS_MODEM_DRIVER`, bruges USB automatisk.
Behold den eksisterende `SMS_MODEM_HOST_DEVICE` og baudrate.

Scriptet tager konsistente SQLite-backups, kopierer `.env` til en beskyttet
backupmappe og tagger de to gamle images, før noget udskiftes. Det bygger og
validerer de nye images og pauser kun aktive watchdog-timere under genstarten.
OpenWA-containeren og dens login genskabes ikke. Hvis genstart/health fejler,
forsøger scriptet at starte de tidligere images igen.

Databasen udvides med en separat kildekvitteringstabel. Eksisterende tabeller
ændres ikke. SQLite foreign-key-kaskader er aktiveret i Pager, så sletning
også rydder tilhørende retry-state og kvalitetsbeslutninger.

Efter opdateringen vises modem, WhatsApp og kø på Overblik. En almindelig
testalarm skal behandles gennem det eksisterende Test opt-in; scriptet sender
ingen beskeder. `Send testbesked` i UI er en brugerhandling.

## Forbered Cudy uden at skifte fra USB

Cudy dokumenterer SMS og AT-kommandoer i
[LT300-guiden](https://docs.cudy.com/user_guide/4g5g_router/lt300/).
Adapterens felter og login følger den officielle
[LT300-emulator](https://support.cudy.com/emulator/LT300/) og dens
[sysauth.js](https://support.cudy.com/emulator/LT300/luci-static/bootstrap/js/sysauth.js).
Dette er en firmwareafhængig webgrænseflade; fysisk routertest mangler endnu.

Tilslut SIM og Ethernet. Kontrollér, at Cudys egen side kan modtage SMS.
Tilføj følgende i `/opt/SBR-Pager-Gateway/.env`, med din egen adgangskode:

```dotenv
SMS_MODEM_DRIVER=usb
CUDY_BASE_URL=http://192.168.10.1
CUDY_USERNAME=admin
CUDY_PASSWORD=DIN_ROUTER_ADGANGSKODE
CUDY_HTTP_TIMEOUT_SECONDS=15
CUDY_POLL_SECONDS=5
```

Brug routerens faktiske LAN-IP. Hvis adgangskoden indeholder `$`, `#` eller
andre dotenv-specialtegn, anvend enkelt anførselstegn i `.env` omkring værdien.
Filen og backupkopierne må ikke deles eller committes.

Genskab Gateway, så den indlæser routerkonfigurationen, men beholder USB:

```bash
docker compose --env-file .env -f compose/sms-gateway/docker-compose.yml up -d --no-deps sms-gateway
```

Åbn **Forbindelser → Test Cudy-forbindelse**. Testen læser login, SIM, signal,
registrering og lager. Den læser/sletter ingen SMS og ændrer ikke routerens
APN, netværksindstillinger eller radiofunktion.

## Skift SMS-modtagelse til Cudy

Skift først, når forbindelsestesten virker. Flyt eventuelt alarm-SIM'et fra
USB til routeren; et SIM kan kun sidde ét sted ad gangen. Sæt:

```dotenv
SMS_MODEM_DRIVER=cudy
```

Start Cudy-konfigurationen på samme projekt og volumen:

```bash
docker compose --env-file .env -f compose/sms-gateway/cudy.yml up -d --no-deps sms-gateway
docker compose --env-file .env -f compose/sms-gateway/cudy.yml ps
```

Den kræver ikke, at USB-enheden findes. Boot-service og watchdog vælger nu
også Cudy-filen. Lad routerens SMS-funktion beholde indgående SMS, indtil
programmet har importeret dem. Brug ikke automatisk sletning på routeren.

Prøv en kort test-SMS, en lang/delt test-SMS og en opfølgende Sending 2.
Kontrollér dansk tegnsæt, korrekt station/Test-filter, sammenhængende
alarmhændelse og reel WhatsApp-kvittering. Gentag efter genstart af Gateway
og af hele serveren. Ukendt AT-formular eller manglende kvittering stopper
adapteren med en fejl i stedet for at slette beskederne.

**Cudy-adapteren modtager SMS. Udgående SMS og svar på `status` er ikke
implementeret via AT-webformularen.** Eksisterende udgående jobs beholdes,
og nye afsendelsesforsøg afvises tydeligt. Routeren beholder sin originale
firmware; ingen firmwareinstallation eller router-reset udføres.

## Skift tilbage til USB

Sæt `SMS_MODEM_DRIVER=usb`, flyt SIM tilbage hvis nødvendigt, og kør:

```bash
docker compose --env-file .env -f compose/sms-gateway/docker-compose.yml up -d --no-deps sms-gateway
```

USB-readerens AT-kommandoer, radio-recovery og device mapping er bevaret.
De to konfigurationer skal ikke køre samtidigt.

## Rollback af programopdateringen

Backupmappen fra opdateringsscriptet indeholder `rollback-pager.yml` og
`rollback-gateway.yml`. Brug den faktiske sti, og den Gateway-fil der matcher
den tidligere kilde:

```bash
docker compose --env-file .env -f compose/sms-whatsapp/compose.yml -f manual-backups/pager-update-TIDSPUNKT/rollback-pager.yml up -d --no-build --no-deps sms-whatsapp
docker compose --env-file .env -f compose/sms-gateway/docker-compose.yml -f manual-backups/pager-update-TIDSPUNKT/rollback-gateway.yml up -d --no-build --no-deps sms-gateway
```

Rollback bruger tidligere images og samme volumener. Stop ikke/slet ikke
volumener med `down -v`. Nye tabeller er additive, så normale image-rollbacks
kræver ikke tilbagerulning af databasen og tab af nyere alarmhistorik.

## Validering

Regressionstests, eksisterende indbyggede Dockerfile-checks og frontendens
desktop-/mobilvisning valideres før levering. Fysiske SIM800C-/LT300-tests
og afsendelse på den rigtige WhatsApp-konto udføres på installationen.

Designpreview bruger udelukkende eksempeldata:

![SBR Pager designpreview](images/sms-pager-overview.png)
