"""Строгое голосование трёх независимых прогонов инспекции."""

from __future__ import annotations

from copy import deepcopy


INSPECTION_RUNS = 3
CONSENSUS_MIN_VOTES = 2


class InspectionConsensusError(RuntimeError):
    """Невозможно получить валидное решение 2 из 3."""


def summarize_model_health(model_health) -> list[dict]:
    """Свернуть три запуска одной пары camera/model в одну UI-строку."""

    grouped = {}
    order = []
    for row in model_health:
        if not isinstance(row, dict):
            continue
        key = (row.get("role"), row.get("model"))
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(row)

    summary = []
    for key in order:
        rows = sorted(
            grouped[key],
            key=lambda row: int(row.get("run") or 0),
        )
        elapsed = [float(row.get("elapsed_ms") or 0.0) for row in rows]
        errors = [str(row.get("error")) for row in rows if row.get("error")]
        summary.append({
            "role": key[0],
            "model": key[1],
            "ok": len(rows) == INSPECTION_RUNS and all(
                bool(row.get("ok")) for row in rows
            ),
            "runs": len(rows),
            "elapsed_ms": sum(elapsed) / len(elapsed) if elapsed else 0.0,
            "elapsed_total_ms": sum(elapsed),
            "detections": int(rows[-1].get("detections") or 0) if rows else 0,
            "detections_by_run": [
                int(row.get("detections") or 0) for row in rows
            ],
            "error": "; ".join(errors) if errors else None,
        })
    return summary


def _require_three(items, label: str) -> list:
    values = list(items)
    if len(values) != INSPECTION_RUNS:
        raise InspectionConsensusError(
            f"{label}: ожидалось {INSPECTION_RUNS} прогона, получено {len(values)}"
        )
    return values


def _strict_majority(states, label: str) -> tuple[bool, int, int]:
    values = _require_three(states, label)
    if any(type(value) is not bool for value in values):
        raise InspectionConsensusError(
            f"{label}: каждый результат голосования должен быть bool"
        )

    positive_votes = sum(values)
    negative_votes = len(values) - positive_votes
    if positive_votes >= CONSENSUS_MIN_VOTES:
        return True, positive_votes, negative_votes
    if negative_votes >= CONSENSUS_MIN_VOTES:
        return False, positive_votes, negative_votes
    raise InspectionConsensusError(
        f"{label}: нет строгого большинства {CONSENSUS_MIN_VOTES} из "
        f"{INSPECTION_RUNS}"
    )


