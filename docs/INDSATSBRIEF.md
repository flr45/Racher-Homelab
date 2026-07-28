# Indsatsbrief på Racher Homelab

## Forudsætninger

Core- og data-stakken skal være startet, så de eksterne Docker-netværk `proxy` og `backend` findes, og PostgreSQL-containeren kører.

## Opsæt miljøvariabler

Kopiér `.env.example` til `.env` i repository-roden og udfyld mindst:

- `POSTGRES_PASSWORD`
- `INDSATSBRIEF_FLASK_SECRET_KEY`
- `INDSATSBRIEF_ACCESS_CODE`
- `INDSATSBRIEF_OPENAI_API_KEY`
- `INDSATSBRIEF_APP_BASE_URL`

Lav en stærk Flask-hemmelighed med:

```bash
openssl rand -hex 32
```

Den rigtige `.env` må aldrig pushes til GitHub.

## Start Indsatsbrief

Fra repository-roden:

```bash
docker compose --env-file .env -f compose/indsatsbrief/compose.yml pull
docker compose --env-file .env -f compose/indsatsbrief/compose.yml up -d
```

Se status og logs:

```bash
docker compose --env-file .env -f compose/indsatsbrief/compose.yml ps
docker compose --env-file .env -f compose/indsatsbrief/compose.yml logs -f indsatsbrief
```

## Nginx Proxy Manager

Opret en Proxy Host med:

- Domain Name: `indsatsbrief.racher.dk`
- Scheme: `http`
- Forward Hostname/IP: `indsatsbrief`
- Forward Port: `8000`
- Websockets Support: slået til
- Block Common Exploits: slået til

Under SSL vælges et nyt Let's Encrypt-certifikat, `Force SSL` og `HTTP/2 Support`.

## Opdatering

```bash
docker compose --env-file .env -f compose/indsatsbrief/compose.yml pull
docker compose --env-file .env -f compose/indsatsbrief/compose.yml up -d
```

## Fejlsøgning

Kontrollér først containerens health-status:

```bash
docker inspect --format='{{json .State.Health}}' indsatsbrief
```

Kontrollér derefter forbindelsen til PostgreSQL og de relevante miljøvariabler i containerens logs. Vis aldrig API-nøgler eller adgangskoder i screenshots eller delte logs.
