# ConveyerSeven — глубокий анализ: мёртвый код, избыточность, ошибки

Дата: 2026-08-11. Ветка: `arena/019ff188-conveyerseven` от `d119877`.

## 0. Как проверялось

| Инструмент | Результат |
|---|---|
| `pytest` | **297 passed / 25 c** |
| `ruff check .` (конфиг проекта) | **чисто** |
| `ruff --select ALL` | 6 800+ замечаний, почти все стилистические (ANN/D/RUF001) |
| `vulture --min-confidence 60` | 88 кандидатов (большинство — ложные, разобраны ниже) |
| AST-скан «определено, но нигде не используется» (py + js + html + json + md) | см. §1 |
| Скан неиспользуемых CSS-классов и JS-функций | см. §3 |
| Сверка реестра правил ↔ реализаций ↔ `thresholds.json` | **расхождений 0** |
| Живой прогон `ui_simulation.py` | пуск → 15 шагов → дренаж без потерь (8 деталей: GOOD 4 / BAD 3 / CLEANUP 1) → `STOPPED`, 0 ошибок в логе |
| Фаззинг API (мусорные роли, режимы, команды, path traversal) | все 400/404, обхода нет |
| `coverage` | **47% общий**, 60% по `core/` |

Проект в хорошем состоянии: тесты зелёные, линтер чистый, TODO/FIXME — ноль, голых `except:` — ноль,
mutable default args — ноль, валидация входа API строгая. Найденное ниже — это то, что осталось
под этим слоем.

---

## 1. КРИТИЧНО: реальные ошибки

### 1.1. Редактирование порогов в HMI сломано наглухо (403 навсегда)

`vision/ui/server/routes_api.py:129-146` требует два заголовка:

```python
is_admin = (x_operator_role or "").casefold() == "admin"
valid_pin = (settings.admin_pin != "CHANGE_ME"
             and x_admin_pin is not None
             and hmac.compare_digest(x_admin_pin, settings.admin_pin))
if not is_admin or not valid_pin:
    return JSONResponse({"ok": False, "error": "Требуются роль Admin и X-Admin-Pin"}, status_code=403)
```

А фронтенд отправляет запрос вообще без этих заголовков —
`vision/ui/static/js/thresholds.js:417` → `apiPostJson('/api/thresholds', {...})`, и `apiPostJson`
(`core.js:384`) ставит только `Content-Type`. Во всём `static/js/` строк `X-Admin-Pin` /
`X-Operator-Role` нет ни одной, UI-элемента для ввода PIN или выбора роли в `index.html` тоже нет.

Проверено эмпирически:

```
no headers          -> 403 {'ok': False, 'error': 'Требуются роль Admin и X-Admin-Pin'}
admin + default pin -> 403 (потому что admin_pin == "CHANGE_ME" отвергается по условию)
```

**Последствие:** кнопка «Сохранить» в панели порогов не работает никогда, ни при какой
конфигурации. Оператор видит «Не удалось сохранить пороги». При этом `README.md:135` прямо
обещает: «Пороги можно менять в HMI в состояниях IDLE/STOPPED; изменения сохраняются в файл».

**Почему не поймали тесты:** ни один тест не дёргает `POST /api/thresholds` — покрытие
`routes_api.py` 33%. Тест на 403 без заголовков прошёл бы и на исправном коде.

**Варианты решения** (нужно решение владельца, это вопрос модели безопасности, а не багфикс):
* а) добавить в HMI поле PIN и слать `X-Admin-Pin` + `X-Operator-Role: admin` из `saveThresholds()`;
* б) если HMI и так физически защищён (киоск на 127.0.0.1), снять требование PIN и
  оставить существующую защиту по состоянию линии (`thresholds_editable()` — IDLE/STOPPED);
* в) промежуточный: требовать PIN только когда `ADMIN_PIN` реально задан в `.env`.

В любом случае нужен тест на успешный путь POST.

### 1.2. `FramePipeline` — мёртвая подсистема, создаётся и никогда не запускается

`main.py:450` конструирует `FramePipeline(cameras, vision, inspector, event_bus)` и кладёт в
`self.pipeline`, `main.py:614` передаёт его в `ProductionCycle`. Но `pipeline.start()` не
вызывается **нигде** в продакшн-коде — только в `tests/test_frame_pipeline.py`. Потоки
`cv-grabber-producer` / `cv-inference-consumer` не стартуют, `is_running` всегда `False`.

Следствия по всей цепочке:

* `ProductionCycle` работает с ним только через `if pipeline.is_running:` (`production_cycle.py:988,1262`) —
  все эти ветки мёртвые;
