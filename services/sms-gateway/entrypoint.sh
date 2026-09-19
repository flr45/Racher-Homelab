#!/bin/sh
set -u

WEB_PID=""
READER_PID=""

cleanup() {
  if [ -n "$READER_PID" ]; then
    kill "$READER_PID" 2>/dev/null || true
  fi
  if [ -n "$WEB_PID" ]; then
    kill "$WEB_PID" 2>/dev/null || true
  fi
  if [ -n "$READER_PID" ]; then
    wait "$READER_PID" 2>/dev/null || true
  fi
  if [ -n "$WEB_PID" ]; then
    wait "$WEB_PID" 2>/dev/null || true
  fi
}

trap 'cleanup; exit 0' TERM INT
trap cleanup EXIT

gunicorn \
  --bind 0.0.0.0:8080 \
  --workers 1 \
  --threads 4 \
  --timeout 60 \
  app:app &
WEB_PID=$!

attempt=0
until python - <<'PY'
import urllib.error
import urllib.request

try:
    with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=1) as response:
        raise SystemExit(0 if response.status == 200 else 1)
except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
    raise SystemExit(1)
PY
do
  if ! kill -0 "$WEB_PID" 2>/dev/null; then
    echo "FEJL: SMS Gateway web-API stoppede under opstart." >&2
    wait "$WEB_PID"
    exit $?
  fi

  attempt=$((attempt + 1))
  if [ "$attempt" -ge 40 ]; then
    echo "FEJL: SMS Gateway web-API blev ikke klar inden for 20 sekunder." >&2
    exit 1
  fi
  sleep 0.5
done

echo "SMS Gateway web-API er klar; starter modem-reader."
python modem_reader_sbr.py &
READER_PID=$!

while :; do
  if ! kill -0 "$WEB_PID" 2>/dev/null; then
    wait "$WEB_PID"
    exit $?
  fi
  if ! kill -0 "$READER_PID" 2>/dev/null; then
    wait "$READER_PID"
    exit $?
  fi
  sleep 1
done