def combine_rule_results(rule_results_by_run) -> tuple[list, dict, int]:
    """Объединить результаты defect rules по схеме 2 из 3.

    Возвращает ``(final_results, metadata, evidence_run_index)``. Геометрия
    каждого финального правила берётся из последнего прогона, который
    поддерживает итог этого правила. Общий evidence-прогон выбирается по
    максимальному совпадению со всем вектором итоговых решений.
    """

    runs = _require_three(rule_results_by_run, "defect rules")
    runs = [list(results) for results in runs]
    if not runs[0]:
        if any(runs[1:]):
            raise InspectionConsensusError(
                "defect rules: набор правил различается между прогонами"
            )
        return [], {
            "runs": INSPECTION_RUNS,
            "required_votes": CONSENSUS_MIN_VOTES,
            "evidence_run": INSPECTION_RUNS,
            "rules": {},
        }, INSPECTION_RUNS - 1

    names_by_run = [
        [str(getattr(result, "rule_name", "")) for result in results]
        for results in runs
    ]
    expected_names = names_by_run[0]
    if not all(expected_names == names for names in names_by_run[1:]):
        raise InspectionConsensusError(
            "defect rules: порядок или набор правил различается между прогонами"
        )
    if any(not name for name in expected_names):
        raise InspectionConsensusError("defect rules: найдено правило без имени")
    if len(expected_names) != len(set(expected_names)):
        raise InspectionConsensusError("defect rules: имена правил дублируются")

    decisions = []
    vote_rows = []
    for result_index, rule_name in enumerate(expected_names):
        states = []
        for run_index, results in enumerate(runs):
            state = getattr(results[result_index], "triggered", None)
            if type(state) is not bool:
                raise InspectionConsensusError(
                    f"{rule_name}: прогон {run_index + 1} вернул не-bool triggered"
                )
            states.append(state)
        decision, triggered_votes, normal_votes = _strict_majority(
            states,
            rule_name,
        )
        decisions.append(decision)
        vote_rows.append({
            "triggered_votes": int(triggered_votes),
            "normal_votes": int(normal_votes),
            "decision": "triggered" if decision else "normal",
            "states": list(states),
        })

    scores = [
        sum(
            bool(result.triggered) == decision
            for result, decision in zip(results, decisions, strict=True)
        )
        for results in runs
    ]
    # При равенстве используем самый свежий кадр.
    evidence_index = max(
        range(INSPECTION_RUNS),
        key=lambda index: (scores[index], index),
    )

    final_results = []
    rules_metadata = {}
    for result_index, (rule_name, decision, vote_row) in enumerate(
        zip(expected_names, decisions, vote_rows, strict=True)
    ):
        matching_runs = [
            run_index
            for run_index, results in enumerate(runs)
            if bool(results[result_index].triggered) == decision
        ]
        if len(matching_runs) < CONSENSUS_MIN_VOTES:
            raise InspectionConsensusError(
                f"{rule_name}: нет двух результатов, подтверждающих итог"
            )
        source_index = (
            evidence_index
            if evidence_index in matching_runs
            else matching_runs[-1]
        )
        final_result = deepcopy(runs[source_index][result_index])
        final_result.triggered = decision
        details = deepcopy(getattr(final_result, "details", {}) or {})
        consensus = {
            "runs": INSPECTION_RUNS,
            "required_votes": CONSENSUS_MIN_VOTES,
            **vote_row,
            "source_run": source_index + 1,
            "evidence_run": evidence_index + 1,
        }
        details["consensus"] = consensus
        final_result.details = details
        final_results.append(final_result)
        rules_metadata[rule_name] = deepcopy(consensus)

    metadata = {
        "runs": INSPECTION_RUNS,
        "required_votes": CONSENSUS_MIN_VOTES,
        "evidence_run": evidence_index + 1,
        "agreement_scores": scores,
        "rules": rules_metadata,
    }
    return final_results, metadata, evidence_index


def combine_presence_results(presence_results) -> tuple[object, dict, int]:
    """Проголосовать ``empty_tray`` служебного правила part_presence."""

    results = _require_three(presence_results, "part_presence")
    empty_states = []
    for run_index, result in enumerate(results):
        if getattr(result, "rule_name", None) != "part_presence":
            raise InspectionConsensusError(
                f"part_presence: неверное правило в прогоне {run_index + 1}"
            )
        details = getattr(result, "details", None)
        if not isinstance(details, dict) or type(details.get("empty_tray")) is not bool:
            raise InspectionConsensusError(
                f"part_presence: прогон {run_index + 1} не вернул bool empty_tray"
            )
        empty_states.append(details["empty_tray"])

    is_empty, empty_votes, present_votes = _strict_majority(
        empty_states,
        "part_presence",
    )
    matching_runs = [
        index for index, state in enumerate(empty_states) if state == is_empty
    ]
    evidence_index = matching_runs[-1]
    final_result = deepcopy(results[evidence_index])
    final_result.triggered = False
    details = deepcopy(final_result.details)
    details["empty_tray"] = is_empty
    consensus = {
        "runs": INSPECTION_RUNS,
        "required_votes": CONSENSUS_MIN_VOTES,
        "empty_votes": int(empty_votes),
        "present_votes": int(present_votes),
        "decision": "empty" if is_empty else "present",
        "states": ["empty" if state else "present" for state in empty_states],
        "source_run": evidence_index + 1,
        "evidence_run": evidence_index + 1,
    }
    details["consensus"] = consensus
    final_result.details = details
    return final_result, consensus, evidence_index
