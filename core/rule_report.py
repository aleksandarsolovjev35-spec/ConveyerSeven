"""Формирование строк отчёта по правилам дефектов для HMI и диагностики.

Каждое правило со своей развёрнутой телеметрией имеет отдельный форматтер
``_detail_<rule>``: он возвращает список строк, которые UI показывает под
правилом. Правила без собственного форматтера получают общее описание
сработавших ролей через :func:`_generic_failure_rows`.
"""

# Названия порогов для анализа кадра: (правило, ключ метрики) -> понятный
# оператору label (как в панели «Пороги правил»). UI показывает порог
# рядом с названием правила и замер под ним.
# Новый блок анализа кадра полностью повторяет блок порогов правил:
#   Геометрия входного окна
#     B после перекладины: макс., px [Значение]
#       [Значение] [Значение] [Значение]
METRIC_PARAM_LABELS = {
    # Здесь ключ метрики не всегда совпадает с ключом файла порогов:
    # например, max_excess_depth_px — вычисленный замер, а не порог. Подписи
    # согласованы с панелью «Пороги правил», но описывают именно то значение,
    # которое оператор видит в карточке анализа.
    ("long_omission", "excess_component_min_px"):
        "Мин. число пикселей в компоненте избытка, px",
    ("long_omission", "top_line_max_residual_px"):
        "Макс. остаточное отклонение верхней линии, px",
    ("short_omission", "excess_component_min_px"):
        "Мин. число пикселей в компоненте избытка, px",
    ("short_omission", "top_line_max_residual_px"):
        "Макс. остаточное отклонение верхней линии, px",
    ("contacts_long", "damper_open_max_px"):
        "Макс. перепад заслонки по ряду, px",
    ("contacts_short", "damper_open_max_px"):
        "Макс. открытие заслонки, px",
    ("contacts_long", "gap_dev_max_px"):
        "Макс. разброс расстояний до пропуска, px",
    # Дополнительные метрики контактов и omission для run_cards
    ("long_omission", "max_excess_depth_px"):
        "Макс. глубина избытка, px",
    ("long_omission", "largest_component_px"):
        "Крупнейший фрагмент, px",
    ("short_omission", "max_excess_depth_px"):
        "Макс. глубина избытка, px",
    ("short_omission", "largest_component_px"):
        "Крупнейший фрагмент, px",
    ("contacts_long", "rect_width_px"):
        "Ширина эталона контакта, px",
    ("contacts_long", "rect_height_px"):
        "Высота эталона контакта, px",
    ("contacts_short", "rect_width_px"):
        "Ширина эталона контакта, px",
    ("contacts_short", "rect_height_px"):
        "Высота эталона контакта, px",
    ("top_contacts", "edge_distance_deviation_ratio"):
        "Допуск разброса отступа до края, доля размера контакта",
    # top_contacts групповой
    ("top_contacts", "found"):
        "Валидных контактов, шт",
    ("top_contacts", "found_raw"):
        "Найдено контактов (сырые), шт",
    ("top_contacts", "group_L_median_px"):
        "Группа L: медиана дистанции, px",
    ("top_contacts", "group_R_median_px"):
        "Группа R: медиана дистанции, px",
    ("top_contacts", "group_T_median_px"):
        "Группа T: медиана дистанции, px",
    ("top_contacts", "group_B_median_px"):
        "Группа B: медиана дистанции, px",
    ("top_contacts", "group_L_deviation_px"):
        "Группа L: макс. отклонение, px",
    ("top_contacts", "group_R_deviation_px"):
        "Группа R: макс. отклонение, px",
    ("top_contacts", "group_T_deviation_px"):
        "Группа T: макс. отклонение, px",
    ("top_contacts", "group_B_deviation_px"):
        "Группа B: макс. отклонение, px",
    # top_contacts по контактам
    ("top_contacts", "contact_1_distance_px"):
        "Контакт #1: дистанция до края, px",
    ("top_contacts", "contact_2_distance_px"):
        "Контакт #2: дистанция до края, px",
    ("top_contacts", "contact_3_distance_px"):
        "Контакт #3: дистанция до края, px",
    ("top_contacts", "contact_4_distance_px"):
        "Контакт #4: дистанция до края, px",
    ("top_contacts", "contact_5_distance_px"):
        "Контакт #5: дистанция до края, px",
    ("top_contacts", "contact_6_distance_px"):
        "Контакт #6: дистанция до края, px",
    ("top_contacts", "contact_7_distance_px"):
        "Контакт #7: дистанция до края, px",
    ("top_contacts", "contact_8_distance_px"):
        "Контакт #8: дистанция до края, px",
    ("top_contacts", "contact_9_distance_px"):
        "Контакт #9: дистанция до края, px",
    ("top_contacts", "contact_10_distance_px"):
        "Контакт #10: дистанция до края, px",
    ("top_contacts", "contact_11_distance_px"):
        "Контакт #11: дистанция до края, px",
    ("top_contacts", "contact_12_distance_px"):
        "Контакт #12: дистанция до края, px",
    ("top_contacts", "contact_13_distance_px"):
        "Контакт #13: дистанция до края, px",
    ("top_contacts", "contact_14_distance_px"):
        "Контакт #14: дистанция до края, px",
    ("top_contacts", "contact_1_deviation_px"):
        "Контакт #1: отклонение, px",
    ("top_contacts", "contact_2_deviation_px"):
        "Контакт #2: отклонение, px",
    ("top_contacts", "contact_3_deviation_px"):
        "Контакт #3: отклонение, px",
    ("top_contacts", "contact_4_deviation_px"):
        "Контакт #4: отклонение, px",
    ("top_contacts", "contact_5_deviation_px"):
        "Контакт #5: отклонение, px",
    ("top_contacts", "contact_6_deviation_px"):
        "Контакт #6: отклонение, px",
    ("top_contacts", "contact_7_deviation_px"):
        "Контакт #7: отклонение, px",
    ("top_contacts", "contact_8_deviation_px"):
        "Контакт #8: отклонение, px",
    ("top_contacts", "contact_9_deviation_px"):
        "Контакт #9: отклонение, px",
    ("top_contacts", "contact_10_deviation_px"):
        "Контакт #10: отклонение, px",
    ("top_contacts", "contact_11_deviation_px"):
        "Контакт #11: отклонение, px",
    ("top_contacts", "contact_12_deviation_px"):
        "Контакт #12: отклонение, px",
    ("top_contacts", "contact_13_deviation_px"):
        "Контакт #13: отклонение, px",
    ("top_contacts", "contact_14_deviation_px"):
        "Контакт #14: отклонение, px",
    ("top_contacts", "contact_1_rect_fits"):
        "Контакт #1: прямоугольник",
    ("top_contacts", "contact_2_rect_fits"):
        "Контакт #2: прямоугольник",
    ("top_contacts", "contact_3_rect_fits"):
        "Контакт #3: прямоугольник",
    ("top_contacts", "contact_4_rect_fits"):
        "Контакт #4: прямоугольник",
    ("top_contacts", "contact_5_rect_fits"):
        "Контакт #5: прямоугольник",
    ("top_contacts", "contact_6_rect_fits"):
        "Контакт #6: прямоугольник",
    ("top_contacts", "contact_7_rect_fits"):
        "Контакт #7: прямоугольник",
    ("top_contacts", "contact_8_rect_fits"):
        "Контакт #8: прямоугольник",
    ("top_contacts", "contact_9_rect_fits"):
        "Контакт #9: прямоугольник",
    ("top_contacts", "contact_10_rect_fits"):
        "Контакт #10: прямоугольник",
    ("top_contacts", "contact_11_rect_fits"):
        "Контакт #11: прямоугольник",
    ("top_contacts", "contact_12_rect_fits"):
        "Контакт #12: прямоугольник",
    ("top_contacts", "contact_13_rect_fits"):
        "Контакт #13: прямоугольник",
    ("top_contacts", "contact_14_rect_fits"):
        "Контакт #14: прямоугольник",
    # top_platform
    ("top_platform", "placement"):
        "Положение эталона",
    ("top_platform", "shift_distance_px"):
        "Смещение центра, px",
    ("top_platform", "angle_deg"):
        "Угол платформы, °",
    ("top_platform", "rect_width_px"):
        "Ширина эталона платформы, px",
    ("top_platform", "rect_height_px"):
        "Высота эталона платформы, px",
    # platform_contacts_overlap
    ("platform_contacts_overlap", "excess_component_min_px"):
        "Мин. число пикселей в компоненте заплыва, px",
    ("platform_contacts_overlap", "largest_component_px"):
        "Крупнейший компонент заплыва, px",
    ("platform_contacts_overlap", "used_contacts"):
        "Контактов в области, шт",
    ("platform_contacts_overlap", "boundary_width_px"):
        "Ширина границы области, px",
    ("platform_contacts_overlap", "boundary_height_px"):
        "Высота границы области, px",
    # sinks (TOP)
    ("sinks", "sinks_hits"):
        "Пересечений раковин, шт",
    ("sinks", "shell_1_forbidden_px"):
        "Раковина #1: запрещ. пиксели, px",
    ("sinks", "shell_2_forbidden_px"):
        "Раковина #2: запрещ. пиксели, px",
    ("sinks", "shell_1_central_px"):
        "Раковина #1: центр. перехл., px",
    ("sinks", "shell_2_central_px"):
        "Раковина #2: центр. перехл., px",
    ("sinks", "shell_1_platform_px"):
        "Раковина #1: платформа, px",
    ("sinks", "shell_2_platform_px"):
        "Раковина #2: платформа, px",
    ("sinks", "shell_1_contacts_px"):
        "Раковина #1: контакты, px",
    ("sinks", "shell_2_contacts_px"):
        "Раковина #2: контакты, px",
    # glass
    ("glass", "glass_hits"):
        "Совпадений стекла, шт",
    ("glass", "glass_1_platform_px"):
        "Стекло #1: платформа, px",
    ("glass", "glass_2_platform_px"):
        "Стекло #2: платформа, px",
    ("glass", "glass_1_pin_px"):
        "Стекло #1: пины, px",
    ("glass", "glass_2_pin_px"):
        "Стекло #2: пины, px",
    ("glass", "glass_1_ring_px"):
        "Стекло #1: кольцо, px",
    ("glass", "glass_2_ring_px"):
        "Стекло #2: кольцо, px",
    ("glass", "glass_1_union_px"):
        "Стекло #1: union, px",
    ("glass", "glass_2_union_px"):
        "Стекло #2: union, px",
    # glass_on_contacts
    ("glass_on_contacts", "glass_count"):
        "Стекол, шт",
    ("glass_on_contacts", "pins_found"):
        "Пинов, шт",
    ("glass_on_contacts", "glass_contact_pairs"):
        "Пар стекло/контакт, шт",
    ("glass_on_contacts", "glass_1_contact_1_overlap_px"):
        "Стекло #1 → контакт #1: перехл., px",
    ("glass_on_contacts", "glass_1_contact_2_overlap_px"):
        "Стекло #1 → контакт #2: перехл., px",
    ("glass_on_contacts", "glass_2_contact_1_overlap_px"):
        "Стекло #2 → контакт #1: перехл., px",
    ("glass_on_contacts", "glass_2_contact_2_overlap_px"):
        "Стекло #2 → контакт #2: перехл., px",
    ("window_geometry", "top_px_min"): "T до перекладины: мин., px",
    ("window_geometry", "top_px_max"): "T до перекладины: макс., px",
    ("window_geometry", "bottom_px_min"): "B после перекладины: мин., px",
    ("window_geometry", "bottom_px_max"): "B после перекладины: макс., px",
}

