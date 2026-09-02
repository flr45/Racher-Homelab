# WhatsApp via OpenWA

Pager Gateway kan sende godkendte pageralarmer til den enkelte brugers WhatsApp via en lokal OpenWA-instans.

## Arkitektur

```text
PDL -> parser -> adaptivt filter/dubletkontrol -> stationsrouting -> SQLite
                                                    |
                                                    +-> Web Push
                                                    +-> Pushover
                                                    +-> WhatsApp -> OpenWA -> WhatsApp
```

WhatsApp ligger efter pagerens eksisterende filter. Meldinger med `delivery_eligible=0` bliver derfor ikke sendt.

## Sikkerhed og drift

OpenWA er en uofficiel WhatsApp-gateway. Brug et separat WhatsApp-nummer, som ikke er dit private hovednummer. Pushover/Web Push bør fortsat være den primære/fallback leveringskanal.

OpenWA-dashboard/API er som standard kun bundet til `127.0.0.1:2785` på Pi'en. Pager-containeren taler med OpenWA på det interne Docker-netværk via `http://openwa:2785`.

## 1. Opdatér og start stacken

```bash
cd /opt/racher-pager/runtime-repo
sudo git fetch origin feature/pager-gateway-mvp
sudo git reset --hard origin/feature/pager-gateway-mvp

sudo env PAGER_GATEWAY_ENV=/etc/racher-pager/gateway.env \
  /opt/racher-pager/integration/pager-compose.sh pull openwa

sudo env PAGER_GATEWAY_ENV=/etc/racher-pager/gateway.env \
  /opt/racher-pager/integration/pager-compose.sh up -d --build
```

## 2. Åbn OpenWA lokalt

Fra en computer med SSH/Tailscale-adgang til Pi'en:

```bash
ssh -L 2785:127.0.0.1:2785 racher@<PI-IP>
```

Åbn derefter `http://127.0.0.1:2785` i browseren.

Opret/initialisér OpenWA, forbind det dedikerede WhatsApp-nummer og opret en **operator API key**. Sessionen bør hedde `pager`.

## 3. Gem pagerens OpenWA-konfiguration

Redigér `/etc/racher-pager/gateway.env` på Pi'en:

```text
PAGER_WHATSAPP_ENABLED=1
PAGER_OPENWA_URL=http://openwa:2785
PAGER_OPENWA_API_KEY=<OPERATOR_API_KEY>
PAGER_OPENWA_SESSION=pager
PAGER_OPENWA_TIMEOUT=10
```

Genstart derefter stacken:

```bash
sudo env PAGER_GATEWAY_ENV=/etc/racher-pager/gateway.env \
  /opt/racher-pager/integration/pager-compose.sh up -d --force-recreate
```

## 4. Aktivér WhatsApp på en bruger

Log ind i Pager Gateway. På Alarm-fanen vises nu et WhatsApp-kort.

Brugeren kan:

- gemme eget WhatsApp-nummer,
- aktivere/deaktivere WhatsApp-alarm,
- sende en testbesked.

Danske 8-cifrede numre normaliseres automatisk til `+45`. Internationale numre skal gemmes som E.164, fx `+4512345678`.

Administrator kan desuden bruge:

- `GET /api/whatsapp/status`
- `GET /api/whatsapp/users`
- `PUT /api/whatsapp/users/<user_id>`
- `GET /api/whatsapp/deliveries`

## Routing

WhatsApp følger samme bruger-routing som Web Push:

- station/område matcher brugerens valgte stationer,
- `receive_all` modtager alle godkendte meldinger,
- ukendt station går kun til admins eller `receive_all`,
- deaktiverede brugere modtager ikke WhatsApp,
- støj og dubletter med `delivery_eligible=0` sendes ikke.

## Leveringslog

Tabellen `whatsapp_deliveries` gemmer én række pr. alarm og bruger. `UNIQUE(message_id, user_id)` forhindrer samme alarm i at blive sendt to gange til samme bruger, selv hvis dispatch-hooket skulle blive kaldt igen.

Status er `queued`, `sent` eller `failed`; OpenWA message-id og en kort fejltekst gemmes, når de findes.
