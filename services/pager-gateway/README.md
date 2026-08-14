# Racher Pager Gateway

Webbaseret gateway til POCSAG-meldinger. Systemet er lavet, så næsten hele løsningen kan færdiggøres og testes uden scanner; den fysiske radio kobles på til sidst.

## Vigtigt princip: gem rådata, rout bagefter

Gatewayen bruger **ikke** `(A)`, `(S)`, `(K)`, `(L)` eller `(R)` som et hårdt inputfilter. Enhver gyldig dekodet pager-melding gemmes med rålinjen intakt.

Kendte stationsmarkører kan bruges som ekstra metadata. Den kommende routing/notification-filtrering skal primært kunne bruge POCSAG RIC/capcode og kun bruge tekstmarkører som supplement/fallback. Meldinger må ikke forsvinde fra råhistorikken, fordi en klassifikation ikke lykkes.

## Arkitektur

```text
Antenne -> Scanner -> discriminator -> FSK-USB -> USB serial / FTDI
                                                   |
                                      /dev/serial/by-id/...
                                                   |
                                                   v
                                        PDL 3.2.0 headless
                                                   |
                                                   v
                                  /var/lib/racher-pager/pdl.log
                                                   |
                                                   v
                                         Racher Pager Gateway
                                      |       |       |        |
                                   SQLite   PWA    Web Push  Pushover
                                      |
                                      +-- admin system command queue
                                      +-- runtime health/status
                                      +-- FSK-USB hardware status
                                      +-- audit log
                                                   |
                                                   v
                                         root-ejet host-agent
                                   /          |          |          \
                              Network      Backup     Update      systemd
                              Manager      restore    rollback     services
```

FSK-USB er primært input. Interfacet leverer den slicede bitstrøm som USB-seriel, og PDL's eksisterende Linux RS232-bitstream-path føder den videre ind i POCSAG-dekoderen. ALSA/soundcard-input beholdes kun som fallback.

Racher-patchen til headless mode kan bruge `PDL_RS232_DEVICE`, så den konkrete adapter kan fastlåses til en stabil `/dev/serial/by-id/...`-sti i stedet for at være afhængig af `ttyUSB0`-nummerering.

Upstream er fastlåst til PDL 3.2.0 commit `f37a24ee45b06f35703d513d48780c9334c4ff89`.

## Roller og login

Der findes to roller:

- `admin`: ser alarmer, diagnostik, netværk, backup/recovery, update/rollback, systemstatus, simulator, indstillinger og brugeradministration.
- `user`: ser kun alarmfeed/historik og kan aktivere PWA-notifikationer på egne enheder.

Når databasen er tom, sender `/login` automatisk videre til `/setup`. Her oprettes den **første administrator**. Når første konto eksisterer, lukker setup-flowet, og nye brugere kan kun oprettes fra en admin-konto.

Adgangskoder gemmes med PBKDF2-HMAC-SHA256. Sessions er HttpOnly/SameSite og alle muterende routes er CSRF-beskyttet.

## PWA og Web Push

Gatewayen indeholder manifest, service worker og VAPID Web Push. Hver bruger kan tilmelde sin egen telefon/computer.

I den nuværende MVP sendes nye pageralarmer til alle aktive push-subscriptions. Per-station/per-user routing er bevidst ikke slået til endnu, før filterreglerne er fastlagt.

VAPID private key genereres lokalt i dataområdet og returneres aldrig via API'et.

Web Push kræver secure context: HTTPS i normal drift. `localhost` kan bruges til udvikling. Når Pi'en sættes i rigtig drift bag HTTPS sættes `PAGER_COOKIE_SECURE=1`.

## Sikker admin-systemstyring

Webcontaineren får ikke root- eller Docker-socket-adgang. Admin-handlinger lægges i en SQLite-kø, som en separat root-ejet host-agent behandler.

Tilladte handlinger er eksplicitte og valideres både i web/backend og igen i host-agenten. Der bruges ikke vilkårlig `sh -c`/shell-tekst fra webinput.