# Правила, у которых есть развёрнутая построчная телеметрия в правой панели.
DETAILED_RULES = (
    "window_geometry",
    "contacts_long",
    "contacts_short",
    "top_contacts",
    "top_platform",
    "platform_contacts_overlap",
    "long_omission",
    "short_omission",
)

NO_MEASUREMENT = "нет измерения"


def _detail_window_geometry(per_role: dict) -> list:
    detail_lines = []
    for role, role_details in per_role.items():
        if not isinstance(role_details, dict):
            continue
        reason = role_details.get("reason")
        if reason:
            detail_lines.append(
                f"{role}: найдено {int(role_details.get('found') or 0)}/"
                f"{int(role_details.get('expected_count') or 0)}"
            )
            continue
        top_limits = role_details.get("top_limits_px") or [0, 0]
        bottom_limits = role_details.get("bottom_limits_px") or [0, 0]
        detail_lines.append(
            f"{role}: T {float(top_limits[0]):g}-"
            f"{float(top_limits[1]):g} px; B "
            f"{float(bottom_limits[0]):g}-"
            f"{float(bottom_limits[1]):g} px"
        )
        ignored = int(role_details.get("ignored") or 0)
        if ignored:
            detail_lines.append(
                f"{role}: лишних detections показано серым: {ignored}"
            )
        for item in role_details.get("items") or []:
            index = int(item.get("index") or 0)
            if not item.get("valid"):
                detail_lines.append(
                    f"{role} #{index}: нет измерения T/B"
                )
                continue
            suffix = []
            if item.get("top_fail"):
                suffix.append("T вне допуска")
            if item.get("bottom_fail"):
                suffix.append("B вне допуска")
            text = (
                f"{role} #{index}: "
                f"T={float(item.get('top_px') or 0):.1f} px; "
                f"B={float(item.get('bottom_px') or 0):.1f} px"
            )
            if suffix:
                text += "; " + ", ".join(suffix)
            detail_lines.append(text)
    return detail_lines


