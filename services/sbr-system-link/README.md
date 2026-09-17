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

En release-installer kan indeholde et offentligt provisionerings-endpoint og en **engangskode**. Ved første start:

1. Windows-programmet kalder `POST /api/provision` over HTTPS.
2. Engangskoden forbruges og kan ikke bruges igen.
3. Pi'en opretter et unikt `clientId` til den pc.
4. Pi'en udleder en unik klientnøgle fra sin private master-nøgle og returnerer klientnøglen én gang.
5. Windows gemmer klient-ID og klientnøgle krypteret med Windows DPAPI.
6. Fremtidige System Link requests signeres med den unikke klientnøgle og headeren `X-System-Link-Client`.

Master-nøglen forlader aldrig Pi'en. En mistet/udpakket installer indeholder derfor ikke den permanente System Link-nøgle, og engangskoden er værdiløs efter første succesfulde registrering.

## Sikkerhed

- Endpointet forventer schema `sbr-pager-gateway.system-link.v1`.
- Hele request-body signeres med HMAC-SHA256.
- Hver installation får sin egen klient-ID/nøgle.
- `deliveryId` er unik pr. klient, så retries er idempotente og ikke giver dubletter.
- Containerens host-port bindes kun til `127.0.0.1`.
- Cloudflare Tunnel bør route direkte til containeren på Docker-netværket `proxy`.
- Legacy `SYSTEM_LINK_SECRET` kan midlertidigt bruges til ældre Windows-builds under migration.

## Deploy på Pi

Fra `~/Racher-Homelab` efter koden er på `main`:

```bash
cd ~/Racher-Homelab
git checkout main
git pull

MASTER="$(openssl rand -hex 32)"
PROVISION="$(openssl rand -hex 24)"

printf '\nSBR_SYSTEM_LINK_MASTER_SECRET=%s\n' "$MASTER" >> .env
printf 'SBR_SYSTEM_LINK_PROVISION_TOKEN=%s\n' "$PROVISION" >> .env
printf 'SBR_SYSTEM_LINK_PUBLIC_ENDPOINT=%s\n' 'https://link.example.dk/api/system-link' >> .env

docker compose --env-file .env \
  -f compose/sbr-system-link/compose.yml \
  up -d --build

curl -fsS http://127.0.0.1:8098/health
```

`MASTER` må aldrig lægges i installeren eller GitHub. `PROVISION` er den engangskode, der skal indsprøjtes i den konkrete Windows-installer. Når den installer har provisioneret én pc, afviser receiveren genbrug af samme token.

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

Cloudflared-containeren skal være tilsluttet Docker-netværket `proxy`. Der skal **ikke** åbnes port 8098 i routeren.

De offentlige endpoints bliver derefter typisk:

```text
POST https://link.example.dk/api/provision
POST https://link.example.dk/api/system-link
```

## Windows release-build

Build-workflowet læser disse GitHub Actions secrets og skriver dem ind i `build_config.py` umiddelbart før PyInstaller bygger EXE'en:

```text
SBR_SYSTEM_LINK_PROVISION_URL=https://link.example.dk/api/provision
SBR_SYSTEM_LINK_MESSAGE_ENDPOINT=https://link.example.dk/api/system-link
SBR_SYSTEM_LINK_PROVISION_TOKEN=<engangskoden fra Pi'en>
```

Ingen af disse værdier commits til Git-repoet. Den permanente `SBR_SYSTEM_LINK_MASTER_SECRET` må aldrig være en GitHub Actions secret til Windows-builden.

## Data

Alarmdata gemmes i Docker-volume `sbr_system_link_data` i SQLite-databasen `/data/system-link.db`.

Receiveren har blandt andet tabellerne:

- `installations` – registrerede klient-ID'er, version/maskinenavn, sidste aktivitet og revocation-status
- `provision_tokens` – hashes af allerede forbrugte engangskoder
- `deliveries` – alarmtekst og hændelsesmetadata, inkl. klient-ID

Health endpoint:

```text
GET /health
```
