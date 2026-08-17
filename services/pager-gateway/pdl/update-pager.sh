#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${PAGER_GATEWAY_ENV:-/etc/racher-pager/gateway.env}"
[[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }
RUNTIME_REPO="${PAGER_RUNTIME_REPO:-/opt/racher-pager/runtime-repo}"
DEPLOY_BRANCH="${PAGER_DEPLOY_BRANCH:-main}"
STATE_ROOT="${PAGER_DATA_HOST_PATH:-/var/lib/racher-pager}"
UPDATE_DIR="$STATE_ROOT/update"
INTEGRATION_DIR="${PAGER_INTEGRATION_DIR:-/opt/racher-pager/integration}"
NETWORK_DIR="${PAGER_NETWORK_INSTALL_DIR:-/opt/racher-pager/network}"
COMPOSE_SCRIPT="$INTEGRATION_DIR/pager-compose.sh"
BACKUP_SCRIPT="$INTEGRATION_DIR/backup-pager.sh"
LOCK_FILE="${PAGER_MAINTENANCE_LOCK:-/run/racher-pager/maintenance.lock}"
PDL_BINARY="${PDL_BINARY:-/opt/racher-pager/pdl/bin/pdl}"
PDL_BACKUP="$UPDATE_DIR/pdl-before-update"
UPDATE_LOG="$UPDATE_DIR/last-update.log"

if [[ "$EUID" -ne 0 ]]; then
  echo "update-pager.sh skal køre som root via host-agenten." >&2
  exit 1
fi
if [[ ! -d "$RUNTIME_REPO/.git" ]]; then
  echo "Runtime-repository mangler: $RUNTIME_REPO" >&2
  exit 1
fi

mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "En update/rollback/restore kører allerede." >&2; exit 1; }
mkdir -p "$UPDATE_DIR"

# Keep one complete local transcript. The host-agent captures stdout, so merge
# stderr into stdout here as well; a failed update must never hide the command
# that triggered the automatic rollback.
exec > >(tee "$UPDATE_LOG") 2>&1

step() {
  printf '\n[update] %s\n' "$1"
}

CURRENT="$(git -C "$RUNTIME_REPO" rev-parse HEAD)"
step "Henter $DEPLOY_BRANCH fra origin (nuværende ${CURRENT:0:12})"
git -C "$RUNTIME_REPO" fetch --prune origin "$DEPLOY_BRANCH"
TARGET="$(git -C "$RUNTIME_REPO" rev-parse FETCH_HEAD)"

if [[ "$CURRENT" == "$TARGET" ]]; then
  printf '%s\n' "$CURRENT" > "$UPDATE_DIR/current-sha"
  echo "Gateway er allerede opdateret: ${CURRENT:0:12}"
  exit 0
fi

if ! git -C "$RUNTIME_REPO" merge-base --is-ancestor "$CURRENT" "$TARGET"; then
  echo "Remote branch er ikke en fast-forward fra nuværende version. Brug rollback eller manuel deployment." >&2
  exit 1
fi

PDL_CHANGED=0
if ! git -C "$RUNTIME_REPO" diff --quiet "$CURRENT" "$TARGET" -- \
  services/pager-gateway/pdl/patch_headless.py \
  services/pager-gateway/pdl/install-pdl.sh; then
  PDL_CHANGED=1
fi

step "Laver sikkerhedsbackup"
[[ -x "$BACKUP_SCRIPT" ]] && "$BACKUP_SCRIPT"
printf '%s\n' "$CURRENT" > "$UPDATE_DIR/previous-sha"
printf '%s\n' "$CURRENT" > "$UPDATE_DIR/current-sha"

restore_host_runtime_from_checkout() {
  local checkout="$1"
  local service_dir="$checkout/services/pager-gateway"
  local pdl_dir="$service_dir/pdl"

  # Older releases of install-system-agent.sh did not copy these host-executed
  # files. Copy them explicitly so an automatic rollback restores the complete
  # appliance, not just the Docker image and git checkout.
  [[ -f "$pdl_dir/configure-pdl.sh" ]] && install -m 0755 "$pdl_dir/configure-pdl.sh" "$INTEGRATION_DIR/configure-pdl.sh"
  [[ -f "$pdl_dir/run-pdl-headless.sh" ]] && install -m 0755 "$pdl_dir/run-pdl-headless.sh" "$INTEGRATION_DIR/run-pdl-headless.sh"
  if [[ -f "$service_dir/network_portal.py" && -d "$NETWORK_DIR" ]]; then
    install -m 0755 "$service_dir/network_portal.py" "$NETWORK_DIR/network_portal.py"
  fi

  if [[ -f "$pdl_dir/install-system-agent.sh" ]]; then
    REPO_ROOT="$checkout" \
    PAGER_RUNTIME_REPO="$checkout" \
    PAGER_DATA_HOST_PATH="$STATE_ROOT" \
    PAGER_BACKUP_DIR="${PAGER_BACKUP_DIR:-/var/backups/racher-pager}" \
      bash "$pdl_dir/install-system-agent.sh"
  fi
  systemctl try-restart racher-pdl.service >/dev/null 2>&1 || true
  systemctl try-restart racher-pager-network-portal.service >/dev/null 2>&1 || true
}

