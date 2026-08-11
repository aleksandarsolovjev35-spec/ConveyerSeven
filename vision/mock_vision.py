"""Vision digital twin returning valid empty detections without model weights."""

from __future__ import annotations

from typing import Any


class MockVisionCluster:
    """Drop-in VisionCluster substitute for UI and workflow simulation."""

    def __init__(self) -> None:
        self.models: dict[str, object] = {}
        self.last_health: list[dict[str, Any]] = []

    def warmup(self) -> None:
        """Perform no model work in simulation mode."""

    def process_all(self, frames: dict[str, object]) -> dict[str, list[dict[str, Any]]]:
        """Return empty but healthy detections for every supplied role."""
        self.last_health = [{"role": role, "model": "simulation", "ok": True, "elapsed_ms": 0.0, "detections": 0, "error": None} for role in frames]
        return {role: [] for role in frames}
