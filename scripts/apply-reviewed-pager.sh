#!/usr/bin/env bash
# Install a pinned review without losing the host's unpushed/local changes.
set -Eeuo pipefail
umask 077

APP_DIR="${SBR_PAGER_ROOT:-/opt/SBR-Pager-Gateway}"
REVIEW_SHA="${1:-}"
[[ "$REVIEW_SHA" =~ ^[0-9a-f]{40}$ ]] || {
  echo "Angiv den gennemgåede commit-SHA som første argument." >&2; exit 1;
}
cd "$APP_DIR"
[[ -f .env ]] || { echo "Serverens .env mangler." >&2; exit 1; }
git fetch --no-tags origin codex/pager-reliability-review
[[ "$(git rev-parse FETCH_HEAD)" == "$REVIEW_SHA" ]] || {
  echo "Review-grenen er ændret; installationen stopper før kode- eller driftsændringer." >&2; exit 1;
}

# This rollout was prepared for the topology inspected on racherserver.
# Read labels and mounts only; never display the containers' environment.
python3 - <<'PY'
import json
from pathlib import Path
import subprocess

expected = {
    'sbr-sms-whatsapp': ('sms-whatsapp', 'sms-whatsapp', 'sms-whatsapp_sms_whatsapp_data'),
    'sbr-pager-sms-gateway': ('sms-gateway', 'sms-gateway', 'sms-gateway_sms_gateway_data'),
    'racher-sms-openwa': ('sms-whatsapp', 'openwa', None),
}
for name, (project, service, volume) in expected.items():
    data = json.loads(subprocess.check_output(['docker', 'inspect', name]))[0]
    labels = data['Config'].get('Labels') or {}
    if labels.get('com.docker.compose.project') != project or labels.get('com.docker.compose.service') != service:
        raise SystemExit(f'Stop: Compose-projekt eller service afviger for {name}')
    if volume and not any(m.get('Type') == 'volume' and m.get('Name') == volume and m.get('Destination') == '/data' for m in data['Mounts']):
        raise SystemExit(f'Stop: datavolumen afviger for {name}')
driver = ''
for line in Path('.env').read_text().splitlines():
    key, sep, value = line.strip().partition('=')
    if sep and key == 'SMS_MODEM_DRIVER':
        driver = value.strip().strip('\"\'').lower()
if driver != 'cudy':
    raise SystemExit('Stop: kontrollér SMS_MODEM_DRIVER i .env; denne installation bevarer aktiv Cudy-drift')
PY

STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
BACKUP_DIR="$(git rev-parse --git-path pager-review-backups)/$STAMP"
mkdir -p "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"
chmod 700 "$BACKUP_DIR"
ORIGINAL_SHA="$(git rev-parse HEAD)"
ORIGINAL_BRANCH="$(git symbolic-ref --quiet --short HEAD || true)"
BACKUP_BRANCH="backup/pager-before-review-$STAMP"
git branch "$BACKUP_BRANCH" "$ORIGINAL_SHA"
cp .env "$BACKUP_DIR/env.backup"
chmod 600 "$BACKUP_DIR/env.backup"
git archive --format=tar HEAD | gzip > "$BACKUP_DIR/committed-source.tar.gz"
git diff --binary --no-ext-diff > "$BACKUP_DIR/working-tree.patch"
git diff --cached --binary --no-ext-diff > "$BACKUP_DIR/index.patch"
printf '%s\n' "$ORIGINAL_SHA" > "$BACKUP_DIR/old-head.txt"
printf '%s\n' "$ORIGINAL_BRANCH" > "$BACKUP_DIR/old-branch.txt"

STASH_SHA=""
SOURCE_SWITCHED=false
restore_source() {
  local status="$1"
  trap - ERR INT TERM
  if [[ "$SOURCE_SWITCHED" == true ]]; then
    if ! git switch "${ORIGINAL_BRANCH:-$BACKUP_BRANCH}"; then
      echo "Koden kunne ikke skiftes tilbage automatisk. Original kode: $BACKUP_DIR" >&2
      exit "$status"
    fi
  fi
  if [[ -n "$STASH_SHA" ]]; then
    git stash apply --index "$STASH_SHA" || {
      echo "Lokale rettelser kræver manuel gendannelse fra $BACKUP_DIR; stash $STASH_SHA er bevaret." >&2
      exit "$status"
    }
  fi
  echo "Installation stoppet. Den oprindelige kode er gendannet; backup: $BACKUP_DIR" >&2
  exit "$status"
}
trap 'restore_source $?' ERR
trap 'restore_source 130' INT
trap 'restore_source 143' TERM

if ! git diff --quiet || ! git diff --cached --quiet; then
  git stash push -m "SBR Pager før gennemgået opdatering $STAMP"
  STASH_SHA="$(git rev-parse refs/stash)"
  printf '%s\n' "$STASH_SHA" > "$BACKUP_DIR/stash.txt"
fi
SOURCE_SWITCHED=true
git switch -c "deploy/pager-review-$STAMP" "$REVIEW_SHA"

# The established updater backs up SQLite/env/images, keeps OpenWA untouched,
# and rolls application images back if startup/health fails.
bash scripts/update-sbr-pager.sh
trap - ERR INT TERM
echo "Gennemgået opdatering installeret: $REVIEW_SHA"
echo "Original kode og lokale rettelser er gemt: $BACKUP_DIR"
echo "Original Git-gren: $BACKUP_BRANCH. En eventuel stash er bevaret."
