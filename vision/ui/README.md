# Line Monitor UI architecture

UI uses ordered classic browser modules and does not require a production bundler.

## JavaScript load order

1. `core.js` — constants, state, DOM cache, API and shared helpers.
2. `motion.js` — привод анимаций: телеметрия ленты, осей и фазы шага.
3. `boot.js` — splash, boot polling and readiness.
4. `diagnostics.js` — pre-start checks and distributor diagnostics.
5. `status.js` — backend status, OFFLINE, process telemetry and part path.
6. `controls.js` — START/STOP/EXIT, errors and state overlay.
7. `cameras.js` — camera selection, RAW/RULES and frame refresh.
8. `jog.js` — dead-man hold/heartbeat/release logic.
9. `pause.js` — in-cycle pause and bounded belt correction held dead-man style.
10. `history.js` — recent parts, archive gallery and fullscreen.
11. `bootstrap.js` — hotkeys, initialization and test-only hook.

Functions may call functions from later modules only after all scripts have loaded and `bootstrap.js` starts the UI. Do not change the order in `templates/index.html` without updating the asset-order regression test.

## CSS modules

- `base.css` — tokens, reset, splash and global layout.
- `camera.css` — preview strip, main camera and state overlay.
- `axis.css` — common axis widgets.
- `history-strip.css` — recent part strip.
- `stats.css` — statistics, line cells and defect list.
- `jog.css` — two-button hold JOG.
- `nudge.css` — bounded belt correction panel shown only while paused; buttons are held, not clicked.
- `controls.css` — footer and main control buttons.
- `gallery.css` — archive modal and fullscreen.
- `process.css` — process line, distributor, diagnostics and OFFLINE/error additions.
- `motion.css` — non-blocking fades, panel collapse/expand and frame/content transitions.
- `belt.css` — движение, повторяющее работу конвейера: зубчатые ленты, приводные валы, ход пути деталей, кулачки заслонок и операции текущего шага.

## Движение интерфейса = работа конвейера

В интерфейсе нет декоративной анимации. Любое движение — отображение
физического процесса, подтверждённого backend. Единственный источник
движения — `motion.js`: он превращает телеметрию в CSS-переменные, а
`belt.css` рисует по ним геометрию.

| Что видит оператор | Чем вызвано |
|---|---|
| Ход зубчатой ленты и вращение валов | `process.conveyor` (`POS`/`TGT` из `I2`) |
| Смещение ячеек пути деталей | доля пройденного шага, `--belt-intra` |
| Отбивка шага и подсветка счётчика | рост `line_status.step` |
| Ручной ход ленты | `jog.busy` и `jog.direction` |
| Коррекция в паузе | приращение `pause.nudge_offset` в микрошагах |
| Поворот кулачков заслонок | `dist1_position` / `dist2_position` |
| Вспышка затвора, проход анализа, метка сортировки | `process.phase` текущего шага |
| Приезд карточки в историю | деталь, впервые появившаяся на линии |
| Заполнение ленты на пусковом экране | `/api/boot` progress |

Переменные привода: `--belt-phase`, `--belt-intra`, `--belt-speed`,
`--belt-dir`, `--belt-teeth`, `--belt-cadence`, `--drive-turn`,
`--dist1-turn`, `--dist2-turn`, `--boot-progress`.

Правила, которые нельзя нарушать:

- Анимация не запускается по таймеру «для красоты». Если механизм стоит,
  соответствующая переменная не меняется и интерфейс неподвижен.
- Остановка линии, отпускание JOG и потеря связи обязаны приводить
  интерфейс в полный покой (`freezeBeltMotion`, класс `belt-running`).
- Скачок координаты больше `BELT_RESYNC_CELLS` считается пересинхронизацией
  счётчика, а не ходом ленты, и подхватывается мгновенно.
- Бесконечные `animation` допустимы только на время работы механизма и
  синхронизируются с измеренным темпом шага `--belt-cadence`.
- Всё движение идёт через `transform` и `opacity`, положение пути деталей
  не пишется в inline-стили.
- `prefers-reduced-motion` обязан оставлять интерфейс статичным.

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
- New motion must be fed by backend telemetry through `motion.js` and must
  stop when the corresponding mechanism stops.

## Tests

```bash
npm ci
npm run test:ui
```

The JSDOM suite loads the exact modules in the exact production order from `index.html`.
