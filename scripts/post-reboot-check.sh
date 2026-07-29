#!/usr/bin/env bash
set -Eeuo pipefail

pass=0
fail=0
ok() { printf '[OK] %s\n' "$*"; pass=$((pass + 1)); }
error() { printf '[FEJL] %s\n' "$*" >&2; fail=$((fail + 1)); }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${RACHER_ENV_FILE:-$REPO_ROOT/.env}"
HEALTH_URL="${RACHER_HEALTH_URL:-http://127.0.0.1:81}"

if command -v systemctl >/dev/null 2>&1 && systemctl is-enabled --quiet racher-os.service; then
  ok "Racher OS autostart er aktiveret"
else
  error "Racher OS autostart er ikke aktiveret"
fi

if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet racher-os.service; then
  ok "Racher OS systemd-service er aktiv"
else
  error "Racher OS systemd-service er ikke aktiv"
fi

if docker info >/dev/null 2>&1; then
  ok "Docker Engine svarer efter reboot"
else
  error "Docker Engine svarer ikke"
fi

if [[ -f "$ENV_FILE" ]]; then
  ok ".env er tilgængelig"
else
  error ".env mangler"
fi

if curl --fail --silent --show-error --max-time 10 "$HEALTH_URL" >/dev/null 2>&1; then
  ok "Health endpoint svarer"
else
  error "Health endpoint svarer ikke: $HEALTH_URL"
fi

for stack in compose/data/compose.yml compose/core/compose.yml; do
  if docker compose --env-file "$ENV_FILE" -f "$REPO_ROOT/$stack" ps --status running --quiet | grep -q .; then
    ok "$stack har kørende containere"
  else
    error "$stack har ingen kørende containere"
  fi
done

printf '\nResultat efter reboot: %d OK, %d fejl\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
