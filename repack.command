#!/usr/bin/env bash
# Fast repack for macOS: UI build + package only.
# Run ./build_installer_mac.sh once first (downloads the Python runtime).
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x "build/python/macos-arm64/bin/python3" ] && [ ! -x "build/python/macos-x64/bin/python3" ]; then
  echo "ERROR: Bundled Python runtime is missing."
  echo "Run ./build_installer_mac.sh once for a full build first."
  exit 1
fi

export CSC_IDENTITY_AUTO_DISCOVERY=false
npm run dist:mac

echo
echo "Done. Distributables:"
ls -1 release/*.dmg release/*mac*.zip 2>/dev/null || true
