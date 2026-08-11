"""Tests for CV multithreaded FramePipeline (Producer-Consumer) and device detection."""

import queue
import threading
import time
from unittest.mock import MagicMock, patch

from core.event_bus import EventBus
from core.production_cycle import ProductionCycle
from vision.frame_pipeline import FramePipeline
from vision.mock_vision import get_optimal_device as mock_get_device
from vision.vision_cluster import get_optimal_device as cluster_get_device


class FakeCamerasSource:
    """Thread-safe fake camera source for pipeline tests."""

    def __init__(self, roles=("INPUT_LEFT", "INPUT_RIGHT")):
        self.roles = tuple(roles)
        self._counter = 0
        self._lock = threading.Lock()

    def capture_all(self):
        with self._lock:
            self._counter += 1
            idx = self._counter
        return {role: f"frame_{idx}_{role}" for role in self.roles}


class FakeVisionBackend:
    """Fake vision backend tracking processed frame batches."""

    def __init__(self):
        self.processed = []
        self.last_health = [{"role": "INPUT_LEFT", "ok": True}]
        self._lock = threading.Lock()

    def process(self, frames):
        with self._lock:
            self.processed.append(frames)
        return {role: [{"class": "part", "confidence": 0.99}] for role in frames}


def test_optimal_device_detection():
    """Verify get_optimal_device correctly checks hardware availability."""
    assert mock_get_device() == "cpu"

    # Default fallback should return a valid backend string
    device = cluster_get_device()
    assert device in ("cuda", "mps", "openvino", "cpu")

    mock_torch = MagicMock()
    mock_torch.cuda.is_available.return_value = True

    # When cuda is available
    with patch.dict("sys.modules", {"torch": mock_torch}):
        assert cluster_get_device() == "cuda"

    # When mps is available and cuda is not
    mock_torch.cuda.is_available.return_value = False
    mock_torch.backends.mps.is_available.return_value = True
    with patch.dict("sys.modules", {"torch": mock_torch}):
        assert cluster_get_device() == "mps"

    # When neither cuda nor mps is available
    mock_torch.cuda.is_available.return_value = False
    mock_torch.backends.mps.is_available.return_value = False
    with patch.dict("sys.modules", {"torch": mock_torch, "openvino": None}):
        with patch("importlib.util.find_spec", return_value=None):
            assert cluster_get_device() == "cpu"


def test_frame_pipeline_start_and_stop():
    """Verify FramePipeline can start, run worker threads, and stop cleanly."""
    cameras = FakeCamerasSource()
    vision = FakeVisionBackend()
    pipeline = FramePipeline(cameras=cameras, vision=vision, queue_size=1)

    assert not pipeline.is_running
    pipeline.start()
    assert pipeline.is_running

    # Wait for at least one inference result
    result = None
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        result = pipeline.get_latest_result(timeout=0.2)
        if result and "vision_results" in result:
            break
        time.sleep(0.05)

    assert result is not None
    assert "frames" in result
    assert "vision_results" in result
    assert "INPUT_LEFT" in result["vision_results"]

    pipeline.stop()
    assert not pipeline.is_running


def test_frame_pipeline_bounded_queue_drops_old():
    """Verify raw_frames_queue and results_queue maintain maxsize and drop stale data."""
    cameras = FakeCamerasSource()
    vision = FakeVisionBackend()
    pipeline = FramePipeline(cameras=cameras, vision=vision, queue_size=1)

    # Directly test bounded put
    q = queue.Queue(maxsize=1)
    pipeline._put_drop_old(q, "item1")
    pipeline._put_drop_old(q, "item2")
    pipeline._put_drop_old(q, "item3")

    assert q.qsize() == 1
    assert q.get_nowait() == "item3"


