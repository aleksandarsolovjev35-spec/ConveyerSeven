"""Структурированная сводка по правилу для правой панели HMI — переписана с учётом требования «на каждый объект».

Каждое правило теперь выводит не только агрегированные пороги, но и измерения
по каждому обнаруженному объекту (окну, контакту, раковине и т.д.). В блоке
«Анализ кадра» это выглядит как карточки порогов правил (один замер на порог):

  Геометрия входного окна
    B после перекладины: макс., px [40]
      [32]
    Окно #1: верх, px — [25]
      [25]
...

Карточка —  ``{"role": ..., "ok": bool, "verdict": ..., "found": [...], "metrics": [...]}``
Метрика — ``{"label", "value", "limit", "ok", "key", "object"}``.

Поле ``object`` у метрики задаёт имя объекта (окно, контакт, раковина,
стекло и т.п.), к которому относится замер. Панель «Анализ кадра»
группирует метрики по этому полю: **один объект — один блок со всеми
замерами, касающимися именно его**. Метрики без ``object`` (агрегаты и
пороги правила целиком) остаются в общем блоке правила.
"""

# top_contacts: 4 группы + 14 контактов × 3 метрики ≈ 46; с запасом.
METRICS_PER_ROLE_LIMIT = 80

_UNKNOWN = "—"


def _number(value, digits=1):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == int(number) and abs(number) < 1e9:
        return str(int(number))
    return f"{number:.{digits}f}"


def _metric(label, value, limit=None, ok=None, unit="", key=None, object=None):
    value_text = _number(value)
    if value_text is None:
        return None
    limit_text = _number(limit)
    value_raw = None
    limit_raw = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value_raw = float(value)
    if isinstance(limit, (int, float)) and not isinstance(limit, bool):
        limit_raw = float(limit)
    # Для составного значения вида "11.5×8.6" число не парсится — value_raw остаётся None, это нормально
    # (δ не считается, но замер показывается).
    if value_raw is None and isinstance(value, str):
        # попытка распарсить первый компонент? пропускаем
        pass
    metric = {
        "label": label,
        "value": f"{value_text}{unit}",
        "limit": f"{limit_text}{unit}" if limit_text is not None else None,
        "ok": None if ok is None else bool(ok),
        "value_raw": value_raw,
        "limit_raw": limit_raw,
        "key": key,
    }
    # Имя объекта (окно #N, контакт #N, раковина #N, стекло #N ...):
    # «Анализ кадра» группирует замеры в блоки по объектам.
    if object:
        metric["object"] = str(object)
    return metric


def _within(value, limit):
    try:
        return float(value) <= float(limit)
    except (TypeError, ValueError):
        return None


def _at_least(value, limit):
    try:
        return float(value) >= float(limit)
    except (TypeError, ValueError):
        return None


def _finite_numbers(values) -> list:
    numbers = []
    for value in values or []:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number:  # not NaN
            numbers.append(number)
    return numbers


def _count_found(role_details: dict) -> list:
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
    ignored = role_details.get("ignored") or role_details.get("ignored_windows")
    if ignored:
        found.append(f"отфильтровано: {_number(ignored)}")
    confirmed = role_details.get("confirmed_sinks")
    if confirmed is not None:
        found.append(f"подтверждено раковин: {_number(confirmed)}")
    return found


