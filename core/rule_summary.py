"""Структурированная сводка по правилу для правой панели HMI.

Оператору важно видеть не только причину брака, но и картину целиком:
что именно нашли камеры, какие получились показатели и укладываются ли они
в допуск. Здесь телеметрия правила превращается в набор карточек по ролям:

``{"role": ..., "ok": bool, "verdict": ..., "found": [...], "metrics": [...]}``

Каждая метрика — ``{"label", "value", "limit", "ok"}``: значение и допуск
рядом, поэтому UI одинаково наглядно показывает и норму, и отклонение.
"""

METRICS_PER_ROLE_LIMIT = 5

_UNKNOWN = "—"


def _number(value, digits=1):
    """Аккуратно отформатировать число (целые — без дробной части)."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == int(number) and abs(number) < 1e9:
        return str(int(number))
    return f"{number:.{digits}f}"


def _metric(label, value, limit=None, ok=None, unit=""):
    value_text = _number(value)
    if value_text is None:
        return None
    limit_text = _number(limit)
    return {
        "label": label,
        "value": f"{value_text}{unit}",
        "limit": f"{limit_text}{unit}" if limit_text is not None else None,
        "ok": None if ok is None else bool(ok),
    }


def _within(value, limit):
    """Проверить ``value <= limit`` там, где оба значения числовые."""
    try:
        return float(value) <= float(limit)
    except (TypeError, ValueError):
        return None


def _count_found(role_details: dict) -> list:
    """Что реально обнаружено камерой: объекты и их количество."""
    found = []
    pairs = (
        ("окна", "windows_found", "expected_count"),
        ("объекты", "found", "expected_count"),
        ("объекты (сырые)", "found_raw", "expected_count"),
        ("раковины", "sinks_total", None),
        ("стёкла", "glasses_total", None),
        ("контакты valid", "valid_contacts", None),
    )
    seen = set()
    for label, key, expected_key in pairs:
        if key not in role_details:
            continue
        value = role_details.get(key)
        if value is None:
            continue
        expected = role_details.get(expected_key) if expected_key else None
        text = f"{label}: {_number(value)}"
        if expected is not None:
            text += f"/{_number(expected)}"
        if text not in seen:
            seen.add(text)
            found.append(text)
    ignored = role_details.get("ignored") or role_details.get(
        "ignored_windows"
    )
    if ignored:
        found.append(f"отфильтровано: {_number(ignored)}")
    confirmed = role_details.get("confirmed_sinks")
    if confirmed is not None:
        found.append(f"подтверждено раковин: {_number(confirmed)}")
    return found


def _role_metrics(rule_name: str, role_details: dict) -> list:
    """Показатели правила по одной камере — значение рядом с допуском."""
    metrics = []

    def add(metric):
        if metric is not None:
            metrics.append(metric)

    if rule_name in ("long_omission", "short_omission"):
        thickness = role_details.get("allowed_thickness_px")
        excess = role_details.get("excess_pixels")
        add(_metric(
            "избыток", excess, role_details.get("excess_component_min_px"),
            ok=not role_details.get("triggered"), unit=" px",
        ))
        add(_metric("допустимая толщина", thickness, unit=" px"))
        add(_metric(
            "глубина", role_details.get("max_excess_depth_px"), unit=" px",
        ))
        residual = role_details.get("top_line_actual_max_residual_px")
        residual_max = role_details.get("top_line_max_residual_px")
        add(_metric(
            "отклонение линии", residual, residual_max,
            ok=_within(residual, residual_max), unit=" px",
        ))

    elif rule_name in ("contacts_long", "contacts_short"):
        tolerance = (
            role_details.get("line_tolerance_px")
            or role_details.get("tolerance")
        )
        for label, key in (
            ("Δ верх", "delta_top"),
            ("Δ низ", "delta_bottom"),
            ("Δ высота", "delta_height"),
        ):
            if key in role_details:
                add(_metric(
                    label, role_details.get(key), tolerance,
                    ok=_within(role_details.get(key), tolerance), unit=" px",
                ))
        tilt = role_details.get("omission_tilt_check") or {}
        ratio = tilt.get("distance_trend_ratio")
        if ratio is None:
            ratio = tilt.get("distance_delta_ratio")
        ratio_max = role_details.get("omission_tilt_ratio_max")
        if ratio is not None:
            add(_metric(
                "наклон", ratio, ratio_max, ok=_within(ratio, ratio_max),
            ))
        scale = (role_details.get("inscribe_check") or {}).get(
            "scale_px_per_mm"
        )
        add(_metric("масштаб", scale, unit=" px/mm"))

    elif rule_name == "top_contacts":
        for group in ("L", "R", "T", "B"):
            check = (role_details.get("group_checks") or {}).get(group) or {}
            deviation = check.get("max_deviation_px")
            allowed = check.get("allowed_deviation_px")
            if deviation is None:
                continue
            add(_metric(
                f"группа {group}", deviation, allowed,
                ok=_within(deviation, allowed), unit=" px",
            ))

    elif rule_name == "top_platform":
        placement = {
            "centered": "по центру",
            "shifted": "сдвинут",
            "not_fitted": "не вписался",
        }.get(role_details.get("placement"), role_details.get("placement"))
        if placement:
            metrics.append({
                "label": "положение",
                "value": str(placement),
                "limit": None,
                "ok": role_details.get("placement") == "centered",
            })
        add(_metric("смещение", role_details.get("shift_distance_px"), unit=" px"))
        add(_metric("угол", role_details.get("angle_deg"), unit="°"))

    elif rule_name == "platform_contacts_overlap":
        add(_metric(
            "заплыв", role_details.get("excess_pixels"),
            role_details.get("excess_component_min_px"),
            ok=not role_details.get("triggered"), unit=" px",
        ))
        add(_metric(
            "макс. компонент",
            role_details.get("largest_component_pixels"), unit=" px",
        ))

    elif rule_name == "window_geometry":
        top_limits = role_details.get("top_limits_px") or []
        bottom_limits = role_details.get("bottom_limits_px") or []
        items = role_details.get("items") or []
        bad = [item for item in items if not item.get("valid")
               or item.get("top_fail") or item.get("bottom_fail")]
        if len(top_limits) == 2:
            metrics.append({
                "label": "допуск T",
                "value": f"{_number(top_limits[0])}…{_number(top_limits[1])} px",
                "limit": None,
                "ok": None,
            })
        if len(bottom_limits) == 2:
            metrics.append({
                "label": "допуск B",
                "value": (
                    f"{_number(bottom_limits[0])}…"
                    f"{_number(bottom_limits[1])} px"
                ),
                "limit": None,
                "ok": None,
            })
        if items:
            add(_metric(
                "окон вне допуска", len(bad), 0, ok=not bad,
            ))

    elif rule_name in ("window_sinks", "sinks"):
        hits = role_details.get("hits") or []
        add(_metric(
            "пересечений", len(hits), role_details.get("overlap_min_px"),
            ok=not hits,
        ))
        if hits:
            worst = max(
                (hit.get("overlap_px") or hit.get("overlap_pixels") or 0)
                for hit in hits
            )
            add(_metric(
                "макс. перекрытие", worst,
                role_details.get("overlap_min_px"), ok=False, unit=" px",
            ))

    elif rule_name in ("glass", "glass_on_contacts"):
        hits = role_details.get("hits") or role_details.get("pairs") or []
        add(_metric("совпадений стекла", len(hits), 0, ok=not hits))

    # Универсальные показатели, если специфичных не нашлось.
    if not metrics:
        for label, key, unit in (
            ("найдено", "found", ""),
            ("пересечение", "overlap_px", " px"),
            ("площадь", "mask_area_px2", " px²"),
        ):
            if key in role_details:
                add(_metric(label, role_details.get(key), unit=unit))

    if len(metrics) > METRICS_PER_ROLE_LIMIT:
        metrics = metrics[:METRICS_PER_ROLE_LIMIT]
    return metrics


_REASON_TEXT = {
    "no_scale": "нет масштаба",
    "no_valid_platform": "не найдена платформа",
    "invalid_platform_bbox": "некорректная платформа",
    "invalid_platform_orientation": "не определена ориентация",
    "invalid_contact_masks": "нет масок контактов",
    "insufficient_valid_contact_masks": "мало валидных контактов",
    "insufficient_valid_contacts": "мало валидных контактов",
    "invalid_contact_layout": "нарушена раскладка контактов",
    "layout_groups_failed": "нарушена раскладка контактов",
    "missing_glass_mask": "нет маски стекла",
    "missing_pin_mask": "нет маски пина",
    "empty_case_ring": "пустое кольцо корпуса",
    "case_central_not_inside_case": "смещён центр корпуса",
    "inner_platform_reference_not_fitted": "не построен эталон платформы",
}


def _reason_text(reason) -> str:
    if not reason:
        return ""
    text = str(reason)
    if text in _REASON_TEXT:
        return _REASON_TEXT[text]
    if text.startswith("wrong_count"):
        return "неверное количество объектов"
    if text.startswith("wrong_pin_count"):
        return "неверное количество пинов"
    if text.startswith("invalid_case"):
        return "некорректный корпус"
    return text.replace("_", " ")


def _role_verdict(role_details: dict) -> tuple:
    """Вернуть ``(ok, текст вердикта)`` по одной камере."""
    if role_details.get("skipped"):
        return None, "нет измерения" + (
            f" · {_reason_text(role_details.get('reason'))}"
            if role_details.get("reason") else ""
        )
    if role_details.get("triggered"):
        reason = _reason_text(role_details.get("reason"))
        return False, f"отклонение{f' · {reason}' if reason else ''}"
    reason = _reason_text(role_details.get("reason"))
    if reason:
        return None, f"без измерения · {reason}"
    return True, "в допуске"


def build_rule_summary(rule_name: str, details: dict) -> list:
    """Сводка по правилу: по карточке на каждую камеру."""
    per_role = details.get("per_role")
    if not isinstance(per_role, dict) or not per_role:
        return []

    cards = []
    for role, role_details in per_role.items():
        if not isinstance(role_details, dict):
            continue
        ok, verdict = _role_verdict(role_details)
        cards.append({
            "role": role,
            "ok": ok,
            "verdict": verdict,
            "found": _count_found(role_details),
            "metrics": _role_metrics(rule_name, role_details),
        })
    # Сначала камеры с отклонением: причина решения всегда сверху.
    cards.sort(key=lambda card: (card["ok"] is not False, card["role"]))
    return cards


def build_presence_summary(details: dict) -> list:
    """Сводка правила присутствия детали по обеим входным камерам."""
    limits = details.get("false_positive_max_count_by_role") or {}
    cards = []
    for role, raw_key, effective_key in (
        ("INPUT_LEFT", "flatness_left", "effective_flatness_left"),
        ("INPUT_RIGHT", "flatness_right", "effective_flatness_right"),
    ):
        found = details.get(raw_key)
        if found is None:
            continue
        limit = limits.get(role)
        present = None
        if isinstance(limit, int):
            present = int(found) > limit
        metrics = [
            metric for metric in (
                _metric(
                    "flatness", found, limit,
                    ok=present if present is not None else None,
                ),
                _metric("зачтено", details.get(effective_key)),
            ) if metric is not None
        ]
        cards.append({
            "role": role,
            "ok": present,
            "verdict": (
                "деталь видна" if present
                else ("деталь не видна" if present is False else _UNKNOWN)
            ),
            "found": [f"flatness: {_number(found)}"],
            "metrics": metrics,
        })
    return cards
