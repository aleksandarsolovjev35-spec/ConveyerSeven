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