def _role_metrics(rule_name: str, role_details: dict) -> list:
    metrics = []

    def add(metric):
        if metric is not None:
            metrics.append(metric)

    # ─── Omission ───────────────────────────────────────────
    if rule_name in ("long_omission", "short_omission"):
        thickness = role_details.get("allowed_thickness_px")
        excess = role_details.get("excess_pixels")
        component_min = role_details.get("excess_component_min_px")
        # «избыток» + порог размера фрагмента — основная пара для UI
        # и выбора картинки (key=excess_component_min_px).
        add(_metric(
            "избыток", excess, component_min,
            ok=(
                not role_details.get("triggered")
                if excess is not None else None
            ),
            unit=" px",
            key="excess_component_min_px",
        ))
        add(_metric(
            "доп. толщина", thickness, unit=" px",
            key="allowed_thickness_px",
        ))
        add(_metric(
            "глубина", role_details.get("max_excess_depth_px"),
            unit=" px", key="max_excess_depth_px",
        ))
        # Макс. остаток — информационный: валидность линии определяется
        # долей точек у линии (см. метрику ниже), а не худшей точкой.
        residual = role_details.get("top_line_actual_max_residual_px")
        residual_max = role_details.get("top_line_max_residual_px")
        add(_metric(
            "отклонение линии", residual, residual_max,
            unit=" px",
            key="top_line_max_residual_px",
        ))
        inlier_ratio = role_details.get("top_line_actual_inlier_ratio")
        inlier_ratio_min = role_details.get("top_line_min_inlier_ratio")
        add(_metric(
            "доля у линии", inlier_ratio, inlier_ratio_min,
            ok=_at_least(inlier_ratio, inlier_ratio_min),
            key="top_line_min_inlier_ratio",
        ))
        add(_metric(
            "крупн. фрагмент",
            role_details.get("largest_component_pixels"),
            unit=" px",
            key="largest_component_px",
        ))
        if role_details.get("found") is not None:
            expected = role_details.get("expected_count")
            add(_metric(
                "Найдено, шт", role_details.get("found"), expected,
                key="found",
            ))

    # ─── Длинные контакты 5 шт ───────────────────────────────
    elif rule_name == "contacts_long":
        expected = role_details.get("expected_count") or 5
        found = role_details.get("found")
        if found is not None:
            add(_metric(
                "Найдено контактов, шт", found, expected,
                ok=int(found) == int(expected),
                key="found",
            ))
        damper_open = role_details.get("damper_open_px")
        damper_open_max = role_details.get("damper_open_max_px")
        gap_dev = role_details.get("gap_dev_px")
        gap_dev_max = role_details.get("gap_dev_max_px")
        if damper_open is None and damper_open_max is not None:
            # Порог виден даже без успешного замера (нет опорной линии).
            add(_metric(
                "Порог заслонки, px", damper_open_max, unit=" px",
                key="damper_open_max_px",
            ))
        else:
            add(_metric(
                "Заслонка: перепад, px", damper_open, damper_open_max,
                ok=_within(damper_open, damper_open_max),
                unit=" px", key="damper_open_max_px",
            ))
        if gap_dev is None and gap_dev_max is not None:
            add(_metric(
                "Порог разброса стен, px", gap_dev_max, unit=" px",
                key="gap_dev_max_px",
            ))
        else:
            add(_metric(
                "Стены: разброс, px", gap_dev, gap_dev_max,
                ok=_within(gap_dev, gap_dev_max),
                unit=" px", key="gap_dev_max_px",
            ))
        # Прямолинейность ряда — информационно (на вердикт не влияет).
        straight = role_details.get("straight_dev_max_px")
        if straight is not None:
            add(_metric(
                "Прямолинейность (инфо), px", straight,
                unit=" px", key="straight_dev_max_px",
            ))
        if role_details.get("rect_width_px") is not None:
            add(_metric(
                "Ширина эталона, px", role_details.get("rect_width_px"),
                unit=" px", key="rect_width_px",
            ))
        if role_details.get("rect_height_px") is not None:
            add(_metric(
                "Высота эталона, px", role_details.get("rect_height_px"),
                unit=" px", key="rect_height_px",
            ))
        # На каждый контакт
        items = role_details.get("items") or []
        for it in items:
            idx = int(it.get("index") or 0)
            if not idx:
                continue
            obj = f"Контакт #{idx}"
            gap_distance = it.get("omission_distance_px")
            gap_deviation = it.get("gap_deviation_px")
            straight_dev = it.get("straight_dev_px")
            rect_fits = it.get("rect_fits")
            if gap_distance is not None:
                add(_metric(
                    f"Контакт #{idx}: расстояние до линии пропуска, px",
                    gap_distance, unit=" px",
                    key=f"contact_{idx}_omission_dist_px", object=obj,
                ))
            if gap_deviation is not None:
                add(_metric(
                    f"Контакт #{idx}: Δ стены, px", gap_deviation,
                    gap_dev_max,
                    ok=_within(abs(float(gap_deviation)), gap_dev_max),
                    unit=" px",
                    key=f"contact_{idx}_gap_dev_px", object=obj,
                ))
            if straight_dev is not None:
                add(_metric(
                    f"Контакт #{idx}: прямолинейность (инфо), px",
                    straight_dev, unit=" px",
                    key=f"contact_{idx}_straight_dev_px", object=obj,
                ))
            if rect_fits is not None:
                add(_metric(
                    f"Контакт #{idx}: прямоугольник",
                    1 if rect_fits else 0, 1, ok=bool(rect_fits),
                    key=f"contact_{idx}_rect_fits", object=obj,
                ))

    # ─── Короткие контакты 2 шт ──────────────────────────────
    elif rule_name == "contacts_short":
        expected = role_details.get("expected_count") or 2
        found = role_details.get("found")
        if found is not None:
            add(_metric(
                "Найдено контактов, шт", found, expected,
                ok=int(found) == int(expected),
                key="found",
            ))
        area_min = role_details.get("area_absolute_min_px2")
        if area_min is not None:
            add(_metric(
                "Мин. площадь, px²", area_min, unit=" px²",
                key="area_absolute_min_px2",
            ))
        damper_open = role_details.get("damper_open_px")
        damper_open_max = role_details.get("damper_open_max_px")
        if damper_open is None and damper_open_max is not None:
            add(_metric(
                "Порог заслонки, px", damper_open_max, unit=" px",
                key="damper_open_max_px",
            ))
        else:
            add(_metric(
                "Заслонка: открытие, px", damper_open, damper_open_max,
                ok=_within(damper_open, damper_open_max),
                unit=" px", key="damper_open_max_px",
            ))
        straight = role_details.get("straight_delta_y_px")
        if straight is not None:
            add(_metric(
                "Δцентров по Y (инфо), px", straight,
                unit=" px", key="straight_delta_y_px",
            ))
        if role_details.get("rect_width_px") is not None:
            add(_metric(
                "Ширина эталона, px", role_details.get("rect_width_px"),
                unit=" px", key="rect_width_px",
            ))
        if role_details.get("rect_height_px") is not None:
            add(_metric(
                "Высота эталона, px", role_details.get("rect_height_px"),
                unit=" px", key="rect_height_px",
            ))
        items = role_details.get("items") or []
        for it in items:
            idx = int(it.get("index") or 0)
            if not idx:
                continue
            obj = f"Контакт #{idx}"
            top_y = it.get("top_y")
            bottom_y = it.get("bottom_y")
            height = it.get("height_px")
            rect_fits = it.get("rect_fits")
            omission = it.get("omission_distance_px")
            if top_y is not None:
                add(_metric(
                    f"Контакт #{idx}: верх, px", top_y,
                    unit=" px", key=f"contact_{idx}_top_y", object=obj,
                ))
            if bottom_y is not None:
                add(_metric(
                    f"Контакт #{idx}: низ, px", bottom_y,
                    unit=" px", key=f"contact_{idx}_bottom_y", object=obj,
                ))
            if height is not None:
                add(_metric(
                    f"Контакт #{idx}: высота, px", height,
                    unit=" px", key=f"contact_{idx}_height_px", object=obj,
                ))
            if rect_fits is not None:
                add(_metric(
                    f"Контакт #{idx}: прямоугольник",
                    1 if rect_fits else 0, 1, ok=bool(rect_fits),
                    key=f"contact_{idx}_rect_fits", object=obj,
                ))
            if omission is not None:
                add(_metric(
                    f"Контакт #{idx}: расстояние до линии пропуска, px",
                    omission, unit=" px",
                    key=f"contact_{idx}_omission_dist_px", object=obj,
                ))

    # ─── Контакты сверху 14 шт ───────────────────────────────
    elif rule_name == "top_contacts":
        found = role_details.get("found")
        found_raw = role_details.get("found_raw")
        if found is not None:
            add(_metric(
                "Валидных контактов, шт", found, 14,
                ok=int(found) == 14, key="found",
            ))
        elif found_raw is not None:
            add(_metric(
                "Найдено контактов, шт", found_raw, 14,
                ok=int(found_raw) == 14, key="found_raw",
            ))
        # Групповые пороги
        for group in ("L", "R", "T", "B"):
            check = (role_details.get("group_checks") or {}).get(group) or {}
            deviation = check.get("max_deviation_px")
            allowed = check.get("allowed_deviation_px")
            median = check.get("median_distance_px")
            if median is not None:
                add(_metric(
                    f"Группа {group}: медиана дист., px", median,
                    unit=" px", key=f"group_{group}_median_px",
                ))
            if deviation is None:
                continue
            add(_metric(
                f"Группа {group}: откл., px", deviation, allowed,
                ok=_within(deviation, allowed), unit=" px",
                key=f"group_{group}_deviation_px",
            ))
        # На каждый контакт
        items = role_details.get("items") or []
        for it in items:
            idx = int(it.get("index") or 0)
            group = it.get("group") or ""
            distance = it.get("distance_px")
            deviation = it.get("deviation_px")
            allowed = it.get("allowed_deviation_px")
            rect_fits = it.get("rect_fits")
            if idx:
                lab = f"Контакт #{idx} {group}".strip() + ":"
                obj = f"Контакт #{idx} ({group})" if group else f"Контакт #{idx}"
            else:
                lab = f"Контакт {group}:"
                obj = f"Контакт ({group})" if group else None
            if distance is not None:
                add(_metric(
                    f"{lab} дист. до края, px", distance,
                    unit=" px", key=f"contact_{idx}_distance_px", object=obj,
                ))
            if deviation is not None:
                add(_metric(
                    f"{lab} откл., px", deviation, allowed,
                    ok=_within(deviation, allowed), unit=" px",
                    key=f"contact_{idx}_deviation_px", object=obj,
                ))
            if rect_fits is not None:
                add(_metric(
                    f"{lab} прямоугольник",
                    1 if rect_fits else 0, 1, ok=bool(rect_fits),
                    key=f"contact_{idx}_rect_fits", object=obj,
                ))

    # ─── Платформа ───────────────────────────────────────────
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
                "value_raw": None,
                "limit_raw": None,
                "key": "placement",
            })
        add(_metric(
            "Смещение, px", role_details.get("shift_distance_px"),
            unit=" px", key="shift_distance_px",
        ))
        add(_metric(
            "Угол, °", role_details.get("angle_deg"),
            unit="°", key="angle_deg",
        ))
        add(_metric(
            "Ширина эталона, px", role_details.get("rect_width_px"),
            unit=" px", key="rect_width_px",
        ))
        add(_metric(
            "Высота эталона, px", role_details.get("rect_height_px"),
            unit=" px", key="rect_height_px",
        ))

    # ─── Заплыв платформы ────────────────────────────────────
    elif rule_name == "platform_contacts_overlap":
        add(_metric(
            "Заплыв, px", role_details.get("excess_pixels"),
            role_details.get("excess_component_min_px"),
            ok=not role_details.get("triggered"), unit=" px",
            key="excess_component_min_px",
        ))
        add(_metric(
            "Макс. компонент, px",
            role_details.get("largest_component_pixels"),
            unit=" px", key="largest_component_px",
        ))
        add(_metric(
            "Контакты области, шт", role_details.get("used_contacts"),
            unit="", key="used_contacts",
        ))
        add(_metric(
            "Ширина области, px", role_details.get("boundary_width_px"),
            unit=" px", key="boundary_width_px",
        ))
        add(_metric(
            "Высота области, px", role_details.get("boundary_height_px"),
            unit=" px", key="boundary_height_px",
        ))

    # ─── Геометрия входного окна — 7 окон ────────────────────
    elif rule_name == "window_geometry":
        top_limits = role_details.get("top_limits_px") or []
        bottom_limits = role_details.get("bottom_limits_px") or []
        top_values = list(role_details.get("top_values_px") or [])
        bottom_values = list(role_details.get("bottom_values_px") or [])
        items = list(role_details.get("items") or [])

        # Если массивы T/B не пришли, собираем из items — иначе окна
        # пропадают из анализа кадра.
        if not top_values and items:
            top_values = [
                it.get("top_px") for it in sorted(
                    items, key=lambda row: int(row.get("index") or 0),
                )
                if it.get("top_px") is not None
            ]
        if not bottom_values and items:
            bottom_values = [
                it.get("bottom_px") for it in sorted(
                    items, key=lambda row: int(row.get("index") or 0),
                )
                if it.get("bottom_px") is not None
            ]

        found = role_details.get("found")
        expected = role_details.get("expected_count") or 7
        if found is not None:
            add(_metric(
                "Найдено окон, шт", found, expected,
                ok=int(found) == int(expected), key="found",
            ))

        top_nums = _finite_numbers(top_values)
        bottom_nums = _finite_numbers(bottom_values)
        if len(top_limits) == 2 and top_nums:
            min_top = min(top_nums)
            max_top = max(top_nums)
            add(_metric(
                "T до перекладины: мин., px", min_top, top_limits[0],
                ok=min_top >= float(top_limits[0]), unit=" px",
                key="top_px_min",
            ))
            add(_metric(
                "T до перекладины: макс., px", max_top, top_limits[1],
                ok=max_top <= float(top_limits[1]), unit=" px",
                key="top_px_max",
            ))
        if len(bottom_limits) == 2 and bottom_nums:
            min_bottom = min(bottom_nums)
            max_bottom = max(bottom_nums)
            add(_metric(
                "B после перекладины: мин., px", min_bottom, bottom_limits[0],
                ok=min_bottom >= float(bottom_limits[0]), unit=" px",
                key="bottom_px_min",
            ))
            add(_metric(
                "B после перекладины: макс., px", max_bottom, bottom_limits[1],
                ok=max_bottom <= float(bottom_limits[1]), unit=" px",
                key="bottom_px_max",
            ))

        # На каждое окно: T, B и статус допуска (с порогами диапазона).
        by_index = {
            int(it.get("index") or 0): it
            for it in items
            if int(it.get("index") or 0) > 0
        }
        max_windows = max(len(top_values), len(bottom_values), len(by_index), 0)
        for idx in range(1, max_windows + 1):
            if idx > 14:
                break
            it = by_index.get(idx)
            t = None
            b = None
            if idx - 1 < len(top_values):
                try:
                    t = float(top_values[idx - 1])
                except (TypeError, ValueError):
                    t = None
            if idx - 1 < len(bottom_values):
                try:
                    b = float(bottom_values[idx - 1])
                except (TypeError, ValueError):
                    b = None
            if t is None and it is not None and it.get("top_px") is not None:
                try:
                    t = float(it.get("top_px"))
                except (TypeError, ValueError):
                    t = None
            if b is None and it is not None and it.get("bottom_px") is not None:
                try:
                    b = float(it.get("bottom_px"))
                except (TypeError, ValueError):
                    b = None

            obj = f"Окно #{idx}"
            if t is not None:
                nearest_top = None
                top_ok = None
                if len(top_limits) == 2:
                    try:
                        low, high = float(top_limits[0]), float(top_limits[1])
                        top_ok = low <= float(t) <= high
                        if float(t) < low:
                            nearest_top = low
                        elif float(t) > high:
                            nearest_top = high
                        else:
                            nearest_top = (
                                low if abs(float(t) - low) <= abs(high - float(t))
                                else high
                            )
                    except (TypeError, ValueError):
                        nearest_top = None
                metric = _metric(
                    f"Окно #{idx}: верх, px", t, nearest_top,
                    ok=top_ok, unit=" px",
                    key=f"window_{idx}_top_px", object=obj,
                )
                if metric is not None and len(top_limits) == 2:
                    # Понятный оператору диапазон вместо одной границы.
                    metric["limit"] = (
                        f"{_number(top_limits[0])}-{_number(top_limits[1])} px"
                    )
                add(metric)

            if b is not None:
                nearest_bot = None
                bottom_ok = None
                if len(bottom_limits) == 2:
                    try:
                        low, high = float(bottom_limits[0]), float(bottom_limits[1])
                        bottom_ok = low <= float(b) <= high
                        if float(b) < low:
                            nearest_bot = low
                        elif float(b) > high:
                            nearest_bot = high
                        else:
                            nearest_bot = (
                                low if abs(float(b) - low) <= abs(high - float(b))
                                else high
                            )
                    except (TypeError, ValueError):
                        nearest_bot = None
                metric = _metric(
                    f"Окно #{idx}: низ, px", b, nearest_bot,
                    ok=bottom_ok, unit=" px",
                    key=f"window_{idx}_bottom_px", object=obj,
                )
                if metric is not None and len(bottom_limits) == 2:
                    metric["limit"] = (
                        f"{_number(bottom_limits[0])}-"
                        f"{_number(bottom_limits[1])} px"
                    )
                add(metric)

            if it is not None:
                top_fail = it.get("top_fail")
                bottom_fail = it.get("bottom_fail")
                valid = it.get("valid")
                if top_fail is not None or bottom_fail is not None or valid is False:
                    ok = bool(valid is not False) and not (
                        top_fail or bottom_fail
                    )
                    add(_metric(
                        f"Окно #{idx}: в допуске",
                        1 if ok else 0, 1, ok=ok,
                        key=f"window_{idx}_ok", object=obj,
                    ))

        if items:
            bad = [
                it for it in items
                if not it.get("valid")
                or it.get("top_fail")
                or it.get("bottom_fail")
            ]
            add(_metric(
                "Окон вне допуска, шт", len(bad), 0,
                ok=not bad, key="windows_out_of_tolerance",
            ))

    # ─── Раковины в окнах ────────────────────────────────────
    elif rule_name == "window_sinks":
        hits = role_details.get("hits") or []
        limit = role_details.get("overlap_min_px")
        add(_metric(
            "Пересечений, шт", len(hits), 0,
            ok=not hits, key="overlap_count",
        ))
        # На каждый объект — пара раковина/окно
        for h in hits:
            sink_idx = h.get("sink_index")
            win_idx = h.get("window_index")
            overlap = h.get("overlap_px")
            if overlap is not None:
                # Брак: overlap >= порога (перекрытие слишком большое)
                ok = None
                if limit is not None:
                    try:
                        ok = float(overlap) < float(limit)
                    except (TypeError, ValueError):
                        ok = None
                add(_metric(
                    f"Раковина #{sink_idx} → окно #{win_idx}: перехл., px",
                    overlap, limit, ok=ok, unit=" px",
                    key=f"sink_{sink_idx}_win_{win_idx}_overlap_px",
                    object=f"Раковина #{sink_idx} → окно #{win_idx}",
                ))
        if hits:
            worst = max((hit.get("overlap_px") or 0) for hit in hits)
            ok = None
            if limit is not None:
                try:
                    ok = float(worst) < float(limit)
                except (TypeError, ValueError):
                    ok = False
            add(_metric(
                "Макс. перекрытие, px", worst, limit,
                ok=ok if ok is not None else False, unit=" px",
                key="max_overlap_px",
            ))

    # ─── Раковины корпуса ────────────────────────────────────
    elif rule_name == "sinks":
        hits = role_details.get("hits") or []
        add(_metric(
            "Пересечений, шт", len(hits), 0,
            ok=not hits, key="sinks_hits",
        ))
        for h in hits:
            idx = h.get("sink_index")
            obj = f"Раковина #{idx}"
            forbidden = h.get("forbidden_pixels")
            central = h.get("central_overlap_px")
            platform = h.get("platform_overlap_px")
            contacts = h.get("contacts_overlap_px")
            if forbidden is not None:
                add(_metric(
                    f"Shell #{idx}: запрещ., px", forbidden,
                    unit=" px", key=f"shell_{idx}_forbidden_px", object=obj,
                ))
            if central is not None:
                add(_metric(
                    f"Shell #{idx}: центр. перехл., px", central,
                    unit=" px", key=f"shell_{idx}_central_px", object=obj,
                ))
            if platform is not None:
                add(_metric(
                    f"Shell #{idx}: платформа, px", platform,
                    unit=" px", key=f"shell_{idx}_platform_px", object=obj,
                ))
            if contacts is not None:
                add(_metric(
                    f"Shell #{idx}: контакты, px", contacts,
                    unit=" px", key=f"shell_{idx}_contacts_px", object=obj,
                ))

    # ─── Стекло ──────────────────────────────────────────────
    elif rule_name == "glass":
        hits = role_details.get("hits") or []
        add(_metric(
            "Совпадений стекла, шт", len(hits), 0,
            ok=not hits, key="glass_hits",
        ))
        for h in hits:
            idx = h.get("glass_index")
            obj = f"Стекло #{idx}"
            plat = h.get("platform_overlap_px")
            pin = h.get("pin_overlap_px")
            ring = h.get("ring_overlap_px")
            union = h.get("cleanup_overlap_px")
            if plat is not None:
                add(_metric(
                    f"Стекло #{idx}: платформа, px", plat,
                    unit=" px", key=f"glass_{idx}_platform_px", object=obj,
                ))
            if pin is not None:
                add(_metric(
                    f"Стекло #{idx}: пины, px", pin,
                    unit=" px", key=f"glass_{idx}_pin_px", object=obj,
                ))
            if ring is not None:
                add(_metric(
                    f"Стекло #{idx}: кольцо, px", ring,
                    unit=" px", key=f"glass_{idx}_ring_px", object=obj,
                ))
            if union is not None:
                add(_metric(
                    f"Стекло #{idx}: union, px", union,
                    unit=" px", key=f"glass_{idx}_union_px", object=obj,
                ))

    elif rule_name == "glass_on_contacts":
        pairs = role_details.get("pairs") or []
        hits = role_details.get("hits")
        if isinstance(hits, int):
            add(_metric("Стекла, шт", hits, unit="", key="glass_count"))
        pins = role_details.get("pins_found")
        if pins is not None:
            add(_metric(
                "Пинов, шт", pins, 14,
                ok=int(pins) == 14, key="pins_found",
            ))
        add(_metric(
            "Пар стекло/контакт, шт", len(pairs), 0,
            ok=not pairs, key="glass_contact_pairs",
        ))
        for p in pairs:
            g_idx = p.get("glass_index")
            c_idx = p.get("contact_index")
            overlap = p.get("overlap_pixels")
            if overlap is not None:
                add(_metric(
                    f"Стекло #{g_idx} → контакт #{c_idx}: перехл., px",
                    overlap, unit=" px",
                    key=f"glass_{g_idx}_contact_{c_idx}_overlap_px",
                    object=f"Стекло #{g_idx} → контакт #{c_idx}",
                ))

    # Универсальный fallback: хотя бы счётчик/площадь, если метрик нет.
    if not metrics:
        for label, key, unit in (
            ("Найдено, шт", "found", ""),
            ("Найдено (сырые), шт", "found_raw", ""),
            ("Пересечение, px", "overlap_px", " px"),
            ("Площадь, px²", "mask_area_px2", " px²"),
        ):
            if key in role_details and role_details.get(key) is not None:
                add(_metric(label, role_details.get(key), unit=unit, key=key))

    if len(metrics) > METRICS_PER_ROLE_LIMIT:
        metrics = metrics[:METRICS_PER_ROLE_LIMIT]
    return metrics


