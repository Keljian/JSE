#!/usr/bin/env bash
set -euo pipefail

PYTHON_VERSION="${PYTHON_VERSION:-3.11.15}"
PYTHON_BUILD_RELEASE="${PYTHON_BUILD_RELEASE:-20260610}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$ROOT/build"
CACHE_DIR="$BUILD_DIR/cache"
RUNTIME_ROOT="$BUILD_DIR/python"
REQUIREMENTS="$ROOT/requirements.txt"
ARCHS="${ARCHS:-arm64 x64}"

mkdir -p "$CACHE_DIR" "$RUNTIME_ROOT"

resolve_asset_url() {
  local platform="$1"
  printf 'https://github.com/astral-sh/python-build-standalone/releases/download/%s/cpython-%s%%2B%s-%s-install_only.tar.gz\n' \
    "$PYTHON_BUILD_RELEASE" "$PYTHON_VERSION" "$PYTHON_BUILD_RELEASE" "$platform"
}

prepare_arch() {
  local arch="$1"
  local platform=""
  local runtime_dir=""

  case "$arch" in
    arm64)
      platform="aarch64-apple-darwin"
      runtime_dir="$RUNTIME_ROOT/macos-arm64"
      ;;
    x64)
      platform="x86_64-apple-darwin"
      runtime_dir="$RUNTIME_ROOT/macos-x64"
      ;;
    *)
      echo "Unsupported macOS arch: $arch" >&2
      exit 1
      ;;
  esac

  local asset_url
  asset_url="$(resolve_asset_url "$platform")"
  local archive="$CACHE_DIR/$(basename "$asset_url")"

  if [[ ! -f "$archive" ]]; then
    echo "Downloading Python $PYTHON_VERSION standalone runtime for $arch..."
    curl --fail --location --retry 3 --retry-delay 2 "$asset_url" -o "$archive"
  fi

  echo "Extracting Python runtime for $arch..."
  rm -rf "$runtime_dir"
  mkdir -p "$runtime_dir"
  tar -xzf "$archive" -C "$runtime_dir" --strip-components 1

  local python_bin="$runtime_dir/bin/python3"

  echo "Installing Python dependencies for $arch..."
  PYTHONNOUSERSITE=1 "$python_bin" -m pip install --upgrade --ignore-installed --no-warn-script-location -r "$REQUIREMENTS"

  echo "Verifying bundled Python for $arch..."
  PYTHONNOUSERSITE=1 "$python_bin" -c "import selenium, openai, google.generativeai, requests, pdfplumber, docx; print('bundled mac python ok')"
}

for arch in $ARCHS; do
  prepare_arch "$arch"
done
