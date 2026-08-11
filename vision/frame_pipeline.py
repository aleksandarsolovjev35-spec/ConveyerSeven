"""Multithreaded CV Producer-Consumer frame capture and inference pipeline."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias

from core.app_logging import get_logger

log = get_logger("vision.pipeline")

Frame: TypeAlias = Any
FrameSet: TypeAlias = Mapping[str, Frame]
InferenceResult: TypeAlias = dict[str, list[dict[str, Any]]]


class FrameSource(Protocol):
    """Captures one coherent set of camera frames."""

    def __call__(self) -> FrameSet:
        """Return the most recent coherent frame set."""


class FrameInferencer(Protocol):
    """Runs inference over one frame set."""

    def __call__(self, frames: FrameSet) -> InferenceResult:
        """Return detections for every supplied camera role."""


class ResultConsumer(Protocol):
    """Receives inference results outside capture and inference threads."""

    def __call__(self, packet: InferencePacket) -> None:
        """Apply business logic to a completed inference packet."""


@dataclass(frozen=True, slots=True)
class FramePacket:
    """Timestamped camera snapshot flowing from capture to inference."""

    captured_at: float
    frames: FrameSet


@dataclass(frozen=True, slots=True)
class InferencePacket:
    """Inference result flowing from inference to business logic."""

    captured_at: float
    inferred_at: float
    frames: FrameSet
    detections: InferenceResult
    model_health: list[dict[str, Any]] | None = None


class FramePipeline:
    """Asynchronous CV Producer-Consumer pipeline decoupling frame capture and neural inference.

    Thread 1 (Grabber / Producer): Continuously reads fresh frames from CameraManager
    and deposits them into raw_frames_queue (capacity=1..2, dropping stale frames).

    Thread 2 (Inference / Consumer): Consumes raw frames from raw_frames_queue, executes
    neural network inference via VisionCluster, and deposits inference results into
    results_queue as well as optionally emitting 'vision:result_ready' on event_bus.
    """

    def __init__(
        self,
        cameras: Any,
        vision: Any,
        inspector: Any = None,
        event_bus: Any = None,
        *,
        queue_size: int = 1,
        capture_interval: float = 0.0,
    ) -> None:
        """Initialize the pipeline with camera capture source and vision model backend."""
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        if capture_interval < 0.0:
            raise ValueError("capture_interval must be non-negative")

        self.cameras = cameras
        self.vision = vision
        self.inspector = inspector
        self.event_bus = event_bus
        self.capture_interval = capture_interval

        # Bounded capacity queues (drops old on overflow to ensure freshest data)
        self.raw_frames_queue: queue.Queue[FramePacket | dict[str, Any]] = queue.Queue(maxsize=queue_size)
        self.results_queue: queue.Queue[InferencePacket | dict[str, Any]] = queue.Queue(maxsize=queue_size)

        self._shutdown_event = threading.Event()
        self._pause_event = threading.Event()
        self._grabber_thread: threading.Thread | None = None
        self._inference_thread: threading.Thread | None = None

        self._lock = threading.Lock()
        self._latest_result: dict[str, Any] | None = None
        self._error: BaseException | None = None

    @property
    def is_running(self) -> bool:
        """Whether background worker threads are currently active."""
        return bool(
            self._grabber_thread
            and self._grabber_thread.is_alive()
            and self._inference_thread
            and self._inference_thread.is_alive()
            and not self._shutdown_event.is_set()
        )

    @property
    def error(self) -> BaseException | None:
        """Return the first worker exception if any occurred."""
        with self._lock:
            return self._error

    def start(self) -> None:
        """Start grabber (producer) and inference (consumer) threads."""
        with self._lock:
            if self.is_running:
                log.warning("FramePipeline is already running")
                return

            self._shutdown_event.clear()
            self._pause_event.clear()
            self._error = None

            self._grabber_thread = threading.Thread(
                target=self._grabber_loop,
                name="cv-grabber-producer",
                daemon=True,
            )
            self._inference_thread = threading.Thread(
                target=self._inference_loop,
                name="cv-inference-consumer",
                daemon=True,
            )

            self._grabber_thread.start()
            self._inference_thread.start()
            log.info("FramePipeline started (Producer: Grabber, Consumer: Inference)")

    def stop(self, timeout: float = 2.0) -> None:
        """Stop background worker threads gracefully."""
        self._shutdown_event.set()
        if self._grabber_thread and self._grabber_thread.is_alive():
            self._grabber_thread.join(timeout=timeout)
        if self._inference_thread and self._inference_thread.is_alive():
            self._inference_thread.join(timeout=timeout)

        self._grabber_thread = None
        self._inference_thread = None
        log.info("FramePipeline stopped")

    def pause(self) -> None:
        """Pause frame capture and inference without tearing down threads."""
        self._pause_event.set()

    def resume(self) -> None:
        """Resume frame capture and inference."""
        self._pause_event.clear()

    def flush(self) -> None:
        """Drain all queued frames and results."""
        while not self.raw_frames_queue.empty():
            try:
                self.raw_frames_queue.get_nowait()
            except queue.Empty:
                break
        while not self.results_queue.empty():
            try:
                self.results_queue.get_nowait()
            except queue.Empty:
                break

    # ------------------ Producer (Grabber Thread) ------------------

    def _grabber_loop(self) -> None:
        """Continuously grab frames from cameras and push the latest to raw_frames_queue."""
        log.debug("Grabber thread started")
        while not self._shutdown_event.is_set():
            if self._pause_event.is_set():
                self._shutdown_event.wait(0.05)
                continue

            try:
                captured_at = time.monotonic()
                frames = self._capture_frames()
                if frames:
                    packet = FramePacket(captured_at=captured_at, frames=frames)
                    self._put_drop_old(self.raw_frames_queue, packet)
            except BaseException as exc:
                self._record_error(exc)
                log.error("Grabber thread error: %s", exc)
                self._shutdown_event.wait(0.1)

            if self.capture_interval > 0:
                self._shutdown_event.wait(self.capture_interval)

        log.debug("Grabber thread exiting")

    def _capture_frames(self) -> dict[str, Any] | None:
        """Fetch frame set from camera manager or callable."""
        if self.cameras is None:
            return None
        if callable(getattr(self.cameras, "capture_all", None)):
            return self.cameras.capture_all()
        if callable(self.cameras):
            return self.cameras()
        return None

    # ------------------ Consumer (Inference Thread) ------------------

    def _inference_loop(self) -> None:
        """Consume raw frames, execute vision model inference, and publish results."""
        log.debug("Inference thread started")
        while not self._shutdown_event.is_set():
            try:
                packet = self.raw_frames_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if self._pause_event.is_set():
                continue

            try:
                if isinstance(packet, FramePacket):
                    captured_at = packet.captured_at
                    frames = packet.frames
                elif isinstance(packet, dict):
                    captured_at = packet.get("captured_at", time.monotonic())
                    frames = packet.get("frames", {})
                else:
                    frames = packet
                    captured_at = time.monotonic()

                # Run model inference
                detections = self._run_inference(frames)
                inferred_at = time.monotonic()
                health = getattr(self.vision, "last_health", None)

                result_packet = InferencePacket(
                    captured_at=captured_at,
                    inferred_at=inferred_at,
                    frames=frames,
                    detections=detections,
                    model_health=list(health) if health else None,
                )

                result_dict = {
                    "captured_at": captured_at,
                    "inferred_at": inferred_at,
                    "frames": frames,
                    "vision_results": detections,
                    "model_health": health,
                }

                with self._lock:
                    self._latest_result = result_dict

                self._put_drop_old(self.results_queue, result_packet)

                # Emit event bus signal if registered
                if self.event_bus is not None and callable(getattr(self.event_bus, "emit", None)):
                    try:
                        self.event_bus.emit("vision:result_ready", result_dict)
                    except Exception as emit_err:
                        log.warning("Failed to emit vision:result_ready: %s", emit_err)

            except BaseException as exc:
                self._record_error(exc)
                log.error("Inference thread error: %s", exc)

        log.debug("Inference thread exiting")

    def _run_inference(self, frames: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        """Execute inference on the vision cluster / inferencer."""
        if not frames or self.vision is None:
            return {}
        if callable(getattr(self.vision, "process", None)):
            return self.vision.process(frames)
        if callable(getattr(self.vision, "process_all", None)):
            return self.vision.process_all(frames)
        if callable(self.vision):
            return self.vision(frames)
        return {role: [] for role in frames}

    # ------------------ Retrieval Helpers ------------------

    def get_latest_result(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Retrieve the latest inference result packet, waiting up to timeout seconds."""
        if timeout is not None and timeout > 0:
            try:
                packet = self.results_queue.get(timeout=timeout)
                if isinstance(packet, InferencePacket):
                    return {
                        "captured_at": packet.captured_at,
                        "inferred_at": packet.inferred_at,
                        "frames": packet.frames,
                        "vision_results": packet.detections,
                        "model_health": packet.model_health,
                    }
                if isinstance(packet, dict):
                    return packet
            except queue.Empty:
                pass

        with self._lock:
            if self._latest_result is not None:
                return dict(self._latest_result)
        return None

    def get_raw_frames(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Retrieve the freshest raw camera frames."""
        try:
            packet = self.raw_frames_queue.get(timeout=timeout)
            if isinstance(packet, FramePacket):
                return packet.frames
            if isinstance(packet, dict) and "frames" in packet:
                return packet["frames"]
            return packet
        except queue.Empty:
            return None

    # ------------------ Utility ------------------

    @staticmethod
    def _put_drop_old(target_queue: queue.Queue[Any], item: Any) -> None:
        """Insert an item into the bounded queue, dropping stale item on overflow."""
        while True:
            try:
                target_queue.put_nowait(item)
                break
            except queue.Full:
                try:
                    target_queue.get_nowait()
                except queue.Empty:
                    pass

    def _record_error(self, error: BaseException) -> None:
        """Record worker failure."""
        with self._lock:
            if self._error is None:
                self._error = error


# Aliases for compatibility
VisionWorker = FramePipeline
LatestFramePipeline = FramePipeline
