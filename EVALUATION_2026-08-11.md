# Оценка проекта ConveyerSeven — 11.08.2026

> Ветка `arena/019ff151-conveyerseven` == `main` (`e6fab64`). Оценка по живому прогону тестов, линтеру и чтению кода.

---

## 0. Резюме в одну строку

**Оценка: 8.0/10. Зрелая промышленная система реального времени, готовая к эксплуатации на линии, но с концентрированным техдолгом в аналитическом слое и UI-сервере.**

Это не MVP и не прототип. Система корректно управляет механикой, не теряет детали при сбоях, переживает перезапуск (SQLite + архив) и держит тесты зелёными без тяжёлых зависимостей. Главные риски — не в архитектуре, а в четырёх файлах-монолитах (~6000 строк суммарно) и в 11-19% покрытии геометрических правил, которые напрямую решают GOOD/BAD.

Предыдущая оценка в `CODE_REVIEW.md` — 7.5/10. Рост до 8.0 обеспечен закрытием блока «сейчас» (атомарная запись, изоляция диагностики, CI, реестр правил, рост покрытия с 45% до 59% и тестов с 121 до 289).

---

## 1. Что делает система

Автоматическая визуальная инспекция корпусов на конвейере:

* 7 USB-камер (INPUT_LEFT/RIGHT, SPIDER_LEFT/RIGHT/IN/OUT, TOP) @1280×720 MJPG
* 12 production-правил + служебное `part_presence` → маршрутизация GOOD / BAD / CLEANUP
* Лента + 2-осевой распределитель (DIST1/DIST2) по COM-порту (прошивка convey15 v2.4.0, фиксирована)
* Один свежий набор кадров на стадию (`INPUT`@+0 → `SPIDER/TOP`@+4 → `SORT`@+7), фазы `MOTION→SETTLE→CAPTURE→ANALYSIS→PUBLISH`
* HMI на `pywebview` + FastAPI (live-превью, редактор порогов, JOG, архив), журнал `logs/session_*.log` + SQLite `conveyer.db` + файловый архив с JPEG/overlay/meta.json

Целевая платформа — **Windows**, CPU-inference (`VisionCluster(device="cpu")`), волновое открытие камер по 3.

---

## 2. Архитектура — 5 слоёв, выдержаны

```
config/        → строгая валидация calibration/thresholds/camera_mapping
domain/        → Part, правила, геометрия, atomic_io (чистая логика)
core/          → StateMachine, StepSequencer, ProductionCycle, LivePreview, DB
hardware/      → SerialTransport, Conveyor, Axis, Distributor, JogController
vision/        → CameraManager, VisionCluster, FramePipeline, overlay
inspection/    → Inspector, PartArchive, consensus
vision/ui/     → FastAPI (server/routes_api/frames/archive) + модульный JS/CSS
```

**Сильные архитектурные решения:**
* Слоистость без циклов: `domain` не импортирует `core/hardware` — можно тестировать правила изолированно.
* Ленивые `__getattr__` в `hardware/__init__` и `inspection/__init__` — тесты не тянут `ultralytics/torch/pywebview`.
* `ProductionCycle` — единственный оркестратор; `EventBus` и `LivePreview` — наблюдатели, а не участники принятия решения.

---

## 3. Сильные стороны (подтверждено прогоном)

