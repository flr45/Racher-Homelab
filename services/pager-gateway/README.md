# Racher Pager Gateway

Webbaseret gateway til POCSAG-meldinger. Systemet er lavet, så næsten hele løsningen kan færdiggøres og testes uden scanner; den fysiske radio kobles på til sidst.

## Vigtigt princip: send alt

Gatewayen bruger **ikke** `(A)`, `(S)`, `(K)`, `(L)` eller `(R)` som filter. Enhver gyldig dekodet pager-melding gemmes og kan videresendes.

Kendte stationsmarkører bruges kun som ekstra metadata/titel. Meldinger uden stationsmarkør, fx `$8 ISL ...`, `@6 ØF ...` og `VCT - ISL-Eftersyn ...`, behandles på samme måde og må aldrig kasseres af gatewayen.

## Arkitektur

```text
Scanner -> USB-lydkort -> PDL 3.2.0 headless -> /var/lib/racher-pager/pdl.log
                                                    |
                                                    v
                                         Racher Pager Gateway
                                      |       |       |        |
                                   SQLite   PWA    Web Push  Pushover
                                      |
                                      +-- admin system command queue
                                      +-- runtime health/status
                                                   |
                                                   v
                                         root-ejet host-agent
```

PDL forbliver upstream decoder. Racher-integrationen ændrer ikke POCSAG-dekoderen; den tilføjer kun en lille `--headless` live-mode, så ALSA capture kan køre uden GTK/WebKit-vindue.

Upstream er fastlåst til PDL 3.2.0 commit `f37a24ee45b06f35703d513d48780c9334c4ff89`.

## Roller og login

Der findes to roller:

- `admin`: ser alarmer samt systemstatus, simulator, indstillinger og brugeradministration.
- `user`: ser kun alarmfeed/historik og kan aktivere PWA-notifikationer på egne enheder.

Når databasen er tom, sender `/login` automatisk videre til `/setup`. Her oprettes den **første administrator**. Når første konto eksisterer, lukker setup-flowet, og nye brugere kan kun oprettes fra en admin-konto.

Adgangskoder gemmes med PBKDF2-HMAC-SHA256. Sessions er HttpOnly/SameSite og alle muterende routes er CSRF-beskyttet.

## PWA og Web Push

Gatewayen indeholder manifest, service worker og VAPID Web Push. Hver bruger kan tilmelde sin egen telefon/computer. Nye pageralarmer sendes til alle aktive push-subscriptions.

VAPID private key genereres lokalt i dataområdet og returneres aldrig via API'et.

Web Push kræver secure context: HTTPS i normal drift. `localhost` kan bruges til udvikling. Når Pi'en sættes i rigtig drift bag HTTPS sættes `PAGER_COOKIE_SECURE=1`.

## Admin systemstyring

Webcontaineren får ikke root- eller Docker-socket-adgang. Admin-knapper opretter i stedet en kommando i SQLite. En separat root-ejet systemd-agent på Pi'en accepterer kun denne whitelist:

- `restart-pdl`
- `restart-gateway`
- `reboot`

Der kan ikke sendes vilkårlige shell-kommandoer fra webappen.

Host-agenten skriver også løbende faktiske Pi-målinger til SQLite: PDL-service, gateway-container, ALSA capture-enheder, diskplads, CPU-temperatur, host uptime, PDL-log og backupstatus. Admin-siden viser dem som en klargøringsliste. Manglende USB-lydkort eller PDL-data vises som **afventer**, så Pi'en kan gøres færdig hjemme uden scanner.

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

På et nyt Raspberry Pi OS 64-bit system, efter `Racher-Homelab` er klonet:

```bash
cd ~/Racher-Homelab
bash services/pager-gateway/install-pager-gateway.sh
```

Bootstrap-scriptet er idempotent og:

1. installerer Docker, Compose, ALSA-værktøjer og SQLite,
2. opretter `/var/lib/racher-pager`,
3. bygger den fastlåste PDL 3.2.0 med headless-patch,
4. installerer og aktiverer `racher-pdl.service`,
5. bygger og starter `racher-pager-gateway`-containeren,
6. sætter gatewayen til PDL-loginput,
7. installerer den root-ejede health/system-agent,
8. installerer daglig backup og opretter første backup,
9. viser gatewayens lokale URL og slutstatus.

Scriptet skal køres som normal bruger, **ikke** som `sudo bash`; det bruger selv `sudo` hvor det er nødvendigt.

Eksisterende `/etc/racher-pager/gateway.env` bevares ved genkørsel. Det betyder bl.a., at en senere HTTPS-port, `PAGER_COOKIE_SECURE=1` og VAPID-konfiguration ikke nulstilles af en opdatering/reparation.

Det er forventet, at PDL/USB-lyd kan stå som afventende hjemme. Systemet er stadig klargjort til den senere scanner-test.

## PDL og scanner-test

Før første rigtige radio-test findes lydkortets ALSA-navn med:

```bash
arecord -L
```

Konfiguration ligger i `/etc/racher-pager/pdl.env`. Dekodede linjer skrives til `/var/lib/racher-pager/pdl.log`.

Når USB-lydkortet er kendt, ændres fx:

```text
PDL_CAPTURE_DEVICE=default
PDL_SAMPLE_RATE=48000
```

og PDL genstartes fra admin-siden eller med:

```bash
sudo systemctl restart racher-pdl
```

## Backup

`racher-pager-backup.timer` laver daglig lokal backup. Første backup køres straks under bootstrap.

Standardplacering:

```text
/var/backups/racher-pager/
```

Backup indeholder en konsistent SQLite-backup samt relevante lokale secrets/PDL-konfiguration, når filerne findes. Arkiverne er root-only (`0600`) og standard-retention er 14 dage.

Status kan ses med:

```bash
systemctl status racher-pager-backup.timer --no-pager
```

## Test uden scanner

Admin-simulatoren sender en testalarm gennem samme flow som en rigtig PDL-melding:

```text
Simulator -> SQLite -> alarmfeed -> PWA Web Push -> Pushover
```

Det betyder, at login, brugere, historik, notifikationer og systemadministration kan gøres færdige hjemme, før Pi'en fysisk kobles på scanneren.

## Når Pi'en skal online

Den lokale bootstrap bruger HTTP og `PAGER_COOKIE_SECURE=0` ved første installation. Næste driftstrin er reverse proxy/HTTPS og en stabil ekstern adresse. Først når HTTPS er sat op aktiveres `PAGER_COOKIE_SECURE=1` og PWA Web Push testes på telefonerne.
