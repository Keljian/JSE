#!/usr/bin/env bash
set -euo pipefail

echo "Building JSE macOS redistributables..."

export CSC_IDENTITY_AUTO_DISCOVERY=false

./tools/prepare_python_runtime_mac.sh
corepack npm install
corepack npm run dist:mac

echo ""
echo "macOS output:"
find ./release -maxdepth 1 \( -name "*.dmg" -o -name "*mac*.zip" -o -name "*.zip" \) -print
