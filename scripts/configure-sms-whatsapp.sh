#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"
SESSION_ID="${1:-}"
CONFIG_DIR="$HOME/.config/racher"

if [[ -z "$SESSION_ID" ]]; then
  echo "Brug: bash scripts/configure-sms-whatsapp.sh <OPENWA_SESSION_ID>" >&2
  exit 2
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Mangler $ENV_FILE" >&2
  exit 1
fi

mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

move_secret_if_present() {
  local source="$1"
  local target="$2"
  if [[ -f "$source" ]]; then
    mv "$source" "$target"
  fi
  if [[ -f "$target" ]]; then
    chmod 600 "$target"
  fi
}

move_secret_if_present "$ROOT/.openwa-admin-key" "$CONFIG_DIR/openwa-admin-key"
move_secret_if_present "$ROOT/.openwa-sms-whatsapp-key" "$CONFIG_DIR/openwa-sms-whatsapp-key"

OPERATOR_KEY_FILE="$CONFIG_DIR/openwa-sms-whatsapp-key"
ADMIN_PASSWORD_FILE="$CONFIG_DIR/sms-whatsapp-admin-password"

if [[ ! -s "$OPERATOR_KEY_FILE" ]]; then
  echo "Mangler OpenWA operator-key: $OPERATOR_KEY_FILE" >&2
  exit 1
fi

if [[ ! -s "$ADMIN_PASSWORD_FILE" ]]; then
  python3 - <<'PY' > "$ADMIN_PASSWORD_FILE"
import secrets
print(secrets.token_urlsafe(24))
PY
  chmod 600 "$ADMIN_PASSWORD_FILE"
fi

if ! grep -Eq '^SMS_GATEWAY_API_TOKEN=.+' "$ENV_FILE"; then
  echo "SMS_GATEWAY_API_TOKEN mangler i .env" >&2
  exit 1
fi

export SMSWA_SESSION_ID="$SESSION_ID"
export SMSWA_OPERATOR_KEY="$(tr -d '\r\n' < "$OPERATOR_KEY_FILE")"
export SMSWA_ADMIN_PASSWORD="$(tr -d '\r\n' < "$ADMIN_PASSWORD_FILE")"
export SMSWA_INGEST_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export SMSWA_SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

cp "$ENV_FILE" "$ENV_FILE.sms-whatsapp-backup"
chmod 600 "$ENV_FILE.sms-whatsapp-backup"

python3 - "$ENV_FILE" <<'PY'
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8") if path.exists() else ""
lines = text.splitlines()

values = {
    "SMS_WHATSAPP_INGEST_TOKEN": os.environ["SMSWA_INGEST_TOKEN"],
    "SMS_WHATSAPP_ADMIN_USERNAME": "admin",
    "SMS_WHATSAPP_ADMIN_PASSWORD": os.environ["SMSWA_ADMIN_PASSWORD"],
    "SMS_WHATSAPP_SESSION_SECRET": os.environ["SMSWA_SESSION_SECRET"],
    "SMS_WHATSAPP_COOKIE_SECURE": "false",
    "SMS_WHATSAPP_PORT": "8091",
    "SMS_WHATSAPP_POLL_SECONDS": "2",
    "SMS_WHATSAPP_IGNORE_COMMANDS": "status,server status,serverstatus",
    "SMS_WHATSAPP_SMS_GATEWAY_URL": "http://sms-gateway:8080",
    "SMS_WHATSAPP_OPENWA_URL": "http://openwa:2785/api",
    "SMS_WHATSAPP_OPENWA_API_KEY": os.environ["SMSWA_OPERATOR_KEY"],
    "SMS_WHATSAPP_OPENWA_SESSION_ID": os.environ["SMSWA_SESSION_ID"],
}

remaining = dict(values)
out = []
for line in lines:
    if "=" in line and not line.lstrip().startswith("#"):
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
            continue
    out.append(line)

if out and out[-1].strip():
    out.append("")
out.append("# SMS -> WhatsApp gateway")
for key, value in remaining.items():
    out.append(f"{key}={value}")

path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
path.chmod(0o600)
PY

unset SMSWA_OPERATOR_KEY SMSWA_ADMIN_PASSWORD SMSWA_INGEST_TOKEN SMSWA_SESSION_SECRET SMSWA_SESSION_ID

echo "✅ SMS→WhatsApp miljø er konfigureret"
echo "✅ OpenWA-nøgler er flyttet ud af repoet til $CONFIG_DIR"
echo "✅ Admin-adgangskoden ligger i $ADMIN_PASSWORD_FILE"
echo "✅ Session: ${SESSION_ID:0:8}…"
echo "ℹ️  Ingen hemmeligheder er vist på skærmen"
