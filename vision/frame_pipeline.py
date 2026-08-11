"""Bounded producer-consumer pipeline for latest-frame vision inference."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias


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

    def __call__(self, packet: "InferencePacket") -> None:
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


class LatestFramePipeline:
    """Three isolated workers connected by capacity-one latest-value queues.

    A full queue never blocks camera capture.  The stale packet is discarded,
    preserving bounded memory and ensuring that inference works on the newest
    available image rather than accumulating latency.
    """

    def __init__(
        self,
        source: FrameSource,
        infer: FrameInferencer,
        consume: ResultConsumer,
        *,
        capture_interval_seconds: float = 0.0,
    ) -> None:
        """Configure the pipeline; workers are only created by ``start``."""
        if capture_interval_seconds < 0.0:
            raise ValueError("capture_interval_seconds must be non-negative")
        self._source = source
        self._infer = infer
        self._consume = consume
        self._capture_interval_seconds = capture_interval_seconds
        self._frames: queue.Queue[FramePacket] = queue.Queue(maxsize=1)
        self._results: queue.Queue[InferencePacket] = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._threads: tuple[threading.Thread, ...] = ()
        self._error_lock = threading.Lock()
        self._error: BaseException | None = None

    @property
    def error(self) -> BaseException | None:
        """Return the first worker failure, if one occurred."""
        with self._error_lock:
            return self._error

    def start(self) -> None:
        """Start capture, inference, and business-logic workers once."""
        if self._threads:
            raise RuntimeError("frame pipeline is already started")
        self._stop.clear()
        self._threads = tuple(
            threading.Thread(target=target, name=name, daemon=True)
            for name, target in (
                ("frame-capture", self._capture_loop),
                ("frame-inference", self._inference_loop),
                ("frame-business", self._consumer_loop),
            )
        )
        for thread in self._threads:
            thread.start()

    def stop(self, timeout_seconds: float = 2.0) -> None:
        """Signal workers and wait a bounded interval for their completion."""
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout_seconds)
        self._threads = ()

    def _capture_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._put_latest(
                    self._frames,
                    FramePacket(captured_at=time.monotonic(), frames=self._source()),
                )
            except BaseException as error:
                self._record_error(error)
                return
            self._stop.wait(self._capture_interval_seconds)

    def _inference_loop(self) -> None:
        while not self._stop.is_set():
            try:
                frame_packet = self._frames.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._put_latest(
                    self._results,
                    InferencePacket(
                        captured_at=frame_packet.captured_at,
                        inferred_at=time.monotonic(),
                        frames=frame_packet.frames,
                        detections=self._infer(frame_packet.frames),
                    ),
                )
            except BaseException as error:
                self._record_error(error)
                return

    def _consumer_loop(self) -> None:
        while not self._stop.is_set():
            try:
                packet = self._results.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._consume(packet)
            except BaseException as error:
                self._record_error(error)
                return

    @staticmethod
    def _put_latest(target: queue.Queue[Any], packet: Any) -> None:
        """Insert a packet without waiting; discard exactly one stale packet."""
        try:
            target.put_nowait(packet)
        except queue.Full:
            try:
                target.get_nowait()
            except queue.Empty:
                pass
            target.put_nowait(packet)

    def _record_error(self, error: BaseException) -> None:
        """Store the first failure and terminate every worker."""
        with self._error_lock:
            if self._error is None:
                self._error = error
        self._stop.set()
