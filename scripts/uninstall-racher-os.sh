#!/usr/bin/env bash
set -Eeuo pipefail

fail() { printf '[uninstall] FEJL: %s\n' "$*" >&2; exit 1; }
log() { printf '[uninstall] %s\n' "$*"; }

[[ "${EUID}" -ne 0 ]] || fail "Kør som normal bruger, ikke root."
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${RACHER_ENV_FILE:-$REPO_ROOT/.env}"
CONFIRM="${1:-}"

[[ "$CONFIRM" == "STOP-RACHER-OS" ]] || fail "Bekræft med: $0 STOP-RACHER-OS"
command -v docker >/dev/null 2>&1 || fail "Docker mangler."
[[ -f "$ENV_FILE" ]] || fail ".env mangler: $ENV_FILE"

for stack in compose/core/compose.yml compose/data/compose.yml; do
  [[ -f "$REPO_ROOT/$stack" ]] || continue
  docker compose --env-file "$ENV_FILE" -f "$REPO_ROOT/$stack" down --remove-orphans
 done

if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files racher-os.service >/dev/null 2>&1; then
  sudo systemctl disable --now racher-os.service || true
  sudo rm -f /etc/systemd/system/racher-os.service
  sudo systemctl daemon-reload
fi

log "Racher OS er stoppet og autostart fjernet."
log "Data, backups, .env og repository er bevaret."
