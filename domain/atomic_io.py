"""Атомарная запись файлов конфигурации и артефактов.

Все настройки линии (пороги, конфиг архива, метаданные партии) пишутся по
одной схеме: временный файл рядом с целевым -> fsync содержимого ->
``os.replace`` -> fsync каталога. Пропадание питания в любой момент оставит
на диске либо прежнюю версию файла, либо новую целиком, но не обрезанную.

``os.replace`` атомарен только в пределах одной файловой системы, поэтому
временный файл создаётся в каталоге назначения, а не в /tmp.
"""

from __future__ import annotations

import contextlib
import json
import os


def _fsync_dir(path: str) -> None:
    """Зафиксировать саму запись каталога после ``os.replace``.

    Без этого переименование может не пережить отключение питания даже
    после fsync содержимого. На платформах без ``O_DIRECTORY`` (Windows)
    операция недоступна и тихо пропускается.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        fd = os.open(directory, getattr(os, "O_DIRECTORY", os.O_RDONLY))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: str, content: bytes) -> None:
    """Записать байты в ``path`` целиком или не записать вовсе."""
    import time as _time

    path = os.fspath(path)
    # Уникальное имя временного файла исключает гонку при параллельной записи
    # из разных потоков/процессов в один и тот же целевой файл.
    temp_path = f"{path}.tmp.{os.getpid()}.{_time.time_ns()}"
    try:
        with open(temp_path, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(temp_path)
        raise
    _fsync_dir(path)


def atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    """Записать текст в ``path`` атомарно."""
    atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path: str, payload, *, indent: int = 2) -> None:
    """Сериализовать ``payload`` в JSON и записать атомарно.

    Сериализация выполняется в память до открытия файла: ошибка
    сериализации не оставит на диске временный файл.
    """
    text = json.dumps(payload, indent=indent, ensure_ascii=False) + "\n"
    atomic_write_text(path, text)
