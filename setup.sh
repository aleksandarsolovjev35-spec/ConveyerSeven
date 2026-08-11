#!/usr/bin/env bash
# ConveyerSeven — установка зависимостей (Linux / macOS / WSL)
# Создаёт .venv и ставит зависимости. Для симуляции достаточно лёгкого набора.
set -e
cd "$(dirname "$0")"

PY=""
for cand in python3.12 python3.11 python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11+ не найден. Установите python3."
  exit 1
fi

echo "[SETUP] Python: $($PY --version)"

if [ ! -d ".venv" ]; then
  echo "[SETUP] Создаю .venv через $PY -m venv"
  "$PY" -m venv .venv
fi

.venv/bin/pip install --upgrade pip

MODE="${1:-}"
if [ "$MODE" = "--simulation" ] || [ "$MODE" = "--light" ]; then
  echo "[SETUP] Ставлю лёгкие зависимости для симуляции (без torch/ultralytics)..."
  .venv/bin/pip install -r requirements-ci.txt
  echo "[SETUP] Готово. Запуск: ./run.sh --simulation --no-webview"
else
  echo "[SETUP] Ставлю requirements.txt (полный набор)..."
  # На headless-серверах opencv-python требует libGL; при ошибке ставим headless
  if ! .venv/bin/pip install -r requirements.txt; then
    echo "[SETUP] Полная установка не удалась — пробую headless-вариант..."
    sed 's/opencv-python[^ ]*/opencv-python-headless>=4.10/' requirements.txt > /tmp/requirements_headless.txt
    .venv/bin/pip install -r /tmp/requirements_headless.txt
  fi
  echo "[SETUP] Готово. Запуск:"
  echo "  Production:  ./run.sh"
  echo "  Симуляция:   ./run.sh --simulation --no-webview"
fi
