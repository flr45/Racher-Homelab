# Automatiske opdateringer med Watchtower

Watchtower kontrollerer hver nat, om der findes nye Docker-images til de apps, der udtrykkeligt er mærket til automatisk opdatering.

Aktiverede apps:

- Minutregnskab
- Vagtbytte web
- Vagtbytte worker
- Indsatsbrief

Databaser, Nginx Proxy Manager, Portainer og øvrig kerneinfrastruktur opdateres ikke automatisk.

## Start Watchtower

Kør fra roden af `Racher-Homelab`:

```bash
docker compose --env-file .env -f compose/watchtower/compose.yml up -d
```

Kontrollér status:

```bash
docker compose -f compose/watchtower/compose.yml ps
docker logs watchtower --tail 100
```

## Standardtidspunkt

Standardplanen er hver nat kl. 04:00 dansk tid:

```env
WATCHTOWER_SCHEDULE=0 0 4 * * *
```

Watchtower bruger et cron-format med seks felter, hvor det første felt er sekunder.

## Sådan virker det

1. GitHub Actions bygger og publicerer et nyt `latest`-image i GHCR.
2. Watchtower opdager den nye image-version ved næste kontrol.
3. Det nye image downloades.
4. Den berørte container genstartes.
5. Det gamle lokale image ryddes op.

Kun containere med denne label opdateres:

```yaml
labels:
  com.centurylinklabs.watchtower.enable: "true"
```

## Manuel kontrol med det samme

Kør en engangskontrol uden at ændre den faste tidsplan:

```bash
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  containrrr/watchtower:1.7.1 \
  --run-once \
  --label-enable \
  --cleanup
```

## Notifikationer

`WATCHTOWER_NOTIFICATION_URL` kan sættes til en Shoutrrr-kompatibel URL, eksempelvis Discord, Gotify eller SMTP. Lad feltet være tomt, indtil notifikationer er konfigureret.

Hemmeligheder må kun ligge i den lokale `.env` og må aldrig pushes til GitHub.

## Begrænsning

Watchtower genstarter containere med det nye image, men laver ikke automatisk rollback, hvis den nye applikationsversion er defekt. Uptime Kuma og container-healthchecks bruges til at opdage fejl; automatisk rollback kan tilføjes som et særskilt deployment-trin senere.
