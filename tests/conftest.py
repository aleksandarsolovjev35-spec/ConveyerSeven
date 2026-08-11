"""Общая подготовка пакета тестов.

Тесты запускаются из корня репозитория (`python -m pytest tests -q`), но
гарантированно добавляют корень в sys.path, чтобы импорты `core.*`,
`hardware.*` и т.д. работали и при запуске из другого каталога.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