def _reference_missing_lines(role, role_details, detail_lines):
    reason = role_details.get("reason")
    if reason in ("no_valid_omission_top_line", "omission_reference_too_short"):
        detail_lines.append(
            f"{role}: нет опорной линии пропуска ({reason})"
        )
        return True
    return False


def _detail_contacts_long(per_role: dict) -> list:
    detail_lines = []
    for role, role_details in per_role.items():
        if not isinstance(role_details, dict):
            continue
        reason = role_details.get("reason")
        if reason and str(reason).startswith("wrong_count"):
            detail_lines.append(
                f"{role}: найдено {int(role_details.get('found') or 0)}/5"
            )
            continue
        if reason == "invalid_contact_masks":
            indices = ", ".join(
                f"#{index}"
                for index in role_details.get("invalid_mask_indices", [])
            )
            detail_lines.append(
                f"{role}: нет segmentation mask контакта: {indices}"
            )
            continue
        detail_lines.append(
            f"{role}: rectangle {float(role_details.get('rect_width_px') or 0):g}x"
            f"{float(role_details.get('rect_height_px') or 0):g} px"
        )
        ignored = int(role_details.get("ignored") or 0)
        if ignored:
            detail_lines.append(
                f"{role}: лишних contacts показано серым: {ignored}"
            )
        if _reference_missing_lines(role, role_details, detail_lines):
            continue
        gap_max = float(role_details.get("gap_dev_max_px") or 0)
        detail_lines.append(
            f"{role}: заслонка перепад "
            f"{float(role_details.get('damper_open_px') or 0):.1f}/"
            f"{float(role_details.get('damper_open_max_px') or 0):.1f} px; "
            f"стены разброс "
            f"{float(role_details.get('gap_dev_px') or 0):.1f}/{gap_max:.1f} px"
        )
        straight = role_details.get("straight_dev_max_px")
        if straight is not None:
            detail_lines.append(
                f"{role}: прямолинейность (инфо) "
                f"{float(straight):.1f} px"
            )
        for item in role_details.get("items") or []:
            index = int(item.get("index") or 0)
            distance = item.get("omission_distance_px")
            distance_text = (
                f"{float(distance):.1f} px"
                if distance is not None else "—"
            )
            deviation = item.get("gap_deviation_px")
            deviation_text = (
                f"{float(deviation):+.1f}/{gap_max:.1f} px"
                if deviation is not None else "—"
            )
            text = (
                f"{role} #{index}: "
                f"rect {'OK' if item.get('rect_fits') else 'FAIL'}; "
                f"стена {distance_text}; "
                f"Δ стены {deviation_text}"
            )
            detail_lines.append(text)
    return detail_lines


def _detail_contacts_short(per_role: dict) -> list:
    detail_lines = []
    for role, role_details in per_role.items():
        if not isinstance(role_details, dict):
            continue
        reason = role_details.get("reason")
        if reason and str(reason).startswith("wrong_count"):
            detail_lines.append(
                f"{role}: найдено {int(role_details.get('found') or 0)}/2; "
                f"area min "
                f"{float(role_details.get('area_absolute_min_px2') or 0):g} px²"
            )
            invalid_indices = role_details.get(
                "invalid_mask_indices", []
            )
            if invalid_indices:
                detail_lines.append(
                    f"{role}: нет segmentation mask контакта: "
                    + ", ".join(
                        f"#{index}" for index in invalid_indices
                    )
                )
            continue
        if reason == "invalid_contact_masks":
            indices = ", ".join(
                f"#{index}"
                for index in role_details.get("invalid_mask_indices", [])
            )
            detail_lines.append(
                f"{role}: нет segmentation mask контакта: {indices}"
            )
            continue
        detail_lines.append(
            f"{role}: area min "
            f"{float(role_details.get('area_absolute_min_px2') or 0):g} px²; "
            f"rectangle "
            f"{float(role_details.get('rect_width_px') or 0):g}x"
            f"{float(role_details.get('rect_height_px') or 0):g} px"
        )
        ignored = int(role_details.get("ignored") or 0)
        if ignored:
            detail_lines.append(
                f"{role}: лишних contacts показано серым: {ignored}"
            )
        if _reference_missing_lines(role, role_details, detail_lines):
            continue
        detail_lines.append(
            f"{role}: заслонка открытие "
            f"{float(role_details.get('damper_open_px') or 0):.1f}/"
            f"{float(role_details.get('damper_open_max_px') or 0):.1f} px"
        )
        straight = role_details.get("straight_delta_y_px")
        if straight is not None:
            detail_lines.append(
                f"{role}: Δцентров по Y (инфо) {float(straight):.1f} px"
            )
        for item in role_details.get("items") or []:
            distance = item.get("omission_distance_px")
            distance_text = (
                f"{float(distance):.1f} px"
                if distance is not None else "—"
            )
            detail_lines.append(
                f"{role} #{int(item.get('index') or 0)}: "
                f"top={float(item.get('top_y') or 0):.1f}; "
                f"bottom={float(item.get('bottom_y') or 0):.1f}; "
                f"height={float(item.get('height_px') or 0):.1f} px; "
                f"rect {'OK' if item.get('rect_fits') else 'FAIL'}; "
                f"стена {distance_text}"
            )
    return detail_lines


