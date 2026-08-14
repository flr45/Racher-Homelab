#!/usr/bin/env bash
set -euo pipefail

STATE_ROOT="${PAGER_STATE_ROOT:-/var/lib/racher-pager}"
PDL_STATE_DIR="${PDL_STATE_DIR:-$STATE_ROOT/pdl}"
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
ShowTone=1
ShowNumeric=1
ShowMisc=1
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
echo "POCSAG decoder: 512=$BAUD_512 1200=$BAUD_1200 2400=$BAUD_2400"
