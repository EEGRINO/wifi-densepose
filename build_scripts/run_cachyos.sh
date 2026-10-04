#!/usr/bin/env bash
# Запуск десктоп-приложения на CachyOS / Arch-семействе (venv + зависимости).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/desktop_app"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck source=/dev/null
source .venv/bin/activate
pip install -U pip wheel
pip install -r requirements.txt

exec python ruview_desktop.py