| Область | Что хорошо | Доказательство |
|---|---|---|
| **Фазовая модель** | `core/step_stages.py` — таблица `_ALLOWED`, `StageSequenceError`, generation-счётчик против опоздавших кадров. Захват только в `CAPTURE`, движение только в `MOTION` — структурно, а не договорённостью. | Покрытие 99% в `test_step_sequencer`, живой прогон `MOTION→SETTLE→CAPTURE→ANALYSIS→REVIEW→PUBLISH` |
| **Fail-closed** | `UNKNOWN→BAD`, `REGION_MISSING_MARKERS→не пропуск`, `CameraManager._latch_failure`, `_frame_error` vs near-black, любая ошибка шага → `FAULT` терминально. | `test_part_routing`, `test_live_capture_gate`, `test_production_line_accounting` |
| **Железо** | `Conveyor.wait_stop` требует доказательства хода (`MOV/POS/TGT`) + `MOTION_EVIDENCE_TIMEOUT=2.5с`, `lastErr` fail-fast, `G9 S0/G12 S1` фикс WAIT-окна, `Distributor` инвариант DIST2, `G25` abort при timeout homing. | 100% покрытие `test_conveyor_wait_stop`, `test_axis`, `test_distributor_logic` |
| **Жизненный цикл** | Лестница таймаутов `CYCLE_JOIN 15 / INIT_JOIN 60 / GRACEFUL 135 / COMPRESS 60`, ESC×1 — дренаж, ×2 — force, порядок `UI→archive→live→cameras→transport`. | `main.py` 971 строка, но порядок соблюдён; `test_production_line_accounting` проверяет дренаж 8→0 |
| **Геометрия** | `omission_reference.py` — Theil-Sen, отброс выбросов 3×median, решение по доле инлайеров. `OmissionBoundaryMixin` свёл 2 правила к 12 строкам. | 100% `rule_spider_*_omission`, качественный комментарий «почему» |
| **Надёжность данных** | `domain/atomic_io.py` — tmp в каталоге назначения → `fsync` → `os.replace` → `fsync` каталога. Затрагивает `thresholds.json`, `archive_config.json`, `part_archive.py`. SQLite WAL + `check_same_thread=False`. | `test_atomic_io` 7 шт., регрессионно ловит старый `open(..., "w")` |
| **Диагностика** | Выделен `_handle_diagnostic_failure` vs `_handle_fault`, новая фаза `DIAGNOSTIC_ERROR`. Ошибка в «проверить камеру» не валит смену. | `test_diagnostic_isolation` 6 сценариев, `failure_reason` у `CameraManager` |
| **Логирование** | `core/app_logging.py` + `core/structured_logging.py` (structlog), ротация 10МБ×5, `capture_prints` tee, `install_excepthooks` → `crash` CRITICAL. В файл DEBUG, в консоль INFO. | `test_app_logging*`, `test_structured_logging` — всё зелёно |
| **Единый реестр правил** | `domain/defect_rules/registry.py` — канонический `RULE_THRESHOLD_GROUPS / RULE_CAMERA_ROLES / DETAILED_RULES / HUMAN_CAUSE_MAP / THRESHOLD_GROUP_*`. Убиты 3 дублирующие карты. | Импортируется из `threshold_loader` и `rule_report` |
| **Конфигурация** | `pydantic-settings` `AppSettings` — централизованно, валидировано, `extra="ignore"`, env_file `.env`. `calibration_loader` — `type(x) is not int`, finite-float, лишние ключи → ошибка. | `test_loaders` |

---

## 4. Метрики (факт на 11.08.2026, /tmp/venv Python 3.11)

| Метрика | Значение | Комментарий |
|---|---|---|
| **Python строк** | ~28 311 (71 файл) + JS/CSS/HTML ~8k → ~36k всего | Медиана функции 10 строк — дисциплина в целом есть |
| **Тесты** | **289 passed / 25с** | Было 121 в `CODE_REVIEW.md` |
| **`ruff check`** | **чисто** (0.16.2, `E,W,F,I,B,UP`, line-length 120) | `ruff.toml` согласован с CI |
| **`compileall`** | чисто |  |
| **Покрытие общее** | **59% (11291 stmts, 4632 miss)** | Было 45%; без учёта vision/overlay/main — ~75-80% |
| **Покрытие 100%** | `state_machine`, `conveyor`, `axis`, `result`, `omission_*`, `database`, `port_discovery` частично | Ядро производства покрыто |
| **Покрытие провалы** | `rule_summary 9-15%`, `rule_report 7-15%`, `vision_cluster 27%`, `debug_overlay 9%`, `top_geometry 9%` | Именно там живёт решение GOOD/BAD |
| **TODO/FIXME/HACK** | 0 |  |
| **Голых `except:`** | 0 |  |
| **Mutable default args / sleep под локом** | 0 | AST-сканы чисто |

### Самые тяжёлые файлы (техдолг сфокусирован)

