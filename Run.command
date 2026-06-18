#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v node >/dev/null 2>&1; then
  osascript -e 'display dialog "JSE needs Node.js first. Install the LTS version from https://nodejs.org, then run this again." buttons {"OK"} default button "OK" with icon caution' >/dev/null
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  osascript -e 'display dialog "JSE needs Python 3 first. Install Python from https://www.python.org/downloads/macos/, then run this again." buttons {"OK"} default button "OK" with icon caution' >/dev/null
  exit 1
fi

if [[ ! -d ".venv" ]]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

if [[ ! -d "node_modules" ]]; then
  npm install
fi

export PYTHON="$PWD/.venv/bin/python"
npm run start
