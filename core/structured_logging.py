"""Structlog configuration for human console output and JSON audit files."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import structlog


def configure_structlog(log_dir: Path) -> Path:
    """Configure structlog and return the JSON-lines ``app.log`` location."""
    log_dir.mkdir(parents=True, exist_ok=True)
    json_path = log_dir / "app.log"
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    structlog.configure(
        processors=[*shared_processors, structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.WriteLoggerFactory(file=json_path.open("a", encoding="utf-8")),
        cache_logger_on_first_use=True,
    )
    return json_path


def get_struct_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a structured logger to enrich records with part and line context."""
    return structlog.get_logger(name)
