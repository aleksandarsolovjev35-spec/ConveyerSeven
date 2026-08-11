"""Validated contracts exchanged between the production core and HMI."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class UIStatusSchema(BaseModel):
    """Stable minimum contract for the HMI line-status payload."""

    model_config = ConfigDict(extra="allow", strict=True)

    state: str
    exit_requested: bool
    fault_reason: str | None
    step: int = Field(ge=0)
    in_line: int = Field(ge=0)
    total: int = Field(ge=0)
    good: int = Field(ge=0)
    rejected: int = Field(ge=0)
    cleanup: int = Field(ge=0)
    empty: int = Field(ge=0)
    line_parts: list[dict[str, Any]]
    controls: dict[str, bool]
    process: dict[str, Any]
    live: dict[str, Any]
    diagnostics: dict[str, Any]

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Validate a status mapping and return JSON-safe data for FastAPI."""
        return cls.model_validate(value).model_dump(mode="json")
