# Installation på Raspberry Pi 5

## 1. Raspberry Pi Imager

Installér **Raspberry Pi OS Lite 64-bit** på NVMe-disken. I Imager vælges:

- værtsnavn: `racher-homelab`
- SSH aktiveret med nøgle eller stærk adgangskode
- bruger oprettet
- tidszone: `Europe/Copenhagen`
- Wi-Fi kun hvis kabel ikke bruges

Brug helst fast netværkskabel og reserver Pi'ens IP-adresse i routeren.

## 2. Første login

```bash
ssh <bruger>@racher-homelab.local
```

## 3. Hent repository og kør bootstrap

```bash
git clone https://github.com/flr45/Racher-Homelab.git ~/homelab/Racher-Homelab
cd ~/homelab/Racher-Homelab
chmod +x scripts/*.sh
./scripts/bootstrap.sh
```

Log ud og ind igen bagefter.

## 4. Opret miljøfil

```bash
cd ~/homelab/Racher-Homelab
cp .env.example .env
nano .env
```

Udskift alle `CHANGE_ME`-værdier med lange, unikke adgangskoder. `.env` må aldrig pushes til GitHub.

## 5. Start kernetjenester

```bash
docker compose --env-file .env -f compose/core/compose.yml up -d
```

Portainer åbnes første gang på:

```text
https://<PI-IP>:9443
```

Nginx Proxy Manager åbnes på:

```text
http://<PI-IP>:81
```

Skift standard-login med det samme.

## 6. Start datatjenester

```bash
docker compose --env-file .env -f compose/data/compose.yml up -d
```

PostgreSQL og Redis eksponeres ikke direkte på hjemmenetværket. Apps skal forbindes gennem Docker-netværket `backend`.

## 7. Minutregnskab

Minutregnskab-filen forventer et container-image i GitHub Container Registry. Når app-repository og image er klar:

```bash
docker compose --env-file .env -f compose/minutregnskab/compose.yml up -d
```

I Nginx Proxy Manager oprettes en Proxy Host med destination:

- Forward hostname: `minutregnskab`
- Forward port: `8000`
- Websockets: aktiveret
- SSL: Let's Encrypt og Force SSL

## 8. Backup

Manuel backup:

```bash
./scripts/backup.sh
```

Cron-eksempel hver nat kl. 03:15:

```cron
15 3 * * * /home/<bruger>/homelab/Racher-Homelab/scripts/backup.sh >> /home/<bruger>/homelab/backup.log 2>&1
```

Backup på samme SSD beskytter ikke mod diskfejl. Kopiér senere backups til en ekstern disk eller anden maskine.

## 9. Opdatering

```bash
./scripts/update-stacks.sh
```

Kontrollér altid Uptime Kuma og relevante apps efter opdatering.
