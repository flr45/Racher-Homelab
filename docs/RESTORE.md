# Restore Center – driftsprocedure

Denne procedure gendanner aldrig direkte oven i aktive data. Racher OS validerer først backupen og kan derefter stage Control Center-data i en ny isoleret Docker-volume.

## 1. Opret en backup

```bash
cd ~/homelab/Racher-Homelab
./scripts/backup.sh
```

En komplet backup indeholder mindst:

- `MANIFEST.json`
- `SHA256SUMS`
- `postgres.dump`
- `npm-database.sql.gz`
- `control-center-data.tar.gz`

Control Center-arkivet indeholder SQLite-databasen med metrics, audit, events, notifikationer og deploymenthistorik.

## 2. Valider backupen

```bash
./scripts/restore.sh 2026-01-01_12-00-00 --dry-run
```

Dry-run kontrollerer:

- at backupnavnet ikke kan bryde ud af backupmappen
- at manifest og checksumliste findes
- SHA-256 for alle registrerede filer
- gzip-integriteten for Nginx Proxy Manager-dumpet
- tar-integriteten for Control Center-data
- PostgreSQL-dumpet med `pg_restore --list`, når værktøjet findes lokalt

Ingen data ændres under dry-run.

## 3. Stage Control Center-data

```bash
./scripts/restore.sh 2026-01-01_12-00-00 --stage-control-center
```

Scriptet opretter en ny volume med navnet:

```text
racher-control-center-restore-<backup-navn>
```

Backupen pakkes ud i denne volume. Den aktive `racher-control-center_control-center-data` ændres ikke.

## 4. Inspicér staging-volumen

```bash
docker run --rm \
  -v racher-control-center-restore-2026-01-01_12-00-00:/restore:ro \
  alpine:3.20 \
  find /restore -maxdepth 2 -type f -ls
```

Kontrollér især, at `racher-os.db` findes og har en realistisk størrelse.

## 5. Cutover

Cutover udføres som en særskilt vedligeholdelseshandling med planlagt nedetid. Den aktive volume må først erstattes, når:

1. backupen er valideret
2. staging-data er inspiceret
3. der findes en ny backup af den aktuelle driftstilstand
4. Control Center og workeren er stoppet
5. rollback-planen er dokumenteret

Automatisk overskrivning af aktive data er bevidst ikke en del af restore-scriptet.

## Opdatering af stacks

`scripts/update-stacks.sh` validerer nu Compose-konfigurationen før opdatering og inkluderer `compose/control-center`.

```bash
./scripts/update-stacks.sh
```

Efter opdatering skal følgende kontrolleres:

```bash
docker compose --env-file .env -f compose/control-center/compose.yml ps
./scripts/restore.sh latest --dry-run
```
