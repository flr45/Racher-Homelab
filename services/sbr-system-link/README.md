# SBR System Link Receiver

Lille modtager til Pi/homelab, som tager imod godkendte alarmdata fra **SBR Pager Gateway** over HTTPS.

Windows-pc'en behøver kun almindelig internetadgang. Den behøver ikke Tailscale, VPN, Docker eller anden ekstra software ud over SBR Pager Gateway.

## Flow

```text
SMS dongle -> SBR Pager Gateway -> WhatsApp
                           |
                           +-> HTTPS / Cloudflare Tunnel -> sbr-system-link -> SQLite
```

## Automatisk provisionering

Receiveren understøtter flere Windows-pc'er på samme offentlige endpoint. Hver pc får sin egen engangskode, sit eget `clientId` og sin egen klientnøgle.

Ved første start:

1. Windows-programmet kalder `POST /api/provision` over HTTPS med sin engangskode.
2. Engangskoden forbruges atomisk og kan ikke bruges igen.
3. Pi'en opretter et unikt `clientId` til den pc.
4. Pi'en udleder en unik klientnøgle fra sin private master-nøgle og returnerer klientnøglen én gang.
5. Windows gemmer klient-ID og klientnøgle krypteret med Windows DPAPI.
6. Fremtidige System Link requests signeres med den unikke klientnøgle og headeren `X-System-Link-Client`.

Master-nøglen forlader aldrig Pi'en. En mistet/udpakket installer indeholder derfor ikke den permanente System Link-nøgle, og en brugt engangskode er værdiløs.

## Flere Windows-maskiner

Opret en ny kode lokalt på Pi'en for hver maskine:

```bash
docker exec sbr-system-link python app.py create-token --label "Station-PC-01"
docker exec sbr-system-link python app.py create-token --label "Station-PC-02"
docker exec sbr-system-link python app.py create-token --label "Station-PC-03"
```

Kommandoen viser tokenet **én gang**. Tokenet gemmes kun som SHA-256 hash i receiverens database.

Se status uden at vise hemmelige tokens:

```bash
docker exec sbr-system-link python app.py list-tokens
```

Tilbagekald en ubrugt kode med dens ID:

```bash
docker exec sbr-system-link python app.py revoke-token 3
```

Alle maskiner bruger samme endpoints, fx:

```text
POST https://link.baseunit.dk/api/provision
POST https://link.baseunit.dk/api/system-link
```

## Sikkerhed

- Endpointet forventer schema `sbr-pager-gateway.system-link.v1`.
- Hele request-body signeres med HMAC-SHA256.
- Hver installation får sin egen klient-ID/nøgle.
- Engangskoder opbevares kun som hashes på Pi'en.
- Flere engangskoder kan være aktive samtidig og administreres kun lokalt via `docker exec`.
- `deliveryId` er unik pr. klient, så retries er idempotente og ikke giver dubletter.
- Containerens host-port bindes kun til `127.0.0.1`.
- Cloudflare Tunnel bør route direkte til containeren på Docker-netværket `proxy`.
- Legacy `SYSTEM_LINK_PROVISION_TOKEN` og `SYSTEM_LINK_SECRET` kan midlertidigt bruges under migration, men er ikke nødvendige for nye installationer.

## Deploy på Pi

Fra `~/Racher-Homelab` efter koden er på `main`:

```bash
cd ~/Racher-Homelab
git checkout main
git pull

MASTER="$(openssl rand -hex 32)"
printf '\nSBR_SYSTEM_LINK_MASTER_SECRET=%s\n' "$MASTER" >> .env
printf 'SBR_SYSTEM_LINK_PUBLIC_ENDPOINT=%s\n' 'https://link.baseunit.dk/api/system-link' >> .env

docker compose --env-file .env \
  -f compose/sbr-system-link/compose.yml \
  up -d --build

curl -fsS http://127.0.0.1:8098/health
```

`MASTER` må aldrig lægges i installeren eller GitHub. Efter containeren er startet oprettes per-PC engangskoder med `create-token`.

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
Hostname: link.baseunit.dk
Service:  http://sbr-system-link:8098
```

Cloudflared-containeren skal være tilsluttet Docker-netværket `proxy`. Der skal **ikke** åbnes port 8098 i routeren.

## Windows release-build

For hver Windows-pc oprettes en ny engangskode. Build-workflowet læser disse GitHub Actions secrets og skriver dem ind i `build_config.py` umiddelbart før PyInstaller bygger EXE'en:

```text
SBR_SYSTEM_LINK_PROVISION_URL=https://link.baseunit.dk/api/provision
SBR_SYSTEM_LINK_MESSAGE_ENDPOINT=https://link.baseunit.dk/api/system-link
SBR_SYSTEM_LINK_PROVISION_TOKEN=<engangskoden for denne pc/installer>
```

Ingen af værdierne commits til Git-repoet. Den permanente `SBR_SYSTEM_LINK_MASTER_SECRET` må aldrig være en GitHub Actions secret til Windows-builden.

## Data

Alarmdata gemmes i Docker-volume `sbr_system_link_data` i SQLite-databasen `/data/system-link.db`.

Receiveren har blandt andet tabellerne:

- `installations` – registrerede klient-ID'er, version/maskinenavn, sidste aktivitet og revocation-status
- `provision_codes` – hashes/status for de nye per-PC engangskoder
- `provision_tokens` – legacy audit for den tidligere ene miljøvariabel-kode
- `deliveries` – alarmtekst og hændelsesmetadata, inkl. klient-ID

Health endpoint:

```text
GET /health
```
