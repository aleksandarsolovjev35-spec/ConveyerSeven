"""Единичный прогон инспекции (тройное голосование убрано).

Раньше каждая стадия снимала три набора кадров и голосовала 2 из 3.
Теперь стадия выполняется по одному свежему кадру, а вспомогательные
функции этого модуля остались только для построения отчёта (run_cards,
статусы области, выбор картинки). ``INSPECTION_RUNS = 1`` держит
структуры данных совместимыми: ``run_cards`` — один элемент, голосов —
один, evidence — единственный прогон.
"""

from __future__ import annotations

from copy import deepcopy


INSPECTION_RUNS = 1
CONSENSUS_MIN_VOTES = 1

# Причины, по которым правило «не смогло построить область» (fail-closed):
# отсутствие/невалидность области означает срабатывание, а не пропуск.
# По этим маркерам UI показывает «ОБЛАСТЬ НЕ ПОСТРОЕНА» в статусах прогонов.
REGION_MISSING_MARKERS = (
    # omission (long/short)
    "no_detections", "missing_or_invalid_mask", "mask_too_small",
    "no_valid_omission", "no_valid_omission_top_line",
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


def _require_runs(items, label: str) -> list:
    values = list(items)
    if len(values) != INSPECTION_RUNS:
        raise InspectionConsensusError(
            f"{label}: ожидалось прогонов: {INSPECTION_RUNS}, "
            f"получено: {len(values)}"
        )
    return values


def _strict_majority(states, label: str) -> tuple[bool, int, int]:
    values = _require_runs(states, label)
    if any(type(value) is not bool for value in values):
        raise InspectionConsensusError(
            f"{label}: каждый результат должен быть bool"
        )

    positive_votes = sum(values)
    negative_votes = len(values) - positive_votes
    if positive_votes >= CONSENSUS_MIN_VOTES:
        return True, positive_votes, negative_votes
    if negative_votes >= CONSENSUS_MIN_VOTES:
        return False, positive_votes, negative_votes
    raise InspectionConsensusError(
        f"{label}: нет решения {CONSENSUS_MIN_VOTES} из "
        f"{INSPECTION_RUNS}"
    )


def combine_rule_results(rule_results_by_run) -> tuple[list, dict, int]:
    """Вернуть результаты defect rules единственного прогона.

    Возвращает ``(final_results, metadata, evidence_run_index)``. При одном
    прогоне это по сути передача результатов насквозь: итог правила — его
    ``triggered`` в этом прогоне, evidence — сам прогон.
    """

    runs = _require_runs(rule_results_by_run, "defect rules")
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
                f"{rule_name}: нет результатов, подтверждающих итог"
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
        # Единственный замер по этому правилу: UI показывает под правилом
        # «замер порога», а выбор картинки (select_picture_run) идёт по
        # близости замера к порогу.
        run_results = [results[result_index] for results in runs]
        consensus["run_cards"] = _run_summary_cards(rule_name, run_results)
        # Статус области по каждому прогону: «В НОРМЕ» / «ОТКЛОНЕНИЕ» /
        # «ОБЛАСТЬ НЕ ПОСТРОЕНА» — для fail-closed дефектов (omission,
        # стекло) оператор видит, в каком прогоне область не построена.
        consensus["run_status"] = _run_statuses(rule_name, run_results)
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


def _run_summary_cards(rule_name: str, run_results) -> list:
    """Сводка метрик правила по прогону (для «замера»).

    Возвращает список — ``build_rule_summary`` по каждому прогону (при
    одном прогоне — один элемент). Для ``part_presence`` — сводка по
    входным камерам. Если у прогона нет ``per_role`` (пропуск), карточка
    пустая: в UI покажется «—». Импорт ``core.rule_summary`` выполняется
    здесь (внутри функции), чтобы не создавать цикл:
    ``core.production_cycle`` импортирует ``inspection.consensus``, а
    ``core/__init__`` тянет ``production_cycle``.
    """
    from core.rule_summary import build_presence_summary, build_rule_summary

    cards = []
    for result in run_results:
        details = getattr(result, "details", {}) or {}
        if rule_name == "part_presence":
            cards.append(build_presence_summary(details))
            continue
        per_role = details.get("per_role")
        if isinstance(per_role, dict) and per_role:
            cards.append(build_rule_summary(rule_name, details))
        else:
            cards.append([])
    return cards


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
        return any(
            reason.startswith(marker)
            for marker in REGION_MISSING_MARKERS
        )
    return False


def _run_statuses(rule_name: str, run_results) -> list:
    """Статус правила по каждому прогону (для fail-closed дефектов).

    Возвращает список из одного элемента; каждый — список записей по ролям::

        [{"role": "SPIDER_LEFT", "status": "ОБЛАСТЬ НЕ ПОСТРОЕНА",
          "reason": "no_detections"}, ...]

    Статусы: ``В НОРМЕ``, ``ОТКЛОНЕНИЕ``, ``ОБЛАСТЬ НЕ ПОСТРОЕНА``,
    ``НЕТ ИЗМЕРЕНИЯ``. Для ``part_presence`` — ``КОРПУС`` / ``ПУСТО``.
    """
    statuses = []
    for result in run_results:
        details = getattr(result, "details", {}) or {}
        if rule_name == "part_presence":
            statuses.append([{
                "role": "INPUT",
                "status": "ПУСТО" if details.get("empty_tray") else "КОРПУС",
                "reason": None,
            }])
            continue
        per_role = details.get("per_role")
        if not isinstance(per_role, dict) or not per_role:
            statuses.append([])
            continue
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
                rows.append({
                    "role": role, "status": "ОТКЛОНЕНИЕ", "reason": None,
                })
            elif role_details.get("skipped"):
                rows.append({
                    "role": role, "status": "НЕТ ИЗМЕРЕНИЯ", "reason": None,
                })
            else:
                rows.append({
                    "role": role, "status": "В НОРМЕ", "reason": None,
                })
        statuses.append(rows)
    return statuses


def _metric_table(result) -> dict:
    """Числовые метрики правила по прогонам.

    Возвращает ``{(role, ключ_метрики): entry}``, где entry::

        {"role": ..., "label": ..., "limit": текст порога,
         "values": {run_index: текст значения},
         "ok_runs": [(расстояние, run_index), ...],   # замеры в норме
         "bad_runs": [(расстояние, run_index), ...]}  # замеры за порогом

    Ключ включает **роль камеры**: одна и та же метрика у разных камер
    (например, «избыток» SPIDER_LEFT и SPIDER_RIGHT) не смешивается.
    Ключ метрики — ``key`` (или label, если key нет). Метрики без числовых
    значения/порога (текстовые факты, конфиг) в таблицу не попадают.
    """
    details = getattr(result, "details", {}) or {}
    consensus = details.get("consensus")
    run_cards = (
        consensus.get("run_cards")
        if isinstance(consensus, dict) else None
    )
    if not isinstance(run_cards, list) or len(run_cards) != INSPECTION_RUNS:
        return {}
    table = {}
    for run_index, cards in enumerate(run_cards):
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
                key = (role, metric_key)
                entry = table.setdefault(key, {
                    "role": role,
                    "label": metric.get("label") or metric.get("key") or "—",
                    "limit": metric.get("limit"),
                    "values": {},
                    "ok_runs": [],
                    "bad_runs": [],
                })
                entry["values"][run_index] = metric.get("value")
                (entry["ok_runs"] if bool(ok) else entry["bad_runs"]).append(
                    (distance, run_index),
                )
    return table


def _pick_metric_run(table: dict, triggered: bool):
    """Решающая метрика правила и прогон внутри неё.

    Возвращает ``(metric_entry, run_index)`` или ``None``.

    * для сработавшего правила решающая метрика — вышедшая за порог
      (наименее плохой дефект); если таких нет — по всем метрикам;
    * для нормального правила — метрика с минимальным расстоянием в норме;
    * внутри метрики приоритет у замера **в норме** с минимальным
      расстоянием до порога, иначе — у брака с минимальным расстоянием.
    """
    if not table:
        return None
    if triggered:
        with_bad = [
            (metric, min(dist for dist, _ in metric["bad_runs"]))
            for metric in table.values()
            if metric["bad_runs"]
        ]
        if with_bad:
            metric = min(with_bad, key=lambda item: item[1])[0]
            if metric["ok_runs"]:
                return metric, min(
                    metric["ok_runs"], key=lambda item: (item[0], -item[1]),
                )[1]
            return metric, min(
                metric["bad_runs"], key=lambda item: (item[0], -item[1]),
            )[1]
    with_ok = [
        (metric, min(dist for dist, _ in metric["ok_runs"]))
        for metric in table.values()
        if metric["ok_runs"]
    ]
    if with_ok:
        metric = min(with_ok, key=lambda item: item[1])[0]
        return metric, min(
            metric["ok_runs"], key=lambda item: (item[0], -item[1]),
        )[1]
    all_bad = [
        (metric, min(dist for dist, _ in metric["bad_runs"]))
        for metric in table.values()
        if metric["bad_runs"]
    ]
    if all_bad:
        metric = min(all_bad, key=lambda item: item[1])[0]
        return metric, min(
            metric["bad_runs"], key=lambda item: (item[0], -item[1]),
        )[1]
    return None


def _picture_choice(final_results):
    """Полный выбор прогона для картинки.

    Возвращает ``(run_index, rule_name, metric_entry)`` или ``None``.
    Решающие правила — сработавшие при браке, иначе все. Сравнение
    расстояний выполняется **внутри одной метрики** (метрика выбирается
    сначала), поэтому выбор не «прыгает» между разными метриками.
    """
    decisive = [r for r in final_results if bool(getattr(r, "triggered", False))]
    if not decisive:
        decisive = list(final_results)

    norm_candidates = []  # (расстояние, run_index, rule_name, metric_entry)
    bad_candidates = []
    for result in decisive:
        pick = _pick_metric_run(
            _metric_table(result),
            bool(getattr(result, "triggered", False)),
        )
        if pick is None:
            continue
        metric, run_index = pick
        is_ok = any(run == run_index for _, run in metric["ok_runs"])
        pairs = metric["ok_runs"] if is_ok else metric["bad_runs"]
        distance = next(
            (dist for dist, run in pairs if run == run_index),
            None,
        )
        if distance is None:
            continue
        item = (
            distance,
            run_index,
            getattr(result, "rule_name", ""),
            metric,
        )
        (norm_candidates if is_ok else bad_candidates).append(item)

    if norm_candidates:
        best = min(norm_candidates, key=lambda c: (c[0], -c[1]))
        return best[1], best[2], best[3]
    if bad_candidates:
        best = min(bad_candidates, key=lambda c: (c[0], -c[1]))
        return best[1], best[2], best[3]
    return None


def select_picture_run(final_results) -> int | None:
    """Выбрать прогон (0), по которому строить картинку с разметкой.

    Решающие правила — сработавшие при браке, иначе все. Внутри правила
    сначала выбирается решающая метрика (для сработавшего — вышедшая за
    порог; для нормального — ближайшая к порогу). При одном прогоне это
    всегда индекс 0, если у правила есть числовые пороги.

    Возвращает ``None``, если ни у одного правила нет числовых порогов
    (тогда вызывающий использует единственный прогон).
    """
    choice = _picture_choice(final_results)
    return choice[0] if choice else None


def describe_picture_run(final_results, run_index: int) -> str:
    """Почему для картинки выбран этот прогон.

    Всегда согласован с :func:`select_picture_run`: сообщает ту же
    решающую метрику и её замер. Без числовых порогов — «единственный
    прогон».
    """
    choice = _picture_choice(final_results)
    if choice is None or choice[0] != run_index:
        return "единственный прогон (нет числовых порогов)"
    _, rule_name, metric = choice
    value = metric.get("values", {}).get(run_index)
    limit = metric.get("limit")
    is_ok = any(run == run_index for _, run in metric.get("ok_runs", []))
    verdict = "норма" if is_ok else "брак"
    role_prefix = (
        f"{metric.get('role')} · "
        if metric.get("role") else ""
    )
    return (
        f"{rule_name}: {role_prefix}{metric.get('label') or '—'} "
        f"{value if value is not None else '—'} "
        f"(порог {limit if limit is not None else '—'}) — "
        f"{verdict}, ближе всего к порогу"
    )


def combine_presence_results(presence_results) -> tuple[object, dict, int]:
    """Определить ``empty_tray`` служебного правила part_presence.

    При одном прогоне итог — результат этого единственного прогона.
    """

    results = _require_runs(presence_results, "part_presence")
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
        # Замер по входным камерам: flatness прогона.
        "run_cards": _run_summary_cards("part_presence", results),
        # Статус прогона: «КОРПУС» / «ПУСТО».
        "run_status": _run_statuses("part_presence", results),
    }
    details["consensus"] = consensus
    final_result.details = details
    return final_result, consensus, evidence_index
