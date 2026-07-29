"""Живой просмотр во время движения и статический расчёт правил."""

import threading
import time
import unittest
from types import SimpleNamespace

from core.live_preview import LiveCaptureGate, LivePreview
from core.production_cycle import ProductionCycle

ROLES = (
    "INPUT_LEFT",
    "INPUT_RIGHT",
    "SPIDER_LEFT",
    "SPIDER_RIGHT",
    "SPIDER_IN",
    "SPIDER_OUT",
    "TOP",
)


class RecordingMonitor:
    def __init__(self):
        self.server = SimpleNamespace(active_camera_role="TOP")
        self.frame_updates = []
        self.overlay_clears = 0
        self.lock = threading.Lock()

    def update(self, **kwargs):
        with self.lock:
            frames = kwargs.get("frames")
            if frames:
                self.frame_updates.append(set(frames))
            if kwargs.get("vision_results") == {} and kwargs.get("rule_results") == []:
                self.overlay_clears += 1


class TrackingCameras:
    """Камеры, фиксирующие источник каждого чтения и их пересечения.

    Чтение занимает ненулевое время, поэтому наложение live-чтения на
    чтение для инспекции обнаруживается явно, а не по косвенным признакам.
    """

    READ_DURATION = 0.01

    def __init__(self):
        self.mapping = {role: index for index, role in enumerate(ROLES)}
        self.lock = threading.Lock()
        self.live_reads = 0
        self.inspection_reads = 0
        self.moving = False
        self.read_while_moving = []
        self.concurrent_reads = []
        self._in_flight = set()

    def _read(self, source):
        with self.lock:
            if source == "live":
                self.live_reads += 1
            else:
                self.inspection_reads += 1
            if self.moving:
                self.read_while_moving.append(source)
            if self._in_flight - {source}:
                self.concurrent_reads.append(
                    (source, sorted(self._in_flight))
                )
            self._in_flight.add(source)
        try:
            # Реальное чтение камеры не мгновенно: даём окно для гонки.
            time.sleep(self.READ_DURATION)
        finally:
            with self.lock:
                self._in_flight.discard(source)

    def capture_single(self, role):
        self._read("live")
        return object()

    def capture_roles(self, roles):
        self._read("live")
        return {role: object() for role in roles}

    def capture_all(self):
        self._read("inspection")
        return {role: object() for role in ROLES}


class LiveCaptureGateTests(unittest.TestCase):
    def test_pause_waits_for_in_flight_live_read_to_finish(self):
        gate = LiveCaptureGate()
        entered = threading.Event()
        release = threading.Event()
        paused_at = []

        def live_worker():
            with gate.live_read() as allowed:
                self.assertTrue(allowed)
                entered.set()
                release.wait(1.0)

        worker = threading.Thread(target=live_worker)
        worker.start()
        self.assertTrue(entered.wait(1.0))

        def pauser():
            gate.pause(timeout=2.0)
            paused_at.append(time.monotonic())

        pause_thread = threading.Thread(target=pauser)
        pause_thread.start()
        time.sleep(0.05)
        # Пауза обязана ждать, пока live-чтение не завершится.
        self.assertEqual(paused_at, [])
        release.set()
        worker.join(1.0)
        pause_thread.join(1.0)
        self.assertEqual(len(paused_at), 1)
        self.assertTrue(gate.paused)

    def test_live_read_is_refused_while_paused(self):
        gate = LiveCaptureGate()
        self.assertTrue(gate.pause())
        with gate.live_read() as allowed:
            self.assertFalse(allowed)
        gate.resume()
        with gate.live_read() as allowed:
            self.assertTrue(allowed)

    def test_pause_times_out_instead_of_racing_inspection(self):
        gate = LiveCaptureGate()
        entered = threading.Event()
        release = threading.Event()

        def live_worker():
            with gate.live_read():
                entered.set()
                release.wait(2.0)

        worker = threading.Thread(target=live_worker)
        worker.start()
        self.assertTrue(entered.wait(1.0))
        self.assertFalse(gate.pause(timeout=0.1))
        release.set()
        worker.join(1.0)

    def test_failed_pause_does_not_leave_preview_blocked_forever(self):
        """Неудачная пауза обязана сняться сама.

        Иначе повисший счётчик навсегда остановил бы live-просмотр, и
        оператор до перезапуска видел бы замерший кадр.
        """
        gate = LiveCaptureGate()
        entered = threading.Event()
        release = threading.Event()

        def live_worker():
            with gate.live_read():
                entered.set()
                release.wait(2.0)

        worker = threading.Thread(target=live_worker)
        worker.start()
        self.assertTrue(entered.wait(1.0))
        self.assertFalse(gate.pause(timeout=0.1))
        release.set()
        worker.join(1.0)

        self.assertFalse(gate.paused)
        with gate.live_read() as allowed:
            self.assertTrue(allowed)

    def test_reset_clears_nested_pauses(self):
        gate = LiveCaptureGate()
        gate.pause()
        gate.pause()
        gate.reset()
        self.assertFalse(gate.paused)
        with gate.live_read() as allowed:
            self.assertTrue(allowed)