Handlingerne omfatter:

- genstart PDL, gateway, tunnel eller hele Pi'en,
- manuel backup og valideret restore,
- gateway update og rollback,
- tilføj/fjern Racher-administrerede Wi-Fi-profiler,
- start/stop fallback-hotspot.

Wi-Fi-passwords returneres ikke i kommandohistorikken. Den aktive payload ryddes efter behandling. Setup-hotspottets Password/PIN ligger i root-only `/etc/racher-pager/network.env` og kan vises af admin via runtime-status.

Admin-hændelser logges i `audit_log` uden password/token-payloads.

## Live diagnostik

Host-agenten og FSK-statusproben skriver løbende faktiske Pi-målinger til SQLite:

- PDL-service og PDL-log,
- FSK-USB tilsluttet/afventer,
- stabil device path og underliggende `ttyUSB`/`ttyACM`,
- FTDI/udev metadata og driver,
- 19200 8N1 konfiguration,
- om PDL aktuelt har den valgte serielenhed åben,
- gateway-container,
- CPU-temperatur, disk og host uptime,
- Wi-Fi-forbindelse, IP og signal,
- internetstatus,
- fallback-hotspot,
- Cloudflare Tunnel-service/version,
- backupkatalog,
- installeret gateway- og rollback-version.

Manglende FSK-USB eller PDL-data vises som **afventer**, så Pi'en kan gøres færdig hjemme uden scanner.

## Lokal test på Mac

```bash
cd ~/Racher-Homelab/services/pager-gateway
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p .data
PAGER_DATA_DIR="$PWD/.data" python app.py
```

Åbn `http://localhost:8088`. Første gang bliver du sendt til `/setup` for at oprette admin.

PDL kan ikke bygges direkte på macOS; selve PDL-buildet udføres på Raspberry Pi OS/Debian/Ubuntu.

## Én-kommando installation på Raspberry Pi

På Raspberry Pi OS Lite 64-bit, efter `Racher-Homelab` er klonet:

```bash
cd ~/Racher-Homelab
bash services/pager-gateway/install-pager-gateway.sh
```

Bootstrap-scriptet skal køres som normal bruger, **ikke** med `sudo bash`; scriptet bruger selv `sudo` hvor nødvendigt.

Bootstrap gør bl.a. følgende:

1. installerer Docker/Compose, NetworkManager, SQLite og nødvendige systemværktøjer,
2. laver en isoleret pager-runtime i `/opt/racher-pager/runtime-repo`, så admin-opdateringer ikke ændrer brugerens normale Homelab-checkout,
3. opretter `/var/lib/racher-pager`, lokale env-filer og backupområde,
4. bygger den fastlåste PDL 3.2.0 med headless + explicit-device patch,
5. installerer PDL som systemd-service med FSK-USB som standardinput,
6. installerer Wi-Fi mobility og fallback-portal,
7. bygger/starter Pager Gateway og sætter PDL som produktionsinput,
8. installerer root-ejet health/system-agent, FSK-USB statusprobe og recovery-helpers,
9. installerer daglig backup og opretter første backup,
10. gemmer installeret commit som version/reference og viser slutstatus samt fallback-Wi-Fi Password/PIN.

Eksisterende `/etc/racher-pager/gateway.env`, `network.env` og `pdl.env` bevares ved genkørsel. Senere HTTPS-, tunnel-, netværks- og hardwareindstillinger nulstilles derfor ikke af en repair/bootstrap.

## Wi-Fi mobility og flytning mellem netværk

Pi'en bruger DHCP og skal ikke have en hardcoded lokal IP.

Kendte netværk kan tilføjes fra adminpanelet. Racher-oprettede profiler får interne navne i formatet `racher-wifi-<hash>` og kan fjernes igen fra admin.

Hvis Pi'en ikke har internet efter standardmæssigt 180 sekunder, kan system-agenten starte fallback-netværket:

```text
SSID: Racher-Pager-Setup
IP:   10.42.0.1
Web:  http://10.42.0.1/
```

