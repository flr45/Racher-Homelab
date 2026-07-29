#!/usr/bin/env bash
set -Eeuo pipefail

log() { printf '[upgrade] %s\n' "$*"; }
fail() { printf '[upgrade] FEJL: %s\n' "$*" >&2; exit 1; }

[[ "${EUID}" -ne 0 ]] || fail "Kør som normal bruger, ikke root."
command -v git >/dev/null 2>&1 || fail "git mangler."
command -v docker >/dev/null 2>&1 || fail "Docker mangler."

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${RACHER_ENV_FILE:-$REPO_ROOT/.env}"
HEALTH_URL="${RACHER_HEALTH_URL:-http://127.0.0.1:81}"
OLD_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD)"
TARGET="${1:-origin/main}"

[[ -f "$ENV_FILE" ]] || fail ".env mangler."
git -C "$REPO_ROOT" diff --quiet && git -C "$REPO_ROOT" diff --cached --quiet || fail "Arbejdstræet har lokale ændringer."

rollback() {
  log "Ruller tilbage til $OLD_COMMIT"
  git -C "$REPO_ROOT" reset --hard "$OLD_COMMIT"
  "$REPO_ROOT/scripts/install-racher-os.sh" || true
}
trap 'rollback' ERR

git -C "$REPO_ROOT" fetch --prune origin
git -C "$REPO_ROOT" rev-parse --verify "$TARGET^{commit}" >/dev/null 2>&1 || fail "Målet findes ikke: $TARGET"
git -C "$REPO_ROOT" merge-base --is-ancestor "$OLD_COMMIT" "$TARGET" || fail "Kun fast-forward upgrades understøttes."
git -C "$REPO_ROOT" checkout --detach "$TARGET"
"$REPO_ROOT/scripts/install-racher-os.sh"
curl --fail --silent --show-error --max-time 10 "$HEALTH_URL" >/dev/null
trap - ERR
log "Upgrade gennemført fra $OLD_COMMIT til $(git -C "$REPO_ROOT" rev-parse HEAD)"