* подписка `event_bus.subscribe("vision:result_ready", self._on_vision_result_ready)`
  (`production_cycle.py:98`) никогда не срабатывает — событие эмитит только `_inference_loop`;
* поле `self._async_vision_results` (`production_cycle.py:95,207`) пишется в обработчике, который
  не вызывается, и **не читается нигде**;
* `_pipeline_latest_vision` / `_pipeline_latest_health` (`production_cycle.py:1254-1255`) только
  присваиваются `None`, и потом читаются через `getattr` в `1515-1516` и `1619-1620` — то есть
  `precomputed_vision` всегда `None`. Это ~40 строк «совместимости» вокруг всегда-пустого значения;
* `get_raw_frames()`, `results_queue`, алиасы `VisionWorker` / `LatestFramePipeline`
  (`frame_pipeline.py:355-356`) — не используются никем;
* протоколы `FrameSource` / `FrameInferencer` / `ResultConsumer` (`frame_pipeline.py:21-38`) —
  не используются как аннотации нигде.

**Важно:** это не случайный недосмотр, а осознанно нейтрализованная архитектура. Комментарий в
`production_cycle.py:1248-1253` объясняет, почему асинхронный кадр нельзя использовать в инспекции
(его timestamp не привязан к физическому шагу ленты → можно проанализировать соседнюю деталь).
То есть pipeline был признан опасным для production-контура и обезврежен, но не удалён.

**Рекомендация:** удалить `vision/frame_pipeline.py` (356 строк) + `tests/test_frame_pipeline.py`
(240 строк) + проводку в `main.py` и `production_cycle.py`, либо — если он нужен как будущий
задел под live-превью — явно задокументировать «сознательно не запускается» и убрать хотя бы
мёртвые поля `_async_vision_results` / `_pipeline_latest_*`. Сейчас это ~600 строк, которые
выглядят рабочими и вводят в заблуждение при чтении.

### 1.3. `MockCamera` не реализует часть контракта `CameraManager`

`main.py:149` в пути восстановления камер делает `getattr(cameras, "reopen_roles", None)`.
У `MockCamera` нет `reopen_roles`, `open_cameras`, `load_config`. Спасает только `getattr`-защита,
но это значит, что путь восстановления слабых камер **вообще не проверяется в симуляции** —
там, где его удобнее всего было бы протестировать. Дрейф контракта: если завтра кто-то вызовет
`reopen_roles` без `getattr`, симуляция упадёт, а тесты этого не покажут.

---

## 2. Мёртвый Python-код (можно удалять)

Проверено сквозным поиском по `.py`, `.js`, `.html`, `.json`, `.md`, `.yml`, `.sh`, `.bat` —
у перечисленного нет ни одной ссылки, кроме определения.

| Что | Где | Строк | Комментарий |
|---|---|---|---|
| `hardware/interfaces.py` целиком | весь файл | 52 | `ISerialTransport` / `IConveyor` / `ICamera` не импортируются **нигде**, даже в аннотациях. Protocol-контракты, которые ничего не контролируют |
| `HardwareConnectionError` | `core/exceptions.py:10` | 3 | не поднимается и не ловится нигде. Соседние `VisionModelError` / `SafetyStopError` — используются |
| `StateMachine.request_emergency_stop` | `core/state_machine.py:149` | 3 | переход `E_STOP` не инициируется ниоткуда. При этом `_TRANSITIONS` строит E_STOP-переходы из всех состояний (`state_machine.py:66-67`), а `production_cycle.py:1950` проверяет `State.E_STOP` — состояние недостижимо. По AUDIT.md §1.2 модель безопасности — «любая ошибка → FAULT», так что E_STOP просто лишний |
| `FrameSource`, `FrameInferencer`, `ResultConsumer` | `frame_pipeline.py:21-38` | 20 | см. §1.2 |
| `VisionWorker`, `LatestFramePipeline` | `frame_pipeline.py:355-356` | 2 | «алиасы совместимости» без потребителей |
| `FramePipeline.get_raw_frames` | `frame_pipeline.py:320` | 11 | см. §1.2 |
| `_async_vision_results` + `_on_vision_result_ready` | `production_cycle.py:95,205-207` | 6 | см. §1.2 |
| `LiveMonitorApi.choose_archive_folder` | `vision/ui/live_monitor.py:17` | 15 | JS-мост `pywebview.api.choose_archive_folder` не вызывается ни из одного `.js`. Выбор папки архива в UI сделан текстовым полем |
| `TeeStream.writelines` | `core/app_logging.py:137` | 3 | не вызывается; для файлоподобного объекта формально полезен, но `print` его не использует |
| `PartArchive.startup_error` | `part_archive.py:87,92,146` | — | присваивается в трёх местах, **не читается никогда**. Ошибка старта архива молча теряется — стоит либо показывать в HMI, либо убрать |

