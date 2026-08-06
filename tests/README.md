# Тесты

Покрывают изменения последних правок: одиночный прогон инспекции (вместо
тройного голосования 2 из 3), атомарную публикацию снимка в UI и
синхронизацию «сначала монитор, потом UI».

## Backend (Python)

Используют стандартный `unittest`, сторонних dev-зависимостей не требуется —
нужны только зависимости проекта (`pip install -r requirements.txt`).

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

`test_ui_server_http.py` использует `fastapi.testclient` (для HTTP-запросов
к реальному UIServer). На свежих версиях starlette (1.x) для этого нужен
`pip install httpx2`. Если TestClient недоступен — тест пропускается с
пояснением, остальной набор не падает.

- `test_consensus_single_run.py` — `INSPECTION_RUNS == 1`:
  проход результатов насквозь, отклонение многопрогоновых входов,
  part_presence, выбор «картинки», сводка model health.
- `test_rule_report_single_run.py` — форма строк отчёта: один замер
  в `run_cards`, `vote_details` с `total_runs == 1`.
- `test_ui_server_atomic_publish.py` — `frame_version` (`_cache_version`)
  растёт только при реальном изменении содержимого; повторные публикации
  тех же объектов не бампают версию; run_frames+run_rule_results
  публикуются одним вызовом.
- `test_inspector_single_run.py` — интеграция Inspector с фейковыми
  vision/decision: пустой лоток и деталь, defect rules выполняются один раз,
  run_frames/run_rule_results по одному элементу.
- `test_production_cycle_single_capture.py` — `_stage_capture` снимает один
  раз; `_stage_analysis` публикует единый снимок (кадры + run_frames +
  run_rule_results одним вызовом); слияние INPUT+SPIDER.
- `test_ui_server_http.py` — интеграционный HTTP-тест UIServer через
  FastAPI TestClient: /api/status, /frame (RAW/RULES/run/preview),
  /api/mode, /api/active_camera, /api/cameras, обработка edge cases.
- `test_conveyor.py` — параметры конвейера (speed, accel,
  steps_per_division) сохраняются в атрибуты: UI-анимация линии учитывает
  реальную скорость из calibration.json.
- `test_cycle_integration.py` — полный производственный цикл на фейковом
  «железе» (камеры/конвейер/распределитель/инспектор): деталь создаётся
  на входе, на +4 проверяется spider, на +7 уходит (BAD -> сброс); пустые
  лотки считаются; каждый шаг публикует единый снимок (кадры + правила +
  статус линии).
- `test_camera_mapping.py` — загрузка и валидация camera_mapping.json.
- `test_state_machine.py` — переходы StateMachine (START/STOP/PAUSE/FAULT),
  колбэки, exit/force-exit.
- `test_step_stages.py` — строгий порядок фаз StepSequencer (нарушение
  порядка -> StageSequenceError), передача камер инспекции, reset.
- `test_live_preview_gate.py` — LiveCaptureGate: пауза блокирует live-чтения,
  вложенные паузы, ожидание активного чтения, reset.
- `test_conveyor.py` — параметры конвейера сохраняются в атрибуты
  (speed/accel/steps_per_division), невалидная геометрия отклоняется.
- `test_axis.py` — Axis: валидация, move_absolute, home, парсинг статусов,
  verify_homed.
- `test_distributor.py` — Distributor: валидация позиций, маршруты
  BAD/CLEANUP, status для UI, cancel_check, emergency_stop.
- `test_decision_engine.py` — DecisionEngine: создание из thresholds.json,
  правила по ролям, пустые детекции.
- `test_part_archive.py` — PartArchive: буферизация кадров, финализация,
  meta.json, run1-изображения (run2/run3 не создаются при одиночном
  прогоне), zip-сжатие, отключённый архив.

## Frontend (Node)

Запускаются через VM с заглушенным DOM (нет браузера и pywebview):

```bash
node tests/frontend/test_sync_gate.mjs
node tests/frontend/test_single_run_ui.mjs
node tests/frontend/test_dead_code.mjs
```

- `harness.mjs` — фейковый DOM + загрузка UI-модулей в том же порядке,
  что и в `templates/index.html`.
- `test_sync_gate.mjs` — при статичной публикации анализа цвет корпуса на
  линии, новые корпуса, карточки правил и превью ждут кадр главной камеры
  и применяются одним махом; fallback-таймер; в движении гейт не включается;
  движение линии гейтом не блокируется.
- `test_single_run_ui.mjs` — один замер на порог, бейджи без счётчика,
  отсутствие `data-run`, одиночное число detections, строка «КАРТИНКА»
  без номера прогона.
- `test_dead_code.mjs` — удалённые функции/состояние отсутствуют в рантайме
  и в исходниках (хоткей N, run-cyclable, fa-run-status, set_run_rule_results
  и т.п.).
