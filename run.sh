#!/usr/bin/env bash
# ConveyerSeven — кроссплатформенный запуск (Linux / macOS / WSL)
# Использование:
#   ./run.sh                  # production (требует камеры, COM, веса)
#   ./run.sh --simulation     # симуляция без железа (рекомендуется для проверки)
#   ./run.sh --help           # показать опции main.py

set -e
cd "$(dirname "$0")"

# Создать venv если нет
if [ ! -f ".venv/bin/python" ]; then
  echo "[SETUP] .venv не найден — создаю..."
  if [ -f "setup.sh" ]; then
    bash setup.sh
  else
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -r requirements.txt
  fi
fi

exec .venv/bin/python main.py "$@"
