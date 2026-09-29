#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
SERVICE_DIR="$REPO_ROOT/services/pager-gateway"
DATA_DIR="${PAGER_DATA_HOST_PATH:-/var/lib/racher-pager}"
AGENT_DIR="${PAGER_SYSTEM_AGENT_INSTALL_DIR:-/opt/racher-pager/system-agent}"
INTEGRATION_DIR="${PAGER_INTEGRATION_DIR:-/opt/racher-pager/integration}"
NETWORK_DIR="${PAGER_NETWORK_INSTALL_DIR:-/opt/racher-pager/network}"
BACKUP_DIR="${PAGER_BACKUP_DIR:-/var/backups/racher-pager}"
RUNTIME_REPO="${PAGER_RUNTIME_REPO:-/opt/racher-pager/runtime-repo}"
GATEWAY_ENV="${PAGER_GATEWAY_ENV:-/etc/racher-pager/gateway.env}"
TMPFILES_PATH="/etc/tmpfiles.d/racher-pager-runtime.conf"
UNIT_PATH="/etc/systemd/system/racher-pager-system-agent.service"
FSK_UNIT_PATH="/etc/systemd/system/racher-pager-fsk-status.service"
FSK_TIMER_PATH="/etc/systemd/system/racher-pager-fsk-status.timer"
WATCHDOG_UNIT_PATH="/etc/systemd/system/racher-pager-gateway-watchdog.service"
WATCHDOG_TIMER_PATH="/etc/systemd/system/racher-pager-gateway-watchdog.timer"
PDL_LOGROTATE_PATH="/etc/logrotate.d/racher-pager-pdl"
HARDWARE_WATCHDOG_CONF="/etc/systemd/system.conf.d/racher-pager-watchdog.conf"
WATCHDOG_RUNTIME_DIR="/run/racher-pager"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "System-agenten installeres kun på Linux/Raspberry Pi." >&2
  exit 1
fi

for required in system_agent.py fsk_status_agent.py gateway_watchdog.py storage.py network_portal.py; do
  [[ -f "$SERVICE_DIR/$required" ]] || { echo "Mangler $SERVICE_DIR/$required" >&2; exit 1; }
done
for required in backup-pager.sh restore-pager.sh update-pager.sh rollback-pager.sh pager-compose.sh configure-pdl.sh run-pdl-headless.sh diagnose-reception.sh; do
  [[ -f "$SERVICE_DIR/pdl/$required" ]] || { echo "Mangler $SERVICE_DIR/pdl/$required" >&2; exit 1; }
done

