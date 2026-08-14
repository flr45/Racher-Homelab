#!/usr/bin/env bash
set -euo pipefail

STATE_ROOT="${PAGER_STATE_ROOT:-/var/lib/racher-pager}"
PDL_STATE_DIR="${PDL_STATE_DIR:-$STATE_ROOT/pdl}"
CAPTURE_DEVICE="${PDL_CAPTURE_DEVICE:-default}"
SAMPLE_RATE="${PDL_SAMPLE_RATE:-48000}"
BAUD_512="${PDL_BAUD_512:-1}"
BAUD_1200="${PDL_BAUD_1200:-1}"
BAUD_2400="${PDL_BAUD_2400:-1}"
INVERT="${PDL_INVERT:-0}"

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
Enabled=1
Invert=$INVERT
CaptureDevice=$CAPTURE_DEVICE
PlaybackDevice=default

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
echo "Capture device: $CAPTURE_DEVICE"
echo "POCSAG: 512=$BAUD_512 1200=$BAUD_1200 2400=$BAUD_2400"
