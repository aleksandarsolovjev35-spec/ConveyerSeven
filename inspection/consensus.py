"""Формирование итогов одиночного прогона инспекции.

Итог правила равен его ``triggered`` по единственному свежему набору кадров.
Модуль проверяет контракт результатов и формирует метаданные для правой
панели и архива: ``run_cards``, ``run_status`` и сведения о решении.
"""

from __future__ import annotations

from copy import deepcopy


INSPECTION_RUNS = 1
CONSENSUS_MIN_VOTES = 1

# Причины, по которым правило «не смогло построить область» (fail-closed):
# отсутствие/невалидность области означает срабатывание, а не пропуск.
# По этим маркерам UI показывает «ОБЛАСТЬ НЕ ПОСТРОЕНА» в статусе прогона.
REGION_MISSING_MARKERS = (
    # omission (long/short)
    "no_detections", "missing_or_invalid_mask", "mask_too_small",
    "no_valid_omission", "no_valid_omission_top_line",
    "omission_reference_too_short",
    # платформа / контакты сверху
    "no_valid_platform", "invalid_platform_bbox",
    "invalid_platform_orientation", "invalid_contact_masks",
    "insufficient_valid_contact_masks", "insufficient_valid_contacts",
    "invalid_contact_layout", "layout_groups_failed",
    "contact_boundary_not_built", "inner_platform_reference_not_fitted",
    # стекло / корпус
    "reference_invalid", "missing_glass_mask", "missing_pin_mask",
    "empty_case_ring", "case_central_not_inside_case",
    "invalid_case_count", "invalid_case_central_count", "invalid_case_ring",
    # окна
    "invalid_window_reference_count", "invalid_window_masks",
    "invalid_sink_masks",
)


class InspectionConsensusError(RuntimeError):
    """Невозможно получить валидный результат прогона."""


def summarize_model_health(model_health) -> list[dict]:
    """Свернуть запуски пары camera/model в одну UI-строку."""
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
        last = rows[-1] if rows else {}
        summary.append({
            "role": key[0],
            "model": key[1],
            "ok": all(bool(row.get("ok")) for row in rows) if rows else False,
            "runs": len(rows),
            "elapsed_ms": sum(elapsed) / len(elapsed) if elapsed else 0.0,
            "elapsed_total_ms": sum(elapsed),
            "detections": int(last.get("detections") or 0),
            "detections_by_run": [
                int(row.get("detections") or 0) for row in rows
            ],
            "error": "; ".join(errors) if errors else None,
        })
    return summary


def _one_run(items, label: str) -> object:
    """Проверить и вернуть единственный результат инспекции."""
    values = list(items)
    if len(values) != INSPECTION_RUNS:
        raise InspectionConsensusError(
            f"{label}: ожидалось прогонов: {INSPECTION_RUNS}, "
            f"получено: {len(values)}"
        )
    return values[0]


def _run_summary_cards(rule_name: str, result) -> list:
    """Карточка замеров правила для единственного прогона."""
    # Импорт внутри функции, чтобы не создавать цикл
    # (core.production_cycle -> inspection.consensus -> core.rule_summary).
    from core.rule_summary import build_presence_summary, build_rule_summary

    details = getattr(result, "details", {}) or {}
    if rule_name == "part_presence":
        return [build_presence_summary(details)]
    per_role = details.get("per_role")
    if isinstance(per_role, dict) and per_role:
        return [build_rule_summary(rule_name, details)]
    return [[]]


def _region_missing(role_details: dict) -> bool:
    """Область правила в прогоне не построена (fail-closed)."""
    if not isinstance(role_details, dict):
        return False
    if role_details.get("valid") is False:
        return True
    if role_details.get("skipped"):
        return True
    reason = role_details.get("reason")
    if isinstance(reason, str):
        return any(reason.startswith(m) for m in REGION_MISSING_MARKERS)
    return False