def _detail_top_contacts(per_role: dict) -> list:
    detail_lines = []
    for role, role_details in per_role.items():
        if not isinstance(role_details, dict):
            continue
        reason = role_details.get("reason")
        if reason and str(reason).startswith("wrong_count"):
            detail_lines.append(
                f"{role}: найдено {int(role_details.get('found_raw') or 0)}/14"
            )
            continue
        if reason == "insufficient_valid_contact_masks":
            detail_lines.append(
                f"{role}: valid contact masks "
                f"{int(role_details.get('found') or 0)}/14"
            )
            indices = role_details.get("invalid_mask_indices", [])
            if indices:
                detail_lines.append(
                    f"{role}: нет segmentation mask: "
                    + ", ".join(f"#{index}" for index in indices)
                )
            continue
        if reason == "no_valid_platform":
            detail_lines.append(f"{role}: нет valid platform mask")
            continue
        if reason == "invalid_platform_bbox":
            detail_lines.append(f"{role}: нет valid platform bbox")
            continue
        if reason == "layout_groups_failed":
            counts = role_details.get("group_counts") or {}
            detail_lines.append(
                f"{role}: layout "
                + ", ".join(
                    f"{group}={int(counts.get(group) or 0)}/"
                    f"{TopContactsRuleCount}"
                    for group, TopContactsRuleCount in (
                        ("L", 5), ("R", 5), ("T", 2), ("B", 2)
                    )
                )
            )
            continue
        ignored = int(role_details.get("ignored") or 0)
        if ignored:
            detail_lines.append(
                f"{role}: лишних contacts показано серым: {ignored}"
            )
        for group in ("L", "R", "T", "B"):
            check = (role_details.get("group_checks") or {}).get(group) or {}
            detail_lines.append(
                f"{role} {group}: distance median "
                f"{float(check.get('median_distance_px') or 0):.1f} px; "
                f"max deviation "
                f"{float(check.get('max_deviation_px') or 0):.1f}/"
                f"{float(check.get('allowed_deviation_px') or 0):.1f} px"
            )
        for item in role_details.get("items") or []:
            detail_lines.append(
                f"{role} #{int(item.get('index') or 0)} {item.get('group')}: "
                f"distance {float(item.get('distance_px') or 0):.1f} px; "
                f"deviation {float(item.get('deviation_px') or 0):.1f}/"
                f"{float(item.get('allowed_deviation_px') or 0):.1f} px; "
                f"rect {float(item.get('rect_width_px') or 0):g}x"
                f"{float(item.get('rect_height_px') or 0):g} px "
                f"{'OK' if item.get('rect_fits') else 'FAIL'}"
            )
    return detail_lines


def _detail_top_platform(per_role: dict) -> list:
    detail_lines = []
    for role, role_details in per_role.items():
        if not isinstance(role_details, dict):
            continue
        reason = role_details.get("reason")
        if reason == "no_valid_platform":
            detail_lines.append(f"{role}: нет valid platform mask")
            continue
        if reason == "invalid_platform_orientation":
            detail_lines.append(f"{role}: не построена orientation platform")
            continue
        placement = role_details.get("placement") or "not_fitted"
        placement_text = {
            "centered": "по центру",
            "shifted": "сдвинут",
            "not_fitted": "не вписался",
        }.get(placement, str(placement))
        detail_lines.append(
            f"{role}: rectangle "
            f"{float(role_details.get('rect_width_px') or 0):g}x"
            f"{float(role_details.get('rect_height_px') or 0):g} px; "
            f"angle {float(role_details.get('angle_deg') or 0):.1f}°"
        )
        detail_lines.append(
            f"{role}: {placement_text}; shift "
            f"{float(role_details.get('shift_distance_px') or 0):.1f} px"
        )
    return detail_lines


def _detail_platform_overlap(per_role: dict) -> list:
    detail_lines = []
    for role, role_details in per_role.items():
        if not isinstance(role_details, dict):
            continue
        reason = role_details.get("reason")
        if reason == "no_valid_platform":
            detail_lines.append(f"{role}: нет valid platform mask")
            continue
        if reason == "invalid_platform_orientation":
            detail_lines.append(f"{role}: не построена orientation platform")
            continue
        if reason == "contact_boundary_not_built":
            groups = role_details.get("contact_groups") or {}
            group_text = "/".join(
                f"{side}{int(groups.get(side) or 0)}"
                for side in ("L", "R", "T", "B")
            )
            detail_lines.append(
                f"{role}: область по контактам не построена "
                f"({group_text})"
            )
            continue
        detail_lines.append(
            f"{role}: boundary "
            f"{float(role_details.get('boundary_width_px') or 0):g}x"
            f"{float(role_details.get('boundary_height_px') or 0):g} px; "
            f"component min "
            f"{int(role_details.get('excess_component_min_px') or 0)} px; "
            f"contacts {int(role_details.get('used_contacts') or 0)}"
        )
        detail_lines.append(
            f"{role}: largest component "
            f"{int(role_details.get('largest_component_pixels') or 0)} px; "
            f"confirmed "
            f"{int(role_details.get('excess_pixels') or 0)} px"
        )
    return detail_lines


def _detail_omission(per_role: dict) -> list:
    detail_lines = []
    for role, role_details in per_role.items():
        if not isinstance(role_details, dict):
            continue
        reason = role_details.get("reason")
        if reason:
            detail_lines.append(
                f"{role}: нет valid omission reference ({reason})"
            )
            continue
        line = (
            f"{role}: толщина "
            f"{float(role_details.get('allowed_thickness_px') or 0):.1f} px; "
            f"component min "
            f"{int(role_details.get('excess_component_min_px') or 0)} px; "
            f"residual "
            f"{float(role_details.get('top_line_actual_max_residual_px') or 0):.1f}/"
            f"{float(role_details.get('top_line_max_residual_px') or 0):.1f} px"
        )
        ratio_actual = role_details.get("top_line_actual_inlier_ratio")
        ratio_limit = role_details.get("top_line_min_inlier_ratio")
        if ratio_actual is not None and ratio_limit is not None:
            line += (
                f"; доля у линии "
                f"{float(ratio_actual):.2f}/{float(ratio_limit):.2f}"
            )
        detail_lines.append(line)

        detail_lines.append(
            f"{role}: largest component "
            f"{int(role_details.get('largest_component_pixels') or 0)} px; "
            f"confirmed "
            f"{int(role_details.get('excess_pixels') or 0)} px; "
            f"max depth "
            f"{float(role_details.get('max_excess_depth_px') or 0):.1f} px"
        )
    return detail_lines


