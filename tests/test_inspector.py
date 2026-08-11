"""Inspector: pipeline кадры → модели → правила → результат.

Inspector — glue-слой между VisionCluster, DecisionEngine и DebugRecorder.
Тесты используют фейки Vision/Decision/Recorder и проверяют:

* inspect_input_consensus: пустой лоток vs корпус с дефектами;
* inspect_spider_consensus: дефекты spider-стадии;
* _single_stage_frames: валидация контракта (один набор кадров);
* progress callback вызывается на каждом этапе;
* _raw_overlays создаёт overlay для каждой роли.
"""

import numpy as np

from domain.defect_rules.base import RuleResult
from inspection.inspector import Inspector


class FakeVision:
    """Детерминированный vision-кластер: возвращает заданные детекции."""

    def __init__(self, detections_by_role=None):
        self._detections = dict(detections_by_role or {})
        self.last_health = []

    def process_all(self, frames):
        results = {}
        for role in frames:
            results[role] = list(self._detections.get(role, []))
        self.last_health = [
            {"role": role, "model": "fake.pt", "ok": True,
             "elapsed_ms": 10.0, "detections": len(dets), "error": None}
            for role, dets in results.items()
        ]
        return results


class FakeDecision:
    """Детерминированный decision engine: возвращает заданные rule_results."""

    def __init__(self, rule_results=None, thresholds=None):
        self._results = list(rule_results or [])
        self.thresholds = dict(thresholds or {
            "INPUT_LEFT.input_window_geometry_min_confidence": 0.3,
            "INPUT_RIGHT.input_window_geometry_min_confidence": 0.3,
            "input_part_presence_false_positive_max_count": 2,
        })

    def evaluate_all_detailed(self, vision_results, frames=None):
        return list(self._results)

    def evaluate_rules_detailed(self, rules, vision_results, frames=None):
        return list(self._results)


class FakeRecorder:
    """Рекордер без записи на диск: возвращает пустые аннотации."""

    def __init__(self):
        self.calls = []

    def process(self, part_id, step, frames, rule_results):
        self.calls.append({
            "part_id": part_id, "step": step,
            "roles": list(frames.keys()),
        })
        return {}


def _make_frames(roles):
    """Создать минимальные numpy-кадры для заданных ролей."""
    return {role: np.zeros((720, 1280, 3), dtype=np.uint8) for role in roles}


class TestSingleStageFrames:
    def test_один_набор_кадров_возвращается(self):
        frames = _make_frames(("INPUT_LEFT", "INPUT_RIGHT"))
        result = Inspector._single_stage_frames(
            [frames], ("INPUT_LEFT", "INPUT_RIGHT"), "input"
        )
        assert set(result.keys()) == {"INPUT_LEFT", "INPUT_RIGHT"}

    def test_пустой_frame_runs_бросает_ошибку(self):
        try:
            Inspector._single_stage_frames(
                [], ("INPUT_LEFT", "INPUT_RIGHT"), "input"
            )
            raise AssertionError("должен был бросить RuntimeError")
        except RuntimeError as exc:
            assert "один набор кадров" in str(exc)

    def test_два_набора_кадров_бросает_ошибку(self):
        f1 = _make_frames(("INPUT_LEFT", "INPUT_RIGHT"))
        f2 = _make_frames(("INPUT_LEFT", "INPUT_RIGHT"))
        try:
            Inspector._single_stage_frames(
                [f1, f2], ("INPUT_LEFT", "INPUT_RIGHT"), "input"
            )
            raise AssertionError("должен был бросить RuntimeError")
        except RuntimeError as exc:
            assert "один набор кадров" in str(exc)

    def test_недостающая_роль_бросает_ошибку(self):
        frames = _make_frames(("INPUT_LEFT",))
        try:
            Inspector._single_stage_frames(
                [frames], ("INPUT_LEFT", "INPUT_RIGHT"), "input"
            )
            raise AssertionError("должен был бросить RuntimeError")
        except RuntimeError as exc:
            assert "Missing" in str(exc)

    def test_лишние_роли_фильтруются(self):
        frames = _make_frames(("INPUT_LEFT", "INPUT_RIGHT", "TOP"))
        result = Inspector._single_stage_frames(
            [frames], ("INPUT_LEFT", "INPUT_RIGHT"), "input"
        )
        assert set(result.keys()) == {"INPUT_LEFT", "INPUT_RIGHT"}


