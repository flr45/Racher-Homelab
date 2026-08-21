#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
RUNTIME_REPO="${PAGER_RUNTIME_REPO:-/opt/racher-pager/runtime-repo}"
DATA_DIR="${PAGER_DATA_HOST_PATH:-/var/lib/racher-pager}"
UNIT_PATH="/etc/systemd/system/racher-pager-boot-verify.service"
SOURCE="$REPO_ROOT/services/pager-gateway/boot_verifier.py"
RUNTIME_SOURCE="$RUNTIME_REPO/services/pager-gateway/boot_verifier.py"

[[ "$(uname -s)" == "Linux" ]] || { echo "Boot-verifikation installeres kun på Linux." >&2; exit 1; }
[[ -f "$SOURCE" ]] || { echo "Mangler $SOURCE" >&2; exit 1; }
[[ -f "$RUNTIME_SOURCE" ]] || { echo "Mangler runtime-fil: $RUNTIME_SOURCE" >&2; exit 1; }

sudo mkdir -p "$DATA_DIR"

sudo tee "$UNIT_PATH" >/dev/null <<EOF
[Unit]
Description=Racher Pager post-boot end-to-end verification
After=network-online.target docker.service racher-pdl.service racher-pager-system-agent.service
Wants=network-online.target docker.service racher-pdl.service racher-pager-system-agent.service

[Service]
Type=oneshot
User=root
UMask=0007
WorkingDirectory=$RUNTIME_REPO/services/pager-gateway
Environment=PAGER_DB_PATH=$DATA_DIR/pager.db
EnvironmentFile=-/etc/racher-pager/gateway.env
ExecStart=/usr/bin/python3 $RUNTIME_SOURCE
TimeoutStartSec=130s
NoNewPrivileges=true
ProtectHome=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable racher-pager-boot-verify.service
# Run once immediately as an installation sanity check. A remote SMS outage is
# recorded as degraded but does not make the installer fail; local component
# failures remain visible through systemctl and runtime_status.
sudo systemctl restart racher-pager-boot-verify.service || true

echo "Racher Pager boot-verifikation er installeret."
echo "Service: racher-pager-boot-verify.service"
echo "Kode:    $RUNTIME_SOURCE"
echo "Status:  systemctl status racher-pager-boot-verify.service --no-pager"
echo "Resultat gemmes i runtime_status som boot_verify_* og vises i Systemoverblik."
