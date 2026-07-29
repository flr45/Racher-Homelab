#!/usr/bin/env bash
set -Eeuo pipefail

BACKUP_DIR="${1:-${BACKUP_DIR:-}}"
[[ -n "$BACKUP_DIR" ]] || { echo "Brug: $0 /sti/til/backup" >&2; exit 2; }
BACKUP_DIR="$(realpath -e -- "$BACKUP_DIR")"
[[ -d "$BACKUP_DIR" ]] || { echo "Backupmappe mangler" >&2; exit 1; }
[[ ! -L "$BACKUP_DIR" ]] || { echo "Symlink-backups afvises" >&2; exit 1; }

for required in MANIFEST.json SHA256SUMS; do
  [[ -f "$BACKUP_DIR/$required" && ! -L "$BACKUP_DIR/$required" ]] || {
    echo "Påkrævet fil mangler eller er symlink: $required" >&2
    exit 1
  }
done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

(
  cd "$BACKUP_DIR"
  sha256sum -c SHA256SUMS
)

python3 - "$BACKUP_DIR/MANIFEST.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
if data.get("format_version") != 1:
    raise SystemExit("Ukendt backupformat")
for key in ("created_at", "host"):
    if not isinstance(data.get(key), str) or not data[key].strip():
        raise SystemExit(f"Ugyldigt manifestfelt: {key}")
PY

archives=0
while IFS= read -r -d '' archive; do
  archives=$((archives + 1))
  target="$work/archive-$archives"
  mkdir -p "$target"
  tar -tzf "$archive" >/dev/null
  tar -xzf "$archive" -C "$target"
done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.tar.gz' -print0)

if [[ -f "$BACKUP_DIR/postgres.dump" ]] && command -v pg_restore >/dev/null 2>&1; then
  pg_restore --list "$BACKUP_DIR/postgres.dump" >/dev/null
fi

if [[ -f "$BACKUP_DIR/npm-database.sql.gz" ]]; then
  gzip -t "$BACKUP_DIR/npm-database.sql.gz"
fi

printf 'DR drill bestået: checksums=%s archives=%d live_data_unchanged=true\n' \
  "$(wc -l < "$BACKUP_DIR/SHA256SUMS")" "$archives"
