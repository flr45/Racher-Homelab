#!/usr/bin/env bash
set -Eeuo pipefail

BASE_URL="${RACHER_SMOKE_URL:-http://127.0.0.1:18080}"
TIMEOUT="${RACHER_SMOKE_TIMEOUT:-90}"

deadline=$((SECONDS + TIMEOUT))
until curl --fail --silent --show-error --max-time 5 "$BASE_URL/health" >/dev/null; do
  (( SECONDS < deadline )) || { echo "Smoke test timeout: $BASE_URL/health" >&2; exit 1; }
  sleep 2
done

health_code="$(curl --silent --output /tmp/racher-health.json --write-out '%{http_code}' --max-time 5 "$BASE_URL/health")"
[[ "$health_code" == "200" ]]

grep -Eq '"status"[[:space:]]*:[[:space:]]*"(ok|healthy)"' /tmp/racher-health.json

headers="$(mktemp)"
body="$(mktemp)"
trap 'rm -f "$headers" "$body" /tmp/racher-health.json' EXIT
modules_code="$(curl --silent --show-error --max-time 5 -D "$headers" -o "$body" --write-out '%{http_code}' "$BASE_URL/api/modules")"
[[ "$modules_code" == "401" ]]
grep -qi '^Cache-Control: no-store' "$headers"
grep -q '"required_permission"' "$body"

identity_code="$(curl --silent --output "$body" --write-out '%{http_code}' --max-time 5 "$BASE_URL/api/identity")"
[[ "$identity_code" == "200" ]]
grep -q '"identity"' "$body"

echo "Racher OS smoke test passed"
