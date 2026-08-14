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

## MVP

- Mobilvenligt dashboard
- SQLite-historik
- Frivillig stationsgenkendelse som metadata
- Simulator til testmeldinger
- Pushover-test og automatisk videresendelse af alle dekodede meldinger
- PDL-logfil som inputkilde
- `/healthz` til watchdog/monitorering

## Lokal test

```bash
cd services/pager-gateway
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p .data
PAGER_DATA_DIR="$PWD/.data" python app.py
```

Åbn `http://localhost:8088`.

## PDL-integration

Den nuværende Linux-version af PDL kan skrive hver dekodet linje til en fil med `-o <file>`. Gatewayen kan tail'e den fil og behandle nye linjer. På Raspberry Pi sætter vi som udgangspunkt stien til `/data/pdl.log`.

PDL har i øjeblikket ikke en ren live `--headless`-tilstand; den normale live capture starter en GTK/WebKit-GUI. Derfor er næste integrationsmilepæl enten:

1. en lille patch til PDL med `--headless`, eller
2. en separat PDL decoder-service, der deler decoderkoden men ikke GUI'en.

Vi undgår at gøre gatewayens webapp afhængig af den beslutning ved at holde inputlaget separat.

## Sikkerhed

Pushover-nøgler gemmes lokalt i SQLite på gatewayen og returneres aldrig i klartekst fra settings-API'et. Før ekstern adgang aktiveres, skal webinterfacet have login og TLS/reverse proxy.
