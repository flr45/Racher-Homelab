#!/usr/bin/env bash
set -Eeuo pipefail

pass=0
warn=0
fail=0

ok() { printf '[OK] %s\n' "$*"; pass=$((pass + 1)); }
warning() { printf '[ADVARSEL] %s\n' "$*"; warn=$((warn + 1)); }
error() { printf '[FEJL] %s\n' "$*" >&2; fail=$((fail + 1)); }

if [[ "$(uname -s)" == "Linux" ]]; then
  ok "Linux registreret"
else
  error "Kun Linux understøttes"
fi

arch="$(uname -m)"
case "$arch" in
  aarch64|arm64) ok "64-bit ARM-arkitektur: $arch" ;;
  x86_64|amd64) warning "AMD64 registreret; Pi-installation forventer ARM64" ;;
  *) error "Ikke-understøttet arkitektur: $arch" ;;
esac

if [[ -r /proc/meminfo ]]; then
  mem_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
  if [[ "${mem_kb:-0}" -ge 3800000 ]]; then
    ok "Mindst 4 GB RAM"
  elif [[ "${mem_kb:-0}" -ge 1900000 ]]; then
    warning "2 GB RAM er minimum; 4 GB eller mere anbefales"
  else
    error "Mindre end 2 GB RAM"
  fi
fi

root_free_kb="$(df -Pk / | awk 'NR==2 {print $4}')"
if [[ "${root_free_kb:-0}" -ge 20971520 ]]; then
  ok "Mindst 20 GB ledig systemdisk"
else
  error "Der kræves mindst 20 GB ledig systemdisk"
fi

for command in sudo apt-get curl git openssl; do
  if command -v "$command" >/dev/null 2>&1; then
    ok "$command findes"
  else
    error "$command mangler"
  fi
done

if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then
    ok "Docker Engine svarer"
  else
    warning "Docker findes, men brugeren har ikke aktiv adgang endnu"
  fi
else
  warning "Docker installeres af bootstrap-scriptet"
fi

if command -v vcgencmd >/dev/null 2>&1; then
  throttle="$(vcgencmd get_throttled 2>/dev/null || true)"
  if [[ "$throttle" == "throttled=0x0" ]]; then
    ok "Ingen Pi-throttling registreret"
  else
    warning "Pi rapporterer: ${throttle:-ukendt}"
  fi
fi

printf '\nResultat: %d OK, %d advarsler, %d fejl\n' "$pass" "$warn" "$fail"
[[ "$fail" -eq 0 ]]
