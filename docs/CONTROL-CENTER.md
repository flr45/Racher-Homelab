# Racher Control Center

Control Center er et mobilvenligt statusdashboard til Raspberry Pi-homelabbet.

Første version viser:

- CPU-, RAM- og diskforbrug
- Raspberry Pi-temperatur og oppetid
- Docker-containere, status, image og healthcheck
- seneste backup fundet i backupmappen
- hurtige links til apps og administrationsværktøjer

Dashboardet er bevidst skrivebeskyttet. Det kan ikke genstarte containere eller starte backups i første version.

## Sikkerhed

Dashboardet læser Docker-status gennem Docker-socketten. Selv en read-only mount af socketfilen skal behandles som følsom adgang. Control Center bør derfor:

- ligge bag Nginx Proxy Manager
- beskyttes med Access List eller anden loginløsning
- ikke eksponeres direkte på en host-port
- kun være tilgængeligt lokalt eller via Tailscale, indtil login er konfigureret

## Image

Når ændringer til `apps/control-center` merges til `main`, bygger GitHub Actions automatisk image til både ARM64 og AMD64:

```text
ghcr.io/flr45/racher-homelab/control-center:latest
```

## Start

Opdatér først den lokale repositorykopi og `.env`:

```bash
cd ~/homelab/Racher-Homelab
git pull --ff-only
nano .env
```

Start derefter stacken:

```bash
docker compose --env-file .env -f compose/control-center/compose.yml pull
docker compose --env-file .env -f compose/control-center/compose.yml up -d
```

Kontrollér:

```bash
docker compose --env-file .env -f compose/control-center/compose.yml ps
docker logs control-center --tail 100
```

## Nginx Proxy Manager

Opret en Proxy Host med:

- Forward hostname: `control-center`
- Forward port: `8080`
- Scheme: `http`
- SSL: Let's Encrypt og Force SSL
- Access List: påkrævet

Et muligt domæne er `control.racher.dk`.

## Automatisk opdatering

Containeren har Watchtower-label og opdateres derfor automatisk efter det natlige backupvindue, når et nyt `latest`-image er publiceret.

## Næste version

Mulige udvidelser:

- historiske grafer
- Uptime Kuma-status via API
- certifikatudløb
- diskplads- og temperaturalarmer
- autentificerede administrative handlinger
- kontrolleret backup nu og genstart af udvalgte apps
