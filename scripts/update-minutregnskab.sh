#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="${HOMELAB_ROOT:-$REPO_ROOT}"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
COMPOSE_FILE="$ROOT/compose/minutregnskab/compose.yml"
NEW_IMAGE="${1:-}"

cd "$ROOT"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Miljøfilen mangler: $ENV_FILE" >&2
  exit 1
fi

if [[ ! "$NEW_IMAGE" =~ ^ghcr\.io/flr45/minutregnskab:sha-[0-9a-f]{40}$ ]]; then
  cat >&2 <<'EOF'
Brug et immutable Minutregnskab-image med fuld commit-SHA.

Eksempel:
  scripts/update-minutregnskab.sh \
    ghcr.io/flr45/minutregnskab:sha-0123456789abcdef0123456789abcdef01234567

:latest accepteres med vilje ikke.
EOF
  exit 1
fi

for key in MINUTREGNSKAB_SECRET_KEY; do
  value="$(sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n 1)"
  if [[ -z "$value" || "$value" == CHANGE_ME* || ${#value} -lt 32 ]]; then
    echo "$key mangler, bruger en placeholder eller er kortere end 32 tegn" >&2
    exit 1
  fi
done

timestamp="$(date +%Y%m%d-%H%M%S)"
backup_root="${HOME}/homelab/manual-backups"
mkdir -p "$backup_root"
db_backup="$backup_root/minutregnskab-pre-update-$timestamp.db"
env_backup="$backup_root/minutregnskab-env-pre-update-$timestamp"

cp "$ENV_FILE" "$env_backup"
chmod 600 "$env_backup"

if docker ps --format '{{.Names}}' | grep -qx minutregnskab; then
  echo "Tager konsistent SQLite-backup..."
  docker exec minutregnskab python -c '
import sqlite3
src = sqlite3.connect("/app/data/minutregnskab.db")
dst = sqlite3.connect("/app/data/.pre-update-backup.db")
src.backup(dst)
dst.close()
src.close()
'
  docker cp minutregnskab:/app/data/.pre-update-backup.db "$db_backup"
  docker exec minutregnskab rm -f /app/data/.pre-update-backup.db
else
  echo "Minutregnskab-containeren kører ikke; afbryder før opdatering." >&2
  exit 1
fi

python3 - "$db_backup" <<'PY'
import sqlite3
import sys

path = sys.argv[1]
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
result = con.execute("PRAGMA integrity_check").fetchone()[0]
con.close()
if result != "ok":
    raise SystemExit(f"Backup-integritet fejlede: {result}")
print(f"Backup OK: {path}")
PY

current_image="$(docker inspect minutregnskab --format '{{.Config.Image}}' 2>/dev/null || true)"
echo "Nuværende image: ${current_image:-ukendt}"
echo "Nyt image:       $NEW_IMAGE"

python3 - "$ENV_FILE" "$NEW_IMAGE" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
new_image = sys.argv[2]
lines = path.read_text().splitlines()
key = "MINUTREGNSKAB_IMAGE"
entry = f"{key}={new_image}"

for i, line in enumerate(lines):
    if line.startswith(key + "="):
        lines[i] = entry
        break
else:
    lines.append(entry)

path.write_text("\n".join(lines) + "\n")
PY
chmod 600 "$ENV_FILE"

rollback() {
  echo "Opdateringen fejlede. Ruller image-konfigurationen tilbage..." >&2
  cp "$env_backup" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --force-recreate || true
  echo "Databasebackup er bevaret her: $db_backup" >&2
}

echo "Validerer Compose..."
if ! docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config --quiet; then
  rollback
  exit 1
fi

echo "Henter pinnet image..."
if ! docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" pull; then
  rollback
  exit 1
fi

echo "Starter ny version..."
if ! docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --force-recreate; then
  rollback
  exit 1
fi

healthy=false
for _ in $(seq 1 45); do
  state="$(docker inspect minutregnskab --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' 2>/dev/null || true)"
  if [[ "$state" == "healthy" ]]; then
    healthy=true
    break
  fi
  if [[ "$state" == "unhealthy" || "$state" == "exited" || "$state" == "dead" ]]; then
    break
  fi
  sleep 2
done

if [[ "$healthy" != true ]]; then
  echo "Den nye container blev ikke healthy." >&2
  docker logs --tail=80 minutregnskab >&2 || true
  rollback
  exit 1
fi

echo
echo "Minutregnskab er opdateret og healthy."
docker inspect minutregnskab --format 'Image: {{.Config.Image}} | Health: {{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}'
echo "Backup: $db_backup"
