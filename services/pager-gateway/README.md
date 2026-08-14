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

Adgangskoder gemmes som Werkzeug password hashes. Sessions er HttpOnly/SameSite og alle muterende routes er CSRF-beskyttet.

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

## PDL på Raspberry Pi

På Raspberry Pi OS 64-bit:

```bash
cd ~/Racher-Homelab/services/pager-gateway/pdl
bash install-pdl.sh
bash install-pdl-service.sh
bash install-system-agent.sh
```

PDL-installeren installerer Linux build-afhængigheder, henter den fastlåste PDL-version, anvender headless-patchen og bygger binæren til Pi'ens egen arkitektur.

Før første rigtige radio-test findes lydkortets ALSA-navn med:

```bash
arecord -L
```

Konfiguration ligger i `/etc/racher-pager/pdl.env`. Dekodede linjer skrives til `/var/lib/racher-pager/pdl.log`.

## Webgateway på Pi

Sæt mindst:

```text
PAGER_DATA_HOST_PATH=/var/lib/racher-pager
PAGER_COOKIE_SECURE=0
PAGER_VAPID_SUBJECT=mailto:admin@example.dk
```

og start:

```bash
cd ~/Racher-Homelab
docker compose -f compose/pager-gateway/docker-compose.yml up -d --build
```

Når HTTPS er sat op, ændres `PAGER_COOKIE_SECURE=1`.

## Test uden scanner

Admin-simulatoren sender en testalarm gennem samme flow som en rigtig PDL-melding:

```text
Simulator -> SQLite -> alarmfeed -> PWA Web Push -> Pushover
```

Det betyder, at login, brugere, historik, notifikationer og systemadministration kan gøres færdige hjemme, før Pi'en fysisk kobles på scanneren.
