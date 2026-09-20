#!/usr/bin/env bash
set -Eeuo pipefail

APP="/opt/SBR-Pager-Gateway"
ENV="$APP/.env"
WA_COMPOSE="$APP/compose/sms-whatsapp/compose.yml"
GW_COMPOSE="$APP/compose/sms-gateway/docker-compose.yml"

cd "$APP"

BIND_IP="$(
    grep '^SMS_WHATSAPP_BIND_IP=' "$ENV"     | tail -1     | cut -d= -f2-
)"

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
docker compose     --env-file "$ENV"     -f "$WA_COMPOSE"     up -d --no-build openwa

echo "Recreater SBR Pager efter bind-IP er klar..."
docker compose     --env-file "$ENV"     -f "$WA_COMPOSE"     up -d --no-build --no-deps --force-recreate sms-whatsapp

echo "Starter SMS Gateway..."
docker compose     --env-file "$ENV"     -f "$GW_COMPOSE"     up -d --no-build sms-gateway

echo "✅ SBR Pager stack sikret"