**Итого безопасно удаляемого: ~120 строк** (без учёта §1.2, где ещё ~600).

### Ложные срабатывания vulture — НЕ удалять

Отмечаю, чтобы никто не «почистил» по отчёту инструмента:

* все `api_*` / `get_*` функции в `routes_api.py`, `routes_frames.py`, `routes_archive.py` —
  это FastAPI-хендлеры, регистрируются декоратором;
* `_configure_connection` (`database.py:90`) — SQLAlchemy `@event.listens_for`, критичен (WAL);
* `__getattr__` в `hardware/`, `vision/`, `inspection/`, `vision/ui/__init__.py` — ленивые импорты,
  на них держится возможность ставить `requirements-ci.txt` без torch/GTK;
* методы `rescan` / `next_camera` / `assign_current` / `back` / `save` / `finish` / `get_frame`
  в `camera_calibration_console.py` — вызываются из `calibration.js` через `pywebview.api.*`;
* поля `model_config` в pydantic-классах — служебные;
* `camera_warmup_seconds` и др. в `AppSettings` — читаются через env, см. ниже.

### Конфиг: поля объявлены, но не используются

`core/config_app.py` документирован как «единая точка конфигурации: код обязан получать
`AppSettings`, а не читать `os.environ` напрямую». Фактически это правило нарушено:

| Поле `AppSettings` | Реальность |
|---|---|
| `camera_warmup_seconds`, `camera_pre_preview_warmup_seconds`, `camera_recovery_warmup_seconds` | **не читаются через settings**; `main.py:407` берёт `CAMERA_WARMUP_SECONDS` напрямую через `_env_clamped_float(os.environ...)`, `camera_manager.py:26,37,88` — тоже напрямую |
| `log_level`, `log_dir` | `app_logging.py:74-76` читает `os.environ["LOG_LEVEL"]` / `["LOG_DIR"]` сам; через settings идёт только `configure_structlog(self.settings.log_dir)` |
| `serial_baud`, `serial_port` | используются |
| `calibration_file`, `thresholds_file`, `archive_config_file`, `camera_mapping_file` | используются |

Итог: валидация pydantic (`ge=0.5, le=10.0` и т.п.) для warmup-полей **не применяется** — вместо
неё дублирующий самодельный клампинг в `main.py`. Два источника истины на один параметр.
Стоит либо провести warmup/log через `settings`, либо убрать неиспользуемые поля из схемы.

---

## 3. Фронтенд: мёртвые CSS и JS

Все 12 JS и 13 CSS подключены в `index.html` — лишних файлов нет. Но внутри:

**Мёртвые JS-функции (2):**
* `faNewCollectThresholds` — `frame-analysis.js:57`
* `stopStatusPolling` — `core.js:534` (определена, не вызывается — polling никогда не
  останавливается штатно; не баг, но и не нужна)

**Мёртвые CSS-классы — 102 из ~420.** Ключевые группы:

| Файл | Мёртвых | Что именно |
|---|---|---|
| `blocks.css` | 37 | ~20 из них (`calibration-*`, `role-name`, `role-camera`, `config-path`, `assigned-count`, `candidate-position`, `saved-message`, `fatal-error`) относятся к **мастеру калибровки**, у которого своя `calibration.css` и своя `index.html`. То есть `blocks.css` (грузится в основном HMI) содержит правила для чужой страницы. Ещё 15 — `fa-*` от **старой** версии панели анализа: живая версия использует префикс `fa-new-*` |
| `process.css` | 23 | `chute-*`, `gate-*`, `token-hold`, `conveyor-belt`, `cell-hold`, `prestart-diagnostics` — визуализация лотков/заслонок, которой в текущем `index.html` нет |
| `axis.css` | 12 из 16 | `axis-idle/moving/ready/homing/open/opening/closing/fault/waiting` — генерируются динамически в `status.js:345,360` (`axis-${state.toLowerCase()}`), **это ложное срабатывание, не удалять**. А вот `axis-bar`, `axis-bar-fill`, `axis-panel` — настоящий мусор |
| `base.css` | 9 | `state-box-*` / `state-*` — тоже динамические (`status.js:315-316`), **не удалять** |
| `history-strip.css` | 3 | `cat-good/bad/cleanup` — динамические (`history.js:34`), **не удалять** |
| `jog.css` | 7 | `jog-hw-*`, `jog-btn-label`, `jog-icon` — настоящий мусор |
| `thresholds.css` | 6 | `fa-obj-*`, `fa-empty-status` — от старой панели анализа |
| `stats.css` | 3 | `defects-empty`, `line-cells-section`, `stats-title` |

