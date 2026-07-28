# Line Monitor UI architecture

UI uses ordered classic browser modules and does not require a production bundler.

## JavaScript load order

1. `core.js` — constants, state, DOM cache, API and shared helpers.
2. `boot.js` — splash, boot polling and readiness.
3. `diagnostics.js` — pre-start checks and distributor diagnostics.
4. `status.js` — backend status, OFFLINE, process telemetry and part path.
5. `controls.js` — START/STOP/EXIT, errors and state overlay.
6. `cameras.js` — camera selection, RAW/RULES and frame refresh.
7. `jog.js` — dead-man hold/heartbeat/release logic.
8. `pause.js` — in-cycle pause and bounded belt correction.
9. `history.js` — recent parts, archive gallery and fullscreen.
10. `bootstrap.js` — hotkeys, initialization and test-only hook.

Functions may call functions from later modules only after all scripts have loaded and `bootstrap.js` starts the UI. Do not change the order in `templates/index.html` without updating the asset-order regression test.

## CSS modules

- `base.css` — tokens, reset, splash and global layout.
- `camera.css` — preview strip, main camera and state overlay.
- `axis.css` — common axis widgets.
- `history-strip.css` — recent part strip.
- `stats.css` — statistics, line cells and defect list.
- `jog.css` — two-button hold JOG.
- `nudge.css` — bounded belt correction panel shown only while paused.
- `controls.css` — footer and main control buttons.
- `gallery.css` — archive modal and fullscreen.
- `process.css` — process line, distributor, diagnostics and OFFLINE/error additions.
- `motion.css` — non-blocking fades, panel collapse/expand and frame/content transitions.

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
- Add every new interaction to `tests/ui_interaction_matrix.js`.

## Tests

```bash
npm ci
npm run test:ui
```

The JSDOM suite loads the exact modules in the exact production order from `index.html`.