def _run_statuses(rule_name: str, result) -> list:
    """Статус правила для единственного прогона.

    Возвращает список из одного элемента со списком записей по ролям:
    ``В НОРМЕ`` / ``ОТКЛОНЕНИЕ`` / ``ОБЛАСТЬ НЕ ПОСТРОЕНА`` / ``НЕТ ИЗМЕРЕНИЯ``.
    Для ``part_presence`` — ``КОРПУС`` / ``ПУСТО``.
    """
    details = getattr(result, "details", {}) or {}
    if rule_name == "part_presence":
        return [[{
            "role": "INPUT",
            "status": "ПУСТО" if details.get("empty_tray") else "КОРПУС",
            "reason": None,
        }]]

    per_role = details.get("per_role")
    if not isinstance(per_role, dict) or not per_role:
        return [[]]

    rows = []
    for role, role_details in per_role.items():
        if not isinstance(role_details, dict):
            continue
        if _region_missing(role_details):
            rows.append({
                "role": role,
                "status": "ОБЛАСТЬ НЕ ПОСТРОЕНА",
                "reason": role_details.get("reason"),
            })
        elif role_details.get("triggered"):
            rows.append({"role": role, "status": "ОТКЛОНЕНИЕ", "reason": None})
        elif role_details.get("skipped"):
            rows.append({"role": role, "status": "НЕТ ИЗМЕРЕНИЯ", "reason": None})
        else:
            rows.append({"role": role, "status": "В НОРМЕ", "reason": None})
    return [rows]


def combine_rule_results(rule_results_by_run) -> tuple[list, dict, int]:
    """Вернуть результаты defect rules единственного прогона.

    При одном прогоне итог правила — его ``triggered`` как есть, evidence —
    этот же прогон (индекс 0). Метаданные сохраняют формат, ожидаемый UI.
    """
    results = list(_one_run(rule_results_by_run, "defect rules"))

    if not results:
        return [], {
            "runs": INSPECTION_RUNS,
            "required_votes": CONSENSUS_MIN_VOTES,
            "evidence_run": INSPECTION_RUNS,
            "agreement_scores": [0],
            "rules": {},
        }, 0

    names = [str(getattr(r, "rule_name", "")) for r in results]
    if any(not name for name in names):
        raise InspectionConsensusError("defect rules: найдено правило без имени")
    if len(names) != len(set(names)):
        raise InspectionConsensusError("defect rules: имена правил дублируются")
    for result in results:
        if type(getattr(result, "triggered", None)) is not bool:
            raise InspectionConsensusError(
                f"{getattr(result, 'rule_name', '')}: правило вернуло не-bool triggered"
            )

    final_results = []
    rules_metadata = {}
    for result in results:
        triggered = bool(result.triggered)
        rule_name = result.rule_name
        final_result = deepcopy(result)
        details = deepcopy(getattr(final_result, "details", {}) or {})
        consensus = {
            "runs": INSPECTION_RUNS,
            "required_votes": CONSENSUS_MIN_VOTES,
            "triggered_votes": 1 if triggered else 0,
            "normal_votes": 0 if triggered else 1,
            "decision": "triggered" if triggered else "normal",
            "states": [triggered],
            "source_run": INSPECTION_RUNS,
            "evidence_run": INSPECTION_RUNS,
            "run_cards": _run_summary_cards(rule_name, result),
            "run_status": _run_statuses(rule_name, result),
        }
        details["consensus"] = consensus
        final_result.details = details
        final_results.append(final_result)
        rules_metadata[rule_name] = deepcopy(consensus)

    metadata = {
        "runs": INSPECTION_RUNS,
        "required_votes": CONSENSUS_MIN_VOTES,
        "evidence_run": INSPECTION_RUNS,
        "agreement_scores": [len(results)],
        "rules": rules_metadata,
    }
    return final_results, metadata, 0


def combine_presence_results(presence_results) -> tuple[object, dict, int]:
    """Определить ``empty_tray`` служебного правила part_presence.

    При одном прогоне итог — результат этого единственного прогона.
    """
    result = _one_run(presence_results, "part_presence")
    if getattr(result, "rule_name", None) != "part_presence":
        raise InspectionConsensusError("part_presence: неверное правило в прогоне")
    details = getattr(result, "details", None)
    if not isinstance(details, dict) or type(details.get("empty_tray")) is not bool:
        raise InspectionConsensusError("part_presence: прогон не вернул bool empty_tray")

    is_empty = details["empty_tray"]
    final_result = deepcopy(result)
    final_result.triggered = False
    new_details = deepcopy(details)
    consensus = {
        "runs": INSPECTION_RUNS,
        "required_votes": CONSENSUS_MIN_VOTES,
        "empty_votes": 1 if is_empty else 0,
        "present_votes": 0 if is_empty else 1,
        "decision": "empty" if is_empty else "present",
        "states": ["empty" if is_empty else "present"],
        "source_run": INSPECTION_RUNS,
        "evidence_run": INSPECTION_RUNS,
        "run_cards": _run_summary_cards("part_presence", result),
        "run_status": _run_statuses("part_presence", result),
    }
    new_details["consensus"] = consensus
    new_details["empty_tray"] = is_empty
    final_result.details = new_details
    return final_result, consensus, 0


