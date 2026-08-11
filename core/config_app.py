"""Strict application configuration loaded from the process environment.

Configuration is intentionally centralised here: runtime code must receive an
:class:`AppSettings` instance rather than read ``os.environ`` directly.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """Validated configuration shared by the ConveyerSeven application.

    Environment variables use upper-case field names.  A local ``.env`` file
    is supported for development but never overrides explicitly supplied
    process environment variables.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    serial_baud: PositiveInt = 115_200
    serial_port: str | None = None
    camera_warmup_seconds: float = Field(default=2.5, ge=0.5, le=10.0)
    camera_pre_preview_warmup_seconds: float = Field(default=2.5, ge=0.0, le=5.0)
    camera_recovery_warmup_seconds: float = Field(default=2.5, ge=0.2, le=10.0)
    camera_mapping_file: Path = Path("camera_mapping.json")
    calibration_file: Path = Path("calibration.json")
    archive_config_file: Path = Path("archive_config.json")
    thresholds_file: Path = Path("thresholds.json")
    log_level: str = "INFO"
    log_dir: Path = Path("logs")


_settings: AppSettings | None = None


def get_settings() -> AppSettings:
    """Return the singleton validated settings instance for this process."""
    global _settings
    if _settings is None:
        _settings = AppSettings()
    return _settings
