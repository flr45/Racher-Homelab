# SBR System Link Receiver

Lille modtager til Pi/homelab, som tager imod godkendte alarmdata fra **SBR Pager Gateway** over HTTPS.

Windows-pc'en behøver kun almindelig internetadgang. Den behøver ikke Tailscale, VPN, Docker eller anden ekstra software ud over SBR Pager Gateway.

## Flow

```text
SMS dongle -> SBR Pager Gateway -> WhatsApp
                           |
                           +-> HTTPS / Cloudflare Tunnel -> sbr-system-link -> SQLite
```

## Sikkerhed

- Endpointet forventer schema `sbr-pager-gateway.system-link.v1`.
- Hele request-body signeres med HMAC-SHA256.
- Den delte nøgle sendes aldrig i requesten.
- `deliveryId` er unik, så retries er idempotente og ikke giver dubletter.
- Containerens host-port bindes kun til `127.0.0.1`.
- Cloudflare Tunnel bør route direkte til containeren på Docker-netværket `proxy`.

## Deploy på Pi

Fra `~/Racher-Homelab`:

```bash
git fetch origin feature/sbr-system-link-cloudflare
git checkout feature/sbr-system-link-cloudflare

SECRET="$(openssl rand -hex 32)"
printf '\nSBR_SYSTEM_LINK_SECRET=%s\n' "$SECRET" >> .env

docker compose --env-file .env \
  -f compose/sbr-system-link/compose.yml \
  up -d --build

curl -fsS http://127.0.0.1:8098/health
```

Gem værdien af `$SECRET`; den samme værdi skal indtastes som **Nøgle** under System Link i SBR Pager Gateways avancerede indstillinger.

Kontrollér containeren:

```bash
docker compose --env-file .env \
  -f compose/sbr-system-link/compose.yml \
  ps

docker logs --tail 100 sbr-system-link
```

## Cloudflare Tunnel

I Cloudflare Zero Trust / Tunnel oprettes et Public Hostname, fx:

```text
Hostname: link.example.dk
Service:  http://sbr-system-link:8098
```

Cloudflared-containeren skal være tilsluttet Docker-netværket `proxy`. Kontrollér fx:

```bash
docker inspect cloudflared --format '{{json .NetworkSettings.Networks}}'
```

Hvis containeren hedder noget andet, find den først med:

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}' | grep -i cloudflare
```

Hvis cloudflared ikke allerede er på `proxy`-netværket, kan den tilsluttes med:

```bash
docker network connect proxy <cloudflared-container-navn>
```

Der skal **ikke** åbnes port 8098 i routeren.

## Windows-konfiguration

I **SBR Pager Gateway -> Avanceret -> System Link**:

```text
Aktiv:    Ja
Endpoint: https://link.example.dk/api/system-link
Nøgle:    <samme SBR_SYSTEM_LINK_SECRET som på Pi>
Timeout:  5 sek.
Retry:    30 sek.
```

Tryk derefter **Test System Link**. En succesfuld test returnerer HTTP 200 og gemmer ikke en falsk alarm.

## Data

Alarmdata gemmes i Docker-volume `sbr_system_link_data` i SQLite-databasen `/data/system-link.db`.

Tabellen `deliveries` indeholder bl.a.:

- delivery ID
- modtaget-tid på Pi'en
- afsender og rå SMS-tekst
- SMS'ens oprindelige modtaget-tid
- station
- alarmtype
- adresse
- hændelsesstatus
- Sending 2/follow-up count
- rå JSON-payload

Health endpoint:

```text
GET /health
```

Alarm endpoint:

```text
POST /api/system-link
```