Password/PIN genereres under installationen og vises i bootstrap-resultatet. Setup-portalen kræver samme PIN, før et nyt Wi-Fi kan gemmes.

Når fallback-hotspottet er startet automatisk, lukkes det igen når normal internetforbindelse er tilbage. Et automatisk hotspot bliver desuden periodisk slukket for at give gemte normale Wi-Fi-profiler en ny chance for at forbinde.

Det betyder, at Pi'en først kan sættes op hjemme og derefter flyttes til scannerlokationen uden reinstallering.

## FSK-USB og scanner-test

FSK-USB forventes som USB-seriel/FTDI. Første hardwaretest på Pi'en bliver derfor:

```bash
lsusb
ls -l /dev/serial/by-id/ 2>/dev/null || true
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || true
```

Konfiguration ligger i `/etc/racher-pager/pdl.env`. Standard er:

```text
PDL_INPUT_MODE=fsk-usb
PDL_RS232_DEVICE=
PDL_RS232_PORT=1
PDL_RS232_BITRATE=19200
PDL_RS232_DECODE_MODE=2
```

Når adapterens stabile by-id er kendt, kan `PDL_RS232_DEVICE` sættes til fx `/dev/serial/by-id/usb-FTDI_...`. Hvis feltet er tomt, auto-detekterer startwrapperen først `/dev/serial/by-id/*` og derefter `/dev/ttyUSB*`/`ttyACM*`.

Dekodede linjer skrives til `/var/lib/racher-pager/pdl.log`. PDL kan genstartes direkte fra admin-siden.

## Backup og restore

`racher-pager-backup.timer` laver daglig lokal backup, og første backup køres straks under bootstrap.

Standardplacering:

```text
/var/backups/racher-pager/
```

Backup omfatter en konsistent SQLite-backup samt lokale session/VAPID-nøgler, PDL-konfiguration og relevante root-only env/tunnel-filer, når de findes. Arkiverne er `0600`, og standard-retention er 14 dage.

Admin kan lave en backup nu og vælge en eksisterende backup til restore. Restore accepterer kun Racher-backupnavne, afviser traversal-stier, integritetstjekker SQLite, laver safety-backup først og genstarter relevante services bagefter.

## Update og rollback

Gateway-opdateringer kører kun mod den isolerede runtime-klon under `/opt/racher-pager/runtime-repo`.

Update-flowet henter deploy-branch, kræver fast-forward, tager backup, gemmer rollback-reference, validerer kode/shell, bygger nyt image og kræver et bestået `/healthz`. Ved fejl rulles automatisk tilbage.

## Cloudflare Tunnel / stabil ekstern adresse

Cloudflare-delen installeres først, når tunnel-token og det endelige hostname kendes. Bootstrap opfinder derfor ikke et domæne eller token.

Når oplysningerne er klar:

```bash
cd /opt/racher-pager/runtime-repo
bash services/pager-gateway/pdl/install-cloudflared.sh \
  '<TUNNEL_TOKEN>' \
  'pager.ditdomæne.dk'
```

Tunnelens public hostname skal konfigureres til gatewayens lokale origin, normalt `http://localhost:8088`. `cloudflared` kører derefter som systemd-service og kan genstartes fra admin.

Når HTTPS er verificeret, sættes `PAGER_COOKIE_SECURE=1`, og PWA/Web Push testes på telefonerne.

## Test uden scanner

Admin-simulatoren sender en testalarm gennem samme flow som en rigtig PDL-melding:

```text
Simulator -> SQLite -> alarmfeed -> PWA Web Push -> Pushover
```

Det betyder, at login, brugere, historik, notifikationer, netværksdiagnostik, backup og systemadministration kan klargøres hjemme før den fysiske scanner-test.

## Drifts- og datanote

Pager-/indsatsdata kan være følsomme. Ekstern adgang bør kun aktiveres for autoriserede brugere, og lokal lovgivning/arbejdsgiverens regler for modtagelse, lagring og videredistribution af pagertrafik skal være afklaret før produktionsdrift.
