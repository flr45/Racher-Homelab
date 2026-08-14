#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${PAGER_GATEWAY_ENV:-/etc/racher-pager/gateway.env}"
[[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }
RUNTIME_REPO="${PAGER_RUNTIME_REPO:-/opt/racher-pager/runtime-repo}"
DEPLOY_BRANCH="${PAGER_DEPLOY_BRANCH:-main}"
STATE_ROOT="${PAGER_DATA_HOST_PATH:-/var/lib/racher-pager}"
UPDATE_DIR="$STATE_ROOT/update"
INTEGRATION_DIR="${PAGER_INTEGRATION_DIR:-/opt/racher-pager/integration}"
COMPOSE_SCRIPT="$INTEGRATION_DIR/pager-compose.sh"
BACKUP_SCRIPT="$INTEGRATION_DIR/backup-pager.sh"
LOCK_FILE="/run/racher-pager-update.lock"

if [[ "$EUID" -ne 0 ]]; then
  echo "update-pager.sh skal køre som root via host-agenten." >&2
  exit 1
fi
if [[ ! -d "$RUNTIME_REPO/.git" ]]; then
  echo "Runtime-repository mangler: $RUNTIME_REPO" >&2
  exit 1
fi

exec 9>"$LOCK_FILE"
flock -n 9 || { echo "En update/rollback kører allerede." >&2; exit 1; }
mkdir -p "$UPDATE_DIR"

CURRENT="$(git -C "$RUNTIME_REPO" rev-parse HEAD)"
git -C "$RUNTIME_REPO" fetch --prune origin "$DEPLOY_BRANCH"
TARGET="$(git -C "$RUNTIME_REPO" rev-parse FETCH_HEAD)"
if [[ "$CURRENT" == "$TARGET" ]]; then
  echo "Gateway er allerede opdateret: ${CURRENT:0:12}"
  exit 0
fi

if ! git -C "$RUNTIME_REPO" merge-base --is-ancestor "$CURRENT" "$TARGET"; then
  echo "Remote branch er ikke en fast-forward fra nuværende version. Brug rollback eller manuel deployment." >&2
  exit 1
fi

[[ -x "$BACKUP_SCRIPT" ]] && "$BACKUP_SCRIPT"
printf '%s\n' "$CURRENT" > "$UPDATE_DIR/previous-sha"
printf '%s\n' "$CURRENT" > "$UPDATE_DIR/current-sha"

rollback_failed_update() {
  local rc=$?
  trap - ERR
  set +e
  echo "Ny version fejlede; ruller automatisk tilbage til ${CURRENT:0:12}." >&2
  git -C "$RUNTIME_REPO" reset --hard "$CURRENT"
  "$COMPOSE_SCRIPT" build pager-gateway
  "$COMPOSE_SCRIPT" up -d --remove-orphans
  printf '%s\n' "$CURRENT" > "$UPDATE_DIR/current-sha"
  exit "$rc"
}
trap 'rollback_failed_update' ERR

git -C "$RUNTIME_REPO" reset --hard "$TARGET"
python3 -m py_compile \
  "$RUNTIME_REPO/services/pager-gateway/app.py" \
  "$RUNTIME_REPO/services/pager-gateway/storage.py" \
  "$RUNTIME_REPO/services/pager-gateway/system_agent.py" \
  "$RUNTIME_REPO/services/pager-gateway/network_portal.py"
for script in "$RUNTIME_REPO/services/pager-gateway/"*.sh "$RUNTIME_REPO/services/pager-gateway/pdl/"*.sh; do
  bash -n "$script"
done

"$COMPOSE_SCRIPT" build --pull pager-gateway
"$COMPOSE_SCRIPT" up -d --remove-orphans

PORT="${PAGER_GATEWAY_PORT:-8088}"
READY=0
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then READY=1; break; fi
  sleep 2
done
[[ "$READY" == "1" ]]

REPO_ROOT="$RUNTIME_REPO" \
PAGER_RUNTIME_REPO="$RUNTIME_REPO" \
PAGER_DATA_HOST_PATH="$STATE_ROOT" \
PAGER_BACKUP_DIR="${PAGER_BACKUP_DIR:-/var/backups/racher-pager}" \
  bash "$RUNTIME_REPO/services/pager-gateway/pdl/install-system-agent.sh"

printf '%s\n' "$TARGET" > "$UPDATE_DIR/current-sha"
date -u +%Y-%m-%dT%H:%M:%SZ > "$UPDATE_DIR/last-update"
trap - ERR

if command -v systemd-run >/dev/null 2>&1; then
  systemd-run --unit=racher-pager-agent-update-restart --on-active=3s \
    /usr/bin/systemctl restart racher-pager-system-agent.service >/dev/null 2>&1 || true
fi

echo "Gateway opdateret: ${CURRENT:0:12} -> ${TARGET:0:12}"