# Persisted runtime identity is the source of truth for every component that
# writes shared pager state. Legacy appliances may not have these keys yet, so
# recover them from the non-root PDL service or an already-correct state owner.
if [[ -f "$GATEWAY_ENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$GATEWAY_ENV"
  set +a
fi

resolve_runtime_identity() {
  local uid="${PAGER_RUNTIME_UID:-}"
  local gid="${PAGER_RUNTIME_GID:-}"
  local pdl_user=""
  local state_uid=""
  local state_gid=""

  if [[ -z "$uid" || -z "$gid" || "$uid" == "0" || "$gid" == "0" ]]; then
    pdl_user="$(systemctl show -p User --value racher-pdl.service 2>/dev/null || true)"
    if [[ -n "$pdl_user" && "$pdl_user" != "root" ]] && id "$pdl_user" >/dev/null 2>&1; then
      uid="$(id -u "$pdl_user")"
      gid="$(id -g "$pdl_user")"
    fi
  fi

  if [[ ( -z "$uid" || -z "$gid" || "$uid" == "0" || "$gid" == "0" ) && -d "$DATA_DIR" ]]; then
    state_uid="$(stat -c '%u' "$DATA_DIR" 2>/dev/null || true)"
    state_gid="$(stat -c '%g' "$DATA_DIR" 2>/dev/null || true)"
    if [[ -n "$state_uid" && -n "$state_gid" && "$state_uid" != "0" && "$state_gid" != "0" ]]; then
      uid="$state_uid"
      gid="$state_gid"
    fi
  fi

  if [[ ( -z "$uid" || -z "$gid" || "$uid" == "0" || "$gid" == "0" ) && -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
    uid="$(id -u "$SUDO_USER")"
    gid="$(id -g "$SUDO_USER")"
  fi

  if [[ -z "$uid" || -z "$gid" || "$uid" == "0" || "$gid" == "0" ]]; then
    echo "Kan ikke fastslå en sikker non-root runtime-bruger til Racher Pager." >&2
    exit 1
  fi

  RUNTIME_UID="$uid"
  RUNTIME_GID="$gid"
}
resolve_runtime_identity

persist_runtime_identity() {
  sudo mkdir -p "$(dirname "$GATEWAY_ENV")"
  sudo touch "$GATEWAY_ENV"
  sudo chmod 0640 "$GATEWAY_ENV"
  for entry in "PAGER_RUNTIME_UID=$RUNTIME_UID" "PAGER_RUNTIME_GID=$RUNTIME_GID"; do
    key="${entry%%=*}"
    if sudo grep -q "^$key=" "$GATEWAY_ENV" 2>/dev/null; then
      sudo sed -i "s/^$key=.*/$entry/" "$GATEWAY_ENV"
    else
      printf '%s\n' "$entry" | sudo tee -a "$GATEWAY_ENV" >/dev/null
    fi
  done
}
persist_runtime_identity

sudo mkdir -p "$DATA_DIR" "$DATA_DIR/pdl" "$AGENT_DIR" "$INTEGRATION_DIR" "$NETWORK_DIR" "$BACKUP_DIR" "$DATA_DIR/update" "$WATCHDOG_RUNTIME_DIR"
sudo touch "$DATA_DIR/pager.db"

# SQLite, PDL and the web container share this state. Repair the exact writable
# runtime paths before services are restarted; this also migrates appliances from
# older root-running gateway releases without recursively changing backup/update
# artifacts that are intentionally root-owned.
sudo chown "$RUNTIME_UID:$RUNTIME_GID" "$DATA_DIR" "$DATA_DIR/pdl" "$DATA_DIR/pager.db"
sudo chmod 2770 "$DATA_DIR"
sudo chmod 0750 "$DATA_DIR/pdl"
sudo chmod 0640 "$DATA_DIR/pager.db"
for relative in pager.db-wal pager.db-shm pdl.log pdl.log.racher-cursor pdl/pdl.ini session-secret vapid-private.pem; do
  runtime_path="$DATA_DIR/$relative"
  if [[ -L "$runtime_path" ]]; then
    echo "Afviser usikker symlink i pager-state: $runtime_path" >&2
    exit 1
  fi
  if [[ -f "$runtime_path" ]]; then
    sudo chown "$RUNTIME_UID:$RUNTIME_GID" "$runtime_path"
    case "$relative" in
      session-secret|vapid-private.pem) sudo chmod 0600 "$runtime_path" ;;
      *) sudo chmod 0640 "$runtime_path" ;;
    esac
  fi
done

# Re-assert the runtime ownership during every boot before long-running services
# start. This makes a stray root-owned state file recoverable without SSH surgery.
sudo tee "$TMPFILES_PATH" >/dev/null <<EOF
d $DATA_DIR 2770 $RUNTIME_UID $RUNTIME_GID - -
d $DATA_DIR/pdl 0750 $RUNTIME_UID $RUNTIME_GID - -
z $DATA_DIR/pager.db 0640 $RUNTIME_UID $RUNTIME_GID - -
z $DATA_DIR/pager.db-wal 0640 $RUNTIME_UID $RUNTIME_GID - -
z $DATA_DIR/pager.db-shm 0640 $RUNTIME_UID $RUNTIME_GID - -
z $DATA_DIR/pdl.log 0640 $RUNTIME_UID $RUNTIME_GID - -
z $DATA_DIR/pdl.log.racher-cursor 0640 $RUNTIME_UID $RUNTIME_GID - -
z $DATA_DIR/pdl/pdl.ini 0640 $RUNTIME_UID $RUNTIME_GID - -
z $DATA_DIR/session-secret 0600 $RUNTIME_UID $RUNTIME_GID - -
z $DATA_DIR/vapid-private.pem 0600 $RUNTIME_UID $RUNTIME_GID - -
EOF
sudo chmod 0644 "$TMPFILES_PATH"
sudo systemd-tmpfiles --create "$TMPFILES_PATH"

sudo chmod 0700 "$BACKUP_DIR"
sudo chmod 0755 "$WATCHDOG_RUNTIME_DIR"

sudo install -m 0755 "$SERVICE_DIR/system_agent.py" "$AGENT_DIR/system_agent.py"
sudo install -m 0755 "$SERVICE_DIR/fsk_status_agent.py" "$AGENT_DIR/fsk_status_agent.py"
sudo install -m 0755 "$SERVICE_DIR/gateway_watchdog.py" "$AGENT_DIR/gateway_watchdog.py"
sudo install -m 0644 "$SERVICE_DIR/storage.py" "$AGENT_DIR/storage.py"

# These files are executed from /opt rather than directly from the git checkout.
# Refresh them on every gateway update so the reported runtime commit and the
# actually-running host code cannot drift apart.
for helper in \
  backup-pager.sh restore-pager.sh update-pager.sh rollback-pager.sh pager-compose.sh \
  configure-pdl.sh run-pdl-headless.sh diagnose-reception.sh; do
  sudo install -m 0755 "$SERVICE_DIR/pdl/$helper" "$INTEGRATION_DIR/$helper"
done
sudo install -m 0755 "$SERVICE_DIR/network_portal.py" "$NETWORK_DIR/network_portal.py"

# PDL has its own persistent raw logfile in addition to the structured SQLite
# history. Refresh logrotate here as well as during first install, because this
# script runs after every gateway update on existing appliances.
sudo tee "$PDL_LOGROTATE_PATH" >/dev/null <<EOF
$DATA_DIR/pdl.log {
    daily
    rotate 30
    maxsize 20M
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF
sudo chmod 0644 "$PDL_LOGROTATE_PATH"

sudo tee "$UNIT_PATH" >/dev/null <<EOF
[Unit]
Description=Racher Pager privileged system action and health agent
After=docker.service racher-pdl.service NetworkManager.service local-fs.target
Wants=docker.service NetworkManager.service

[Service]
Type=simple
User=root
UMask=0007
WorkingDirectory=$AGENT_DIR
Environment=PAGER_DB_PATH=$DATA_DIR/pager.db
Environment=PAGER_STATE_ROOT=$DATA_DIR
Environment=PDL_CONFIG_PATH=$DATA_DIR/pdl/pdl.ini
Environment=PDL_LOG_PATH=$DATA_DIR/pdl.log
Environment=PAGER_BACKUP_DIR=$BACKUP_DIR
Environment=PAGER_INTEGRATION_DIR=$INTEGRATION_DIR
Environment=PAGER_RUNTIME_REPO=$RUNTIME_REPO
EnvironmentFile=-/etc/racher-pager/gateway.env
EnvironmentFile=-/etc/racher-pager/network.env
ExecStart=/usr/bin/python3 $AGENT_DIR/system_agent.py
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectHome=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$DATA_DIR $BACKUP_DIR $RUNTIME_REPO $AGENT_DIR $INTEGRATION_DIR /etc/racher-pager /etc/systemd/system

[Install]
WantedBy=multi-user.target
EOF

sudo tee "$FSK_UNIT_PATH" >/dev/null <<EOF
[Unit]
Description=Racher Pager FSK-USB hardware status probe
After=local-fs.target racher-pdl.service

[Service]
Type=oneshot
User=root
UMask=0007
WorkingDirectory=$AGENT_DIR
Environment=PAGER_DB_PATH=$DATA_DIR/pager.db
Environment=PDL_CONFIG_PATH=$DATA_DIR/pdl/pdl.ini
EnvironmentFile=-/etc/racher-pager/pdl.env
ExecStart=/usr/bin/python3 $AGENT_DIR/fsk_status_agent.py
NoNewPrivileges=true
ProtectHome=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$DATA_DIR
EOF

sudo tee "$FSK_TIMER_PATH" >/dev/null <<'EOF'
[Unit]
Description=Poll Racher Pager FSK-USB hardware status

[Timer]
OnBootSec=5s
OnUnitActiveSec=10s
AccuracySec=1s
Unit=racher-pager-fsk-status.service

[Install]
WantedBy=timers.target
EOF

# Docker's restart policy handles process exits, but not a wedged Gunicorn process
# that remains alive. This independent root-owned watchdog probes /healthz and
# restarts only after three consecutive failures. If dockerd itself is unavailable
# the watchdog escalates to restarting docker.service before retrying the container.
sudo tee "$WATCHDOG_UNIT_PATH" >/dev/null <<EOF
[Unit]
Description=Racher Pager Gateway health watchdog
After=docker.service network.target
Wants=docker.service

[Service]
Type=oneshot
User=root
WorkingDirectory=$AGENT_DIR
RuntimeDirectory=racher-pager
RuntimeDirectoryMode=0755
RuntimeDirectoryPreserve=yes
Environment=PAGER_RUNTIME_DIR=$WATCHDOG_RUNTIME_DIR
Environment=PAGER_WATCHDOG_STATE_FILE=$WATCHDOG_RUNTIME_DIR/gateway-watchdog.failures
Environment=PAGER_MAINTENANCE_LOCK=$WATCHDOG_RUNTIME_DIR/maintenance.lock
Environment=PAGER_WATCHDOG_FAILURE_THRESHOLD=3
EnvironmentFile=-/etc/racher-pager/gateway.env
ExecStart=/usr/bin/python3 $AGENT_DIR/gateway_watchdog.py
TimeoutStartSec=150s
NoNewPrivileges=true
ProtectHome=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$WATCHDOG_RUNTIME_DIR
EOF

sudo tee "$WATCHDOG_TIMER_PATH" >/dev/null <<'EOF'
[Unit]
Description=Poll Racher Pager Gateway health

[Timer]
OnBootSec=45s
OnUnitActiveSec=20s
AccuracySec=2s
Unit=racher-pager-gateway-watchdog.service

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now racher-pager-system-agent.service
sudo systemctl enable --now racher-pager-fsk-status.timer
sudo systemctl enable --now racher-pager-gateway-watchdog.timer

# A pre-hardening gateway may have created pager.db/session/VAPID as root. Only
# force-recreate when the existing gateway container actually runs as root.
# During a normal update update-pager.sh has already rebuilt, recreated and
# health-checked the hardened container. Recreating it again here caused a race:
# update-pager.sh could hit /healthz while Gunicorn was being restarted and then
# roll an otherwise healthy release back. --no-recreate keeps that healthy
# container in place while still creating/starting it on a fresh installation.
if [[ -x "$INTEGRATION_DIR/pager-compose.sh" && -f /etc/racher-pager/gateway.env ]]; then
  COMPOSE_ARGS=(up -d --no-recreate pager-gateway)
  if docker inspect racher-pager-gateway >/dev/null 2>&1; then
    GATEWAY_USER="$(docker inspect -f '{{.Config.User}}' racher-pager-gateway 2>/dev/null || true)"
    case "$GATEWAY_USER" in
      ""|0|0:0|root)
        COMPOSE_ARGS=(up -d --force-recreate pager-gateway)
        ;;
    esac
  fi
  sudo env PAGER_GATEWAY_ENV=/etc/racher-pager/gateway.env \
    "$INTEGRATION_DIR/pager-compose.sh" "${COMPOSE_ARGS[@]}"

  # If the container had to be created/restarted, do not return to the updater
  # until Gunicorn is listening again. This makes the helper safe both during
  # bootstrap and during upgrades from older root-running installations.
  if command -v curl >/dev/null 2>&1; then
    GATEWAY_PORT="${PAGER_GATEWAY_PORT:-8088}"
    GATEWAY_READY=0
    for _ in $(seq 1 60); do
      if curl -fsS "http://127.0.0.1:$GATEWAY_PORT/healthz" >/dev/null 2>&1; then
        GATEWAY_READY=1
        break
      fi
      sleep 1
    done
    if [[ "$GATEWAY_READY" != "1" ]]; then
      echo "Pager Gateway blev ikke klar efter system-agent opdatering." >&2
      exit 1
    fi
  fi
fi

# The previous wrapper could exit repeatedly while FSK-USB was absent and may
# therefore have reached systemd's failed/start-limit state. Clear that state and
# start the refreshed wrapper explicitly; it now remains active while waiting for
# hardware instead of entering a restart loop.
sudo systemctl reset-failed racher-pdl.service >/dev/null 2>&1 || true
sudo systemctl restart racher-pdl.service >/dev/null 2>&1 || true
sudo systemctl try-restart racher-pager-network-portal.service >/dev/null 2>&1 || true

# A process-level watchdog cannot recover a completely frozen Linux userspace or
# kernel. Raspberry Pi exposes a hardware watchdog on supported installations.
# When available, let systemd feed it; if systemd itself stops scheduling for
# roughly 45 seconds the hardware reboots the Pi automatically.
sudo modprobe bcm2835_wdt >/dev/null 2>&1 || true
if [[ -e /dev/watchdog || -e /dev/watchdog0 ]]; then
  sudo mkdir -p "$(dirname "$HARDWARE_WATCHDOG_CONF")"
  sudo tee "$HARDWARE_WATCHDOG_CONF" >/dev/null <<'EOF'
[Manager]
RuntimeWatchdogSec=45s
EOF
  sudo systemctl daemon-reexec
  HARDWARE_WATCHDOG_STATUS="aktiv (45 sek.)"
else
  HARDWARE_WATCHDOG_STATUS="afventer understøttet /dev/watchdog"
fi

echo "Racher Pager system-agent er installeret."
echo "Agentkode:       $AGENT_DIR"
echo "Helpers:         $INTEGRATION_DIR"
echo "FSK probe:       racher-pager-fsk-status.timer (10 sek.)"
echo "Gateway watchdog: racher-pager-gateway-watchdog.timer (20 sek., 3 fejl)"
echo "PDL logrotation:  daglig/20 MB, 30 rotationer"
echo "Runtime identity: uid=$RUNTIME_UID gid=$RUNTIME_GID (boot-guard aktiv)"
echo "Reception diag:  $INTEGRATION_DIR/diagnose-reception.sh"
echo "Pi watchdog:     $HARDWARE_WATCHDOG_STATUS"
echo "Status: sudo systemctl status racher-pager-system-agent --no-pager"