_REASON_TEXT = {
    "no_valid_platform": "не найдена платформа",
    "invalid_platform_bbox": "некорректная платформа",
    "invalid_platform_orientation": "не определена ориентация",
    "invalid_contact_masks": "нет масок контактов",
    "insufficient_valid_contact_masks": "мало валидных контактов",
    "insufficient_valid_contacts": "мало валидных контактов",
    "invalid_contact_layout": "нарушена раскладка контактов",
    "layout_groups_failed": "нарушена раскладка контактов",
    "missing_glass_mask": "нет маски стекла",
    "missing_pin_mask": "нет маски штифта",
    "empty_case_ring": "пустое кольцо корпуса",
    "case_central_not_inside_case": "смещён центр корпуса",
    "inner_platform_reference_not_fitted": "не построен эталон платформы",
    "contact_boundary_not_built": "область по контактам не построена",
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
    if role_details.get("skipped"):
        return None, "нет измерения" + (f" · {_reason_text(role_details.get('reason'))}" if role_details.get("reason") else "")
    if role_details.get("triggered"):
        reason = _reason_text(role_details.get("reason"))
        return False, f"отклонение{f' · {reason}' if reason else ''}"
    reason = _reason_text(role_details.get("reason"))
    if reason:
        return None, f"без измерения · {reason}"
    return True, "в допуске"


def build_rule_summary(rule_name: str, details: dict) -> list:
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
    cards.sort(key=lambda card: (card["ok"] is not False, card["role"]))
    return cards


def build_presence_summary(details: dict) -> list:
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
                _metric("flatness", found, limit, ok=present if present is not None else None, key="false_positive_max_count"),
                _metric("Зачтено, шт", details.get(effective_key), key="effective_flatness"),
            ) if metric is not None
        ]
        cards.append({
            "role": role,
            "ok": present,
            "verdict": ("корпус виден" if present else ("корпус не виден" if present is False else _UNKNOWN)),
            "found": [f"flatness: {_number(found)}"],
            "metrics": metrics,
        })
    return cards
