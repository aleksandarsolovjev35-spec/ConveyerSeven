# Line Monitor UI architecture

UI uses ordered classic browser modules and does not require a production bundler.

## JavaScript load order

1. `core.js` — constants, state, DOM cache, API and shared helpers.
2. `boot.js` — splash, boot polling and readiness.
3. `diagnostics.js` — distributor diagnostics and selected-frame analysis.
4. `status.js` — backend status, OFFLINE, process telemetry and part path.
5. `controls.js` — START/STOP/EXIT, errors and state overlay.
6. `cameras.js` — camera selection, RAW/RULES and frame refresh.
7. `jog.js` — dead-man hold/heartbeat/release logic.
8. `history.js` — recent parts, archive gallery and fullscreen.
9. `bootstrap.js` — hotkeys, initialization and test-only hook.

Functions may call functions from later modules only after all scripts have loaded and `bootstrap.js` starts the UI. Do not change the order in `templates/index.html`.

## CSS modules

- `base.css` — tokens, reset, splash and global layout.
- `blocks.css` — named UI block containment, text clipping and wrap rules.
- `camera.css` — preview strip, main camera and state overlay.
- `axis.css` — common axis widgets.
- `history-strip.css` — recent part strip.
- `stats.css` — statistics, line cells and defect list.
- `jog.css` — two-button hold JOG.
- `controls.css` — footer and main control buttons.
- `gallery.css` — archive modal and fullscreen.
- `process.css` — process line, distributor, diagnostics and OFFLINE/error additions.
- `motion.css` — non-blocking fades, panel collapse/expand and frame/content transitions.

## Блочная карта UI

Каждая крупная зона имеет `data-ui-block`, чтобы её можно было отдельно проверять, ограничивать и не допускать наложения динамических надписей:

- `splash` — экран запуска и ошибка инициализации.
- `operator-header` — статус линии, заголовок и метрики.
- `preview-strip` — семь миниатюр камер.
- `main-camera` + `camera-controls` — главный кадр, режимы RAW/ПРАВИЛА и анализ кадра.
- `process-line` — путь деталей: непрозрачные ворота входа/выхода, из-под которых появляются и уезжают маркеры деталей.
- `history-strip` — последние детали.
- `right-panel` — прокручиваемая правая колонка.
- `cycle-stats`, `defects`, `service-stats`, `distributor`, `jog`, `frame-analysis` — независимые блоки правой колонки.
- `operator-footer` — кнопки ПУСК/СТОП/ВЫХОД и горячие клавиши.
- `gallery` — архивная галерея детали.
- `calibration-header`, `calibration-preview`, `calibration-assignment`, `calibration-footer` — отдельные блоки мастера калибровки.

Правило для новых зон: сначала добавить `data-ui-block`, затем проверить, что длинные русские подписи либо обрезаются через `ellipsis`, либо переносятся через `overflow-wrap: anywhere`.

## Профессиональная HMI-компоновка

- Системные шрифты `Segoe UI` и `Cascadia Mono` работают без сети.
- Нейтральная палитра используется для штатной работы.
- Зелёный, жёлтый и красный зарезервированы для результата и состояния.
- Основной кадр имеет максимальную доступную площадь.
- Статистика использует сетку 2 + 3 показателя.
- В рабочем цикле анализ, статистика и распределитель остаются одновременно видимыми.
- Инженерские и ручные элементы скрываются, когда они не нужны оператору.

## Стандарт операторских обозначений

Внутренние коды API и firmware остаются неизменными, но оператору показываются только согласованные русские названия:

| Внутренний код | Надпись в интерфейсе |
|---|---|
| `IDLE` | `ГОТОВА К ПУСКУ` |
| `RUNNING` | `РАБОТАЕТ` |
| `STOPPING` | `ОСТАНОВКА ЛИНИИ` |
| `STOPPED` | `ОСТАНОВЛЕНА` |
| `FAULT` | `АВАРИЯ` |
| `DIST1_HOME` | `ПРОХОД` |
| `DIST1_OPEN` | `СБРОС` |
| `DIST2_BAD` | `БРАК` |
| `DIST2_CLEANUP` | `ОЧИСТКА` |
| `GOOD` | `ГОДНО` |
| `CLEANUP` | `НА ОЧИСТКУ` |

Названия камер задаются через `CAMERA_ROLE_LABELS`. Аббревиатуры без расшифровки в операторских надписях не используются; исключения — технические идентификаторы `RAW`, `DIST1` и `DIST2`.

## Change rules

- Motion availability must come from backend `line_status.controls`.
- New physical actions must start disabled in HTML.
- A physical action must have backend state validation, local pending lock and API error display.
- Polling requests must not overlap.
- Dynamic operator data must use `textContent`, not untrusted `innerHTML`.