class LivePreviewTests(unittest.TestCase):
    def test_preview_publishes_selected_and_auxiliary_cameras(self):
        cameras = TrackingCameras()
        monitor = RecordingMonitor()
        preview = LivePreview(cameras, monitor, lambda: "TOP")
        preview.start()
        time.sleep(0.25)
        preview.stop()

        published = set()
        for roles in monitor.frame_updates:
            published |= roles
        self.assertEqual(published, set(ROLES))
        self.assertGreater(cameras.live_reads, 0)
        self.assertEqual(cameras.inspection_reads, 0)

    def test_auxiliary_loop_reads_all_roles_except_the_selected_one(self):
        """Вспомогательный цикл берёт остальные шесть камер одним пакетом."""

        class BatchTrackingCameras(TrackingCameras):
            def __init__(self):
                super().__init__()
                self.batches = []

            def capture_roles(self, roles):
                self.batches.append(tuple(roles))
                return super().capture_roles(roles)

        cameras = BatchTrackingCameras()
        preview = LivePreview(cameras, RecordingMonitor(), lambda: "TOP")
        preview.start()
        time.sleep(0.3)
        preview.stop()

        self.assertTrue(cameras.batches)
        for batch in cameras.batches:
            self.assertNotIn("TOP", batch)
            self.assertEqual(set(batch), set(ROLES) - {"TOP"})

    def test_paused_context_blocks_live_reads(self):
        cameras = TrackingCameras()
        preview = LivePreview(cameras, RecordingMonitor(), lambda: "TOP")
        preview.start()
        time.sleep(0.08)
        with preview.paused():
            before = cameras.live_reads
            time.sleep(0.12)
            self.assertEqual(cameras.live_reads, before)
        time.sleep(0.08)
        preview.stop()
        self.assertGreater(cameras.live_reads, 0)

    def test_pause_waits_only_for_camera_access_not_for_ui_publish(self):
        """Гейт держится на чтении камеры, а не на публикации в UI.

        Публикация перекодирует JPEG и может быть медленной. Если бы она
        шла внутри гейта, каждый шаг линии ждал бы её впустую.
        """

        class SlowMonitor:
            def update(self, **kwargs):
                time.sleep(0.12)

        cameras = TrackingCameras()
        preview = LivePreview(cameras, SlowMonitor(), lambda: "TOP")
        preview.start()
        time.sleep(0.15)
        try:
            waits = []
            for _ in range(4):
                started = time.monotonic()
                self.assertTrue(preview.pause(timeout=5.0))
                waits.append(time.monotonic() - started)
                preview.resume()
                time.sleep(0.03)
        finally:
            preview.stop()

        self.assertLess(max(waits), 0.05)

    def test_camera_failure_is_reported_and_stops_preview(self):
        class FailingCameras:
            mapping = {"TOP": 0}

            def capture_single(self, role):
                raise RuntimeError("camera read failed")

            def capture_roles(self, roles):
                return {}

            def capture_all(self):
                raise RuntimeError("camera read failed")

        preview = LivePreview(FailingCameras(), RecordingMonitor(), lambda: "TOP")
        preview.start()
        deadline = time.monotonic() + 1.0
        while not preview.error and time.monotonic() < deadline:
            time.sleep(0.01)
        preview.stop()
        self.assertIn("camera read failed", preview.error)