def _generic_failure_rows(rule_name: str, per_role: dict) -> list:
    failure_rows = []
    for role, role_details in per_role.items():
        if (
            not isinstance(role_details, dict)
            or not role_details.get("triggered")
        ):
            continue
        failures = []
        reason = role_details.get("reason")
        if reason:
            failures.append(str(reason))

        if rule_name == "window_sinks":
            failures = []
            if reason and str(reason).startswith(
                "invalid_window_reference_count"
            ):
                failures.append(
                    "нет семи mask окон: "
                    f"{int(role_details.get('selected_windows') or 0)}/7"
                )
            elif reason == "invalid_window_masks":
                failures.append(
                    "нет segmentation mask окна: "
                    + ", ".join(
                        f"#{index}"
                        for index in role_details.get(
                            "invalid_window_indices", []
                        )
                    )
                )
            elif reason == "invalid_sink_masks":
                failures.append(
                    "нет segmentation mask раковины: "
                    + ", ".join(
                        f"#{index}"
                        for index in role_details.get(
                            "invalid_sink_indices", []
                        )
                    )
                )
            elif not reason:
                threshold = int(
                    role_details.get("overlap_min_px") or 0
                )
                for hit in role_details.get("hits") or []:
                    failures.append(
                        f"раковина #{hit.get('sink_index')} -> "
                        f"окно #{hit.get('window_index')}: "
                        f"overlap {hit.get('overlap_px')} px "
                        f">= {threshold} px"
                    )

        elif rule_name == "sinks":
            failures = []
            if reason == "invalid_sink_masks":
                failures.append(
                    "нет segmentation mask shell: "
                    + ", ".join(
                        f"#{index}"
                        for index in role_details.get(
                            "invalid_sink_indices", []
                        )
                    )
                )
            elif reason == "invalid_case_central_reference":
                failures.append(
                    "case_central reference: "
                    f"{int(role_details.get('case_central_found') or 0)}/1"
                )
            elif reason == "no_valid_platform":
                failures.append("нет valid platform mask")
            elif reason == "invalid_platform_bbox":
                failures.append("нет valid platform bbox")
            elif reason == "insufficient_valid_contacts":
                failures.append(
                    "valid contact masks: "
                    f"{int(role_details.get('valid_contacts') or 0)}/14"
                )
            elif reason == "invalid_contact_layout":
                counts = role_details.get("contact_group_counts") or {}
                failures.append(
                    "contact layout: "
                    + ", ".join(
                        f"{group}={int(counts.get(group) or 0)}/{expected}"
                        for group, expected in (
                            ("L", 5), ("R", 5),
                            ("T", 2), ("B", 2),
                        )
                    )
                )
            elif not reason:
                for hit in role_details.get("hits") or []:
                    failures.append(
                        f"shell #{hit.get('sink_index')}: forbidden "
                        f"{hit.get('forbidden_pixels')} px; "
                        f"central {hit.get('central_overlap_px')} px; "
                        f"platform {hit.get('platform_overlap_px')} px; "
                        f"contacts {hit.get('contacts_overlap_px')} px"
                    )

        elif rule_name == "glass":
            failures = []
            for hit in role_details.get("hits") or []:
                failures.append(
                    f"glass #{hit.get('glass_index')} -> ОЧИСТКА: "
                    f"platform {hit.get('platform_overlap_px')} px; "
                    f"pin {hit.get('pin_overlap_px')} px; "
                    f"ring {hit.get('ring_overlap_px')} px; "
                    f"union {hit.get('cleanup_overlap_px')} px"
                )

        elif rule_name == "glass_on_contacts":
            failures = []
            if reason == "missing_glass_mask":
                failures.append(
                    "нет segmentation mask glass: "
                    + ", ".join(
                        f"#{index}"
                        for index in role_details.get(
                            "invalid_glass_indices", []
                        )
                    )
                )
            elif reason == "no_valid_platform":
                failures.append("нет valid platform mask")
            elif reason == "invalid_platform_bbox":
                failures.append("нет valid platform bbox")
            elif reason == "insufficient_valid_contacts":
                failures.append(
                    "valid contact masks: "
                    f"{int(role_details.get('valid_contacts') or 0)}/14"
                )
            elif reason == "invalid_contact_layout":
                counts = role_details.get("contact_group_counts") or {}
                failures.append(
                    "contact layout: "
                    + ", ".join(
                        f"{group}={int(counts.get(group) or 0)}/{expected}"
                        for group, expected in (
                            ("L", 5), ("R", 5),
                            ("T", 2), ("B", 2),
                        )
                    )
                )
            elif reason and str(reason).startswith("wrong_pin_count"):
                failures.append(
                    f"pins: {int(role_details.get('pins_found') or 0)}/14"
                )
            elif reason == "missing_pin_mask":
                failures.append(
                    "нет pin mask: "
                    + ", ".join(
                        f"#{index}"
                        for index in role_details.get(
                            "invalid_pin_indices", []
                        )
                    )
                )
            elif reason and str(reason).startswith("invalid_case_count"):
                failures.append(
                    f"case: {int(role_details.get('case_found') or 0)}/1"
                )
            elif reason and str(reason).startswith(
                "invalid_case_central_count"
            ):
                failures.append(
                    "case_central: "
                    f"{int(role_details.get('case_central_found') or 0)}/1"
                )
            elif reason == "case_central_not_inside_case":
                failures.append("invalid case ring")
            elif reason == "empty_case_ring":
                failures.append("empty case ring")
            elif not reason:
                for pair in role_details.get("pairs") or []:
                    failures.append(
                        f"glass #{pair.get('glass_index')} -> "
                        f"contact #{pair.get('contact_index')}: "
                        f"overlap {pair.get('overlap_pixels')} px -> БРАК"
                    )


        if failures:
            failure_rows.append(f"{role}: " + "; ".join(failures))
    return failure_rows


_DETAIL_FORMATTERS = {
    "window_geometry": _detail_window_geometry,
    "contacts_long": _detail_contacts_long,
    "contacts_short": _detail_contacts_short,
    "top_contacts": _detail_top_contacts,
    "top_platform": _detail_top_platform,
    "platform_contacts_overlap": _detail_platform_overlap,
    "long_omission": _detail_omission,
    "short_omission": _detail_omission,
}


def _skip_summary(per_role: dict) -> tuple:
    """Вернуть ``(текст, все_ли_роли_пропущены)`` для правила без измерений."""
    skipped_rows = [
        (role, role_details)
        for role, role_details in per_role.items()
        if isinstance(role_details, dict) and role_details.get("skipped")
    ]
    if not skipped_rows:
        return None, False
    reasons = "; ".join(
        f"{role}: {row.get('reason', NO_MEASUREMENT)}"
        for role, row in skipped_rows
    )
    if len(skipped_rows) == len(per_role):
        return f"Не выполнено: {reasons}", True
    return f"Частично выполнено: {reasons}", False


def _status_label(rule_name: str, triggered: bool, details: dict, consensus: dict):
    """Итог правила для правой панели: текст и признак нейтрального статуса."""
    if rule_name == "part_presence" and details.get("empty_tray"):
        label = "КОРПУС НЕ ОБНАРУЖЕН"
        if consensus:
            label += (
                f" · {int(consensus.get('empty_votes') or 0)}/"
                f"{int(consensus.get('runs') or 0)}"
            )
        return label, True
    if rule_name == "part_presence":
        if not consensus:
            return None, False
        return (
            "КОРПУС ОБНАРУЖЕН · "
            f"{int(consensus.get('present_votes') or 0)}/"
            f"{int(consensus.get('runs') or 0)}"
        ), False
    if not consensus:
        return None, False
    votes_key = "triggered_votes" if triggered else "normal_votes"
    return (
        ("СРАБОТАЛО" if triggered else "НОРМА")
        + f" · {int(consensus.get(votes_key) or 0)}/"
        f"{int(consensus.get('runs') or 0)}"
    ), False