Реально мёртвых, за вычетом динамических: **~78 классов**. Самое ценное — вычистить дубли
старой панели `fa-*` (живёт `fa-new-*`) и вынести калибровочные правила из `blocks.css`
в `calibration.css`: сейчас основной HMI тянет CSS чужой страницы.

---

## 4. Избыточные файлы

### 4.1. Документация: 119 КБ, четыре отчёта об одном и том же

| Файл | Размер | Статус |
|---|---|---|
| `README.md` | 35 КБ | актуален, единственный, на который ссылаются остальные |
| `DISTRIBUTOR_LOGIC.md` | 3 КБ | актуален, контракт железа |
| `QUICKSTART.md` | 5.6 КБ | полезен, но 19-21% пересечение с AUDIT/EVALUATION |
| `AUDIT.md` | 17 КБ | **отчёт о прошлой работе**, ссылки только из двух других отчётов |
| `CODE_REVIEW.md` | 29 КБ | **устарел фактически**: заявляет ревизию `ef1abec`, «121 passed», «coverage 45%», «11 файлов тестов». Сейчас: 297 тестов, 27 файлов, 47%/60% |
| `EVALUATION_2026-08-11.md` | 24 КБ | **устарел фактически**: заявляет «289 passed», «покрытие 59% (11291 stmts)». Сейчас 297 тестов и 47% (9457 stmts) — цифры не сходятся ни по одному измерению |
| `README_SIMPLIFIED.md` | 4.6 КБ | сам себя объявляет неактуальным («актуальная документация — в README.md»), на него никто не ссылается |

Три отчёта (`AUDIT` + `CODE_REVIEW` + `EVALUATION` = **70 КБ, 826 строк**) — это исторические
снимки состояния, которые уже разошлись с кодом и будут расходиться дальше. Они не документация,
а changelog-и в форме аудита. Рекомендую: удалить `CODE_REVIEW.md`, `EVALUATION_2026-08-11.md`,
`README_SIMPLIFIED.md`, из `AUDIT.md` перенести в `README.md` раздел «осознанные проектные решения»
(единственная часть, которая не устаревает и объясняет неочевидные вещи — FAULT терминален,
JOG только для центровки, `stage_trace_time: 0.5`). Git-история отчёты сохранит.

### 4.2. CI не включён

`ci/github-actions-ci.yml` лежит вне `.github/workflows/` (`.github/` в репозитории нет вообще).
Причина объяснена в `ci/README.md` — GitHub App не имеет права `workflows`. Итог: **никакой
автоматической проверки на PR не выполняется**, `ruff` и `pytest` гоняются только вручную.
Это и объясняет, как бага §1.1 доехала до main. Нужен один `git mv` от пользователя с
обычными правами.

### 4.3. Docker-конфигурация нерабочая для целевой платформы

* `README`/`EVALUATION` называют целевой платформой **Windows**, а `docker-compose.yml`
  пробрасывает `/dev/video0..6` и `/dev/ttyUSB0` — Linux-only. Compose-путь применим только
  для симуляции;
* `Dockerfile` ставит `requirements.txt`, куда входит `pywebview>=5.3` — в образе нет ни GTK,
  ни Qt (поставлены только `libglib2.0-0 libgl1 libgomp1 libusb-1.0-0`). `import webview`
  в `main.py:11` обёрнут в try/except, так что не падает, но пакет тянется зря;
* `CMD ["python", "main.py"]` без `--no-webview` — в контейнере всегда пойдёт по ветке
  fallback с сообщением об ошибке. Логичнее `CMD ["python","main.py","--no-webview","--host","0.0.0.0"]`;
* `docker-compose.yml` требует `ADMIN_PIN`, который (см. §1.1) ни на что не влияет.

---

## 5. Прочие замечания (не баги, но стоит знать)

1. **Четыре файла-монолита.** `production_cycle.py` 2422 строки, `rule_report.py` 1453,
   `main.py` 1052, `server.py` 964, `rule_summary.py` 930. Это ~6800 строк, треть Python-кода.
   При этом покрытие `rule_summary.py` — **10%**, `rule_report.py` — **18%**, хотя именно они
   формируют то, что оператор видит на экране.