class FakeConveyor:
    """Лента, помечающая интервал фактического движения."""

    def __init__(self, cameras):
        self.cameras = cameras
        self.moves = 0

    def move_step(self):
        self.moves += 1
        self.cameras.moving = True
        time.sleep(0.05)

    def wait_stop(self, progress_callback=None):
        if progress_callback is not None:
            progress_callback({"mov": 0, "wait": 0, "lasterr": 0})
        time.sleep(0.05)
        self.cameras.moving = False

    def emergency_stop(self):
        self.cameras.moving = False


class FakeDistributor:
    def __init__(self):
        self.on_state_changed = None
        self.cancel_check = None
        self.dist1_open_position = 340
        self.dist2_cleanup_position = 340

    status = {
        "dist1_position": 0,
        "dist1_max": 340,
        "dist1_state": "IDLE",
        "dist2_position": 0,
        "dist2_max": 340,
        "dist2_state": "IDLE",
        "dist2_target": "BAD",
        "last_distributor_action": "-",
    }

    def park_production(self):
        return None

    def reset_target(self):
        return None

    def emergency_stop(self):
        return None


class EmptyTrayInspector:
    INPUT_ROLES = ("INPUT_LEFT", "INPUT_RIGHT")
    SPIDER_ROLES = ROLES

    def __init__(self):
        self.vision = SimpleNamespace(last_health=[])

    @staticmethod
    def _empty():
        return SimpleNamespace(
            stage="input",
            defects=[],
            vision_results={},
            rule_results=[],
            annotated={},
            raw_frames={},
            raw_overlay_frames={},
            is_empty_tray=True,
            consensus={},
            model_health=[],
        )

    def inspect_input_consensus(self, **kwargs):
        return self._empty()

    def inspect_spider_consensus(self, **kwargs):
        return None