# === Упрощённые человеческие причины дефектов (для быстрого понимания оператором) ===
HUMAN_CAUSE_MAP = {
    # INPUT
    ("window_geometry", True): "НЕПРАВИЛЬНАЯ ГЕОМЕТРИЯ ОКОН",
    ("window_sinks", True): "РАКОВИНА В ОКНЕ",
    # SPIDER
    ("contacts_long", True): "НАКЛОН / СМЕЩЕНИЕ ДЛИННЫХ КОНТАКТОВ",
    ("contacts_short", True): "НАКЛОН / СМЕЩЕНИЕ КОРОТКИХ КОНТАКТОВ",
    ("long_omission", True): "ИЗБЫТОЧНАЯ ТОЛЩИНА ДЛИННОЙ ПОЛОСЫ ПРОПУСКА",
    ("short_omission", True): "ИЗБЫТОЧНАЯ ТОЛЩИНА КОРОТКОЙ ПОЛОСЫ ПРОПУСКА",
    # TOP
    ("top_contacts", True): "СМЕЩЕНИЕ КОНТАКТОВ НА ПЛАТФОРМЕ",
    ("top_platform", True): "ПЛАТФОРМА НЕ ВПИСАЛАСЬ",
    ("platform_contacts_overlap", True): "ЗАПЛЫВ ПЛАТФОРМЫ",
    ("sinks", True): "РАКОВИНА ВНУТРИ КОРПУСА",
    ("glass", True): "СТЕКЛО НА ПЛАТФОРМЕ / ШТИФТАХ",
    ("glass_on_contacts", True): "СТЕКЛО НА КОНТАКТАХ",
}

def get_human_cause(rule_name: str, triggered: bool, details: dict) -> str | None:
    """Возвращает короткую читаемую причину дефекта."""
    if not triggered:
        return None

    key = (rule_name, True)
    if key in HUMAN_CAUSE_MAP:
        return HUMAN_CAUSE_MAP[key]

    # Fallback: пытаемся вытащить самую важную причину
    per_role = details.get("per_role") or {}
    reasons = []
    for role, rd in per_role.items():
        if isinstance(rd, dict) and rd.get("triggered"):
            r = rd.get("reason")
            if r:
                reasons.append(str(r))
            # для некоторых правил берём первую проблему
            if rule_name in ("window_sinks", "sinks", "glass_on_contacts"):
                # ``glass_on_contacts.hits`` is a count, while its overlap
                # rows live in ``pairs``.  Prefer the rows and only iterate
                # values that actually follow the collection contract.
                entries = rd.get("pairs") or rd.get("hits") or []
                if not isinstance(entries, (list, tuple)):
                    entries = []
                for hit in entries:
                    if hit:
                        reasons.append("пересечение")
                        break

    if reasons:
        return reasons[0].upper().replace("_", " ")[:60]
    return "ДЕФЕКТ"


from core.rule_summary import build_presence_summary, build_rule_summary

SUMMARY_LINES_LIMIT = 4

PART_PRESENCE_RULE = "part_presence"

# Камеры, для которых правило имеет смысл в анализе кадра.
# Панель «Анализ кадра» показывает только вычисления выбранной камеры,
# а не всю группу (INPUT / SPIDER / TOP).
RULE_CAMERA_ROLES = {
    "part_presence": ("INPUT_LEFT", "INPUT_RIGHT"),
    "window_geometry": ("INPUT_LEFT", "INPUT_RIGHT"),
    "window_sinks": ("INPUT_LEFT", "INPUT_RIGHT"),
    "contacts_long": ("SPIDER_LEFT", "SPIDER_RIGHT"),
    "long_omission": ("SPIDER_LEFT", "SPIDER_RIGHT"),
    "contacts_short": ("SPIDER_IN", "SPIDER_OUT"),
    "short_omission": ("SPIDER_IN", "SPIDER_OUT"),
    "top_contacts": ("TOP",),
    "top_platform": ("TOP",),
    "platform_contacts_overlap": ("TOP",),
    "sinks": ("TOP",),
    "glass": ("TOP",),
    "glass_on_contacts": ("TOP",),
}

# Поля part_presence, привязанные к конкретной INPUT-камере.
_PRESENCE_ROLE_FIELDS = {
    "INPUT_LEFT": {
        "flatness": "flatness_left",
        "effective": "effective_flatness_left",
        "ignored": "false_positive_ignored_left",
    },
    "INPUT_RIGHT": {
        "flatness": "flatness_right",
        "effective": "effective_flatness_right",
        "ignored": "false_positive_ignored_right",
    },
}


def _presence_summary(details: dict) -> list:
    """Короткая сводка по правилу присутствия детали."""
    limits = details.get("false_positive_max_count_by_role") or {}
    lines = []
    for role, key in (
        ("INPUT_LEFT", "flatness_left"),
        ("INPUT_RIGHT", "flatness_right"),
    ):
        raw = details.get(key)
        if raw is None and key not in details:
            # Поле отсутствует (срез до одной камеры) — не показываем.
            continue
        if raw is None:
            continue
        found = int(raw or 0)
        limit = limits.get(role)
        limit_text = (
            f" (порог ложных {int(limit)})" if isinstance(limit, int) else ""
        )
        lines.append(f"{role}: flatness {found}{limit_text}")
    return lines


def _failing_roles(per_role: dict) -> set:
    return {
        role
        for role, role_details in per_role.items()
        if isinstance(role_details, dict) and role_details.get("triggered")
    }


def _summary_lines(
    rule_name: str,
    triggered: bool,
    skipped: bool,
    details: dict,
    per_role: dict,
    detail_lines: list,
    detail: str,
) -> list:
    """Компактная, но информативная сводка по правилу для правой панели.

    Показываются только те строки, которые реально влияли на решение по детали:
    для сработавшего правила — роли камер с отклонением, для пропущенного —
    причина отсутствия измерения. Список ограничен ``SUMMARY_LINES_LIMIT``.
    """
    if rule_name == PART_PRESENCE_RULE:
        return _presence_summary(details)

    if skipped:
        return [str(detail)] if detail else []

    if not triggered:
        return []

    lines = []
    if isinstance(per_role, dict) and per_role:
        roles = _failing_roles(per_role) or set(per_role)
        if detail_lines:
            lines = [
                line
                for line in detail_lines
                if str(line).split(":", 1)[0].split(" ", 1)[0] in roles
            ] or list(detail_lines)
        else:
            lines = _generic_failure_rows(rule_name, per_role)
    if not lines and detail:
        lines = [str(detail)]

    if len(lines) > SUMMARY_LINES_LIMIT:
        hidden = len(lines) - SUMMARY_LINES_LIMIT
        lines = lines[:SUMMARY_LINES_LIMIT] + [f"…ещё {hidden} строк(и)"]
    return [str(line) for line in lines]


