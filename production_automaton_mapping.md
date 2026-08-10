# Сопоставление формального автомата и production-реализации

Дата актуализации: 2026-08-10  
Нормативные документы: «Формальный автомат production-линии» и
«Production-спецификация семикамерного конвейера».

## Итог

**Программная реализация приведена в соответствие с нормативной моделью.**
Production и simulator проходят через один `ProductionCycle`, формальный
`ControlCore`, reducer и последовательный command/event loop. Оставшиеся пункты
в разделе «Физическая приёмка» не являются программными расхождениями: это
обязательные HIL/release gates, которые нельзя подтвердить unit-тестом.

## Матрица ответственности

| Область | Production-владелец | Реализация | Статус |
|---|---|---|---|
| LineState | control core | `core/control_core.py`, `core/core_state_machine.py`, `core/line_reducer.py` | соответствует |
| StepPhase | step executor | `core/control_model.py`, `core/production_cycle.py` | соответствует |
| PendingIntent | command arbiter/reducer | `core/command_arbiter.py`, `core/line_reducer.py` | соответствует |
| PauseContinuation | control core | `core/line_reducer.py` | соответствует |
| HealthState | health supervisor | `core/health_supervisor.py`, pre-motion gate в `ProductionCycle` | соответствует |
| DisplayState | atomic publisher | `core/atomic_publisher.py`, `UIServer.update()` | соответствует |
| PersistenceState | persistence boundary | `core/recovery_journal.py`, `inspection/part_archive.py` | соответствует |

## 1. Единственный event loop

`ControlRuntime` создаёт единственный owner-thread. HMI, watchdog и API кладут
команды в очередь и ожидают результат. Очередь сортируется по safety priority:
FORCE EXIT → STOP/EXIT → PAUSE → остальные команды. На owner-thread команды
обрабатываются во время idle, telemetry polling, ожидания inspection workers,
REVIEW и PAUSED.

Production workers выполняют только вычисление. INPUT presence передаётся через
handshake в control thread; Part создаётся и durable-журналируется control core
до продолжения остальных INPUT rules. INPUT/CONTROL результаты применяются
только на control thread после полного aggregate. Отдельного mutable
inspection-lifecycle в production или simulator больше нет.

## 2. Макросостояния

Полный enum содержит:

`BOOTING`, `RECOVERY_REQUIRED`, `IDLE`, `RUNNING`, `PAUSED`, `STOPPING`,
`STOPPED`, `FAULT`, `SHUTTING_DOWN`, `TERMINATED`.

`main.py` создаёт `ControlCore` в BOOTING и переводит его в IDLE только после
обязательных BOOT gates. `ProductionCycle` отказывается принимать core другого
batch или core, не завершивший BOOT. OFFLINE остаётся только вычисляемым HMI
состоянием.

## 3. Атомарный LineSnapshot

`LineSnapshot` хранит macrostate, phase, integer run ID, confirmed current step,
pending intent, pause continuation, immutable Part views, counters, fault latch,
health/display/persistence state и monotonic state version.

`ProductionCycle.current_step` является read-only property из snapshot. Setter
в production запрещён. Изменение выполняет только `LineReducer.commit_step()`
после `MotionConfirmed` с точным evidence.

## 4. Команды и guards

- `command_id` memoized; повтор возвращает тот же сохранённый `CommandResult`.
- START/RESUME/JOG проходят formal guards.
- EXIT использует STOP physics и защёлкивает `exit_after_drain`.
- STOP не может стереть EXIT.
- JOG release не зависит от start guards.
- Диагностика, selected analysis, threshold apply/reload и JOG также проходят
  через control-runtime queue.
- Browser disabled controls не являются backend-защитой.

## 5. STOP, PAUSE, REVIEW

Reducer применяет команды по текущей formal phase:

- до motion — немедленная безопасная граница;
- MOTION_CONFIRM — завершение evidence/commit;
- PAUSE после commit — `INSPECT_COMMITTED_STEP` до SETTLE/capture;
- analysis/persist/publish/review — pending до COMMAND_GATE;
- STOP сильнее PAUSE;
- REVIEW использует server monotonic deadline и не прерывается обычными
  STOP/EXIT/PAUSE;
- FORCE EXIT остаётся единственным немедленным программным прерыванием.

INPUT acceptance защёлкивается до motion и не вычисляется повторно из уже
изменённого macrostate.

## 6. Физическая транзакция

Production-путь выполняет:

`HEALTH_GATE → ROUTE_PREPARE? → JOURNAL_INTENT → MOTION_COMMAND →
MOTION_CONFIRM → STEP_COMMIT → TRANSFER_COMMIT? → POST_MOTION_GATE`.

`MotionTransaction` durable-пишет intent и допускает ровно один command call.
После выдачи команды выполняется только polling. `Conveyor` требует armed
`TGT=38096`, допустимый armed POS, новый `lastReadyMs`, LASTERR=0 и точный final
reset. До exact evidence current step не меняется.

Route Part/category/targets обеих осей и start ready epoch входят в immutable
transaction latch. Команда STOP/PAUSE между route/intent и physical command
закрывает транзакцию без движения.

