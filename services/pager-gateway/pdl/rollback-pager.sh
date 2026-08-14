#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${PAGER_GATEWAY_ENV:-/etc/racher-pager/gateway.env}"
[[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }
RUNTIME_REPO="${PAGER_RUNTIME_REPO:-/opt/racher-pager/runtime-repo}"
STATE_ROOT="${PAGER_DATA_HOST_PATH:-/var/lib/racher-pager}"
UPDATE_DIR="$STATE_ROOT/update"
INTEGRATION_DIR="${PAGER_INTEGRATION_DIR:-/opt/racher-pager/integration}"
COMPOSE_SCRIPT="$INTEGRATION_DIR/pager-compose.sh"
BACKUP_SCRIPT="$INTEGRATION_DIR/backup-pager.sh"
LOCK_FILE="/run/racher-pager-update.lock"

if [[ "$EUID" -ne 0 ]]; then
  echo "rollback-pager.sh skal køre som root via host-agenten." >&2
  exit 1
fi
TARGET_FILE="$UPDATE_DIR/previous-sha"
if [[ ! -s "$TARGET_FILE" ]]; then
  echo "Der findes ingen tidligere gateway-version at rulle tilbage til." >&2
  exit 1
fi
TARGET="$(tr -d '[:space:]' < "$TARGET_FILE")"
if [[ ! "$TARGET" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Ugyldig previous-sha." >&2
  exit 1
fi

exec 9>"$LOCK_FILE"
flock -n 9 || { echo "En update/rollback kører allerede." >&2; exit 1; }
CURRENT="$(git -C "$RUNTIME_REPO" rev-parse HEAD)"
git -C "$RUNTIME_REPO" cat-file -e "$TARGET^{commit}"
[[ -x "$BACKUP_SCRIPT" ]] && "$BACKUP_SCRIPT"

git -C "$RUNTIME_REPO" reset --hard "$TARGET"
"$COMPOSE_SCRIPT" build pager-gateway
"$COMPOSE_SCRIPT" up -d --remove-orphans

PORT="${PAGER_GATEWAY_PORT:-8088}"
READY=0
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then READY=1; break; fi
  sleep 2
done
if [[ "$READY" != "1" ]]; then
  echo "Rollback-versionen bestod ikke healthcheck; gendanner den version der kørte før rollback." >&2
  git -C "$RUNTIME_REPO" reset --hard "$CURRENT"
  "$COMPOSE_SCRIPT" build pager-gateway
  "$COMPOSE_SCRIPT" up -d --remove-orphans
  exit 1
fi

printf '%s\n' "$CURRENT" > "$UPDATE_DIR/previous-sha"
printf '%s\n' "$TARGET" > "$UPDATE_DIR/current-sha"
date -u +%Y-%m-%dT%H:%M:%SZ > "$UPDATE_DIR/last-rollback"

REPO_ROOT="$RUNTIME_REPO" \
PAGER_DATA_HOST_PATH="$STATE_ROOT" \
PAGER_BACKUP_DIR="${PAGER_BACKUP_DIR:-/var/backups/racher-pager}" \
  bash "$RUNTIME_REPO/services/pager-gateway/pdl/install-system-agent.sh"

if command -v systemd-run >/dev/null 2>&1; then
  systemd-run --unit=racher-pager-agent-rollback-restart --on-active=3s \
    /usr/bin/systemctl restart racher-pager-system-agent.service >/dev/null 2>&1 || true
fi

echo "Gateway rullet tilbage: ${CURRENT:0:12} -> ${TARGET:0:12}"
