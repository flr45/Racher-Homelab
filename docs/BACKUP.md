# Automatisk backup

Racher HomeLab tager en natlig backup kl. 03:15, før Watchtower kontrollerer app-opdateringer kl. 04:00.

Backup indeholder:

- konsistent PostgreSQL-dump i custom-format
- konsistent MariaDB-dump til Nginx Proxy Manager
- konsistent SQLite-backup af Control Center
- øvrige Control Center-filer
- Nginx Proxy Manager-data og certifikater
- Portainer-data
- Uptime Kuma-data
- Redis-data
- en kopi af den lokale `.env`
- SHA-256-kontrolsummer for alle filer

PostgreSQL, MariaDB og SQLite sikkerhedskopieres konsistent, mens tjenesterne kører. Rå databasefiler kopieres ikke direkte.

## Konfiguration

Tilpas disse værdier i den lokale `.env`:

```env
BACKUP_ROOT=/home/<brugernavn>/homelab/backups
BACKUP_RETENTION_DAYS=14
BACKUP_MIRROR_DIR=
```

Brug den faktiske Linux-bruger i `BACKUP_ROOT`. På standardinstallationen med brugeren `racher` er stien:

```env
BACKUP_ROOT=/home/racher/homelab/backups
```

`BACKUP_MIRROR_DIR` kan senere pege på en monteret USB-disk eller netværksmappe.

Backup på samme NVMe-disk beskytter ikke mod diskfejl. En ekstern kopi bør derfor aktiveres, når en ekstra disk eller NAS er klar.

## Manuel test

Fra repositoryets rod:

```bash
chmod +x scripts/*.sh
./scripts/backup.sh
```

Kontrollér den seneste backup:

```bash
cd "$(readlink -f "$HOME/homelab/backups/latest")"
sha256sum -c SHA256SUMS
ls -lah
```

## Installér automatisk kørsel

```bash
./scripts/install-backup-timer.sh
```

Kontrollér timeren:

```bash
systemctl status racher-homelab-backup.timer
systemctl list-timers racher-homelab-backup.timer
```

Kør backup manuelt gennem systemd:

```bash
sudo systemctl start racher-homelab-backup.service
```

Se loggen:

```bash
journalctl -u racher-homelab-backup.service -n 100 --no-pager
```

Timeren bruger `Persistent=true`. Hvis Raspberry Pi'en er slukket kl. 03:15, køres den manglende backup efter næste opstart.

## Gendannelse af PostgreSQL

Stop først de apps, der bruger databasen. Opret altid en ny backup, før en eksisterende database overskrives.

Eksempel:

```bash
docker compose --env-file .env -f compose/vagtbytte/compose.yml down
docker compose --env-file .env -f compose/indsatsbrief/compose.yml down

source .env
docker exec postgres dropdb --username "$POSTGRES_USER" --if-exists "$POSTGRES_DB"
docker exec postgres createdb --username "$POSTGRES_USER" "$POSTGRES_DB"
cat /sti/til/postgres.dump | docker exec -i postgres pg_restore \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --clean \
  --if-exists \
  --no-owner \
  --no-privileges
```

Start derefter apps igen og kontrollér deres healthchecks.

## Gendannelse af Nginx Proxy Manager-database

```bash
source .env
gunzip -c /sti/til/npm-database.sql.gz | docker exec -i \
  -e MYSQL_PWD="$NPM_DB_PASSWORD" \
  npm-db mariadb \
  --user="$NPM_DB_USER" \
  "$NPM_DB_NAME"
```

Certifikater og øvrige Docker-volumener bør kun gendannes, mens den berørte container er stoppet.

## Sikkerhed

Backupmappen indeholder `.env` med adgangskoder og API-nøgler. Beskyt derfor backupdisken, del ikke backupfiler offentligt, og giv ikke andre brugere læseadgang til mappen.