## 7. Transfer и архив

После подтверждённого движения distributor подтверждает transfer. Reducer
сразу обновляет физические counters и удаляет Part из tracking. Затем выполняется
archive finalization. Ошибка архива не возвращает Part и не допускает повторной
сортировки.

Archive сохраняет inspection evidence в durable staging уже на PERSIST,
добавляет exact identity в `part.json`, строит exact manifest, пишет
`COMMITTED.json`, выполняет file/directory fsync, atomic rename и parent fsync.
Committed index перестраивается раньше secondary stats; replay игнорирует
staging и сканирует только проверенные committed catalogs.

## 8. Inspection

После POST_MOTION_GATE вычисляются только реальные required roles. Захват
копирует immutable frames из live buffers после freshness boundary.

INPUT и CONTROL вычисляются параллельно:

- presence обеих INPUT ролей выполняется один раз;
- обе absent увеличивают только empty counter;
- хотя бы одна present резервирует и журналирует Part до остальных rules;
- mismatch создаётся только при one-sided presence;
- CONTROL требует ровно один Part на +4 и все пять snapshots;
- технический failure оставляет Part incomplete и вызывает FAULT;
- workers не меняют Part, counters, journal, archive или LineState.

PERSIST получает один полный aggregate. PUBLISH переводит display в REVIEW
только для ролей с физической деталью. COMMAND_GATE/NONE завершаются до final
`InspectionExecutionResult`; publication version точно равна snapshot version.

## 9. START и drain

Первый START создаёт integer run ID, подтверждает reset и выполняет INPUT без
движения; current step остаётся 0. CONTROL в initial transaction отсутствует.

STOPPING запрещает INPUT, presence, empty count и создание Part. При пустом
tracking conveyor command отсутствует. Formal `DISTRIBUTOR_HOME` устанавливается
до команды homing; STOPPED/SHUTTING_DOWN публикуется только после typed
`AxisHomed` confirmation.

## 10. Fault contract

`FaultLatch` сохраняет первый root и добавляет secondary. Formal snapshot
переходит в FAULT, блокирует новые physical commands и фиксирует phase,
transaction, position confidence и possible-step-commit metadata.

Неоднозначное движение оставляет current step прежним и positions unknown.
Ошибка journal после exact physical evidence отмечает possible commit. Ошибка
после transfer сохраняет physical counters/tracking truth. FORCE EXIT переводит
непосредственно в SHUTTING_DOWN, запрещает новые движения и помечает позиции
неизвестными.

## 11. HMI и версии

Heartbeat timeout создаёт обычный PAUSE через
`HmiCommandGateway → ControlRuntime → CommandArbiter`. Reconnect не делает
RESUME.

`UIServer` принимает formal `state_version`, отбрасывает весь устаревший
logical response и не создаёт самостоятельную competing version. Frame cache и
per-role frame versions остаются независимыми.

## 12. Health

Перед новой physical transaction health supervisor атомарно проверяет семь
камер, controller link, conveyor/distributor telemetry contract, workers,
journal/archive, disk reserve, root fault и hardware conflict. Camera recovery,
начавшийся после команды, не отменяет motion; следующий gate или обязательный
snapshot не проходит без восстановления.

## 13. Journal и recovery

Production использует `RecoveryJournal` с fsync, monotonic sequence, integer
run ID и transaction ID. Записываются intent/confirmation, Part/stage/route,
transfer/archive, state/fault и terminal transaction events. Невозможность
записи до команды запрещает physical action; ошибка после exact action приводит
к FAULT с соответствующей physical-boundary metadata.

## 14. Simulator

`ui_emulator.py` больше не содержит независимый lifecycle. Он создаёт
`ProductionCycle + ControlRuntime` и заменяет только camera/conveyor/distributor/
JOG/model adapters. Статус всегда содержит `simulation=true`; HMI сохраняет
постоянную маркировку SIMULATION.

## 15. Автоматические проверки

`tests/test_formal_automaton.py` проверяет 18 нормативных инвариантов, включая
exactly-once motion, stale events, pending priority, pause continuation,
transfer truth, fault latch, no-input drain и atomic versions. Integration tests
дополнительно выполняют production `ProductionCycle` через formal phases.

## Физическая приёмка — обязательна до production release

Следующие пункты нельзя честно закрыть изменением Python-кода и они остаются
**release gates**, как предписано обоими нормативными документами:

1. HIL фактического перемещения одной ячейки при software POS/TGT без encoder.
2. Проверка реальных `TGT=38096`, `lastReadyMs` и final reset на binary v2.4.0.
3. Электрическая проверка независимого физического E-STOP всех опасных осей.
4. Подтверждение home sensors и независимости/последовательности homing.
5. Проверка всех GOOD/CLEANUP/BAD маршрутов с реальными деталями.
6. Калибровка SETTLE, camera/model/rule timeout, reconnect grace и disk reserve.
7. Golden-frame regression и контрольный физический batch.

До документированного прохождения этих gates проект программно соответствует
контракту, но production-релиз по разделу «Production-приёмка» остаётся
заблокированным.
