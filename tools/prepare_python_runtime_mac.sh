#!/usr/bin/env bash
set -euo pipefail

PYTHON_MINOR="${PYTHON_MINOR:-3.11}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$ROOT/build"
CACHE_DIR="$BUILD_DIR/cache"
RUNTIME_ROOT="$BUILD_DIR/python"
REQUIREMENTS="$ROOT/requirements.txt"
ARCHS="${ARCHS:-arm64 x64}"

mkdir -p "$CACHE_DIR" "$RUNTIME_ROOT"

resolve_asset_url() {
  local platform="$1"
  node - "$PYTHON_MINOR" "$platform" <<'NODE'
const https = require("https");

const pythonMinor = process.argv[2];
const platform = process.argv[3];
const apiUrl = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest";

https.get(apiUrl, {
  headers: {
    "User-Agent": "jse-installer",
    "Accept": "application/vnd.github+json"
  }
}, (response) => {
  let body = "";
  response.on("data", (chunk) => body += chunk);
  response.on("end", () => {
    if (response.statusCode < 200 || response.statusCode >= 300) {
      console.error(`GitHub API returned ${response.statusCode}: ${body}`);
      process.exit(1);
    }
    const release = JSON.parse(body);
    const asset = release.assets.find((item) =>
      item.name.startsWith(`cpython-${pythonMinor}.`) &&
      item.name.includes(`-${platform}-`) &&
      item.name.endsWith("-install_only.tar.gz")
    );
    if (!asset) {
      console.error(`No python-build-standalone asset found for Python ${pythonMinor} on ${platform}.`);
      process.exit(1);
    }
    console.log(asset.browser_download_url);
  });
}).on("error", (error) => {
  console.error(error.message);
  process.exit(1);
});
NODE
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
    echo "Downloading Python $PYTHON_MINOR standalone runtime for $arch..."
    curl -L "$asset_url" -o "$archive"
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
