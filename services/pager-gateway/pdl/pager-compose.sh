#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${PAGER_GATEWAY_ENV:-/etc/racher-pager/gateway.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

RUNTIME_REPO="${PAGER_RUNTIME_REPO:-/opt/racher-pager/runtime-repo}"
COMPOSE_FILE="$RUNTIME_REPO/compose/pager-gateway/docker-compose.yml"

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "Mangler compose-fil: $COMPOSE_FILE" >&2
  exit 1
fi

export PAGER_DATA_HOST_PATH="${PAGER_DATA_HOST_PATH:-/var/lib/racher-pager}"
export PAGER_GATEWAY_PORT="${PAGER_GATEWAY_PORT:-8088}"
export PAGER_COOKIE_SECURE="${PAGER_COOKIE_SECURE:-0}"
export PAGER_VAPID_SUBJECT="${PAGER_VAPID_SUBJECT:-mailto:admin@racher.local}"

# The gateway must never silently fall back to root. New installations persist
# the appliance uid/gid in gateway.env. Legacy installations can recover the
# identity from the PDL systemd unit or, as a final safe fallback, from a
# non-root-owned state directory.
resolve_runtime_identity() {
  local uid="${PAGER_RUNTIME_UID:-}"
  local gid="${PAGER_RUNTIME_GID:-}"
  local pdl_user=""
  local dir_uid=""
  local dir_gid=""

  if [[ -z "$uid" || -z "$gid" || "$uid" == "0" || "$gid" == "0" ]]; then
    if command -v systemctl >/dev/null 2>&1; then
      pdl_user="$(systemctl show -p User --value racher-pdl.service 2>/dev/null || true)"
      if [[ -n "$pdl_user" && "$pdl_user" != "root" ]] && id "$pdl_user" >/dev/null 2>&1; then
        uid="$(id -u "$pdl_user")"
        gid="$(id -g "$pdl_user")"
      fi
    fi
  fi

  if [[ ( -z "$uid" || -z "$gid" || "$uid" == "0" || "$gid" == "0" ) && -d "$PAGER_DATA_HOST_PATH" ]]; then
    dir_uid="$(stat -c '%u' "$PAGER_DATA_HOST_PATH")"
    dir_gid="$(stat -c '%g' "$PAGER_DATA_HOST_PATH")"
    if [[ "$dir_uid" != "0" && "$dir_gid" != "0" ]]; then
      uid="$dir_uid"
      gid="$dir_gid"
    fi
  fi

  if [[ -z "$uid" || -z "$gid" || "$uid" == "0" || "$gid" == "0" ]]; then
    echo "Afviser at starte Pager Gateway som root: kan ikke fastslå en sikker runtime uid/gid." >&2
    echo "Kør install-pager-gateway.sh som normal bruger eller sæt PAGER_RUNTIME_UID/PAGER_RUNTIME_GID." >&2
    exit 1
  fi

  export PAGER_RUNTIME_UID="$uid"
  export PAGER_RUNTIME_GID="$gid"
}
resolve_runtime_identity

# Older gateway/host-agent versions may have left runtime state root-owned.
# Repair only the state root and the exact files shared by the unprivileged
# gateway/PDL processes. Refuse symlinks before privileged ownership changes.
if [[ "$EUID" -eq 0 && -d "$PAGER_DATA_HOST_PATH" ]]; then
  if [[ -L "$PAGER_DATA_HOST_PATH" ]]; then
    echo "Afviser usikker symlink som pager-state: $PAGER_DATA_HOST_PATH" >&2
    exit 1
  fi
  chown "$PAGER_RUNTIME_UID:$PAGER_RUNTIME_GID" "$PAGER_DATA_HOST_PATH"
  chmod 2770 "$PAGER_DATA_HOST_PATH"

  if [[ -d "$PAGER_DATA_HOST_PATH/pdl" ]]; then
    if [[ -L "$PAGER_DATA_HOST_PATH/pdl" ]]; then
      echo "Afviser usikker symlink i pager-state: $PAGER_DATA_HOST_PATH/pdl" >&2
      exit 1
    fi
    chown "$PAGER_RUNTIME_UID:$PAGER_RUNTIME_GID" "$PAGER_DATA_HOST_PATH/pdl"
    chmod 0750 "$PAGER_DATA_HOST_PATH/pdl"
  fi

  for name in pager.db pager.db-wal pager.db-shm pdl.log pdl.log.racher-cursor pdl/pdl.ini session-secret vapid-private.pem; do
    path="$PAGER_DATA_HOST_PATH/$name"
    if [[ -L "$path" ]]; then
      echo "Afviser usikker symlink i pager-state: $path" >&2
      exit 1
    fi
    if [[ -f "$path" ]]; then
      chown "$PAGER_RUNTIME_UID:$PAGER_RUNTIME_GID" "$path"
      case "$name" in
        session-secret|vapid-private.pem) chmod 0600 "$path" ;;
        *) chmod 0640 "$path" ;;
      esac
    fi
  done
fi

if docker compose version >/dev/null 2>&1; then
  exec docker compose -f "$COMPOSE_FILE" "$@"
elif command -v docker-compose >/dev/null 2>&1; then
  exec docker-compose -f "$COMPOSE_FILE" "$@"
else
  echo "Docker Compose mangler." >&2
  exit 1
fi