class TestInspectInputEmptyTray:
    def test_пустой_лоток_возвращает_is_empty_tray(self):
        """Нет flatness-детекций → пустой лоток."""
        vision = FakeVision(detections_by_role={
            "INPUT_LEFT": [],
            "INPUT_RIGHT": [],
        })
        decision = FakeDecision()
        recorder = FakeRecorder()
        inspector = Inspector(vision, decision, recorder)

        frames = _make_frames(("INPUT_LEFT", "INPUT_RIGHT"))
        result = inspector.inspect_input_consensus(
            part_id=1, step=0, frame_runs=[frames],
        )
        assert result.is_empty_tray is True
        assert result.defects == []
        assert result.stage == "input"

    def test_пустой_лоток_не_вызывает_decision(self):
        """При пустом лотке evaluate_all_detailed не вызывается."""
        vision = FakeVision(detections_by_role={
            "INPUT_LEFT": [],
            "INPUT_RIGHT": [],
        })
        decision = FakeDecision()
        recorder = FakeRecorder()
        inspector = Inspector(vision, decision, recorder)

        frames = _make_frames(("INPUT_LEFT", "INPUT_RIGHT"))
        inspector.inspect_input_consensus(
            part_id=1, step=0, frame_runs=[frames],
        )
        # recorder не вызывается для пустого лотка
        assert len(recorder.calls) == 0


class TestInspectInputWithPart:
    def test_корпус_без_дефектов(self):
        """Детекции есть, но правила не сработали."""
        # 3 flatness детекции на каждой камере (выше порога 2)
        det = {"class": "flatness", "confidence": 0.8, "bbox": [0, 0, 100, 100]}
        vision = FakeVision(detections_by_role={
            "INPUT_LEFT": [det, det, det],
            "INPUT_RIGHT": [det, det, det],
        })
        decision = FakeDecision(rule_results=[])
        recorder = FakeRecorder()
        inspector = Inspector(vision, decision, recorder)

        frames = _make_frames(("INPUT_LEFT", "INPUT_RIGHT"))
        result = inspector.inspect_input_consensus(
            part_id=1, step=0, frame_runs=[frames],
        )
        assert result.is_empty_tray is False
        assert result.defects == []
        assert result.stage == "input"
        assert len(recorder.calls) == 1

    def test_корпус_с_дефектом(self):
        """Правило window_geometry сработало."""
        det = {"class": "flatness", "confidence": 0.8, "bbox": [0, 0, 100, 100]}
        vision = FakeVision(detections_by_role={
            "INPUT_LEFT": [det, det, det],
            "INPUT_RIGHT": [det, det, det],
        })
        triggered_rule = RuleResult(
            rule_name="window_geometry",
            triggered=True,
            details={"per_role": {}},
            drawings=[],
        )
        decision = FakeDecision(rule_results=[triggered_rule])
        recorder = FakeRecorder()
        inspector = Inspector(vision, decision, recorder)

        frames = _make_frames(("INPUT_LEFT", "INPUT_RIGHT"))
        result = inspector.inspect_input_consensus(
            part_id=1, step=0, frame_runs=[frames],
        )
        assert result.is_empty_tray is False
        assert "window_geometry" in result.defects
        assert result.consensus.get("picture_run") == 1


