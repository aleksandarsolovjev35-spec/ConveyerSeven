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
    """Выполняет одиночную или трёхпроходную инспекцию."""

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

    # Production: три свежих кадра, голосование 2 из 3

    def inspect_input_consensus(
        self,
        part_id: int,
        step: int,
        frame_runs,
        force_bad: bool = False,
    ) -> InspectionResult:
        """INPUT-инспекция по трём независимым наборам свежих кадров.

        Сначала 2 из 3 голосует ``part_presence``. Если деталь подтверждена,
        все три vision-результата проходят каждый INPUT defect rule, включая
        прогон, в котором presence отдельно не подтвердился. Поэтому каждое
        итоговое defect rule всегда имеет ровно три валидных голоса.
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
                f"[EMPTY] Step {step}: majority {presence_vote['empty_votes']}/"
                f"{INSPECTION_RUNS}; evidence flatness L={left_count} R={right_count}"
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
                run_rule_results=[[], [], []],
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
        """SPIDER/TOP-инспекция по трём свежим кадрам с rule-majority."""

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
        # Картинка — по прогону, ближайшему к порогу (в норме), либо
        # ближайшему к порогу браку, если все три замера — брак.
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
                f"{stage}: ожидалось {INSPECTION_RUNS} набора кадров, "
                f"получено {len(runs)}"
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
                else [[], [], []]
            ),
            run_vision_results=[dict(item) for item in vision_runs],
        )

    # Одиночный прогон для диагностики и offline-анализа

    def inspect_input(
        self,
        part_id: int,
        step: int,
        frames: dict,
        force_bad: bool = False,
    ) -> InspectionResult:
        """Одиночная INPUT-инспекция без production-голосования."""

        missing = set(self.INPUT_ROLES) - set(frames)
        if missing:
            raise RuntimeError(f"Missing INPUT camera frames: {sorted(missing)}")

        stage_frames = {role: frames[role] for role in self.INPUT_ROLES}
        vision_results = self.vision.process_all(stage_frames)
        missing_results = set(self.INPUT_ROLES) - set(vision_results)
        if missing_results:
            raise RuntimeError(
                f"Missing INPUT vision results: {sorted(missing_results)}"
            )

        presence_result = self._evaluate_part_presence(vision_results)
        empty = bool(presence_result.details.get("empty_tray"))
        if empty:
            left_count = int(presence_result.details.get("flatness_left") or 0)
            right_count = int(presence_result.details.get("flatness_right") or 0)
            print(
                f"[EMPTY] Step {step}: part not confirmed by both cameras; "
                f"flatness L={left_count} R={right_count}"
            )
            return InspectionResult(
                stage="input",
                defects=[],
                vision_results=vision_results,
                rule_results=[presence_result],
                annotated={},
                raw_frames=stage_frames,
                raw_overlay_frames={},
                is_empty_tray=True,
            )

        res = self._inspect(
            stage="input",
            roles=self.INPUT_ROLES,
            part_id=part_id,
            step=step,
            frames=frames,
            force_bad=force_bad,
            vision_results=vision_results,
            stage_frames=stage_frames,
        )
        res.rule_results = [presence_result] + res.rule_results
        return res

    def inspect_spider(
        self,
        part_id: int,
        step: int,
        frames: dict,
        force_bad: bool = False,
    ) -> InspectionResult:
        """Одиночная SPIDER/TOP-инспекция без production-голосования."""

        return self._inspect(
            stage="spider",
            roles=self.SPIDER_ROLES,
            part_id=part_id,
            step=step,
            frames=frames,
            force_bad=force_bad,
        )

    # Empty tray detector

    def _evaluate_part_presence(self, vision_results: dict):
        rule = InputPartPresenceRule(thresholds=self.decision.thresholds)
        if not rule.enabled:
            raise RuntimeError("part_presence rule is disabled")
        return rule.check(vision_results)

    # Internal single-run path

    def _inspect(
        self,
        stage: str,
        roles: tuple,
        part_id: int,
        step: int,
        frames: dict,
        force_bad: bool,
        vision_results: dict | None = None,
        stage_frames: dict | None = None,
    ) -> InspectionResult:
        missing_frames = set(roles) - set(frames)
        if missing_frames:
            raise RuntimeError(
                f"Missing {stage} camera frames: {sorted(missing_frames)}"
            )
        if stage_frames is None:
            stage_frames = {role: frames[role] for role in roles}

        if vision_results is None:
            vision_results = self.vision.process_all(stage_frames)
        missing_results = set(roles) - set(vision_results)
        if missing_results:
            raise RuntimeError(
                f"Missing {stage} vision results: {sorted(missing_results)}"
            )

        rule_results = self.decision.evaluate_all_detailed(
            vision_results,
            frames=stage_frames,
        )
        defects = [result.defect for result in rule_results if result.triggered]
        if force_bad:
            defects = ["forced_bad"]

        annotated = self.recorder.process(
            part_id=part_id,
            step=step,
            frames=stage_frames,
            rule_results=rule_results,
        )
        raw_overlay_frames = self._raw_overlays(stage_frames, vision_results)

        return InspectionResult(
            stage=stage,
            defects=defects,
            vision_results=vision_results,
            rule_results=rule_results,
            annotated=annotated,
            raw_frames=stage_frames,
            raw_overlay_frames=raw_overlay_frames,
            is_empty_tray=False,
        )

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
