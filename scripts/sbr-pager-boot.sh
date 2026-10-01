#!/usr/bin/env bash
set -Eeuo pipefail

APP="${SBR_PAGER_ROOT:-/opt/SBR-Pager-Gateway}"
ENV="$APP/.env"
WA_COMPOSE="$APP/compose/sms-whatsapp/compose.yml"
GW_COMPOSE="$APP/compose/sms-gateway/docker-compose.yml"

cd "$APP"
WA=(docker compose --env-file "$ENV" -f "$WA_COMPOSE")
OFFSITE_MOUNT="$(sed -n 's/^SMS_WHATSAPP_OFFSITE_MOUNT=//p' "$ENV" | tail -1 | tr -d '\r\"\047')"
if [[ "$OFFSITE_MOUNT" == true ]]; then
  WA+=(-f "$APP/compose/sms-whatsapp/offsite-backup.yml")
fi

MODEM_DRIVER="$(sed -n 's/^SMS_MODEM_DRIVER=//p' "$ENV" | tail -1 | tr -d '\r\"\047')"
if [[ "${MODEM_DRIVER:-usb}" == "cudy" ]]; then
    GW_COMPOSE="$APP/compose/sms-gateway/cudy.yml"
fi

BIND_IP="$(sed -n 's/^SMS_WHATSAPP_BIND_IP=//p' "$ENV" | tail -1 | tr -d '\r\"\047')"

if [[ -z "$BIND_IP" ]]; then
    BIND_IP="127.0.0.1"
fi

echo "SBR Pager bind-IP: $BIND_IP"

if [[ "$BIND_IP" != "127.0.0.1" && "$BIND_IP" != "0.0.0.0" ]]; then
    echo "Venter på $BIND_IP ..."
    for i in $(seq 1 90); do
        if ip -4 addr | grep -Fq "$BIND_IP"; then
            echo "✅ $BIND_IP er klar"
            break
        fi

        sleep 2

        if [[ "$i" == "90" ]]; then
            echo "❌ $BIND_IP kom ikke op"
            exit 1
        fi
    done
fi

echo "Starter OpenWA..."
"${WA[@]}"     up -d --no-build openwa

echo "Recreater SBR Pager efter bind-IP er klar..."
"${WA[@]}"     up -d --no-build --no-deps --force-recreate sms-whatsapp

echo "Starter SMS Gateway..."
docker compose     --env-file "$ENV"     -f "$GW_COMPOSE"     up -d --no-build sms-gateway

echo "✅ SBR Pager stack sikret"
