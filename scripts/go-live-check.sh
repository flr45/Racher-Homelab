#!/usr/bin/env bash
set -Eeuo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
ENV_FILE="${ENV_FILE:-.env}"
HOMELAB_ROOT="${HOMELAB_ROOT:-$HOME/homelab}"
failures=0

ok() { printf '[OK] %s\n' "$*"; }
fail() { printf '[FEJL] %s\n' "$*" >&2; failures=$((failures + 1)); }

if [[ -f "$ENV_FILE" ]]; then
  mode="$(stat -c '%a' "$ENV_FILE")"
  if [[ "$mode" == "600" ]]; then
    ok "$ENV_FILE har rettighed 0600"
  else
    fail "$ENV_FILE skal have rettighed 0600, men har $mode"
  fi

  if grep -Eqi '(change-me|changeme|password123|example-secret|default-password)' "$ENV_FILE"; then
    fail "$ENV_FILE indeholder en kendt standardværdi"
  else
    ok "Ingen kendte standardværdier i $ENV_FILE"
  fi
else
  fail "$ENV_FILE mangler"
fi

for path in "$HOMELAB_ROOT/data" "$HOMELAB_ROOT/backups"; do
  if [[ -d "$path" && -w "$path" ]]; then
    ok "$path er skrivbar"
  else
    fail "$path mangler eller er ikke skrivbar"
  fi
done

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  ok "Docker Engine svarer"
else
  fail "Docker Engine svarer ikke for den aktuelle bruger"
fi

if command -v curl >/dev/null 2>&1; then
  health="$(curl --proto '=http,https' --max-time 10 --silent --show-error --fail "$BASE_URL/health" 2>/dev/null || true)"
  if [[ -n "$health" ]]; then
    ok "Control Center healthcheck svarer"
  else
    fail "Control Center healthcheck svarer ikke på $BASE_URL/health"
  fi
else
  fail "curl mangler"
fi

if command -v vcgencmd >/dev/null 2>&1; then
  throttled="$(vcgencmd get_throttled 2>/dev/null || true)"
  if [[ "$throttled" == "throttled=0x0" ]]; then
    ok "Ingen strøm- eller throttlingfejl"
  else
    fail "Raspberry Pi rapporterer: ${throttled:-ukendt}"
  fi
fi

printf '\nGo-live resultat: %d fejl\n' "$failures"
[[ "$failures" -eq 0 ]]
