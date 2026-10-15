#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="${SBR_PORTAL_COMPOSE_FILE:-$repo_root/compose/sbr-portal/compose.yml}"
env_file="${SBR_PORTAL_ENV_FILE:-$repo_root/.env}"

if [[ ! -f "$env_file" ]]; then
  echo "Miljøfilen blev ikke fundet: $env_file" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$env_file"
set +a

: "${POSTGRES_DB:?POSTGRES_DB mangler i miljøfilen}"
: "${POSTGRES_USER:?POSTGRES_USER mangler i miljøfilen}"
: "${SBR_PORTAL_DB_USER:?SBR_PORTAL_DB_USER mangler i miljøfilen}"
: "${SBR_PORTAL_DB_PASSWORD:?SBR_PORTAL_DB_PASSWORD mangler i miljøfilen}"
: "${SBR_PORTAL_AUTH_SECRET:?SBR_PORTAL_AUTH_SECRET mangler i miljøfilen}"

target_db="${SBR_PORTAL_DB_NAME:-sbr_portal}"
source_db="$POSTGRES_DB"

for identifier in "$source_db" "$target_db" "$POSTGRES_USER" "$SBR_PORTAL_DB_USER"; do
  if [[ ! "$identifier" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    echo "Ugyldigt database-id: $identifier" >&2
    exit 1
  fi
done

if [[ "$source_db" == "$target_db" ]]; then
  echo "Kilde- og måldatabase må ikke være den samme." >&2
  exit 1
fi

echo "Opretter separat database og databasebruger, hvis de ikke findes..."
docker exec -i postgres psql -v ON_ERROR_STOP=1 \
  -U "$POSTGRES_USER" -d postgres \
  -v target_role="$SBR_PORTAL_DB_USER" \
  -v target_password="$SBR_PORTAL_DB_PASSWORD" \
  -v target_db="$target_db" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'target_role', :'target_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'target_role')
\gexec
SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'target_role', :'target_password')
\gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'target_db', :'target_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'target_db')
\gexec
SQL

echo "Starter SBR Portal og kører dens databasemigrationer..."
docker compose --env-file "$env_file" -f "$compose_file" up -d sbr-portal-web

for _ in $(seq 1 60); do
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' sbr-portal-web 2>/dev/null || true)"
  [[ "$health" == "healthy" ]] && break
  [[ "$health" == "unhealthy" ]] && { docker logs --tail 100 sbr-portal-web; exit 1; }
  sleep 2
done

if [[ "$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' sbr-portal-web)" != "healthy" ]]; then
  docker logs --tail 100 sbr-portal-web
  echo "SBR Portal blev ikke klar i tide." >&2
  exit 1
fi

target_rows="$(docker exec postgres psql -At -U "$POSTGRES_USER" -d "$target_db" -c 'SELECT (SELECT count(*) FROM "User") + (SELECT count(*) FROM operational_vehicle);')"
if [[ "$target_rows" != "0" ]]; then
  echo "Måldatabasen indeholder allerede portaldata. Migreringen stoppes uden at overskrive noget." >&2
  exit 1
fi

source_volume="$(docker inspect vagtbytte-web --format '{{range .Mounts}}{{if eq .Destination "/data/operativ-portal"}}{{.Name}}{{end}}{{end}}')"
target_volume="$(docker inspect sbr-portal-web --format '{{range .Mounts}}{{if eq .Destination "/data/operativ-portal"}}{{.Name}}{{end}}{{end}}')"
if [[ -z "$source_volume" || -z "$target_volume" || "$source_volume" == "$target_volume" ]]; then
  echo "Kunne ikke identificere to separate operative datavolumener." >&2
  exit 1
fi

dump_file="$(mktemp "${TMPDIR:-/tmp}/sbr-portal-data.XXXXXX.sql")"
cleanup() {
  rm -f "$dump_file"
}
trap cleanup EXIT

tables=(
  '"User"'
  operational_vehicle
  operational_place
  operational_item
  operational_document
  operational_video
  operational_image
  operational_portal_user_access
  operational_favorite
  operational_recent
  operational_hotspot
  operational_place_hotspot
  operational_vehicle_view
  operational_interactive_node
  operational_interactive_link
)

dump_args=()
for table in "${tables[@]}"; do
  dump_args+=(--table="$table")
done

echo "Tager en læsekopi af de operative databasetabeller..."
docker exec postgres pg_dump -U "$POSTGRES_USER" -d "$source_db" \
  --data-only --disable-triggers --no-owner --no-privileges \
  "${dump_args[@]}" > "$dump_file"

echo "Stopper kun den nye portal under indlæsningen..."
docker compose --env-file "$env_file" -f "$compose_file" stop sbr-portal-web

restart_target() {
  docker compose --env-file "$env_file" -f "$compose_file" up -d sbr-portal-web >/dev/null 2>&1 || true
}
trap 'restart_target; cleanup' ERR

echo "Indlæser data i den separate portal-database..."
docker exec -i postgres psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$target_db" < "$dump_file"

echo "Kopierer billeder og dokumenter til et separat Docker-volumen..."
docker run --rm \
  -v "$source_volume:/source:ro" \
  -v "$target_volume:/target" \
  alpine:3.20 sh -eu -c '
    if find /target -mindepth 1 -print -quit | grep -q .; then
      echo "Målvolumenet er ikke tomt; stopper uden at overskrive filer." >&2
      exit 1
    fi
    cp -a /source/. /target/
    chown -R 1001:1001 /target
  '

trap cleanup EXIT
docker compose --env-file "$env_file" -f "$compose_file" up -d sbr-portal-web

echo "Kontrollerer datamængder..."
docker exec -i postgres psql -At -U "$POSTGRES_USER" -d "$target_db" <<'SQL'
SELECT 'users=' || count(*) FROM "User";
SELECT 'vehicles=' || count(*) FROM operational_vehicle;
SELECT 'places=' || count(*) FROM operational_place;
SELECT 'items=' || count(*) FROM operational_item;
SELECT 'images=' || count(*) FROM operational_image;
SELECT 'documents=' || count(*) FROM operational_document;
SQL

echo "Migreringen er færdig. Vagtbytte har været i drift under hele kopieringen."