def _threshold_breaches(summary_cards: list) -> list:
    """Выделить только показатели, из-за которых правило не прошло.

    Карточки хранят и нормальные измерения, что полезно инженеру, но
    оператору в анализе кадра сначала нужны факт, значение и сам порог.
    Отдельное компактное поле позволяет UI показать именно эти данные без
    разбора внутренней телеметрии правила.
    """
    breaches = []
    for card in summary_cards or []:
        for metric in card.get("metrics") or []:
            if metric.get("ok") is not False:
                continue
            breaches.append({
                "role": card.get("role", ""),
                "label": metric.get("label", "показатель"),
                "key": metric.get("key"),
                "value": metric.get("value", "—"),
                "threshold": metric.get("limit"),
            })
    return breaches


def _threshold_conclusion(
    triggered: bool, human_cause: str | None, breaches: list,
) -> str:
    """Короткий вывод, связывающий отклонение с результатом правила."""
    if not triggered:
        return "Показатели укладываются в заданные пороги"
    if breaches:
        return human_cause or "Значение вышло за заданный порог — правило сработало"
    return human_cause or "Правило сработало: проверьте причину и измерения"


def _extract_vote_details(consensus: dict, rule_name: str) -> dict | None:
    """Извлечь детали прогона для правила из consensus.

    Возвращает структуру с результатом прогона, evidence run для
    отображения в UI.
    """
    if not isinstance(consensus, dict):
        return None
    
    rules_meta = consensus.get("rules", {})
    rule_meta = rules_meta.get(rule_name) if isinstance(rules_meta, dict) else None

    # part_presence uses its own vocabulary (empty/present), so it does not
    # have the ``rules[rule_name]`` entry used by defect rules.  Dropping the
    # vote here made the UI show a blank/green badge for an empty tray.
    if rule_name == PART_PRESENCE_RULE and (
        "empty_votes" in consensus or "present_votes" in consensus
    ):
        empty_votes = int(consensus.get("empty_votes") or 0)
        present_votes = int(consensus.get("present_votes") or 0)
        decision = consensus.get("decision")
        return {
            "triggered_votes": empty_votes,
            "normal_votes": present_votes,
            "empty_votes": empty_votes,
            "present_votes": present_votes,
            "decision": decision,
            "states": list(consensus.get("states") or []),
            "source_run": consensus.get("source_run"),
            "evidence_run": consensus.get("evidence_run"),
            "agreement_scores": consensus.get("agreement_scores") or [],
            "picture_run": consensus.get("picture_run"),
            "picture_reason": consensus.get("picture_reason"),
            "total_runs": int(consensus.get("runs") or 1),
            "required_votes": int(consensus.get("required_votes") or 1),
        }

    if not isinstance(rule_meta, dict):
        return None

    # Базовые метрики
    triggered_votes = int(rule_meta.get("triggered_votes") or 0)
    normal_votes = int(rule_meta.get("normal_votes") or 0)
    decision = rule_meta.get("decision")
    states = rule_meta.get("states") or []
    source_run = rule_meta.get("source_run")
    evidence_run = rule_meta.get("evidence_run")

    # Сводный размер набора решающих правил.
    agreement_scores = consensus.get("agreement_scores") or []

    # Выбранный кадр из metadata инспекции.
    picture_run = consensus.get("picture_run")
    picture_reason = consensus.get("picture_reason")

    return {
        "triggered_votes": triggered_votes,
        "normal_votes": normal_votes,
        "decision": decision,
        "states": states,
        "source_run": source_run,
        "evidence_run": evidence_run,
        "agreement_scores": agreement_scores,
        "picture_run": picture_run,
        "picture_reason": picture_reason,
        "total_runs": int(consensus.get("runs") or 1),
        "required_votes": int(consensus.get("required_votes") or 1),
    }


def _fallback_run_status(run_cards: list) -> list:
    """Статус одиночного запуска из карточек замеров (без consensus).

    Используется для ручных/демо-строк: по ``ok`` карточек прогона —
    «В НОРМЕ» / «ОТКЛОНЕНИЕ» / «НЕТ ИЗМЕРЕНИЯ». Отличить «область не
    построена» от «отклонение» по карточкам нельзя (нет причины) — для
    production-путей статус приходит точный из consensus.
    """
    statuses = []
    for cards in run_cards or []:
        if not isinstance(cards, list) or not cards:
            statuses.append([])
            continue
        rows = []
        for card in cards:
            if not isinstance(card, dict):
                continue
            ok = card.get("ok")
            role = card.get("role", "")
            if ok is True:
                rows.append({"role": role, "status": "В НОРМЕ", "reason": None})
            elif ok is False:
                rows.append({"role": role, "status": "ОТКЛОНЕНИЕ", "reason": None})
            else:
                rows.append({"role": role, "status": "НЕТ ИЗМЕРЕНИЯ", "reason": None})
        statuses.append(rows)
    return statuses


def filter_rule_report_rows(rows) -> list:
    """Оставить только решающие правила.

    Если деталь не обнаружена, все прочие правила не влияли на решение —
    показывается единственная строка «ДЕТАЛЬ НЕ ОБНАРУЖЕНА».
    """
    rows = list(rows or [])
    for row in rows:
        if row.get("name") == PART_PRESENCE_RULE and row.get("part_absent"):
            return [row]
    return rows


def rule_applies_to_role(rule_name: str, role: str | None) -> bool:
    """Правило относится к выбранной камере (или роль не задана)."""
    if not role:
        return True
    roles = RULE_CAMERA_ROLES.get(rule_name)
    if roles is None:
        # Неизвестное правило: оставляем, если role явно есть в данных.
        return True
    return role in roles


def _filter_role_cards(cards, role: str) -> list:
    if not isinstance(cards, list):
        return []
    return [
        card for card in cards
        if isinstance(card, dict) and card.get("role") == role
    ]


def _filter_run_cards(run_cards, role: str) -> list:
    if not isinstance(run_cards, list):
        return []
    return [_filter_role_cards(cards, role) for cards in run_cards]


def _filter_run_status(run_status, role: str) -> list:
    if not isinstance(run_status, list):
        return []
    filtered = []
    for rows in run_status:
        if not isinstance(rows, list):
            filtered.append([])
            continue
        filtered.append([
            row for row in rows
            if isinstance(row, dict) and (
                row.get("role") == role
                # part_presence пишет общий статус role=INPUT — оставляем.
                or row.get("role") in (None, "", "INPUT")
            )
        ])
    return filtered


def _scope_presence_details(details: dict, role: str) -> dict:
    """Оставить в part_presence только поля выбранной INPUT-камеры."""
    scoped = dict(details)
    if role not in _PRESENCE_ROLE_FIELDS:
        return scoped

    for details_key in (
        "min_confidence_by_role",
        "false_positive_max_count_by_role",
        "presence_by_role",
    ):
        raw = details.get(details_key)
        if isinstance(raw, dict):
            scoped[details_key] = {
                key: value for key, value in raw.items() if key == role
            }

    # Убираем поля чужой камеры (None → summary её пропустит).
    other = "INPUT_RIGHT" if role == "INPUT_LEFT" else "INPUT_LEFT"
    for key in _PRESENCE_ROLE_FIELDS[other].values():
        scoped[key] = None
    return scoped


