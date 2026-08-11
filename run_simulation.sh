#!/usr/bin/env bash
# Быстрый запуск лёгкого симулятора UI без железа/весов/pywebview
# Открой http://127.0.0.1:8000 в браузере и нажми ПУСК
set -e
cd "$(dirname "$0")"

if [ ! -f ".venv/bin/python" ]; then
  echo "[SIM] .venv не найден — ставлю лёгкие зависимости..."
  bash setup.sh --simulation
fi

PORT="${1:-8000}"
HOST="${HOST:-0.0.0.0}"
echo "[SIM] Запуск ui_simulation на http://${HOST}:${PORT} ..."
exec .venv/bin/python ui_simulation.py --host "$HOST" --port "$PORT"
