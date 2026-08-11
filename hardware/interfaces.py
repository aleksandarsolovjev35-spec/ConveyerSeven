"""Hardware-abstraction contracts for production and digital-twin adapters."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol


class ISerialTransport(Protocol):
    """Minimal safe serial-controller contract."""

    def send(self, command: str) -> None:
        """Send a controller command exactly once."""

    def query(self, command: str, delay: float = 0.15) -> str:
        """Send a request and return its response."""

    def close(self) -> None:
        """Release the underlying serial resource."""


class IConveyor(Protocol):
    """Motion contract used by the production cycle."""

    transport: ISerialTransport

    def move_step(self) -> None:
        """Start one configured conveyor movement."""

    def wait_stop(
        self,
        timeout: float = 15.0,
        progress_callback: Callable[[dict[str, int | str | None]], None] | None = None,
    ) -> None:
        """Wait until the controller confirms stopped motion."""

    def emergency_stop(self) -> None:
        """Stop all conveyor motion immediately."""


class ICamera(Protocol):
    """Camera-source contract for synchronized stage capture."""

    mapping: dict[str, int]

    def capture_roles(self, roles: tuple[str, ...]) -> dict[str, object]:
        """Capture current frames for exactly the requested roles."""

    def capture_all(self) -> dict[str, object]:
        """Capture current frames for every configured role."""

    def release(self) -> None:
        """Release camera resources."""
