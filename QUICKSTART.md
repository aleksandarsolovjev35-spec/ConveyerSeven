# ConveyerSeven — Быстрый старт

> **Проблема: «нечем запускать» — решено.** Теперь есть 3 способа поднять систему без заводского железа.

---

## Вариант А — Симулятор UI (самый лёгкий, 10 секунд)

Не требует камер, COM-порта, весов YOLO и `pywebview`. Один файл `ui_simulation.py` + `opencv-headless` + `fastapi`.

**Windows:**
```bat
setup.bat
.venv\Scripts\python.exe ui_simulation.py --host 0.0.0.0 --port 8000
```
**Linux / macOS / WSL:**
```bash
bash setup.sh --simulation
bash run_simulation.sh
# или: .venv/bin/python ui_simulation.py --host 0.0.0.0 --port 8000
```
Открой `http://127.0.0.1:8000` → нажми **ПУСК** → линия крутится (8 деталей, GOOD/BAD/CLEANUP, архив, графики).

**Что ставится:** `requirements-ci.txt` (numpy, opencv-headless, fastapi, uvicorn, pyserial, SQLAlchemy, structlog, pytest — без torch/ultralytics/pywebview).

---

## Вариант B — Полный `main.py` в симуляции (вся логика ProductionCycle, но без железа)

Использует те же `ProductionCycle`, `DecisionEngine`, `PartArchive`, `DatabaseManager`, что и боевая линия, только камеры/конвейер/распределитель/модели заменены на моки.

**Windows:**
```bat
setup.bat
.venv\Scripts\python.exe main.py --simulation --no-webview --host 127.0.0.1 --port 8000
```
**Linux / macOS:**
```bash
bash setup.sh --simulation
./run.sh --simulation --no-webview --host 0.0.0.0 --port 8000 --open-browser
```
Открой `http://127.0.0.1:8000`. В логе увидишь `simulation=true; webview=False`. Тот же HMI, что и в проде, но с надписью `SIMULATION` у конвейера.

**Ключи `main.py`:**
| Ключ | Зачем |
|---|---|
| `--simulation` | Включает моки (эквивалент `SIMULATION_MODE=true` в .env) |
| `--no-webview` | Не открывать нативное окно pywebview, только HTTP (нужно на Linux/Docker/Arena) |
| `--host / --port` | Куда биндить FastAPI (0.0.0.0 для превью в Arena) |
| `--open-browser` | Открыть браузер автоматически вместо webview |

**Переменная окружения** `SIMULATION_MODE=true` делает то же, что и `--simulation`.

---

## Вариант C — Docker (идентично заводу)

```bash
cp .env.example .env
# поправь ADMIN_PIN в .env (>=8 символов)
docker compose up --build
# UI на http://127.0.0.1:8000
```
По умолчанию `SIMULATION_MODE=true` в `docker-compose.yml` — контейнерам не нужны устройства. Для боевой линии выставь `SIMULATION_MODE=false` и пробрось `/dev/video*` + `/dev/ttyUSB0` (уже прописаны в compose, раскомментируй нужные).

---

## Вариант D — Боевая линия (Windows, завод)

Требует: Windows 10/11, Python 3.11+, 7 USB-камер, COM-контроллер convey15, веса `weights/*.pt` (см. `README.md`).

```bat
setup.bat
run.bat
```
`setup.bat` сам найдёт `py -3.11 / 3.12 / 3.13`, создаст `.venv` и поставит `requirements.txt`. `run.bat` запустит `main.py` с полноэкранным `pywebview`.

---

## Что было сломано и что починено

| Было | Стало |
|---|---|
| Только `run.bat`/`setup.bat` (Windows) — на Linux/macOS/Arena запустить нечем | Добавлены `run.sh`, `setup.sh`, `run_simulation.sh` (кроссплатформенно) |
| `main.py: import webview` — падение `ModuleNotFoundError` без pywebview даже в симуляции | `webview` теперь опционален: `try/except ImportError`, fallback в headless-режим |
| `main.py` требовал `camera_mapping.json` даже в симуляции | В симуляции файл не требуется |
| Нет CLI — нельзя было `main.py --simulation --no-webview` | Добавлен `argparse` (`--simulation`, `--no-webview`, `--host`, `--port`, `--open-browser`) |
| Нет `.env.example` | Добавлен с `SIMULATION_MODE=true` и комментариями |
| `ui_simulation.py` не рекламировался как быстрый старт | Вынесен в Вариант А, работает без весов |

---

## Проверка

```bash
# Симулятор UI
.venv/bin/python ui_simulation.py --host 127.0.0.1 --port 8001 &
curl http://127.0.0.1:8001/api/status | head

# main.py в симуляции (headless)
SIMULATION_MODE=true .venv/bin/python main.py --no-webview --host 127.0.0.1 --port 8002 &
curl http://127.0.0.1:8002/api/status | head

# Тесты
.venv/bin/python -m pytest -q  # 289 passed
.venv/bin/ruff check .          # All checks passed
```

---

## Какой вариант выбрать?

* **Хочу посмотреть UI за 10 секунд** → Вариант А (`ui_simulation`).
* **Хочу прогнать весь ProductionCycle/БД/архив без железа** → Вариант B (`main.py --simulation --no-webview`).
* **Хочу как на заводе, но на Linux** → Вариант C (Docker).
* **Еду на линию** → Вариант D (Windows + `run.bat`).
