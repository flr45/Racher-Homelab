#!/usr/bin/env bash
set -euo pipefail

PDL_ROOT="${PDL_ROOT:-/opt/racher-pager}"
PDL_BIN="${PDL_BIN:-$PDL_ROOT/pdl/bin/pdl}"
STATE_ROOT="${PAGER_STATE_ROOT:-/var/lib/racher-pager}"
PDL_STATE_DIR="${PDL_STATE_DIR:-$STATE_ROOT/pdl}"
PDL_LOG_PATH="${PDL_LOG_PATH:-$STATE_ROOT/pdl.log}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -x "$PDL_BIN" ]]; then
  echo "PDL binary mangler: $PDL_BIN" >&2
  exit 1
fi

mkdir -p "$PDL_STATE_DIR" "$STATE_ROOT"

if [[ ! -f "$PDL_STATE_DIR/pdl.ini" ]]; then
  "$SCRIPT_DIR/configure-pdl.sh"
fi

touch "$PDL_LOG_PATH"
cd "$PDL_STATE_DIR"

echo "Starter PDL headless"
echo "  binary: $PDL_BIN"
echo "  config: $PDL_STATE_DIR/pdl.ini"
echo "  output: $PDL_LOG_PATH"

exec "$PDL_BIN" --headless -o "$PDL_LOG_PATH" -v 1