rollback_failed_update() {
  local rc="$1"
  local line="$2"
  local command="$3"
  trap - ERR
  set +e
  echo >&2
  echo "[update] FEJL exit=$rc linje=$line kommando=$command" >&2
  echo "Ny version fejlede; ruller automatisk hele Pager-runtime tilbage til ${CURRENT:0:12}." >&2
  git -C "$RUNTIME_REPO" reset --hard "$CURRENT"

  if [[ -f "$PDL_BACKUP" ]]; then
    echo "[update] Gendanner tidligere PDL-binary." >&2
    install -m 0755 "$PDL_BACKUP" "$PDL_BINARY"
  fi

  "$COMPOSE_SCRIPT" build pager-gateway
  "$COMPOSE_SCRIPT" up -d --remove-orphans
  restore_host_runtime_from_checkout "$RUNTIME_REPO" || true
  printf '%s\n' "$CURRENT" > "$UPDATE_DIR/current-sha"
  exit "$rc"
}
trap 'rollback_failed_update "$?" "$LINENO" "$BASH_COMMAND"' ERR

step "Skifter runtime til ${TARGET:0:12}"
git -C "$RUNTIME_REPO" reset --hard "$TARGET"

step "Validerer Python og shell"
python3 -m py_compile \
  "$RUNTIME_REPO/services/pager-gateway/app.py" \
  "$RUNTIME_REPO/services/pager-gateway/app_core.py" \
  "$RUNTIME_REPO/services/pager-gateway/gateway.py" \
  "$RUNTIME_REPO/services/pager-gateway/push_service.py" \
  "$RUNTIME_REPO/services/pager-gateway/storage.py" \
  "$RUNTIME_REPO/services/pager-gateway/system_agent.py" \
  "$RUNTIME_REPO/services/pager-gateway/network_portal.py" \
  "$RUNTIME_REPO/services/pager-gateway/gateway_watchdog.py" \
  "$RUNTIME_REPO/services/pager-gateway/fsk_status_agent.py" \
  "$RUNTIME_REPO/services/pager-gateway/external_monitor.py" \
  "$RUNTIME_REPO/services/pager-gateway/pdl/seed-pdl-cursor.py"
for script in "$RUNTIME_REPO/services/pager-gateway/"*.sh "$RUNTIME_REPO/services/pager-gateway/pdl/"*.sh; do
  bash -n "$script"
done

if [[ "$PDL_CHANGED" == "1" ]]; then
  step "PDL-patch ændret; genbygger decoder"
  if [[ -f "$PDL_BINARY" ]]; then
    cp -a "$PDL_BINARY" "$PDL_BACKUP"
  else
    rm -f "$PDL_BACKUP"
  fi
  bash "$RUNTIME_REPO/services/pager-gateway/pdl/install-pdl.sh"
  systemctl restart racher-pdl.service
fi

# Cursor-based tailing is introduced without replaying the historic PDL log. If
# this appliance does not have a cursor yet, snapshot the current EOF immediately
# before replacing the web process. Lines PDL writes after this point are then
# picked up by the new gateway after its restart.
step "Sikrer PDL læseposition"
python3 "$RUNTIME_REPO/services/pager-gateway/pdl/seed-pdl-cursor.py" "$STATE_ROOT/pdl.log"

step "Bygger ny Pager Gateway-container"
"$COMPOSE_SCRIPT" build --pull pager-gateway
"$COMPOSE_SCRIPT" up -d --remove-orphans

PORT="${PAGER_GATEWAY_PORT:-8088}"
step "Venter på gateway healthcheck på port $PORT"
READY=0
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then READY=1; break; fi
  sleep 2
done
if [[ "$READY" != "1" ]]; then
  echo "Gateway healthcheck blev ikke klar inden for 120 sekunder." >&2
  false
fi

step "Opdaterer host-agent og recovery-helpers"
REPO_ROOT="$RUNTIME_REPO" \
PAGER_RUNTIME_REPO="$RUNTIME_REPO" \
PAGER_DATA_HOST_PATH="$STATE_ROOT" \
PAGER_BACKUP_DIR="${PAGER_BACKUP_DIR:-/var/backups/racher-pager}" \
  bash "$RUNTIME_REPO/services/pager-gateway/pdl/install-system-agent.sh"

step "Verificerer appliance recovery-lag"
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null
systemctl is-active --quiet racher-pager-system-agent.service
systemctl is-active --quiet racher-pager-gateway-watchdog.timer
systemctl is-active --quiet racher-pdl.service

printf '%s\n' "$TARGET" > "$UPDATE_DIR/current-sha"
date -u +%Y-%m-%dT%H:%M:%SZ > "$UPDATE_DIR/last-update"
rm -f "$PDL_BACKUP"
trap - ERR

if command -v systemd-run >/dev/null 2>&1; then
  # Run outside this update process/cgroup after the maintenance command has
  # returned. Restarting the privileged agent applies its new UMask, then the new
  # compose helper repairs any legacy SQLite sidecar ownership and recreates the
  # web container with the unprivileged state-directory uid/gid. This avoids the
  # upgrade race where an old root agent could create a root-owned WAL after the
  # first rootless container start.
  systemd-run --unit=racher-pager-post-update --on-active=3s \
    /bin/bash -c "/usr/bin/systemctl restart racher-pager-system-agent.service; '$COMPOSE_SCRIPT' up -d --force-recreate pager-gateway" \
    >/dev/null 2>&1 || true
fi

echo
echo "Gateway opdateret: ${CURRENT:0:12} -> ${TARGET:0:12}"