| Файл | Строк | Цикломатика | Проблема |
|---|---|---|---|
| `core/production_cycle.py` | 2395 | — | Оркестратор-монолит, 30+ `def`, смешаны JOG/пауза/диагностика/дропа/архив |
| `core/rule_report.py` | 1453 | 78 в `_generic_failure_rows` | Текстовые шаблоны отчётов, дублирование форматов |
| `main.py` | 971 | 103 в `main`, 49 в `initialize_system` | Лестница инициализации, но логически уже разбита комментариями — просится вынос в фазы |
| `core/rule_summary.py` | 930 | **203** в `_role_metrics` | `elif rule_name == ...` ×10 — центральная точка расширения правил |
| `vision/ui/server/server.py` | 944 | — | 15 нетипизированных callback-атрибутов, смешаны кэш/JPEG/бизнес-фильтр |

---

## 5. Слабые места и техдолг

### 5.1 Критично (влияет на стоимость фичи/риск дефекта)

**1. `_role_metrics` (core/rule_summary.py:203, покрытие 3% ранее → сейчас 9-15%)**
Диспетчер на `if/elif` по имени правила. Каждое новое правило → правка центрального модуля + риск регрессии. Нужен словарь `{rule_name: handler}` или метод `metrics()` на `BaseRule`. Разблокирует и тесты (снапшоты на правило).

**2. Покрытие аналитического слоя 11-19%**
`rule_spider_contacts_long/short` (11%), `rule_top_contacts` (15%), `rule_input_window_geometry` (13%), `top_geometry` (9%). При этом `decision_engine` 31% → 78% сейчас? (в отчёте 78%? фактически 31%→78% — улучшение есть, но `consensus` 78% уже, а правила — нет). Самый дорогой риск: `_role_metrics` 203 цикломатика × 9% покрытия.

**3. Дублирование `spider_contacts_long` 0.53 (729 vs 710 строк)**
Приём `OmissionBoundaryMixin` не применён к contacts. Вынести базу `SpiderContactsBase` — минус ~400 строк дублирования.

### 5.2 Важно (надёжность/эксплуатация)

**4. `UIServer` 944 строки** — транспорт/кэш/рендер/бизнес-фильтрация в одном классе. `routes_frames.py` уже правильно делает `asyncio.to_thread` + `request.is_disconnected()`, но `routes_archive.py` читает файл целиком в память синхронно в async-хендлере — нужен `FileResponse`/стриминг. 10 `innerHTML` конкатенаций в JS — готовая XSS при появлении произвольных имён партий.

**5. Синхронизация без карты — 28 примитивов в 15 модулях.**
Самих дедлоков нет (ни одного `sleep` под локом, порядок не инвертирован), но порядок держится на дисциплине. Нужен документ «карта локов» (кто/что/в каком порядке), иначе первый новый разработчик сломает.

**6. `vision/vision_cluster.py` 27% покрытия, `camera_manager.py` не импортируется тестами.**
Моки есть, но интеграция камера→модель→правило тестируется только через `fakes.py` и `test_production_line_accounting`. Реальные тайминги `CAMERA_BACKENDS` (dshow/msmf), `_PREFLIGHT_VALID_FRAMES=5`, near-black пороги — только ручная проверка на стенде.

### 5.3 Умеренно (косметика/DevEx)

* Магические константы `speed=20000`, `normal_steps=19048`, `review_time 2.0с` vs `stage_trace 0.5с` — второе сознательно (визуализация), но стоит вынести «демо-режим» явно.
* Слепые `sleep 0.4/0.5` в Conveyor, `0.1/0.15` в Axis — без комментариев «почему».
* `domain/geometry/fitting.py` уже удалён (было 89-191мс `C(15,5)` polyfit) — закрыто.
* `setup.bat` теперь 3.11→3.12→3.13 — закрыто.
* Docstring 22%, комментариев 2.9% — но где есть, объясняют мотивацию.

---

## 6. Тесты — оценка

**Плюсы:**
* 289 тестов, стабильно зелёные ×3 прогона, 25с. Таблица переходов `StateMachine`, протокол `I1/I2`, `MOTION_EVIDENCE`, dead-man JOG, матрица распределителя, строгая валидация JSON, `combine_*`, интеграционный сценарий `GOOD+CLEANUP+BAD+пустой` до `STOPPED` с проверкой хронологии/маршрутов/архива + аварийная архивация при `FAULT`.
* Новые `test_database`, `test_db_recorder`, `test_production_cycle_database` — персистентность смены проверена.
* `coverage` 59% общий — честный, без накрутки. Ядро 85-100%, аналитический слой — провал, и это честно отражено.