def _scope_consensus_to_role(consensus: dict, role: str) -> dict:
    """Оставить в consensus только карточки/статусы выбранной камеры."""
    scoped = dict(consensus)
    if "run_cards" in scoped:
        scoped["run_cards"] = _filter_run_cards(scoped.get("run_cards"), role)
    if "run_status" in scoped:
        scoped["run_status"] = _filter_run_status(scoped.get("run_status"), role)
    return scoped


def scope_rule_result_to_role(result, role: str | None):
    """Срез RuleResult до данных одной камеры.

    В UI анализа кадра остаются только измерения выбранной роли.
    ``triggered`` берётся из per_role выбранной камеры (если есть),
    чтобы статус «СРАБОТАЛО/НОРМА» соответствовал тому, что видно на кадре.
    """
    if not role or result is None:
        return result

    rule_name = getattr(result, "rule_name", "") or ""
    if not rule_applies_to_role(rule_name, role):
        return None

    import copy
    from types import SimpleNamespace

    details = copy.deepcopy(getattr(result, "details", {}) or {})
    triggered = bool(getattr(result, "triggered", False))

    if rule_name == PART_PRESENCE_RULE:
        details = _scope_presence_details(details, role)
    else:
        per_role = details.get("per_role")
        if isinstance(per_role, dict) and per_role:
            if role not in per_role:
                return None
            role_details = per_role[role]
            details["per_role"] = {role: role_details}
            if isinstance(role_details, dict) and "triggered" in role_details:
                triggered = bool(role_details.get("triggered"))

    consensus = details.get("consensus")
    if isinstance(consensus, dict):
        details["consensus"] = _scope_consensus_to_role(consensus, role)

    return SimpleNamespace(
        rule_name=rule_name,
        triggered=triggered,
        details=details,
        drawings=getattr(result, "drawings", []) or [],
    )


def build_rule_report_rows(results, role: str | None = None) -> list:
    """Собрать строки отчёта; при ``role`` — только выбранная камера."""
    rows = []
    for result in results or []:
        if role:
            scoped = scope_rule_result_to_role(result, role)
            if scoped is None:
                continue
            rows.append(build_rule_report_row(scoped))
        else:
            rows.append(build_rule_report_row(result))
    return filter_rule_report_rows(rows)


def build_rule_report_row(result) -> dict:
    """Собрать одну строку отчёта по правилу для HMI и диагностики."""
    details = getattr(result, "details", {}) or {}
    rule_name = getattr(result, "rule_name", "")
    triggered = bool(result.triggered)
    per_role = details.get("per_role")
    has_per_role = isinstance(per_role, dict) and bool(per_role)

    detail = details.get("reason") or details.get("status")
    skipped = False
    if has_per_role:
        skip_text, skipped = _skip_summary(per_role)
        if skip_text:
            detail = skip_text

    detail_lines = []
    if has_per_role:
        formatter = _DETAIL_FORMATTERS.get(rule_name)
        if formatter is not None:
            detail_lines = formatter(per_role)
            if detail_lines:
                detail = "; ".join(detail_lines)
        elif triggered:
            failure_rows = _generic_failure_rows(rule_name, per_role)
            if failure_rows:
                detail = "; ".join(failure_rows)

    consensus = details.get("consensus")
    if not isinstance(consensus, dict):
        consensus = {}

    if rule_name == "part_presence":
        detail = (
            "КОРПУС НЕ ОБНАРУЖЕН"
            if details.get("empty_tray")
            else "Корпус обнаружен"
        )

    # === НОВАЯ ПРОСТАЯ ПРИЧИНА ДЛЯ ОПЕРАТОРА ===
    human_cause = None
    if triggered:
        human_cause = get_human_cause(rule_name, triggered, details)

    if not detail:
        detail = human_cause or ("Сработало" if triggered else "Норма")

    status_label, neutral = _status_label(
        rule_name, triggered, details, consensus,
    )

    part_absent = bool(
        rule_name == PART_PRESENCE_RULE and details.get("empty_tray")
    )

    if rule_name == PART_PRESENCE_RULE:
        summary_cards = build_presence_summary(details)
    else:
        summary_cards = build_rule_summary(
            rule_name, details if has_per_role else {},
        )

    summary_lines = _summary_lines(
        rule_name,
        triggered,
        skipped,
        details,
        per_role if has_per_role else {},
        detail_lines,
        str(detail),
    )
    threshold_breaches = _threshold_breaches(summary_cards)
    threshold_conclusion = _threshold_conclusion(
        triggered, human_cause, threshold_breaches,
    )

    # Единственный замер для анализа кадра: значение метрики с порогом.
    # Метрики помечаются понятными названиями порогов (METRIC_PARAM_LABELS),
    # как в панели «Пороги правил»; без сопоставления остаётся название
    # самой метрики.
    import copy
    run_cards = copy.deepcopy(consensus.get("run_cards") or [])
    for cards in run_cards:
        for card in cards:
            for metric in card.get("metrics") or []:
                key = metric.get("key")
                if not key:
                    continue
                label = METRIC_PARAM_LABELS.get((rule_name, key))
                if label:
                    metric["label"] = label

    # Статус области по прогонам («ОБЛАСТЬ НЕ ПОСТРОЕНА» и т.п.). В
    # production-путях приходит из consensus (см. combine_rule_results);
    # для ручных/демо-строк выводим из карточек замеров.
    run_status = consensus.get("run_status") or _fallback_run_status(run_cards)

    return {
        "name": result.rule_name,
        "triggered": triggered,
        "skipped": skipped,
        "status_label": status_label,
        "neutral": neutral,
        "show_detail": rule_name in DETAILED_RULES,
        "detail": str(detail),
        "human_cause": human_cause,
        "detail_lines": detail_lines,
        "summary_lines": summary_lines,
        "summary_cards": summary_cards,
        # Замер порога (для анализа кадра).
        "run_cards": run_cards,
        # Статус области по прогонам (для fail-closed дефектов).
        "run_status": copy.deepcopy(run_status),
        # Значения, не прошедшие проверку, их пороги и итог для HMI.
        "threshold_breaches": threshold_breaches,
        "threshold_conclusion": threshold_conclusion,
        # Детали прогона для UI анализа кадра.
        "vote_details": _extract_vote_details(consensus, rule_name),
        "part_absent": part_absent,
        "decisive": bool(part_absent or triggered or skipped),
        "consensus": dict(consensus),
    }
