# Калибровка распределителя

Запускать только при физическом доступе к станку и доступном E-stop:

```bat
run_distributor_calibration.bat
```

## Что калибруется

- `DIST1_OPEN` — координата полностью открытой лопасти axis0;
- `DIST2_BAD` — физический HOME axis1, всегда 0;
- `DIST2_CLEANUP` — координата маршрута CLEANUP axis1.

Скрипт не использует старые значения 340 как обязательные. Оператор вводит кандидаты и подтверждает фактическое положение.

## Последовательность

1. Точное подтверждение `CALIBRATE DISTRIBUTOR COM4`.
2. `G1` и `G25`.
3. Read-only проверка `I2`.
4. Консервативные speed/acceleration.
5. Временные calibration limits: `G31 S0`, `G32 S<maximum>`, `G33 S1`; readback через `I11`.
6. G28 HOME axis0 с проверкой `POS=0`, `HOMED=1`, `LIM=1`.
7. Ввод кандидата DIST1.
8. Наблюдение физической лопасти.
9. Точное `ACCEPT <позиция>`.
10. Возврат axis0 HOME.
11. Та же процедура для DIST2 CLEANUP.
12. Создание `calibration.distributor_candidate.json`.

Основной `calibration.json` изменяется только после точной фразы:

```text
APPLY DISTRIBUTOR CALIBRATION
```

Перед применением создаётся резервная копия:

```text
calibration.before_distributor_YYYYMMDD_HHMMSS.json
```

## После калибровки

1. Запустить `preflight_checks\02_configuration_check.py`.
2. Открыть UI.
3. Проверить кнопками DIST1 HOME/OPEN и DIST2 BAD/CLEANUP.
4. Сравнить физическое положение с маркером UI.
5. Только после этого использовать START.
