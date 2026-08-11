# Непрерывная интеграция

Здесь лежит готовый workflow GitHub Actions — `github-actions-ci.yml`.

## Как включить

Файл нужно положить в `.github/workflows/`:

```bash
mkdir -p .github/workflows
git mv ci/github-actions-ci.yml .github/workflows/ci.yml
git commit -m "Включить CI"
git push
```

Почему он не лежит там сразу: этот коммит подготовлен через GitHub App, а у приложения
нет разрешения `workflows` — GitHub отклоняет push, который создаёт или изменяет файлы
в `.github/workflows/`. Перенос делается одной командой обычным пользователем.

## Что проверяет

Две независимые задачи на каждый push и pull request:

| Задача | Шаги |
|---|---|
| `lint` | `ruff check` с настройками из `ruff.toml` в корне |
| `tests` | `compileall` по всем пакетам, затем `pytest` |

Версия `ruff` зафиксирована (`0.16.2`) и совпадает с `requirements-dev.txt`, поэтому
локальный `ruff check .` даёт тот же результат, что и проверка в PR.

## Зависимости

Тесты в CI ставятся из `requirements-ci.txt` — это тот же набор, что работает на линии,
но без `ultralytics` (тянет torch, сотни мегабайт) и `pywebview` (требует GTK/Qt).
Тестам они не нужны: тяжёлые модели подменяются фейками, а ленивые `__getattr__` в
`hardware/__init__.py` и `inspection/__init__.py` не дают импортировать их раньше времени.
`opencv-python` заменён на `opencv-python-headless` — та же библиотека без GUI-модулей,
поэтому раннеру не нужен `libGL`.

Для локальной разработки используйте `requirements-dev.txt` — там полный набор плюс ruff.

## Локальный прогон тех же проверок

```bash
ruff check .
python -m pytest -q
```