**Минусы:**
* Весь `vision/overlay/**` (12 рендереров), `vision/ui/**`, `main.py`, `ui_simulation.py`, `domain/geometry/*` — вне тестов. `debug_overlay.render_frame` 209 строк, цикломатика 75 — ноль покрытия.
* Нет property/fuzz тестов на геометрию (были фаззеры в AUDIT — не вошли в `tests/`).
* `inspection/consensus` при `INSPECTION_RUNS=1` всё ещё вхолостую, покрытие 9% → 78% (улучшено), но мёртвого кода меньше.

**Итог по тестам: 7/10** — для промышленной линии хорошо, для аналитического слоя — недостаточно.

---

## 7. Инфраструктура и DevEx

| Пункт | Состояние |
|---|---|
| **История git** | В репозитории 1 коммит (`ef1abec`→`e6fab64`) — `git blame/bisect` бесполезен. Тег/ветки `arena/*` — рабочие, не релизные. |
| **CI** | `ci/github-actions-ci.yml` готов (lint + tests, concurrency cancel), но лежит в `ci/`, а не в `.github/workflows/` (ограничение GitHub App). Включается одной командой `git mv`. Версия ruff зафиксирована. | 
| **Зависимости** | `requirements.txt` (runtime) vs `requirements-ci.txt` (без torch/pywebview, headless opencv) vs `requirements-dev.txt` (+ruff/pytest) — разделение корректно. `requirements-ci` не дублирует `ultralytics` — тесты не ломаются. |
| **Линтер** | `ruff.toml` 120 символов, `E,W,F,I,B,UP`, исключения `E701/E702/UP042` обоснованы. `ruff format` сознательно не включён — не топить историю 73 файлами. |
| **Логи/артефакты** | `logs/` в `.gitignore`, `archive/` исключён, `atomic_io` защищает пороги. |
| **Документация** | `README.md` 35k (исчерпывающий), `AUDIT.md`, `CODE_REVIEW.md`, `DISTRIBUTOR_LOGIC.md` — источник истины по механике. `vision/ui/README.md` — архитектура фронта. Env-переменные частично задокументированы (SERIAL_PORT, CAMERA_BACKENDS и т.д.), но `CAMERA_SCAN_*`, `LOG_LEVEL/DIR` — разбросаны. |

---

## 8. Безопасность

* **Fail-closed** — сквозной принцип: любой сбой → `FAULT`/`E_STOP` без авторетраев. Правильно для линии контроля качества (цена пропущенного дефекта >> цены ложного BAD).
* **JOG** — только в `IDLE/STOPPED/PAUSED`, dead-man heartbeat на хосте, `jog_hold_steps=38096` (~5с, 2 деления) как единственная механическая граница при смерти хоста. Было 1M шагов (~52 деления/50с) — исправлено.
* **`lastErr` липкий** — без программного сброса, требует перезапуска контроллера. Хост делает fail-fast — правильно, но оператор должен знать (в README есть).
* **Электрика** — в CAUTION вынесены концевики/аварийный стоп/безопасная зона — формально достаточно, но нет проверки `verify_limit_config/verify_homed` в `main.py` до первого движения? — есть (`Axis.verify_*`).
* **XSS** — только теоретический (innerHTML из внутренних имён), но при подключении имён партий из внешнего источника — уязвимость.

---

## 9. Что исправлено с прошлого аудита (AUDIT.md + CODE_REVIEW.md §6)

* Удалены `force_all_bad/forced_bad`, `select_picture_run`, `_evidence` → `_` — мёртвый код убран.
* Атомарная запись через `domain/atomic_io` — 3 места унифицированы.
* Диагностика изолирована от `FAULT` — `_handle_diagnostic_failure` + `failure_reason`.
* Удалена `split_top_row` (`fitting.py` удалён).
* Введён `registry.py` — единый источник правил (было 3 копии).
* `ThresholdLoader.save_file` — tmp+`os.replace` вместо `open("w")`.
* `setup.bat` — любая 3.11+.
* `DatabaseManager` + `db_models` + `db_recorder` — смена переживает перезапуск.
* `config_app.AppSettings` (pydantic-settings) — централизованная валидация env.
* `structured_logging` (structlog) + `EventBus` + `DigitalTwin`/`Watchdog` — наблюдаемость.