def _metric_table(result) -> dict:
    """Числовые метрики правила (для выбора картинки по близости к порогу)."""
    details = getattr(result, "details", {}) or {}
    consensus = details.get("consensus")
    run_cards = consensus.get("run_cards") if isinstance(consensus, dict) else None
    if not isinstance(run_cards, list) or len(run_cards) != INSPECTION_RUNS:
        return {}
    table = {}
    for cards in run_cards:
        if not isinstance(cards, list):
            continue
        for card in cards:
            if not isinstance(card, dict):
                continue
            role = card.get("role", "")
            for metric in card.get("metrics") or []:
                if not isinstance(metric, dict):
                    continue
                value = metric.get("value_raw")
                limit = metric.get("limit_raw")
                ok = metric.get("ok")
                if value is None or limit is None or ok is None:
                    continue
                try:
                    distance = abs(float(value) - float(limit)) / max(
                        1.0, abs(float(limit)),
                    )
                except (TypeError, ValueError):
                    continue
                metric_key = metric.get("key") or metric.get("label")
                if metric_key is None:
                    continue
                table[(role, metric_key)] = {
                    "role": role,
                    "label": metric.get("label") or metric.get("key") or "—",
                    "limit": metric.get("limit"),
                    "value": metric.get("value"),
                    "ok": bool(ok),
                    "distance": distance,
                }
    return table


def _closest_metric(final_results):
    """Найти метрику, ближайшую к порогу.

    Для сработавшего правила приоритет у вышедшей за порог метрики; для
    нормального — у метрики в норме. При одном прогоне индекс всегда 0.
    """
    decisive = [
        r for r in final_results
        if bool(getattr(r, "triggered", False))
    ] or list(final_results)

    best = None  # (distance, rule_name, metric)
    for result in decisive:
        triggered = bool(getattr(result, "triggered", False))
        for metric in _metric_table(result).values():
            if metric["ok"] == triggered:
                # для сработавшего правила ищем bad-метрику, для нормы — ok
                continue
            candidate = (
                metric["distance"],
                getattr(result, "rule_name", ""),
                metric,
            )
            if best is None or candidate[0] < best[0]:
                best = candidate
    if best is not None:
        return best[1], best[2]

    # Не нашли метрику «решающего» типа: берём ближайшую из любых.
    for result in decisive:
        for metric in _metric_table(result).values():
            candidate = (
                metric["distance"],
                getattr(result, "rule_name", ""),
                metric,
            )
            if best is None or candidate[0] < best[0]:
                best = candidate
    return (best[1], best[2]) if best else None


def select_picture_run(final_results) -> int | None:
    """Выбрать прогон для картинки с разметкой (при одном прогоне — 0).

    Возвращает ``None``, если ни у одного правила нет числовых порогов
    (тогда вызывающий использует единственный прогон).
    """
    return 0 if _closest_metric(final_results) is not None else None


def describe_picture_run(final_results, run_index: int) -> str:
    """Почему для картинки выбран этот прогон."""
    if run_index != 0:
        return "единственный прогон"
    pick = _closest_metric(final_results)
    if pick is None:
        return "единственный прогон (нет числовых порогов)"
    rule_name, metric = pick
    verdict = "норма" if metric["ok"] else "брак"
    role_prefix = f"{metric.get('role')} · " if metric.get("role") else ""
    return (
        f"{rule_name}: {role_prefix}{metric.get('label') or '—'} "
        f"{metric.get('value') if metric.get('value') is not None else '—'} "
        f"(порог {metric.get('limit') if metric.get('limit') is not None else '—'}) — "
        f"{verdict}, ближе всего к порогу"
    )
