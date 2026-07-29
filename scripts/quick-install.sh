#!/usr/bin/env bash
set -Eeuo pipefail

fail() { printf '[quick-install] FEJL: %s\n' "$*" >&2; exit 1; }
log() { printf '[quick-install] %s\n' "$*"; }

[[ "${EUID}" -ne 0 ]] || fail "Kør som normal bruger, ikke root."
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log "1/5 Preflight"
"$REPO_ROOT/scripts/pi-preflight.sh"
log "2/5 Bootstrap"
"$REPO_ROOT/scripts/bootstrap.sh"

if ! docker info >/dev/null 2>&1; then
  fail "Docker-gruppen er endnu ikke aktiv. Log ud og ind, og kør scriptet igen."
fi

log "3/5 Installation"
"$REPO_ROOT/scripts/install-racher-os.sh"
log "4/5 Autostart"
"$REPO_ROOT/scripts/install-systemd-service.sh"
log "5/5 Go-live kontrol"
"$REPO_ROOT/scripts/go-live-check.sh"
log "Racher OS er installeret og klar til genstart."