class ProductionLivePreviewTests(unittest.TestCase):
    """Поведение живого просмотра внутри производственного цикла."""

    def make_cycle(self, cameras, monitor):
        return ProductionCycle(
            FakeConveyor(cameras),
            cameras,
            EmptyTrayInspector(),
            FakeDistributor(),
            monitor=monitor,
            # Паузу просмотра в тестах не держим: она растянула бы каждый
            # шаг на секунды реального времени.
            review_seconds=0,
        )

    def run_steps(self, cycle, seconds=0.6, min_moves=0):
        thread = threading.Thread(target=cycle.start, daemon=True)
        thread.start()
        if min_moves:
            # Первый шаг теперь — контроль детали под камерами без
            # движения ленты, поэтому ждём именно факта проезда.
            deadline = time.monotonic() + 10.0
            while (
                cycle.conveyor.moves < min_moves
                and time.monotonic() < deadline
                and thread.is_alive()
            ):
                time.sleep(0.02)
        time.sleep(seconds)
        cycle.request_force_exit()
        thread.join(3.0)
        self.assertFalse(thread.is_alive())

    def test_live_preview_runs_while_belt_moves(self):
        cameras = TrackingCameras()
        monitor = RecordingMonitor()
        cycle = self.make_cycle(cameras, monitor)
        self.assertTrue(cycle.request_start())
        # Стартовый контроль ленту не двигает, поэтому ждём первый
        # фактический проезд, прежде чем проверять live-кадры в движении.
        self.run_steps(cycle, min_moves=1)

        self.assertGreater(cycle.conveyor.moves, 0)
        # Во время движения оператор получает свежие кадры.
        self.assertTrue(
            any(source == "live" for source in cameras.read_while_moving),
            "во время движения ленты не было ни одного live-кадра",
        )

    def test_inspection_frames_are_never_captured_while_belt_moves(self):
        cameras = TrackingCameras()
        cycle = self.make_cycle(cameras, RecordingMonitor())
        self.assertTrue(cycle.request_start())
        self.run_steps(cycle, min_moves=1)

        self.assertGreater(cameras.inspection_reads, 0)
        self.assertNotIn(
            "inspection",
            cameras.read_while_moving,
            "кадр для defect rules снят во время движения ленты",
        )

    def test_live_read_never_overlaps_inspection_capture(self):
        cameras = TrackingCameras()
        cycle = self.make_cycle(cameras, RecordingMonitor())
        self.assertTrue(cycle.request_start())
        self.run_steps(cycle)

        self.assertGreater(cameras.inspection_reads, 0)
        self.assertGreater(cameras.live_reads, 0)
        self.assertEqual(
            cameras.concurrent_reads,
            [],
            "live-чтение наложилось на захват кадров для инспекции",
        )

    def test_overlays_are_cleared_before_each_movement(self):
        cameras = TrackingCameras()
        monitor = RecordingMonitor()
        cycle = self.make_cycle(cameras, monitor)
        self.assertTrue(cycle.request_start())
        self.run_steps(cycle)

        self.assertGreater(monitor.overlay_clears, 0)

    def test_status_reports_live_and_static_phases_for_the_operator(self):
        cameras = TrackingCameras()
        cycle = self.make_cycle(cameras, RecordingMonitor())

        idle = cycle._build_status()["live"]
        self.assertFalse(idle["running"])
        self.assertFalse(idle["streaming"])
        self.assertFalse(idle["static"])

        self.assertTrue(cycle.request_start())
        running = cycle._build_status()["live"]
        self.assertTrue(running["running"])
        self.assertTrue(running["streaming"])
        self.assertFalse(running["static"])

        # Статическая фаза: поток остановлен, кадры принадлежат правилам.
        cycle.stages.enter_motion()
        cycle.stages.enter_settle()
        cycle.stages.enter_capture()
        try:
            static = cycle._build_status()["live"]
            self.assertTrue(static["static"])
            self.assertFalse(static["streaming"])
            self.assertEqual(static["stage"], "CAPTURE")
        finally:
            cycle.stages.reset()

        self.assertTrue(cycle._build_status()["live"]["streaming"])
        cycle.request_force_exit()
        cycle.live.stop()

    def test_start_from_jog_keeps_live_preview_running(self):
        cameras = TrackingCameras()
        cycle = self.make_cycle(cameras, RecordingMonitor())
        cycle.jog = SimpleNamespace(
            status={"error": None, "busy": False, "hold_steps": 0,
                    "last_action": "-", "direction": None},
            release=lambda reason="": True,
        )

        self.assertTrue(cycle.enter_jog())
        self.assertTrue(cycle.live.running)

        # START автоматически выходит из JOG, но поток обязан остаться.
        self.assertTrue(cycle.request_start())
        self.assertFalse(cycle.jog_active)
        self.assertTrue(cycle.live.running)

        cycle.request_force_exit()
        cycle.live.stop()

    def test_failure_inside_capture_releases_cameras_and_resets_stage(self):
        """Падение на съёмке не должно оставить камеры за инспекцией."""

        class ExplodingCameras(TrackingCameras):
            def capture_all(self):
                raise RuntimeError("camera exploded during capture")

        cameras = ExplodingCameras()
        cycle = self.make_cycle(cameras, RecordingMonitor())
        self.assertTrue(cycle.request_start())

        cycle._run_once_safe()

        self.assertEqual(cycle.state, "FAULT")
        self.assertEqual(cycle.stages.stage.value, "IDLE")
        self.assertFalse(cycle.stages.static)
        self.assertFalse(cycle.live.gate.paused)

    def test_live_preview_stops_after_force_exit(self):
        cameras = TrackingCameras()
        cycle = self.make_cycle(cameras, RecordingMonitor())
        self.assertTrue(cycle.request_start())
        self.run_steps(cycle)

        self.assertFalse(cycle.live.running)
        self.assertFalse(cycle.live.gate.paused)


if __name__ == "__main__":
    unittest.main()