def test_frame_pipeline_event_bus_emission():
    """Verify pipeline emits 'vision:result_ready' events on EventBus."""
    cameras = FakeCamerasSource()
    vision = FakeVisionBackend()
    event_bus = EventBus()

    received = []
    event_bus.subscribe("vision:result_ready", received.append)

    pipeline = FramePipeline(
        cameras=cameras,
        vision=vision,
        event_bus=event_bus,
        queue_size=2,
    )
    pipeline.start()

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not received:
        time.sleep(0.05)

    pipeline.stop()

    assert len(received) >= 1
    assert "frames" in received[0]
    assert "vision_results" in received[0]


def test_frame_pipeline_pause_and_resume():
    """Verify pipeline pauses and resumes frame processing."""
    cameras = FakeCamerasSource()
    vision = FakeVisionBackend()
    pipeline = FramePipeline(cameras=cameras, vision=vision, queue_size=1)

    pipeline.start()
    time.sleep(0.1)

    pipeline.pause()
    pipeline.flush()
    time.sleep(0.15)
    # While paused, no new items should be placed in the results queue
    assert pipeline.results_queue.empty()

    pipeline.resume()
    result = pipeline.get_latest_result(timeout=2.0)
    assert result is not None

    pipeline.stop()


def test_frame_pipeline_error_resilience():
    """Verify pipeline catches camera and vision exceptions without crashing."""
    failing_cameras = MagicMock()
    failing_cameras.capture_all.side_effect = RuntimeError("Camera read disconnected")

    vision = FakeVisionBackend()
    pipeline = FramePipeline(cameras=failing_cameras, vision=vision, queue_size=1)
    pipeline.start()

    time.sleep(0.2)
    assert pipeline.error is not None
    assert isinstance(pipeline.error, RuntimeError)

    pipeline.stop()

def test_production_cycle_with_frame_pipeline_integration():
    """Verify ProductionCycle can use FramePipeline results asynchronously."""
    roles = ("INPUT_LEFT", "INPUT_RIGHT", "SPIDER_LEFT", "SPIDER_RIGHT", "SPIDER_IN", "SPIDER_OUT", "TOP")
    cameras = FakeCamerasSource(roles=roles)
    vision = FakeVisionBackend()
    event_bus = EventBus()

    pipeline = FramePipeline(
        cameras=cameras,
        vision=vision,
        event_bus=event_bus,
        queue_size=2,
    )
    pipeline.start()

    # Wait for initial result
    result = pipeline.get_latest_result(timeout=2.0)
    assert result is not None

    mock_conveyor = MagicMock()
    mock_distributor = MagicMock()
    mock_distributor.dist1_open_position = 340
    mock_distributor.status = {
        "dist1_position": 0, "dist1_max": 340, "dist1_state": "GOOD",
        "dist2_position": 0, "dist2_max": 340, "dist2_state": "IDLE",
        "dist2_target": "BAD", "last_distributor_action": "-",
    }
    mock_inspector = MagicMock()
    mock_inspector.INPUT_ROLES = ("INPUT_LEFT", "INPUT_RIGHT")
    mock_inspector.SPIDER_ROLES = ("SPIDER_LEFT", "SPIDER_RIGHT", "SPIDER_IN", "SPIDER_OUT", "TOP")

    cycle = ProductionCycle(
        conveyor=mock_conveyor,
        cameras=cameras,
        inspector=mock_inspector,
        distributor=mock_distributor,
        pipeline=pipeline,
        event_bus=event_bus,
        settle_seconds=0.0,
        stage_trace_seconds=0.0,
        review_seconds=0.0,
    )

    # Step transitions require IDLE -> MOTION -> SETTLE before CAPTURE
    cycle.stages.enter_motion()
    cycle.stages.enter_settle()

    # Test _stage_capture uses pipeline
    captured_runs = cycle._stage_capture(accept_input_for_this_step=True)
    assert len(captured_runs) == 1
    assert "INPUT_LEFT" in captured_runs[0]
    assert cycle._pipeline_latest_vision is not None

    pipeline.stop()
