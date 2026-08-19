#!/usr/bin/env bash
set -euo pipefail

STATE_ROOT="${PAGER_STATE_ROOT:-/var/lib/racher-pager}"
DB_PATH="${PAGER_DB_PATH:-$STATE_ROOT/pager.db}"
PDL_CONFIG="${PDL_CONFIG_PATH:-$STATE_ROOT/pdl/pdl.ini}"
PDL_LOG="${PDL_LOG_PATH:-$STATE_ROOT/pdl.log}"
RAW_RX_SINCE="${PDL_DIAG_RAW_RX_SINCE:--30 min}"
POCSAG_SINCE="${PDL_DIAG_POCSAG_SINCE:-$RAW_RX_SINCE}"

section() { printf '\n===== %s =====\n' "$1"; }

section "Services"
for unit in racher-pdl.service racher-pager-system-agent.service racher-pager-fsk-status.timer; do
  printf '%-38s %s\n' "$unit" "$(systemctl is-active "$unit" 2>/dev/null || true)"
done

section "FSK / serial"
if [[ -r "$DB_PATH" ]] && command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 -batch -noheader "$DB_PATH" <<'SQL' 2>/dev/null || true
SELECT key || '=' || value
FROM runtime_status
WHERE key IN (
  'fsk_usb_connected','fsk_usb_device','fsk_usb_real_device','fsk_usb_driver',
  'fsk_usb_serial_config','fsk_usb_decode_mode','fsk_usb_four_level','fsk_usb_pdl_in_use'
)
ORDER BY key;
SQL
else
  echo "Runtime-status ikke tilgængelig."
fi

section "PDL decoder config"
if [[ -r "$PDL_CONFIG" ]]; then
  awk '
    /^\[/ { section=$0 }
    /^(Baud512|Baud1200|Baud2400|ShowBoth|Invert|DecodeMode|FourLevel|ShowTone|ShowNumeric|ShowMisc)=/ {
      print section " " $0
    }
  ' "$PDL_CONFIG"
else
  echo "Mangler: $PDL_CONFIG"
fi

section "Rå FSK-input, metadata-only ($RAW_RX_SINCE)"
if command -v journalctl >/dev/null 2>&1; then
  raw_rx="$({ journalctl -u racher-pdl.service --since "$RAW_RX_SINCE" --no-pager 2>/dev/null || true; } | grep '\[FSK-RX\]' | tail -n 40 || true)"
  if [[ -n "$raw_rx" ]]; then
    printf '%s\n' "$raw_rx"
  else
    echo "Ingen [FSK-RX]-summaries i perioden."
  fi
else
  echo "journalctl er ikke tilgængelig."
fi

section "POCSAG preamble scan 512/1200/2400, metadata-only ($POCSAG_SINCE)"
if command -v journalctl >/dev/null 2>&1; then
  pocsag_scan="$({ journalctl -u racher-pdl.service --since "$POCSAG_SINCE" --no-pager 2>/dev/null || true; } | grep '\[POCSAG-SCAN\]' | tail -n 120 || true)"
  if [[ -n "$pocsag_scan" ]]; then
    printf '%s\n' "$pocsag_scan"
  else
    echo "Ingen [POCSAG-SCAN]-summaries i perioden."
  fi
else
  echo "journalctl er ikke tilgængelig."
fi

section "POCSAG-1200 preamble, metadata-only ($POCSAG_SINCE)"
if command -v journalctl >/dev/null 2>&1; then
  pocsag_preamble="$({ journalctl -u racher-pdl.service --since "$POCSAG_SINCE" --no-pager 2>/dev/null || true; } | grep '\[POCSAG-PREAMBLE\].*baud=1200' | tail -n 80 || true)"
  if [[ -n "$pocsag_preamble" ]]; then
    printf '%s\n' "$pocsag_preamble"
  else
    echo "Ingen [POCSAG-PREAMBLE] 1200-summaries i perioden."
  fi
else
  echo "journalctl er ikke tilgængelig."
fi

section "POCSAG før sync, metadata-only ($POCSAG_SINCE)"
if command -v journalctl >/dev/null 2>&1; then
  pocsag_presync="$({ journalctl -u racher-pdl.service --since "$POCSAG_SINCE" --no-pager 2>/dev/null || true; } | grep '\[POCSAG-PRESYNC\]' | tail -n 100 || true)"
  if [[ -n "$pocsag_presync" ]]; then
    printf '%s\n' "$pocsag_presync"
  else
    echo "Ingen [POCSAG-PRESYNC]-summaries i perioden."
  fi
else
  echo "journalctl er ikke tilgængelig."
fi

section "POCSAG efter sync, metadata-only ($POCSAG_SINCE)"
if command -v journalctl >/dev/null 2>&1; then
  pocsag_diag="$({ journalctl -u racher-pdl.service --since "$POCSAG_SINCE" --no-pager 2>/dev/null || true; } | grep '\[POCSAG-DIAG\]' | tail -n 100 || true)"
  if [[ -n "$pocsag_diag" ]]; then
    printf '%s\n' "$pocsag_diag"
  else
    echo "Ingen [POCSAG-DIAG]-summaries i perioden."
  fi
else
  echo "journalctl er ikke tilgængelig."
fi

section "Seneste PDL-output fordelt på type og bitrate"
if [[ -r "$PDL_LOG" ]]; then
  tail -n 2000 "$PDL_LOG" | awk '
    /POCSAG/ {
      mode=""; type=""; baud=""
      for (i=1; i<=NF; i++) {
        if ($i ~ /^POCSAG/) { mode=$i; type=$(i+1); baud=$(i+2); break }
      }
      if (mode != "" && baud ~ /^[0-9]+$/) counts[type " " baud]++
    }
    END {
      total=0
      for (key in counts) total += counts[key]
      print "POCSAG-linjer=" total
      for (key in counts) print counts[key], key
    }
  ' | sort -nr
else
  echo "Mangler: $PDL_LOG"
fi

section "Gateway-resultat fordelt på bitrate og beslutning"
if [[ -r "$DB_PATH" ]] && command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 -batch -header -column "$DB_PATH" <<'SQL' 2>/dev/null || true
SELECT
  COALESCE(CAST(baud AS TEXT), 'ukendt') AS baud,
  CASE WHEN delivery_eligible=1 THEN 'leveres'
       ELSE COALESCE(NULLIF(suppressed_reason,''), 'undertrykt') END AS resultat,
  COUNT(*) AS antal
FROM messages
WHERE source LIKE 'pdl%'
GROUP BY baud, resultat
ORDER BY antal DESC;
SQL
else
  echo "Gateway-database ikke tilgængelig."
fi

section "Logaktivitet"
if [[ -e "$PDL_LOG" ]]; then
  stat -c 'size_bytes=%s modified=%y' "$PDL_LOG" 2>/dev/null || ls -l "$PDL_LOG"
else
  echo "PDL-log findes ikke."
fi

printf '\nDiagnosen viser kun konfiguration og optællinger; alarmtekst og RIC/capcodes udskrives ikke.\n'