2. **Провал покрытия в правилах отбраковки.** Модули, которые буквально решают GOOD/BAD:
   `rule_spider_contacts_long.py` 11%, `rule_spider_contacts_short.py` 11%,
   `rule_input_window_geometry.py` 13%, `top_geometry.py` 9%, `rule_top_platform_overlap.py` 15%.
   Это самый большой риск проекта: ошибка в геометрии здесь означает брак, уехавший в GOOD,
   и она ничем не будет поймана.

3. **Дублирование в spider-правилах.** `rule_spider_contacts_long.py` и `..._short.py` — 52%
   совпадения, 5 идентичных блоков по 15-31 строке (118 строк дословных дублей). Кандидат
   на общий базовый класс.

4. **`object` затеняет builtin** — `rule_summary.py:42`, параметр `_metric(..., object=None)`.
   Работает, но это единственный `A002` в проекте.

5. **87 широких `except Exception`.** В основном оправданы (fail-safe в shutdown, в потоках
   записи), но `hardware/distributor.py:77,167,173,177` — в путях аварийного глушения осей,
   где стоит логировать хотя бы тип исключения: сейчас причина отказа homing теряется.

6. **`self.parts` меняется без блокировки.** `production_cycle.py` читает/пишет список
   деталей из рабочего потока и из `_build_status()`, дёргаемого HTTP-хендлером
   (`.append`/`.remove` в 1561/1829, чтение в 1040, 1108, 1959, 2246). Спасает GIL и то, что
   `list(self.parts)`/`len()` атомарны; но `2246` делает snapshot, а `1195/1218/1321` итерируют
   напрямую — теоретически возможен `RuntimeError: list changed size during iteration`.
   За прогон симуляции не воспроизвелось, но лучше закрыть тем же `_operation_lock`.

7. **`db_recorder.flush()` создаёт `threading.Timer` на каждый вызов** (`db_recorder.py:167`) —
   можно проще через `deadline = time.monotonic() + timeout`. Вызывается редко, не критично.

---

## 6. План действий по приоритету

**Сейчас (ломает функциональность):**
1. Починить `POST /api/thresholds` — выбрать модель авторизации (§1.1) и добавить тест на успешный путь.
2. Перенести `ci/github-actions-ci.yml` → `.github/workflows/ci.yml` (нужен пользователь с правом `workflows`).

**Дальше (снимает ~700 строк мёртвого кода):**
3. Решить судьбу `FramePipeline` (§1.2): удалить или явно задокументировать + убрать мёртвые поля.
4. Удалить `hardware/interfaces.py`, `HardwareConnectionError`, `request_emergency_stop`,
   `choose_archive_folder`, `writelines`, алиасы `VisionWorker`/`LatestFramePipeline` (§2).
5. Убрать `startup_error` или начать его показывать в HMI.
6. Свести конфиг к одному источнику: warmup/log через `AppSettings` либо убрать поля из схемы (§2).

**Потом (гигиена):**
7. Удалить `CODE_REVIEW.md`, `EVALUATION_2026-08-11.md`, `README_SIMPLIFIED.md`; ценное из
   `AUDIT.md` — в `README.md` (§4.1).
8. Вычистить ~78 мёртвых CSS-классов, вынести `calibration-*` из `blocks.css` (§3).
9. Поднять покрытие геометрических правил — это единственный пункт, который реально снижает
   риск брака на линии (§5.2).
10. Docker: `--no-webview --host 0.0.0.0` в `CMD`, убрать `pywebview` из образа (§4.3).

---

## 7. Что проверено и оказалось в порядке

Чтобы не тратить время на повторную проверку:

* реестр правил `domain/defect_rules/registry.py` ↔ 13 реализаций ↔ `thresholds.json` —
  полная взаимная согласованность, ни одного лишнего/недостающего ключа, ROLES совпадают;
* все 97 порогов в `thresholds.json` используются правилами соответствующих ролей;
* все 12 JS и 13 CSS подключены; все API-эндпоинты, кроме `/stream/{role}`,
  `/api/diagnostics/cameras`, `/api/diagnostics/vision-rules`, вызываются из фронтенда
  (эти три — вызываются, но через POST/EventBus, проверено отдельно);
* валидация входа API: мусорные роли/режимы/команды и path traversal — 400/404, обхода нет;
* порядок shutdown в `main.py` корректен (UI → архив → pipeline → live → камеры → COM → БД),
  комментарии объясняют, почему именно так;
* `EventBus` потокобезопасен, `emit` копирует список хендлеров под локом;
* `StepStages` с generation-счётчиком — лучший модуль проекта, 97% покрытия;
* дренаж линии при остановке не теряет детали (проверено живым прогоном);
* `atomic_io` + WAL + `close_open_sessions` — устойчивость к обесточиванию реализована честно.