class TestInspectSpider:
    def test_spider_без_дефектов(self):
        vision = FakeVision(detections_by_role={
            "SPIDER_LEFT": [],
            "SPIDER_RIGHT": [],
            "SPIDER_IN": [],
            "SPIDER_OUT": [],
            "TOP": [],
        })
        decision = FakeDecision(rule_results=[])
        recorder = FakeRecorder()
        inspector = Inspector(vision, decision, recorder)

        frames = _make_frames(inspector.SPIDER_ROLES)
        result = inspector.inspect_spider_consensus(
            part_id=2, step=4, frame_runs=[frames],
        )
        assert result.defects == []
        assert result.stage == "spider"
        assert len(recorder.calls) == 1

    def test_spider_с_дефектом(self):
        vision = FakeVision(detections_by_role={
            "SPIDER_LEFT": [],
            "SPIDER_RIGHT": [],
            "SPIDER_IN": [],
            "SPIDER_OUT": [],
            "TOP": [],
        })
        triggered_rule = RuleResult(
            rule_name="contacts_long",
            triggered=True,
            details={"per_role": {}},
            drawings=[],
        )
        decision = FakeDecision(rule_results=[triggered_rule])
        recorder = FakeRecorder()
        inspector = Inspector(vision, decision, recorder)

        frames = _make_frames(inspector.SPIDER_ROLES)
        result = inspector.inspect_spider_consensus(
            part_id=2, step=4, frame_runs=[frames],
        )
        assert "contacts_long" in result.defects


class TestProgressCallback:
    def test_callback_вызывается_на_input(self):
        det = {"class": "flatness", "confidence": 0.8, "bbox": [0, 0, 100, 100]}
        vision = FakeVision(detections_by_role={
            "INPUT_LEFT": [det, det, det],
            "INPUT_RIGHT": [det, det, det],
        })
        decision = FakeDecision(rule_results=[])
        recorder = FakeRecorder()
        phases = []

        def progress(phase, label, *, part_id=None, roles=()):
            phases.append(phase)

        inspector = Inspector(vision, decision, recorder, on_progress=progress)
        frames = _make_frames(("INPUT_LEFT", "INPUT_RIGHT"))
        inspector.inspect_input_consensus(
            part_id=1, step=0, frame_runs=[frames],
        )
        assert "INPUT_MODELS" in phases
        assert "INPUT_PRESENCE" in phases
        assert "INPUT_GEOMETRY" in phases
        assert "INPUT_DECISION" in phases

    def test_callback_исключение_не_роняет_инспекцию(self):
        vision = FakeVision(detections_by_role={
            "INPUT_LEFT": [],
            "INPUT_RIGHT": [],
        })
        decision = FakeDecision()
        recorder = FakeRecorder()

        def bad_callback(*args, **kwargs):
            raise ValueError("callback упал")

        inspector = Inspector(vision, decision, recorder, on_progress=bad_callback)
        frames = _make_frames(("INPUT_LEFT", "INPUT_RIGHT"))
        result = inspector.inspect_input_consensus(
            part_id=1, step=0, frame_runs=[frames],
        )
        assert result.is_empty_tray is True


class TestRawOverlays:
    def test_raw_overlays_с_детекциями(self):
        det = {"class": "flatness", "confidence": 0.8, "bbox": [10, 10, 50, 50]}
        frames = _make_frames(("INPUT_LEFT",))
        vision_results = {"INPUT_LEFT": [det]}
        result = Inspector._raw_overlays(frames, vision_results)
        assert "INPUT_LEFT" in result
        # Overlay — это numpy array того же размера
        assert result["INPUT_LEFT"].shape == (720, 1280, 3)

    def test_raw_overlays_без_детекций(self):
        frames = _make_frames(("INPUT_LEFT",))
        vision_results = {"INPUT_LEFT": []}
        result = Inspector._raw_overlays(frames, vision_results)
        assert "INPUT_LEFT" in result
        # Без детекций — копия исходного кадра
        assert result["INPUT_LEFT"].shape == (720, 1280, 3)
