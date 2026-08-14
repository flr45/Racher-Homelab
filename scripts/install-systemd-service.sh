#!/usr/bin/env bash
set -Eeuo pipefail

fail() { printf '[systemd] FEJL: %s\n' "$*" >&2; exit 1; }
log() { printf '[systemd] %s\n' "$*"; }

[[ "${EUID}" -ne 0 ]] || fail "Kør som normal bruger, ikke root."
command -v systemctl >/dev/null 2>&1 || fail "systemd er ikke tilgængelig."
command -v docker >/dev/null 2>&1 || fail "Docker mangler."

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${RACHER_ENV_FILE:-$REPO_ROOT/.env}"
SERVICE_PATH="/etc/systemd/system/racher-os.service"
USER_NAME="$(id -un)"

[[ -f "$ENV_FILE" ]] || fail ".env mangler: $ENV_FILE"
[[ "$REPO_ROOT" = /* ]] || fail "Repository-stien skal være absolut."

unit="$(mktemp)"
trap 'rm -f "$unit"' EXIT
cat >"$unit" <<EOF
[Unit]
Description=Racher OS Docker stacks
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=$USER_NAME
WorkingDirectory=$REPO_ROOT
Environment=RACHER_ENV_FILE=$ENV_FILE
ExecStartPre=/bin/bash -c 'for i in {1..60}; do getent hosts registry-1.docker.io >/dev/null 2>&1 && exit 0; echo "Venter på netværk/DNS..."; sleep 2; done; exit 1'
ExecStart=/usr/bin/bash $REPO_ROOT/scripts/install-racher-os.sh
ExecStop=/usr/bin/docker compose --env-file $ENV_FILE -f $REPO_ROOT/compose/core/compose.yml down
ExecStop=/usr/bin/docker compose --env-file $ENV_FILE -f $REPO_ROOT/compose/data/compose.yml down
TimeoutStartSec=900
TimeoutStopSec=180

[Install]
WantedBy=multi-user.target
EOF

sudo install -m 0644 "$unit" "$SERVICE_PATH"
sudo systemctl daemon-reload
sudo systemctl enable racher-os.service
log "Autostart aktiveret. Tjenesten venter op til 120 sekunder på netværk/DNS ved boot."
log "Tjenesten starter ved næste boot."
log "Manuel start: sudo systemctl start racher-os"