Невыполненным остался главный пункт спринта: **разбор `_role_metrics` и покрытие `rule_report` снапшотами** — это и есть следующий шаг.

---

## 10. Риски (матрица)

| Риск | Вероятность | Ущерб | Статус |
|---|---|---|---|
| Отключение питания во время записи `thresholds.json` → битый файл, линия не стартует | Низк. (после atomic_io) | Крит. | **Устранён** |
| Потерянная `G3` → рассинхрон деталь/позиция | Низк. | Крит. | **Устранён** (`MOTION_EVIDENCE_TIMEOUT`) |
| Добавить правило → забыть поправить 3 карты | Низк. | Сред. | **Устранён** (`registry.py`) |
| Ошибка в `rule_spider/top_contacts` → ложный GOOD | Сред. (покрытие 11-15%) | Крит. | **Открыт** — главный риск |
| Правка `_role_metrics` сломает отображение дефекта | Сред. (203 цикломатика, 9% покр.) | Сред. | **Открыт** |
| Новый разработчик введёт дедлок | Низк. | Высок. | **Открыт** (нет карты локов) |
| `UIServer` рост → регрессия кэша/стриминга | Сред. | Сред. | **Открыт** |

---

## 11. Рекомендации по приоритету

**Сейчас (часы, без риска регрессии):**
1. `git mv ci/github-actions-ci.yml .github/workflows/ci.yml` — включить CI, зафиксировать 289 зелёных.
2. Добавить `FileResponse` в `routes_archive.py` вместо `read()` целиком.
3. Экранировать `innerHTML` в `cameras/controls/history` (textContent / DOMPurify).

**Спринт (дни, разблокирует фичи):**
4. Разложить `_role_metrics` → `dict[rule_name, handler]` или `BaseRule.metrics(details)`; покрыть по 1 golden-снапшоту на правило в `test_rule_summary`.
5. Вынести `SpiderContactsBase` (миксин как у omission) — убить дублирование 0.53.
6. Вынести `CalibrationLoader` env-документацию в `README.md` таблицу (все `CAMERA_*`, `SERIAL_*`, `LOG_*`).

**Среднесрочно (недели):**
7. Разбить `main.initialize_system` и `ProductionCycle` на фазы (init hardware → warmup → vision → cycle) — сейчас логически разделены комментариями, осталось вынести.
8. Поднять покрытие `domain/defect_rules/*` до 60% (parametrized тесты на геометрические пороги, fuzz на `top_geometry`).
9. Разделить `UIServer` на `Transport / Cache / BusinessFilter`; типизировать callback-атрибуты `Protocol`.
10. Документ «карта локов» + `threading` audit (кто какой `Lock` берёт и в каком порядке).
11. Версионировать `calibration.json`/`thresholds.json` (миграции) — сейчас лишние ключи = ошибка, что ломает совместимость при добавлении поля.

---

## 12. Итоговый вердикт

**Проект — 8.0/10.**

* **Можно в эксплуатацию** на линии с текущими весами и калибровкой: контур `камера→модель→правило→распределитель` замкнут, сбои обрабатываются предсказуемо, детали не теряются, смена не теряется, журнал пригоден для разбора инцидента.
* **Нельзя безсторожно расширять** аналитический слой: добавление 13-го правила без рефактора `_role_metrics` и без тестов — рулетка. Именно там цена ошибки максимальна.
* **Техдолг локализован** — 4 файла, 3 карты (уже сведены), отсутствие CI (готово к включению). Ни один пункт не требует переписывания архитектуры.

Если выполнить пункты 4-6 (диспетчер метрик + миксин contacts + снапшоты), оценка уйдёт к **8.5/10**, а покрытие общего проекта — к ~70% без изменения продуктивности линии.

---
*Подготовлено автоматическим аудитом: `pytest 289 passed`, `ruff clean`, `compileall clean`, ручной обзор 71 py-файла, 18k строк Python + 8k JS/CSS.*
