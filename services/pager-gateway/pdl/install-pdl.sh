#!/usr/bin/env bash
set -euo pipefail

PDL_REPO="https://github.com/sqpp/PDL.git"
PDL_COMMIT="f37a24ee45b06f35703d513d48780c9334c4ff89"
PDL_ROOT="${PDL_ROOT:-/opt/racher-pager}"
PDL_SRC="${PDL_SRC:-$PDL_ROOT/src/PDL}"
PDL_PREFIX="${PDL_PREFIX:-$PDL_ROOT/pdl}"
PDL_BUILD_JOBS="${PDL_BUILD_JOBS:-2}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "PDL kan kun bygges på Linux. Kør dette script på Raspberry Pi / Debian / Ubuntu." >&2
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "Denne installer understøtter pt. Debian/Raspberry Pi OS/Ubuntu (apt)." >&2
  exit 1
fi

ARCH="$(uname -m)"
echo "Installerer PDL 3.2.0 på Linux/$ARCH"

echo "[1/5] Installerer build-afhængigheder..."
sudo apt-get update
sudo apt-get install -y \
  git cmake g++ pkg-config python3 ffmpeg \
  libssl-dev libasound2-dev libpulse-dev libgtk-3-dev \
  libwebkit2gtk-4.1-dev libcurl4-openssl-dev

echo "[2/5] Henter PDL ved fastlåst upstream commit..."
sudo mkdir -p "$PDL_ROOT/src" "$PDL_PREFIX" "$PDL_ROOT/bin"
sudo chown -R "$(id -u):$(id -g)" "$PDL_ROOT/src" "$PDL_PREFIX" "$PDL_ROOT/bin"

if [[ ! -d "$PDL_SRC/.git" ]]; then
  git clone "$PDL_REPO" "$PDL_SRC"
fi

git -C "$PDL_SRC" fetch --tags origin
git -C "$PDL_SRC" reset --hard
git -C "$PDL_SRC" clean -fdx
git -C "$PDL_SRC" checkout --detach "$PDL_COMMIT"

echo "[3/5] Tilføjer Racher headless-mode og diagnostik..."
python3 "$SCRIPT_DIR/patch_headless.py" "$PDL_SRC"
python3 "$SCRIPT_DIR/patch_headless_direct_decode.py" "$PDL_SRC"
python3 "$SCRIPT_DIR/patch_hw_decode_diag.py" "$PDL_SRC"
python3 "$SCRIPT_DIR/patch_rx_diag_periodic.py" "$PDL_SRC"
python3 "$SCRIPT_DIR/patch_pocsag_512_diag.py" "$PDL_SRC"
python3 "$SCRIPT_DIR/patch_pocsag_1200_preamble.py" "$PDL_SRC"
python3 "$SCRIPT_DIR/patch_pocsag_1200_diag.py" "$PDL_SRC"

echo "[4/5] Bygger PDL..."
cmake -S "$PDL_SRC" -B "$PDL_SRC/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$PDL_PREFIX"
cmake --build "$PDL_SRC/build" --parallel "$PDL_BUILD_JOBS"
cmake --install "$PDL_SRC/build"

echo "[5/5] Verificerer binary..."
if ! "$PDL_PREFIX/bin/pdl" --help 2>&1 | grep -q -- "--headless"; then
  echo "FEJL: Den byggede PDL-binary indeholder ikke --headless." >&2
  exit 1
fi

cat > "$PDL_ROOT/bin/pdl-version" <<EOF
PDL_VERSION=3.2.0
PDL_UPSTREAM_COMMIT=$PDL_COMMIT
PDL_ARCH=$ARCH
EOF

echo
echo "PDL er bygget og installeret:"
echo "  $PDL_PREFIX/bin/pdl"
echo "  upstream commit: $PDL_COMMIT"
echo "Næste trin: ./install-pdl-service.sh"