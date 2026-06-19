#!/usr/bin/env bash
set -euo pipefail

PYTHON_VERSION="${PYTHON_VERSION:-3.11.15}"
PYTHON_BUILD_RELEASE="${PYTHON_BUILD_RELEASE:-20260610}"
ARCH="${ARCH:-x64}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$ROOT/build"
CACHE_DIR="$BUILD_DIR/cache"
RUNTIME_ROOT="$BUILD_DIR/python"
REQUIREMENTS="$ROOT/requirements.txt"

case "$ARCH" in
  x64)
    platform="x86_64-unknown-linux-gnu"
    runtime_dir="$RUNTIME_ROOT/linux-x64"
    ;;
  arm64)
    platform="aarch64-unknown-linux-gnu"
    runtime_dir="$RUNTIME_ROOT/linux-arm64"
    ;;
  *)
    echo "Unsupported Linux architecture: $ARCH" >&2
    exit 1
    ;;
esac

mkdir -p "$CACHE_DIR" "$RUNTIME_ROOT"

asset_url="https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_BUILD_RELEASE}/cpython-${PYTHON_VERSION}%2B${PYTHON_BUILD_RELEASE}-${platform}-install_only.tar.gz"
archive="$CACHE_DIR/$(basename "$asset_url")"

if [[ ! -f "$archive" ]]; then
  echo "Downloading Python $PYTHON_VERSION standalone runtime for Linux $ARCH..."
  curl --fail --location --retry 3 --retry-delay 2 "$asset_url" -o "$archive"
fi

echo "Extracting Python runtime for Linux $ARCH..."
rm -rf "$runtime_dir"
mkdir -p "$runtime_dir"
tar -xzf "$archive" -C "$runtime_dir" --strip-components 1

python_bin="$runtime_dir/bin/python3"

echo "Installing Python dependencies for Linux $ARCH..."
PYTHONNOUSERSITE=1 "$python_bin" -m pip install \
  --upgrade \
  --ignore-installed \
  --no-warn-script-location \
  -r "$REQUIREMENTS"

echo "Verifying bundled Python for Linux $ARCH..."
PYTHONNOUSERSITE=1 "$python_bin" -c \
  "import selenium, openai, google.generativeai, requests, pdfplumber, docx; print('bundled linux python ok')"
