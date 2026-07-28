# Vagtbytte på Racher HomeLab

Vagtbytte består af to containere, der bruger samme Docker-image:

- `vagtbytte-web`: Next.js-webapp på port 3000
- `vagtbytte-worker`: behandler planlagte notifikationer

Begge forbinder til den fælles PostgreSQL-container på Docker-netværket `backend`.

## Første installation

Kør først core- og data-stacks:

```bash
docker compose --env-file .env -f compose/core/compose.yml up -d
docker compose --env-file .env -f compose/data/compose.yml up -d
```

Start derefter Vagtbytte:

```bash
docker compose --env-file .env -f compose/vagtbytte/compose.yml pull
docker compose --env-file .env -f compose/vagtbytte/compose.yml up -d
```

Webcontaineren kører automatisk `prisma migrate deploy` før appen starter.

## Første admin- og VC-konto

Når webcontaineren er sund, køres bootstrap én gang:

```bash
docker compose --env-file .env -f compose/vagtbytte/compose.yml exec vagtbytte-web node scripts/production-bootstrap.mjs
```

Kontroller derefter login, og fjern bootstrap-adgangskoderne fra den lokale `.env`.

## VAPID-nøgler

Nøgler kan genereres fra Vagtbytte-repositoryet med:

```bash
npm run notifications:generate-keys
```

Den offentlige nøgle placeres i `VAGTBYTTE_VAPID_PUBLIC_KEY`, og den private nøgle placeres i `VAGTBYTTE_VAPID_PRIVATE_KEY`.

## Nginx Proxy Manager

Opret en Proxy Host med:

- Forward hostname: `vagtbytte-web`
- Forward port: `3000`
- Scheme: `http`
- Websockets: aktiveret
- SSL: Let's Encrypt og Force SSL

## Kontrol

```bash
docker compose --env-file .env -f compose/vagtbytte/compose.yml ps
docker compose --env-file .env -f compose/vagtbytte/compose.yml logs -f vagtbytte-web
docker compose --env-file .env -f compose/vagtbytte/compose.yml logs -f vagtbytte-worker
```

Healthcheck:

```text
https://DIT-DOMÆNE/api/health
```

## Opdatering

```bash
docker compose --env-file .env -f compose/vagtbytte/compose.yml pull
docker compose --env-file .env -f compose/vagtbytte/compose.yml up -d
```

Databasen skal sikkerhedskopieres før større opdateringer og migrationer.
