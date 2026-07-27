# Проверки перед запуском Transporter

Эта папка предназначена для оператора. Внутренняя папка `tests/` содержит regression-тесты разработчика и напрямую оператором обычно не используется.

## Быстрый порядок

После распаковки проекта:

```bat
setup.bat
```

Если `camera_mapping.json` отсутствует, сначала выполнить `run_camera_calibration.bat` либо запустить `run.bat` и пройти автоматически открывшийся мастер. После создания mapping:

```bat
preflight_checks\00_RUN_SOFTWARE_ONLY.bat
preflight_checks\00_RUN_ALL_NO_MOTION.bat
run.bat
```

После открытия UI, но до нажатия START, выполнить чек-лист:

```text
preflight_checks\08_OPERATOR_CHECKLIST_BEFORE_START_RU.md
```

## Состав папки

| Файл | Для чего нужен | Камеры | COM | Движение |
|---|---|---:|---:|---:|
| `00_RUN_SOFTWARE_ONLY.bat` | Все программные проверки одним запуском | нет | нет | нет |
| `00_RUN_ALL_NO_MOTION.bat` | Полная проверка, включая камеры и контроллер | да | да, только I2 | нет |
| `01_environment_check.py` | Windows, CPython 3.11 и Python-зависимости | нет | нет | нет |
| `02_configuration_check.py` | calibration, thresholds, camera mapping, model mapping | нет | нет | нет |
| `03_model_files_check.py` | Наличие, размер и SHA-256 всех 12 `.pt` | нет | нет | нет |
| `04_model_load_and_warmup.py` | Реальная загрузка и CPU warmup всех моделей | нет | нет | нет |
| `05_seven_cameras_check.py` | Семь кадров 1280×720 и защита от near-black | да | нет | нет |
| `06_controller_no_motion_check.py` | Поиск контроллера и проверка ответа `I2` | нет | да | нет |
| `07_AUTOMATED_CODE_AND_UI_TESTS.bat` | Python regression + JavaScript/JSDOM UI matrix | fake | fake | нет |
| `08_OPERATOR_CHECKLIST_BEFORE_START_RU.md` | Что проверить в открытом UI перед START | да | да | только ручная диагностика |
| `09_DISTRIBUTOR_CALIBRATION_RU.md` | Отдельная процедура калибровки DIST1/DIST2 | нет | да | да, guarded |
| `10_CAMERA_CALIBRATION_RU.md` | Оконный мастер назначения Camera ID семи ролям | да | нет | нет |

## Разница двух общих запусков

### Software-only

```bat
preflight_checks\00_RUN_SOFTWARE_ONLY.bat
```

Можно выполнять без подключённого станка. Проверяет окружение, JSON-конфигурацию, наличие моделей, загрузку/warmup моделей и автоматизированные тесты.

### Full no-motion

```bat
preflight_checks\00_RUN_ALL_NO_MOTION.bat
```

Дополнительно открывает семь камер и COM-порт. В контроллер отправляется только read-only запрос `I2`. Команды `G1`, `G3`, `G20`, `G27`, `G28` и другие команды движения этим набором не отправляются.

Открытие COM может перезапустить Arduino. Механизм должен находиться в безопасном состоянии, несмотря на отсутствие motion-команд со стороны теста.

## Условия успешного результата

Каждый файл заканчивается строкой `... PASSED`. При первой ошибке общий `.bat` останавливается и возвращает ненулевой exit code.

Нельзя запускать `run.bat`, если любой обязательный этап завершился ошибкой.

## Текущая известная проблема

Если `INPUT_RIGHT` остаётся чёрной, файл `05_seven_cameras_check.py` обязан завершиться ошибкой near-black. Это правильное поведение; обходить его нельзя.
