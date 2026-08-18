#!/usr/bin/env bash
set -euo pipefail

STATE_ROOT="${PAGER_STATE_ROOT:-/var/lib/racher-pager}"
PDL_STATE_DIR="${PDL_STATE_DIR:-$STATE_ROOT/pdl}"
PAGER_DB_PATH="${PAGER_DB_PATH:-$STATE_ROOT/pager.db}"
INPUT_MODE="${PDL_INPUT_MODE:-fsk-usb}"
CAPTURE_DEVICE="${PDL_CAPTURE_DEVICE:-default}"
SAMPLE_RATE="${PDL_SAMPLE_RATE:-48000}"
BAUD_512="${PDL_BAUD_512:-1}"
BAUD_1200="${PDL_BAUD_1200:-1}"
BAUD_2400="${PDL_BAUD_2400:-1}"
INVERT="${PDL_INVERT:-0}"
RS232_PORT="${PDL_RS232_PORT:-1}"
RS232_BITRATE="${PDL_RS232_BITRATE:-19200}"
RS232_DECODE_MODE="${PDL_RS232_DECODE_MODE:-2}"

# The web UI stores decoder choices in pager.db. This script is also run by
# systemd before every PDL start, so the database must be the final source of
# truth for baud/invert. Otherwise a restart would silently overwrite an admin's
# choice with the older values from /etc/racher-pager/pdl.env.
if command -v sqlite3 >/dev/null 2>&1 && [[ -f "$PAGER_DB_PATH" ]]; then
  DB_BAUD="$(sqlite3 -batch -noheader -cmd '.timeout 2000' "$PAGER_DB_PATH" \
    "SELECT value FROM settings WHERE key='pocsag_baud' LIMIT 1;" 2>/dev/null || true)"
  case "${DB_BAUD,,}" in
    512)
      BAUD_512=1; BAUD_1200=0; BAUD_2400=0
      ;;
    1200)
      BAUD_512=0; BAUD_1200=1; BAUD_2400=0
      ;;
    2400)
      BAUD_512=0; BAUD_1200=0; BAUD_2400=1
      ;;
    auto|"")
      BAUD_512=1; BAUD_1200=1; BAUD_2400=1
      ;;
    *)
      echo "Ignorerer ugyldig pocsag_baud i databasen: $DB_BAUD" >&2
      ;;
  esac

  DB_INVERT="$(sqlite3 -batch -noheader -cmd '.timeout 2000' "$PAGER_DB_PATH" \
    "SELECT value FROM settings WHERE key='invert' LIMIT 1;" 2>/dev/null || true)"
  case "${DB_INVERT,,}" in
    inverted) INVERT=1 ;;
    normal|auto|"") INVERT=0 ;;
    *) echo "Ignorerer ugyldig invert-indstilling i databasen: $DB_INVERT" >&2 ;;
  esac
fi

case "$INPUT_MODE" in
  fsk-usb|rs232)
    AUDIO_ENABLED=0
    RS232_ENABLED=1
    ;;
  audio|alsa)
    AUDIO_ENABLED=1
    RS232_ENABLED=0
    ;;
  *)
    echo "Ugyldig PDL_INPUT_MODE: $INPUT_MODE (brug fsk-usb eller audio)" >&2
    exit 1
    ;;
esac

mkdir -p "$PDL_STATE_DIR"

cat > "$PDL_STATE_DIR/pdl.ini" <<EOF
[POCSAG]
Enable=1
Baud512=$BAUD_512
Baud1200=$BAUD_1200
Baud2400=$BAUD_2400
FNU=0
# Behold en ALPHA-fortolkning når PDL vurderer samme payload lige sandsynligt
# som NUMERIC. Så får vi stadig den læsbare tekstvariant i råloggen.
ShowBoth=1

[Audio]
SampleRate=$SAMPLE_RATE
Config=1
Enabled=$AUDIO_ENABLED
Invert=$INVERT
CaptureDevice=$CAPTURE_DEVICE
PlaybackDevice=default

[RS232]
Enabled=0
Port=$RS232_PORT
Bitrate=$RS232_BITRATE
DecodeMode=$([[ "$RS232_ENABLED" == "1" ]] && echo "$RS232_DECODE_MODE" || echo 0)
FourLevel=0

[Monitor]
Paging=1
ACARS=0
MOBITEX=0
ERMES=0

[General]
ShowTone=0
# Behold også NUMERIC-decodes i PDL-råloggen. Gatewayen gemmer dem til
# diagnostik men markerer PDL non-alpha som decoder-non-alpha, så de bliver
# ikke leveret som alarm/Pushover. Det gør fejlklassificerede sider synlige i
# stedet for at de forsvinder inde i PDL før gatewayen kan undersøge dem.
ShowNumeric=1
ShowMisc=0
# Behold gentagne ALPHA-decodes i råloggen. Racher Pager filtrerer dem efter
# modtagelse, så en mellemkommende støjlinje ikke kan omgå dubletkontrollen.
BlockDuplicate=0

[Log]
Enabled=0
File=
EOF

chmod 0640 "$PDL_STATE_DIR/pdl.ini"
echo "PDL-konfiguration skrevet til $PDL_STATE_DIR/pdl.ini"
echo "Input mode: $INPUT_MODE"
if [[ "$RS232_ENABLED" == "1" ]]; then
  echo "FSK-USB/RS232: port index=$RS232_PORT, serial=$RS232_BITRATE 8N1, decode mode=$RS232_DECODE_MODE"
else
  echo "ALSA capture device: $CAPTURE_DEVICE @ $SAMPLE_RATE Hz"
fi
echo "POCSAG decoder: 512=$BAUD_512 1200=$BAUD_1200 2400=$BAUD_2400 · invert=$INVERT · ALPHA+diagnostisk NUMERIC-output"
