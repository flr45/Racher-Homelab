# SMS Alarm Gateway

USB-modem-baseret SMS-tjeneste til Racher OS. Første melding videresendes straks, når den indeholder en stationskode. Efterfølgende meldinger fra samme afsender videresendes også straks i et tidsvindue; tidsvinduet bruges kun til at koble melding 2 til den aktive alarm og skaber ingen forsinkelse.

## Stationskoder

| Kode | Station |
|---|---|
| `(A)` | Slagelse |
| `(S)` | Sorø |
| `(K)` | Korsør |
| `(L)` | Skælskør |
| `(R)` | Ruds Vedby |

## Første test uden modem

Start med `SMS_DRY_RUN=true`, så SMS'er logges uden at blive sendt.

```bash
cd compose/sms-gateway
SMS_DRY_RUN=true docker compose up --build
```

Opret en brandmand:

```bash
curl -X POST http://127.0.0.1:8090/api/firefighters \
  -H 'Content-Type: application/json' \
  -d '{"name":"Testperson","phone":"+4512345678","stations":["A","S"],"active":true}'
```

Simulér første alarmmelding:

```bash
curl -X POST http://127.0.0.1:8090/api/incoming \
  -H 'Content-Type: application/json' \
  -d '{"sender":"+4599999999","body":"20:10:28 26-07-29 (A)M+R Redn.-Fastklemt, Maskine o.l."}'
```

Simulér melding 2 fra samme nummer:

```bash
curl -X POST http://127.0.0.1:8090/api/incoming \
  -H 'Content-Type: application/json' \
  -d '{"sender":"+4599999999","body":"Supplerende oplysninger fra alarmcentralen"}'
```

Begge meldinger behandles med det samme.

## Huawei E180

Find modemportene på Raspberry Pi:

```bash
lsusb
dmesg | grep -E 'ttyUSB|ttyACM'
```

Huawei-modemer opretter ofte flere porte. Test dem med et terminalprogram og kommandoen `AT`; den korrekte AT-port svarer `OK`. Sæt derefter f.eks.:

```bash
SMS_MODEM_DEVICE=/dev/ttyUSB2
SMS_DRY_RUN=false
```

Tjenesten sender i SMS-teksttilstand med `AT+CMGF=1` og `AT+CMGS`.

## API

- `GET /health`
- `GET /api/stations`
- `GET /api/firefighters`
- `POST /api/firefighters`
- `PUT /api/firefighters/<id>`
- `POST /api/incoming`
- `GET /api/messages`

## Næste hardwaretrin

Den indgående modemlæser skal kobles til Huawei-portens nye-SMS-notifikationer eller polling af modemlageret. `/api/incoming` er allerede den fælles indgang, så videresendelseslogikken, stationsvalg, modtagere og logning kan testes før modemmet tilsluttes.
