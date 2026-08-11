"""Domain-specific failures used to drive fail-safe application behaviour."""

from __future__ import annotations


class ConveyerError(RuntimeError):
    """Base class for an expected ConveyerSeven operational failure."""


class HardwareConnectionError(ConveyerError):
    """Raised when a camera, serial transport, or motion device is unavailable."""


class VisionModelError(ConveyerError):
    """Raised when loading, warming, or running a vision model fails."""


class SafetyStopError(ConveyerError):
    """Raised when the line must enter a fail-safe emergency-stop state."""
