#!/usr/bin/env bash
set -euo pipefail

PDL_ROOT="${PDL_ROOT:-/opt/racher-pager}"
PDL_BIN="${PDL_BIN:-$PDL_ROOT/pdl/bin/pdl}"
STATE_ROOT="${PAGER_STATE_ROOT:-/var/lib/racher-pager}"
PDL_STATE_DIR="${PDL_STATE_DIR:-$STATE_ROOT/pdl}"
PDL_LOG_PATH="${PDL_LOG_PATH:-$STATE_ROOT/pdl.log}"
INPUT_MODE="${PDL_INPUT_MODE:-fsk-usb}"
DEVICE_WAIT_SECONDS="${PDL_DEVICE_WAIT_SECONDS:-5}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -x "$PDL_BIN" ]]; then
  echo "PDL binary mangler: $PDL_BIN" >&2
  exit 1
fi

mkdir -p "$PDL_STATE_DIR" "$STATE_ROOT"

# Rebuild pdl.ini from the root-owned environment on every start. This makes
# decoder-output hardening take effect after a normal gateway update instead of
# leaving an old persistent pdl.ini behind indefinitely.
"$SCRIPT_DIR/configure-pdl.sh"

touch "$PDL_LOG_PATH"
cd "$PDL_STATE_DIR"

select_fsk_device() {
  local explicit="${PDL_RS232_DEVICE:-}"
  local candidate label

  # A pinned by-id path is authoritative. If it is temporarily missing, wait for
  # that exact interface rather than silently opening some other serial adapter.
  if [[ -n "$explicit" ]]; then
    [[ -e "$explicit" ]] && printf '%s\n' "$explicit"
    return 0
  fi

  shopt -s nullglob
  local candidates=(/dev/serial/by-id/* /dev/ttyUSB* /dev/ttyACM*)
  shopt -u nullglob
  (( ${#candidates[@]} > 0 )) || return 0

  # discriminator.nl uses an FTDI serial interface. Prefer a stable by-id name
  # that identifies FTDI/FT232 when it is present, then fall back to the normal
  # by-id -> ttyUSB -> ttyACM ordering.
  for candidate in "${candidates[@]}"; do
    label="${candidate,,}"
    if [[ "$candidate" == /dev/serial/by-id/* ]] && \
       [[ "$label" == *ftdi* || "$label" == *ft232* || "$label" == *discriminator* ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  printf '%s\n' "${candidates[0]}"
}

if [[ "$INPUT_MODE" == "fsk-usb" || "$INPUT_MODE" == "rs232" ]]; then
  announced_wait=0
  while true; do
    selected_device="$(select_fsk_device)"
    if [[ -n "$selected_device" ]]; then
      export PDL_RS232_DEVICE="$selected_device"
      # Enabled by default on the appliance while reception is being validated.
      # Diagnostics are metadata-only: no raw bytes, RIC/capcodes or message
      # text are written to the journal.
      export PDL_RS232_RX_DIAG="${PDL_RS232_RX_DIAG:-1}"
      export PDL_POCSAG_512_DIAG="${PDL_POCSAG_512_DIAG:-1}"
      export PDL_POCSAG_1200_DIAG="${PDL_POCSAG_1200_DIAG:-1}"
      echo "FSK-USB input: $PDL_RS232_DEVICE"
      echo "FSK-USB rådiagnostik: PDL_RS232_RX_DIAG=$PDL_RS232_RX_DIAG"
      echo "POCSAG-512 diagnostik: PDL_POCSAG_512_DIAG=$PDL_POCSAG_512_DIAG"
      echo "POCSAG-1200 diagnostik: PDL_POCSAG_1200_DIAG=$PDL_POCSAG_1200_DIAG"
      break
    fi
    if [[ "$announced_wait" == "0" ]]; then
      if [[ -n "${PDL_RS232_DEVICE:-}" ]]; then
        echo "FSK-USB afventer den pinnede enhed: $PDL_RS232_DEVICE" >&2
      else
        echo "FSK-USB er ikke tilsluttet endnu; PDL-servicen venter roligt på hardware." >&2
      fi
      announced_wait=1
    fi
    sleep "$DEVICE_WAIT_SECONDS"
  done
fi

echo "Starter PDL headless"
echo "  binary: $PDL_BIN"
echo "  config: $PDL_STATE_DIR/pdl.ini"
echo "  output: $PDL_LOG_PATH"
echo "  input:  $INPUT_MODE"

exec "$PDL_BIN" --headless -o "$PDL_LOG_PATH" -v 1
