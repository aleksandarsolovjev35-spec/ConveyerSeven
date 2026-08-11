"""Digital-twin adapters for running the HMI without factory hardware."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from domain.part import CATEGORY_BAD, CATEGORY_CLEANUP, CATEGORY_GOOD

_DEFAULT_ROLES = ("INPUT_LEFT", "INPUT_RIGHT", "SPIDER_LEFT", "SPIDER_RIGHT", "SPIDER_IN", "SPIDER_OUT", "TOP")


class MockSerialTransport:
    """In-memory serial transport with deterministic controller status replies."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.closed = False

    def send(self, command: str) -> None:
        """Record a command without contacting a physical controller."""
        self.commands.append(command)

    def query(self, command: str, delay: float = 0.15) -> str:
        """Return a stopped healthy controller state for status requests."""
        del delay
        self.commands.append(command)
        return "MOV=0 WAIT=0 POS=0 TGT=0 lastErr=0" if command == "I2" else "0"

    def close(self) -> None:
        """Mark the simulated serial port closed."""
        self.closed = True


class MockConveyor:
    """Non-moving conveyor digital twin retaining issued movement commands."""

    def __init__(self, transport: MockSerialTransport) -> None:
        self.transport = transport
        self.moves = 0
        self.stopped = False

    def move_step(self) -> None:
        """Record a logical conveyor movement."""
        self.moves += 1
        self.stopped = False
        self.transport.send("G3")

    def wait_stop(self, timeout: float = 15.0, progress_callback: Any = None) -> None:
        """Complete simulated motion immediately and report a healthy status."""
        del timeout
        self.stopped = True
        if progress_callback is not None:
            progress_callback({"mov": 0, "wait": 0, "pos": 0, "tgt": 0, "lasterr": 0})

    def emergency_stop(self) -> None:
        """Record the same fail-safe commands as the physical conveyor."""
        self.stopped = True
        self.transport.send("G1")
        self.transport.send("G25")


class MockCamera:
    """Seven-role camera source returning video frames or deterministic blanks."""

    def __init__(self, video_path: Path | None = None, roles: tuple[str, ...] = _DEFAULT_ROLES) -> None:
        self.mapping = {role: index for index, role in enumerate(roles)}
        self._video_path = video_path
        self._capture: Any | None = None
        self._closed = False

    def capture_roles(self, roles: tuple[str, ...]) -> dict[str, np.ndarray]:
        """Return a copy of the same current digital-twin frame per requested role."""
        frame = self._next_frame()
        return {role: frame.copy() for role in roles}

    def capture_all(self) -> dict[str, np.ndarray]:
        """Return a frame for every configured simulated camera."""
        return self.capture_roles(tuple(self.mapping))

    def drain_buffers(self, roles: tuple[str, ...] | None = None) -> None:
        """Consume no buffers; retained for CameraManager compatibility."""
        del roles

    def warmup_all(self, duration: float = 0.0) -> dict[str, dict[str, int]]:
        """Report an immediately healthy camera set."""
        del duration
        return {role: {"reads": 1} for role in self.mapping}

    def warmup_roles(self, roles: tuple[str, ...], duration: float = 0.0) -> dict[str, dict[str, int]]:
        """Report requested roles healthy."""
        del duration
        return {role: {"reads": 1} for role in roles}

    def release(self) -> None:
        """Release a lazily opened video stream."""
        self._closed = True
        if self._capture is not None:
            self._capture.release()

    def _next_frame(self) -> np.ndarray:
        if self._closed:
            raise RuntimeError("mock camera is released")
        if self._video_path is not None:
            import cv2

            if self._capture is None:
                self._capture = cv2.VideoCapture(str(self._video_path))
            ok, frame = self._capture.read()
            if not ok:
                self._capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = self._capture.read()
            if ok and frame is not None:
                return frame
        return np.zeros((720, 1280, 3), dtype=np.uint8)


class MockDistributor:
    """Safe logical distributor that records routes without moving axes."""

    def __init__(self) -> None:
        self.dist1_open_position = 340
        self.dist2_bad_position = 0
        self.dist2_cleanup_position = 340
        self.on_state_changed: Any = None
        self.cancel_check: Any = None
        self.last_action = "SIMULATION READY"

    @property
    def status(self) -> dict[str, Any]:
        """Return a UI-compatible idle distributor status."""
        return {"dist1_position": 0, "dist1_max": 340, "dist1_state": "GOOD", "dist2_position": 0, "dist2_max": 340, "dist2_state": "IDLE", "dist2_target": CATEGORY_BAD, "last_distributor_action": self.last_action}

    def initialize(self) -> None:
        """Complete simulated homing immediately."""
        self.last_action = "SIMULATED HOMED"

    def park_production(self) -> None:
        """Mark the safe simulated route."""
        self.last_action = "SIMULATED PRODUCTION READY"

    def prepare_route(self, category: str, part_id: int | None = None) -> None:
        """Record a valid part route."""
        if category not in (CATEGORY_GOOD, CATEGORY_BAD, CATEGORY_CLEANUP):
            raise ValueError(f"unsupported simulated route: {category}")
        self.last_action = f"SIMULATED PART {part_id or '-'} -> {category}"

    def confirm_transfer(self, part_id: int, category: str) -> None:
        """Record simulated completion of a routed part."""
        self.last_action = f"SIMULATED PART {part_id} -> {category} DONE"

    def emergency_stop(self) -> None:
        """Enter simulated fail-safe state."""
        self.last_action = "SIMULATED EMERGENCY STOP"
