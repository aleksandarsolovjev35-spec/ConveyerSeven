from domain.defect_rules import InputPartPresenceRule

from vision.overlay.raw_overlay import RawOverlay

from inspection.consensus import (
    CONSENSUS_MIN_VOTES,
    INSPECTION_RUNS,
    InspectionConsensusError,
    combine_presence_results,
    combine_rule_results,
    describe_picture_run,
    select_picture_run,
    summarize_model_health,
)
from inspection.result import InspectionResult


class Inspector:
    """Выполняет инспекцию по свежему кадру (без тройного голосования)."""

    INPUT_ROLES = ("INPUT_LEFT", "INPUT_RIGHT")
    SPIDER_ROLES = (
        "SPIDER_LEFT", "SPIDER_RIGHT",
        "SPIDER_IN", "SPIDER_OUT",
        "TOP",
    )

    def __init__(self, vision, decision, recorder):
        self.vision = vision
        self.decision = decision
        self.recorder = recorder

    # Production: один свежий кадр, итог — по этому кадру

    def inspect_input_consensus(
        self,
        part_id: int,
        step: int,
        frame_runs,
        force_bad: bool = False,
    ) -> InspectionResult:
        """INPUT-инспекция по одному свежему набору кадров.

        ``part_presence`` оценивается по кадру; если деталь подтверждена,
        vision-результат проходит каждый INPUT defect rule. Итог правила —
        его ``triggered`` в этом единственном прогоне.
        """

        stage_frame_runs = self._prepare_frame_runs(
            frame_runs,
            self.INPUT_ROLES,
            "input",
        )
        vision_runs, model_health = self._process_vision_runs(
            stage_frame_runs,
            self.INPUT_ROLES,
            "input",
        )
        presence_runs = [
            self._evaluate_part_presence(vision_results)
            for vision_results in vision_runs
        ]
        presence_result, presence_vote, presence_evidence = (
            combine_presence_results(presence_runs)
        )

        if bool(presence_result.details.get("empty_tray")):
            consensus = {
                "runs": INSPECTION_RUNS,
                "required_votes": CONSENSUS_MIN_VOTES,
                "evidence_run": presence_evidence + 1,
                "part_presence": presence_vote,
                "rules": {},
            }
            evidence_index = presence_evidence
            # Пустой лоток — тоже картинка по близости к порогу: показываем
            # прогон с самым пограничным flatness (обычно там, где «призрак»
            # детали ближе всего к порогу ложных срабатываний).
            picture_index = select_picture_run([presence_result])
            if picture_index is None:
                picture_index = evidence_index
            consensus["picture_run"] = picture_index + 1
            consensus["picture_reason"] = describe_picture_run(
                [presence_result], picture_index,
            )
            evidence_index = picture_index
            evidence_frames = stage_frame_runs[evidence_index]
            evidence_vision = vision_runs[evidence_index]
            left_count = int(presence_result.details.get("flatness_left") or 0)
            right_count = int(presence_result.details.get("flatness_right") or 0)
            print(
                f"[EMPTY] Step {step}: пустой лоток по кадру; "
                f"evidence flatness L={left_count} R={right_count}"
            )
            return InspectionResult(
                stage="input",
                defects=[],
                vision_results=evidence_vision,
                rule_results=[presence_result],
                annotated={},
                raw_frames=evidence_frames,
                raw_overlay_frames={},
                is_empty_tray=True,
                consensus=consensus,
                model_health=model_health,
                run_frames=stage_frame_runs,
                run_rule_results=[[]],
            )

        rule_results_by_run = [
            self.decision.evaluate_all_detailed(
                vision_results,
                frames=stage_frames,
            )
            for vision_results, stage_frames in zip(
                vision_runs,
                stage_frame_runs,
                strict=True,
            )
        ]
        final_rule_results, rule_vote, evidence_index = combine_rule_results(
            rule_results_by_run
        )
        consensus = dict(rule_vote)
        consensus["part_presence"] = presence_vote

        # Правило присутствия идёт первым в списке результатов для INPUT.
        final_rule_results = [presence_result] + final_rule_results

        # Картинка строится по прогону, чей замер ближе всего к порогу
        # (в норме), либо ближайшему к порогу браку, если все три — брак.
        picture_index = select_picture_run(final_rule_results)
        if picture_index is None:
            picture_index = evidence_index
        consensus["picture_run"] = picture_index + 1
        consensus["picture_reason"] = describe_picture_run(
            final_rule_results, picture_index,
        )
        evidence_index = picture_index

        return self._build_consensus_result(
            stage="input",
            part_id=part_id,
            step=step,
            force_bad=force_bad,
            stage_frame_runs=stage_frame_runs,
            vision_runs=vision_runs,
            final_rule_results=final_rule_results,
            evidence_index=evidence_index,
            consensus=consensus,
            model_health=model_health,
            run_rule_results=rule_results_by_run,
        )

    def inspect_spider_consensus(
        self,
        part_id: int,
        step: int,
        frame_runs,
        force_bad: bool = False,
    ) -> InspectionResult:
        """SPIDER/TOP-инспекция по одному свежему кадру."""

        stage_frame_runs = self._prepare_frame_runs(
            frame_runs,
            self.SPIDER_ROLES,
            "spider",
        )
        vision_runs, model_health = self._process_vision_runs(
            stage_frame_runs,
            self.SPIDER_ROLES,
            "spider",
        )
        rule_results_by_run = [
            self.decision.evaluate_all_detailed(
                vision_results,
                frames=stage_frames,
            )
            for vision_results, stage_frames in zip(
                vision_runs,
                stage_frame_runs,
                strict=True,
            )
        ]
        final_rule_results, consensus, evidence_index = combine_rule_results(
            rule_results_by_run
        )
        # Картинка — по замеру, ближайшему к порогу (в норме), либо
        # ближайшему к порогу браку.
        picture_index = select_picture_run(final_rule_results)
        if picture_index is None:
            picture_index = evidence_index
        consensus["picture_run"] = picture_index + 1
        consensus["picture_reason"] = describe_picture_run(
            final_rule_results, picture_index,
        )
        evidence_index = picture_index
        return self._build_consensus_result(
            stage="spider",
            part_id=part_id,
            step=step,
            force_bad=force_bad,
            stage_frame_runs=stage_frame_runs,
            vision_runs=vision_runs,
            final_rule_results=final_rule_results,
            evidence_index=evidence_index,
            consensus=consensus,
            model_health=model_health,
            run_rule_results=rule_results_by_run,
        )

    @staticmethod
    def _prepare_frame_runs(frame_runs, roles: tuple, stage: str) -> list[dict]:
        runs = list(frame_runs)
        if len(runs) != INSPECTION_RUNS:
            raise InspectionConsensusError(
                f"{stage}: ожидалось наборов кадров: {INSPECTION_RUNS}, "
                f"получено: {len(runs)}"
            )
        stage_runs = []
        for run_index, frames in enumerate(runs):
            if not isinstance(frames, dict):
                raise InspectionConsensusError(
                    f"{stage}: прогон {run_index + 1} не является словарём кадров"
                )
            missing = set(roles) - set(frames)
            if missing:
                raise RuntimeError(
                    f"Missing {stage} camera frames in run {run_index + 1}: "
                    f"{sorted(missing)}"
                )
            stage_runs.append({role: frames[role] for role in roles})
        return stage_runs

    def _process_vision_runs(
        self,
        stage_frame_runs: list[dict],
        roles: tuple,
        stage: str,
    ) -> tuple[list[dict], list[dict]]:
        vision_runs = []
        model_health = []
        for run_index, stage_frames in enumerate(stage_frame_runs):
            vision_results = self.vision.process_all(stage_frames)
            missing_results = set(roles) - set(vision_results)
            if missing_results:
                raise RuntimeError(
                    f"Missing {stage} vision results in run {run_index + 1}: "
                    f"{sorted(missing_results)}"
                )
            vision_runs.append(vision_results)
            health_rows = getattr(self.vision, "last_health", None)
            if isinstance(health_rows, list):
                for row in health_rows:
                    if not isinstance(row, dict):
                        continue
                    model_health.append({**row, "run": run_index + 1})
        return vision_runs, summarize_model_health(model_health)

    def _build_consensus_result(
        self,
        *,
        stage: str,
        part_id: int,
        step: int,
        force_bad: bool,
        stage_frame_runs: list[dict],
        vision_runs: list[dict],
        final_rule_results: list,
        evidence_index: int,
        consensus: dict,
        model_health: list[dict],
        run_rule_results: list | None = None,
    ) -> InspectionResult:
        defects = [
            result.defect
            for result in final_rule_results
            if result.triggered
        ]
        if force_bad:
            defects = ["forced_bad"]

        evidence_frames = stage_frame_runs[evidence_index]
        evidence_vision = vision_runs[evidence_index]
        annotated = self.recorder.process(
            part_id=part_id,
            step=step,
            frames=evidence_frames,
            rule_results=final_rule_results,
        )
        raw_overlay_frames = self._raw_overlays(
            evidence_frames,
            evidence_vision,
        )
        return InspectionResult(
            stage=stage,
            defects=defects,
            vision_results=evidence_vision,
            rule_results=final_rule_results,
            annotated=annotated,
            raw_frames=evidence_frames,
            raw_overlay_frames=raw_overlay_frames,
            is_empty_tray=False,
            consensus=consensus,
            model_health=model_health,
            run_frames=stage_frame_runs,
            run_rule_results=(
                [list(rows) for rows in run_rule_results]
                if run_rule_results is not None
                else [[]]
            ),
            run_vision_results=[dict(item) for item in vision_runs],
        )

    # Empty tray detector

    def _evaluate_part_presence(self, vision_results: dict):
        rule = InputPartPresenceRule(thresholds=self.decision.thresholds)
        if not rule.enabled:
            raise RuntimeError("part_presence rule is disabled")
        return rule.check(vision_results)

    @staticmethod
    def _raw_overlays(stage_frames: dict, vision_results: dict) -> dict:
        raw_overlay_frames = {}
        for role, frame in stage_frames.items():
            detections = vision_results.get(role, [])
            raw_overlay_frames[role] = (
                RawOverlay.render(frame, detections)
                if detections
                else frame.copy()
            )
        return raw_overlay_frames
