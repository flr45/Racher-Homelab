# Racher Pager Gateway

Webbaseret gateway til POCSAG-meldinger. Første version er hardware-uafhængig, så webinterface, historik og Pushover kan udvikles før Raspberry Pi og scanner er tilsluttet.

## Vigtigt princip: send alt

Gatewayen bruger **ikke** `(A)`, `(S)`, `(K)`, `(L)` eller `(R)` som filter. Enhver gyldig dekodet pager-melding gemmes og kan videresendes via Pushover.

Kendte stationsmarkører bruges kun som ekstra metadata/titel:

- `(A)` Slagelse
- `(S)` Sorø
- `(K)` Korsør
- `(L)` Skælskør
- `(R)` Ruds Vedby

Meldinger uden stationsmarkør, fx `$8 ISL ...`, `@6 ØF ...` og `VCT - ISL-Eftersyn ...`, behandles på samme måde og må aldrig kasseres af gatewayen.

## Arkitektur

```text
Scanner -> USB-lydkort -> PDL 3.2.0 headless -> /var/lib/racher-pager/pdl.log
                                                    |
                                                    v
                                         Racher Pager Gateway
                                          |      |       |
                                       SQLite  Web UI  Pushover
```

PDL forbliver upstream decoder. Racher-integrationen ændrer ikke POCSAG-dekoderen; den tilføjer kun en lille `--headless` live-mode, så ALSA capture kan køre uden GTK/WebKit-vindue.

Upstream er fastlåst til PDL 3.2.0 commit:

`f37a24ee45b06f35703d513d48780c9334c4ff89`

## MVP

- Mobilvenligt dashboard
- SQLite-historik
- Frivillig stationsgenkendelse som metadata
- Simulator til testmeldinger
- Pushover-test og automatisk videresendelse af alle dekodede meldinger
- PDL-logfil som inputkilde
- `/healthz` til watchdog/monitorering
- reproducerbar Linux/ARM PDL-build
- `--headless` live ALSA capture
- systemd service med automatisk genstart

## Lokal test af webgateway på Mac

```bash
cd services/pager-gateway
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p .data
PAGER_DATA_DIR="$PWD/.data" python app.py
```

Åbn `http://localhost:8088`.

PDL kan ikke bygges direkte på macOS; upstream CMake stopper bevidst på Apple. Selve PDL-buildet udføres på Raspberry Pi OS/Debian/Ubuntu.

## PDL på Raspberry Pi

På Raspberry Pi OS 64-bit:

```bash
cd ~/Racher-Homelab/services/pager-gateway/pdl
chmod +x *.sh
./install-pdl.sh
./install-pdl-service.sh
```

Installeren:

1. installerer PDL's Linux build-afhængigheder,
2. henter den fastlåste PDL 3.2.0-kildekode,
3. anvender vores minimale headless-patch,
4. bygger PDL til Pi'ens egen arkitektur,
5. installerer `racher-pdl.service`.

Før første rigtige start findes lydkortets ALSA-navn med:

```bash
arecord -L
```

Redigér derefter:

```bash
sudo nano /etc/racher-pager/pdl.env
```

Eksempel:

```text
PDL_CAPTURE_DEVICE=default
PDL_SAMPLE_RATE=48000
PDL_BAUD_512=1
PDL_BAUD_1200=1
PDL_BAUD_2400=1
PDL_INVERT=0
PAGER_STATE_ROOT=/var/lib/racher-pager
```

Start og følg decoder:

```bash
sudo systemctl start racher-pdl
sudo systemctl status racher-pdl --no-pager
journalctl -u racher-pdl -f
```

Dekodede linjer skrives til:

`/var/lib/racher-pager/pdl.log`

## Del PDL-data med webgatewayen

Når gatewayen køres med Docker på Pi'en sættes:

```text
PAGER_DATA_HOST_PATH=/var/lib/racher-pager
```

Compose binder derefter samme mappe ind som `/data`, og gatewayen læser `/data/pdl.log` direkte.

## Sikkerhed

Pushover-nøgler gemmes lokalt i SQLite på gatewayen og returneres aldrig i klartekst fra settings-API'et. Før ekstern adgang aktiveres, skal webinterfacet have login og TLS/reverse proxy.
