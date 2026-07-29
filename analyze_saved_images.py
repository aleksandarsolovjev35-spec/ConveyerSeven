#!/usr/bin/env python3
r"""Автономная проверка сохранённых кадров через production-модели и rules.

Примеры:
    python analyze_saved_images.py C:\inspection_images
    python analyze_saved_images.py C:\inspection_images --batch
    python analyze_saved_images.py C:\bad_short --batch --folder-role SPIDER_IN
    python analyze_saved_images.py C:\bad_long --batch --folder-role SPIDER_LEFT
    python analyze_saved_images.py samples.json --device cpu
    python analyze_saved_images.py --image TOP=C:\frames\top.jpg
    python analyze_saved_images.py --image INPUT_LEFT=left.jpg --image INPUT_RIGHT=right.jpg

Обычный запуск с папкой открывает интерактивное окно. ENTER запускает анализ,
TAB переключает RAW/ПРАВИЛА. Кнопка «СОХРАНИТЬ ОТЧЁТ» пишет результаты.

Имена файлов для автоматического пакетного поиска:
    TOP.jpg
    INPUT_LEFT.png
    sample_001_TOP.jpg
    sample_001_INPUT_LEFT.jpg

Для нескольких комплектов можно использовать отдельные подпапки или префиксы.
Результаты сохраняются в offline_analysis_results/ и не управляют оборудованием.
"""

from __future__ import annotations

import argparse
import base64
import csv
import html
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterable

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "offline_analysis_results"
EXPECTED_SIZE = (1280, 720)
IMAGE_EXTENSIONS = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
ROLES = (
    "INPUT_LEFT",
    "INPUT_RIGHT",
    "SPIDER_LEFT",
    "SPIDER_RIGHT",
    "SPIDER_IN",
    "SPIDER_OUT",
    "TOP",
)
INPUT_ROLES = ("INPUT_LEFT", "INPUT_RIGHT")
SPIDER_ROLES = (
    "SPIDER_LEFT",
    "SPIDER_RIGHT",
    "SPIDER_IN",
    "SPIDER_OUT",
    "TOP",
)
ROLE_LABELS = {
    "INPUT_LEFT": "КЛАСС 1/6 · ВХОД СЛЕВА",
    "INPUT_RIGHT": "КЛАСС 1/6 · ВХОД СПРАВА",
    "SPIDER_LEFT": "КЛАСС 2/7 · КОНТРОЛЬ СЛЕВА",
    "SPIDER_RIGHT": "КЛАСС 2/7 · КОНТРОЛЬ СПРАВА",
    "SPIDER_IN": "КЛАСС 3/5 · ВНУТРЕННИЙ ВИД",
    "SPIDER_OUT": "КЛАСС 3/5 · НАРУЖНЫЙ ВИД",
    "TOP": "КЛАСС 4 · ВИД СВЕРХУ",
}
UNASSIGNED_LABEL = "НЕ НАЗНАЧЕНО"
ROLE_CHOICES = [UNASSIGNED_LABEL] + [ROLE_LABELS[role] for role in ROLES]
LABEL_TO_ROLE = {label: role for role, label in ROLE_LABELS.items()}
OMISSION_RULES = {"long_omission", "short_omission"}
PART_PRESENCE_RULE = "part_presence"
WINDOW_GEOMETRY_RULE = "window_geometry"
WINDOW_SINKS_RULE = "window_sinks"
TOP_SINKS_RULE = "sinks"
GLASS_RULE = "glass"
GLASS_BAD_RULE = "glass_on_contacts"
PLATFORM_OVERLAP_RULE = "platform_contacts_overlap"
INSCRIBED_RECT_RULES = {
    "contacts_long",
    "contacts_short",
    "top_contacts",
    "top_platform",
}
DETAIL_RULES = {
    *OMISSION_RULES,
    *INSCRIBED_RECT_RULES,
    WINDOW_GEOMETRY_RULE,
    WINDOW_SINKS_RULE,
    TOP_SINKS_RULE,
    GLASS_RULE,
    GLASS_BAD_RULE,
    PLATFORM_OVERLAP_RULE,
}


@dataclass(frozen=True)
class SampleSpec:
    name: str
    images: dict[str, Path]


def safe_name(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-zА-Яа-я_-]+", "_", str(value).strip())
    return normalized.strip("_") or "sample"


def parse_role_assignment(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise ValueError(f"Ожидается ROLE=PATH, получено: {value}")
    role, raw_path = value.split("=", 1)
    role = role.strip().upper()
    if role not in ROLES:
        raise ValueError(f"Неизвестная роль {role}. Допустимо: {', '.join(ROLES)}")
    path = Path(raw_path.strip()).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Изображение не найдено: {path}")
    return role, path


def _match_role(stem: str) -> tuple[str | None, str | None]:
    upper = stem.upper()
    if upper in ROLES:
        return "", upper
    for role in sorted(ROLES, key=len, reverse=True):
        suffix = f"_{role}"
        if upper.endswith(suffix):
            prefix = stem[: -len(suffix)].strip("_- ")
            return prefix, role
    return None, None


def discover_in_directory(folder: Path) -> list[SampleSpec]:
    folder = folder.expanduser().resolve()
    if not folder.is_dir():
        raise NotADirectoryError(f"Папка не найдена: {folder}")

    discovered: list[SampleSpec] = []
    discovered.extend(_discover_groups(folder, folder.name))
    for subfolder in sorted(path for path in folder.iterdir() if path.is_dir()):
        discovered.extend(_discover_groups(subfolder, subfolder.name))

    unique: dict[str, SampleSpec] = {}
    for sample in discovered:
        name = sample.name
        suffix = 2
        while name in unique:
            name = f"{sample.name}_{suffix}"
            suffix += 1
        unique[name] = SampleSpec(name=name, images=sample.images)
    return list(unique.values())


def _discover_groups(folder: Path, base_name: str) -> list[SampleSpec]:
    groups: dict[str, dict[str, Path]] = {}
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        prefix, role = _match_role(path.stem)
        if role is None:
            continue
        group_name = safe_name(prefix or base_name)
        images = groups.setdefault(group_name, {})
        if role in images:
            raise RuntimeError(
                f"Два изображения для {role} в комплекте {group_name}: "
                f"{images[role]} и {path}"
            )
        images[role] = path.resolve()
    return [
        SampleSpec(name=name, images=images)
        for name, images in sorted(groups.items())
        if images
    ]


def load_manifest(path: Path) -> list[SampleSpec]:
    path = path.expanduser().resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_samples = data.get("samples") if isinstance(data, dict) else None
    if raw_samples is None and isinstance(data, dict):
        raw_samples = [{"name": path.stem, "images": data}]
    if not isinstance(raw_samples, list) or not raw_samples:
        raise ValueError("Manifest должен содержать непустой список samples")

    samples = []
    for index, raw_sample in enumerate(raw_samples, start=1):
        if not isinstance(raw_sample, dict):
            raise ValueError(f"samples[{index}] должен быть объектом")
        name = safe_name(raw_sample.get("name") or f"sample_{index:03d}")
        raw_images = raw_sample.get("images")
        if not isinstance(raw_images, dict) or not raw_images:
            raise ValueError(f"В {name} отсутствует images")
        images = {}
        for raw_role, raw_image in raw_images.items():
            role = str(raw_role).upper()
            if role not in ROLES:
                raise ValueError(f"Неизвестная роль {role} в {name}")
            image_path = (path.parent / str(raw_image)).resolve()
            if not image_path.is_file():
                raise FileNotFoundError(f"Изображение не найдено: {image_path}")
            images[role] = image_path
        samples.append(SampleSpec(name=name, images=images))
    return samples


def resolve_samples(input_path: str | None, assignments: Iterable[str]) -> list[SampleSpec]:
    assignments = list(assignments)
    if assignments:
        images = {}
        for value in assignments:
            role, path = parse_role_assignment(value)
            if role in images:
                raise ValueError(f"Роль {role} указана несколько раз")
            images[role] = path
        return [SampleSpec(name="manual", images=images)]

    if not input_path:
        raise ValueError("Укажите папку/manifest или хотя бы один --image ROLE=PATH")
    path = Path(input_path).expanduser().resolve()
    if path.suffix.lower() == ".json":
        return load_manifest(path)
    samples = discover_in_directory(path)
    if not samples:
        raise RuntimeError(
            "Не найдены изображения с именами ролей. "
            "Используйте TOP.jpg, sample_TOP.jpg, manifest JSON или --image ROLE=PATH."
        )
    return samples


def read_image(path: Path):
    payload = np.fromfile(path, dtype=np.uint8)
    frame = cv2.imdecode(payload, cv2.IMREAD_COLOR)
    if frame is None:
        raise RuntimeError(f"OpenCV не смог прочитать изображение: {path}")
    return frame


def inspect_frame_health(frame, *, allow_size_mismatch: bool, allow_near_black: bool) -> dict:
    array = np.asarray(frame)
    if array.ndim != 3 or array.shape[2] < 3:
        raise RuntimeError(f"Некорректная форма изображения: {array.shape}")
    height, width = array.shape[:2]
    if (width, height) != EXPECTED_SIZE and not allow_size_mismatch:
        raise RuntimeError(
            f"Разрешение {width}x{height}; ожидается "
            f"{EXPECTED_SIZE[0]}x{EXPECTED_SIZE[1]}. "
            "Для диагностического запуска используйте --allow-size-mismatch."
        )
    sample = array[::12, ::12, :3].astype(np.float32)
    luminance = sample.mean(axis=2)
    mean = float(luminance.mean())
    p99 = float(np.percentile(luminance, 99))
    near_black = mean <= 5.0 and p99 <= 12.0
    if near_black and not allow_near_black:
        raise RuntimeError(
            f"Почти чёрное изображение: mean={mean:.2f}, p99={p99:.2f}. "
            "Для принудительной проверки используйте --allow-near-black."
        )
    return {
        "width": int(width),
        "height": int(height),
        "mean_luminance": round(mean, 3),
        "p99_luminance": round(p99, 3),
        "near_black": near_black,
    }


def partition_rules(rules, provided_roles: Iterable[str]):
    """Выбрать правила, способные обработать хотя бы одну доступную роль.

    Правила самостоятельно обходят ROLES и проверяют предоставленный кадр.
    Отсутствие парной камеры делает общий результат частичным, но не должно
    блокировать отрисовку и статистику текущего изображения.
    """
    provided = set(provided_roles)
    runnable = []
    for rule in rules:
        required = set(getattr(rule, "ROLES", ()))
        if required & provided:
            runnable.append(rule)
    return runnable, []


def rule_report_row(result, required_roles=()) -> dict:
    details = getattr(result, "details", {}) or {}
    detail = details.get("reason") or details.get("status")
    skipped = False
    per_role = details.get("per_role")
    if isinstance(per_role, dict) and per_role:
        skipped_rows = [
            (role, role_details)
            for role, role_details in per_role.items()
            if isinstance(role_details, dict) and role_details.get("skipped")
        ]
        if len(skipped_rows) == len(per_role):
            skipped = True
            detail = "Не выполнено: " + "; ".join(
                f"{role}: {row.get('reason', 'нет измерения')}"
                for role, row in skipped_rows
            )
        elif skipped_rows:
            detail = "Частично выполнено: " + "; ".join(
                f"{role}: {row.get('reason', 'нет измерения')}"
                for role, row in skipped_rows
            )

    rule_name = getattr(result, "rule_name", "")
    if rule_name in OMISSION_RULES and isinstance(per_role, dict):
        boundary_rows = []
        for role, role_details in per_role.items():
            if not isinstance(role_details, dict):
                continue
            reason = role_details.get("reason")
            if reason:
                boundary_rows.append(
                    f"{role}: нет valid omission reference ({reason})"
                )
                continue
            boundary_rows.append(
                f"{role}: thickness "
                f"{float(role_details.get('allowed_thickness_px') or 0):.1f}px; "
                f"component min "
                f"{int(role_details.get('excess_component_min_px') or 0)}px; "
                f"residual "
                f"{float(role_details.get('top_line_actual_max_residual_px') or 0):.1f}/"
                f"{float(role_details.get('top_line_max_residual_px') or 0):.1f}px"
            )
            boundary_rows.append(
                f"{role}: largest component "
                f"{int(role_details.get('largest_component_pixels') or 0)}px; "
                f"confirmed {int(role_details.get('excess_pixels') or 0)}px; "
                f"max depth "
                f"{float(role_details.get('max_excess_depth_px') or 0):.1f}px"
            )
        if boundary_rows:
            detail = "; ".join(boundary_rows)

    if rule_name == WINDOW_GEOMETRY_RULE and isinstance(per_role, dict):
        geometry_rows = []
        for role, role_details in per_role.items():
            if not isinstance(role_details, dict):
                continue
            found = int(role_details.get("found") or 0)
            expected = int(role_details.get("expected_count") or 0)
            reason = role_details.get("reason")
            if reason:
                geometry_rows.append(f"{role}: найдено {found}/{expected}")
                continue
            top_limits = role_details.get("top_limits_px") or [0.0, 0.0]
            bottom_limits = role_details.get("bottom_limits_px") or [0.0, 0.0]
            geometry_rows.append(
                f"{role}: T {float(top_limits[0]):g}..{float(top_limits[1]):g}px; "
                f"B {float(bottom_limits[0]):g}..{float(bottom_limits[1]):g}px"
            )
            ignored = int(role_details.get("ignored") or 0)
            if ignored:
                geometry_rows.append(
                    f"{role}: лишних detections показано серым: {ignored}"
                )
            for item in role_details.get("items") or []:
                index = int(item.get("index") or 0)
                if not item.get("valid"):
                    geometry_rows.append(
                        f"{role} #{index}: нет измерения T/B"
                    )
                    continue
                failures = []
                if item.get("top_fail"):
                    failures.append("T вне допуска")
                if item.get("bottom_fail"):
                    failures.append("B вне допуска")
                text = (
                    f"{role} #{index}: "
                    f"T={float(item.get('top_px') or 0):.1f}px; "
                    f"B={float(item.get('bottom_px') or 0):.1f}px"
                )
                if failures:
                    text += "; " + ", ".join(failures)
                geometry_rows.append(text)
        if geometry_rows:
            detail = "; ".join(geometry_rows)

    if rule_name == WINDOW_SINKS_RULE and isinstance(per_role, dict):
        sink_rows = []
        for role, role_details in per_role.items():
            if not isinstance(role_details, dict):
                continue
            reason = role_details.get("reason")
            if reason and str(reason).startswith("invalid_window_reference_count"):
                sink_rows.append(
                    f"{role}: нет семи mask окон: "
                    f"{int(role_details.get('selected_windows') or 0)}/7"
                )
                continue
            if reason == "invalid_window_masks":
                indices = ", ".join(
                    f"#{index}"
                    for index in role_details.get("invalid_window_indices", [])
                )
                sink_rows.append(
                    f"{role}: нет segmentation mask окна: {indices}"
                )
                continue
            if reason == "invalid_sink_masks":
                indices = ", ".join(
                    f"#{index}"
                    for index in role_details.get("invalid_sink_indices", [])
                )
                sink_rows.append(
                    f"{role}: нет segmentation mask раковины: {indices}"
                )
                continue
            hits = role_details.get("hits") or []
            if not hits:
                sink_rows.append(f"{role}: норма")
                continue
            threshold = int(role_details.get("overlap_min_px") or 0)
            for hit in hits:
                sink_rows.append(
                    f"{role}: раковина #{hit.get('sink_index')} -> "
                    f"окно #{hit.get('window_index')}; "
                    f"overlap {hit.get('overlap_px')}px >= {threshold}px"
                )
        if sink_rows:
            detail = "; ".join(sink_rows)

    if rule_name == "contacts_long" and isinstance(per_role, dict):
        long_rows = []
        for role, role_details in per_role.items():
            if not isinstance(role_details, dict):
                continue
            reason = role_details.get("reason")
            if reason and str(reason).startswith("wrong_count"):
                long_rows.append(
                    f"{role}: найдено {int(role_details.get('found') or 0)}/5"
                )
                continue
            if reason == "invalid_contact_masks":
                indices = ", ".join(
                    f"#{index}"
                    for index in role_details.get("invalid_mask_indices", [])
                )
                long_rows.append(
                    f"{role}: нет segmentation mask контакта: {indices}"
                )
                continue
            tolerance = float(role_details.get("line_tolerance_px") or 0)
            long_rows.append(
                f"{role}: допуск центров {tolerance:.1f}px; "
                f"наклон {float(role_details.get('level_slope') or 0):.3f}/"
                f"limit {float(role_details.get('max_level_slope') or 0):.3f}; "
                f"rect {float(role_details.get('rect_width_px') or 0):g}x"
                f"{float(role_details.get('rect_height_px') or 0):g}px"
            )
            ignored = int(role_details.get("ignored") or 0)
            if ignored:
                long_rows.append(
                    f"{role}: лишних contacts показано серым: {ignored}"
                )
            omission = role_details.get("omission_tilt_check") or {}
            if omission.get("status") == "error":
                long_rows.append(f"{role}: нет valid reference omission-long")
            else:
                long_rows.append(
                    f"{role}: omission tilt "
                    f"{float(omission.get('distance_trend_ratio') or 0):.3f}/"
                    f"limit {float(role_details.get('omission_tilt_ratio_max') or 0):.3f}"
                )
            for item in role_details.get("items") or []:
                distance = item.get("omission_distance_px")
                distance_text = (
                    f"{float(distance):.1f}px" if distance is not None else "—"
                )
                long_rows.append(
                    f"{role} #{int(item.get('index') or 0)}: "
                    f"center dev {float(item.get('dev_top_px') or 0):.1f}/{tolerance:.1f}px; "
                    f"level {'FAIL' if item.get('top_fail') else 'OK'}; "
                    f"bottom {float(item.get('dev_bottom_px') or 0):.1f}/{tolerance:.1f}px; "
                    f"rect {'OK' if item.get('rect_fits') else 'FAIL'}; "
                    f"d={distance_text}"
                )
        if long_rows:
            detail = "; ".join(long_rows)

    if rule_name == "contacts_short" and isinstance(per_role, dict):
        short_rows = []
        for role, role_details in per_role.items():
            if not isinstance(role_details, dict):
                continue
            reason = role_details.get("reason")
            if reason and str(reason).startswith("wrong_count"):
                short_rows.append(
                    f"{role}: найдено {int(role_details.get('found') or 0)}/2; "
                    f"area min "
                    f"{float(role_details.get('area_absolute_min_px2') or 0):g}px²"
                )
                invalid_indices = role_details.get("invalid_mask_indices", [])
                if invalid_indices:
                    short_rows.append(
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
                short_rows.append(
                    f"{role}: нет segmentation mask контакта: {indices}"
                )
                continue
            tolerance = float(role_details.get("tolerance") or 0)
            short_rows.append(
                f"{role}: area min "
                f"{float(role_details.get('area_absolute_min_px2') or 0):g}px²; "
                f"center dY {float(role_details.get('rect_center_delta_y') or 0):.1f}/"
                f"{float(role_details.get('rect_center_level_tolerance') or 0):.1f}px; "
                f"dTop {float(role_details.get('delta_top') or 0):.1f}/"
                f"{tolerance:.1f}px; dBottom "
                f"{float(role_details.get('delta_bottom') or 0):.1f}/"
                f"{tolerance:.1f}px; dHeight "
                f"{float(role_details.get('delta_height') or 0):.1f}/"
                f"{tolerance:.1f}px"
            )
            short_rows.append(
                f"{role}: rect "
                f"{float(role_details.get('rect_width_px') or 0):g}x"
                f"{float(role_details.get('rect_height_px') or 0):g}px"
            )
            ignored = int(role_details.get("ignored") or 0)
            if ignored:
                short_rows.append(
                    f"{role}: лишних contacts показано серым: {ignored}"
                )
            omission = role_details.get("omission_tilt_check") or {}
            if omission.get("status") == "error":
                short_rows.append(f"{role}: нет valid reference omission-short")
            else:
                short_rows.append(
                    f"{role}: omission tilt "
                    f"{float(omission.get('distance_delta_ratio') or 0):.3f}/"
                    f"limit {float(role_details.get('omission_tilt_ratio_max') or 0):.3f}"
                )
            for item in role_details.get("items") or []:
                distance = item.get("omission_distance_px")
                distance_text = (
                    f"{float(distance):.1f}px" if distance is not None else "—"
                )
                short_rows.append(
                    f"{role} #{int(item.get('index') or 0)}: "
                    f"top={float(item.get('top_y') or 0):.1f}; "
                    f"bottom={float(item.get('bottom_y') or 0):.1f}; "
                    f"height={float(item.get('height_px') or 0):.1f}px; "
                    f"rect {'OK' if item.get('rect_fits') else 'FAIL'}; "
                    f"d={distance_text}"
                )
        if short_rows:
            detail = "; ".join(short_rows)

    if rule_name == "top_platform" and isinstance(per_role, dict):
        platform_rows = []
        for role, role_details in per_role.items():
            if not isinstance(role_details, dict):
                continue
            reason = role_details.get("reason")
            if reason == "no_valid_platform":
                platform_rows.append(f"{role}: нет valid platform mask")
                continue
            if reason == "invalid_platform_orientation":
                platform_rows.append(f"{role}: не построена orientation platform")
                continue
            placement = role_details.get("placement") or "not_fitted"
            placement_text = {
                "centered": "по центру",
                "shifted": "сдвинут",
                "not_fitted": "не вписался",
            }.get(placement, str(placement))
            platform_rows.append(
                f"{role}: rect "
                f"{float(role_details.get('rect_width_px') or 0):g}x"
                f"{float(role_details.get('rect_height_px') or 0):g}px; "
                f"angle {float(role_details.get('angle_deg') or 0):.1f}deg"
            )
            platform_rows.append(
                f"{role}: {placement_text}; shift "
                f"{float(role_details.get('shift_distance_px') or 0):.1f}px"
            )
        if platform_rows:
            detail = "; ".join(platform_rows)

    if (
        rule_name in INSCRIBED_RECT_RULES
        and rule_name not in (
            "contacts_long", "contacts_short", "top_contacts", "top_platform"
        )
        and isinstance(per_role, dict)
    ):
        rectangle_rows = []
        for role, role_details in per_role.items():
            if not isinstance(role_details, dict):
                continue
            check = role_details.get("inscribe_check") or {}
            if check.get("status") == "skipped":
                rectangle_rows.append(
                    f"{role}: прямоугольник не проверен ({check.get('reason', '—')})"
                )
                continue
            width = check.get("rect_width_px")
            height = check.get("rect_height_px")
            fails = int(check.get("fails") or (not check.get("fits", True)))
            if width is None or height is None:
                continue
            text = (
                f"{role}: rect W={float(width):.4g} H={float(height):.4g} px"
                f"; не влезло: {fails}"
            )
            rectangle_rows.append(text)
        if rectangle_rows:
            detail = "; ".join(rectangle_rows)

    if rule_name == "top_contacts" and isinstance(per_role, dict):
        contact_rows = []
        for role, role_details in per_role.items():
            if not isinstance(role_details, dict):
                continue
            reason = role_details.get("reason")
            if reason and str(reason).startswith("wrong_count"):
                contact_rows.append(
                    f"{role}: найдено {int(role_details.get('found_raw') or 0)}/14"
                )
                continue
            if reason == "insufficient_valid_contact_masks":
                contact_rows.append(
                    f"{role}: valid contact masks "
                    f"{int(role_details.get('found') or 0)}/14"
                )
                continue
            if reason == "no_valid_platform":
                contact_rows.append(f"{role}: нет valid platform mask")
                continue
            if reason == "invalid_platform_bbox":
                contact_rows.append(f"{role}: нет valid platform bbox")
                continue
            if reason == "layout_groups_failed":
                counts = role_details.get("group_counts") or {}
                contact_rows.append(
                    f"{role}: layout "
                    + ", ".join(
                        f"{group}={int(counts.get(group) or 0)}/{expected}"
                        for group, expected in (
                            ("L", 5), ("R", 5), ("T", 2), ("B", 2)
                        )
                    )
                )
                continue
            ignored = int(role_details.get("ignored") or 0)
            if ignored:
                contact_rows.append(
                    f"{role}: лишних contacts показано серым: {ignored}"
                )
            for group in ("L", "R", "T", "B"):
                check = (role_details.get("group_checks") or {}).get(group) or {}
                contact_rows.append(
                    f"{role} {group}: distance median "
                    f"{float(check.get('median_distance_px') or 0):.1f}px; "
                    f"max deviation "
                    f"{float(check.get('max_deviation_px') or 0):.1f}/"
                    f"{float(check.get('allowed_deviation_px') or 0):.1f}px"
                )
            for item in role_details.get("items") or []:
                contact_rows.append(
                    f"{role} #{int(item.get('index') or 0)} {item.get('group')}: "
                    f"distance {float(item.get('distance_px') or 0):.1f}px; "
                    f"deviation {float(item.get('deviation_px') or 0):.1f}/"
                    f"{float(item.get('allowed_deviation_px') or 0):.1f}px; "
                    f"rect {float(item.get('rect_width_px') or 0):g}x"
                    f"{float(item.get('rect_height_px') or 0):g}px "
                    f"{'OK' if item.get('rect_fits') else 'FAIL'}"
                )
        if contact_rows:
            detail = "; ".join(contact_rows)

    if rule_name == TOP_SINKS_RULE and isinstance(per_role, dict):
        sink_rows = []
        for role, role_details in per_role.items():
            if not isinstance(role_details, dict):
                continue
            reason = role_details.get("reason")
            if reason == "invalid_sink_masks":
                indices = ", ".join(
                    f"#{index}"
                    for index in role_details.get("invalid_sink_indices", [])
                )
                sink_rows.append(
                    f"{role}: нет segmentation mask shell: {indices}"
                )
                continue
            if reason == "invalid_case_central_reference":
                sink_rows.append(
                    f"{role}: case_central reference "
                    f"{int(role_details.get('case_central_found') or 0)}/1"
                )
                continue
            if reason == "no_valid_platform":
                sink_rows.append(f"{role}: нет valid platform mask")
                continue
            if reason == "invalid_platform_bbox":
                sink_rows.append(f"{role}: нет valid platform bbox")
                continue
            if reason == "insufficient_valid_contacts":
                sink_rows.append(
                    f"{role}: valid contact masks "
                    f"{int(role_details.get('valid_contacts') or 0)}/14"
                )
                continue
            if reason == "invalid_contact_layout":
                counts = role_details.get("contact_group_counts") or {}
                sink_rows.append(
                    f"{role}: contact layout "
                    + ", ".join(
                        f"{group}={int(counts.get(group) or 0)}/{expected}"
                        for group, expected in (
                            ("L", 5), ("R", 5), ("T", 2), ("B", 2)
                        )
                    )
                )
                continue
            hits = role_details.get("hits") or []
            if not hits:
                sink_rows.append(f"{role}: норма")
                continue
            for hit in hits:
                sink_rows.append(
                    f"{role}: shell #{hit.get('sink_index')}; "
                    f"forbidden {hit.get('forbidden_pixels')}px; "
                    f"central {hit.get('central_overlap_px')}px; "
                    f"platform {hit.get('platform_overlap_px')}px; "
                    f"contacts {hit.get('contacts_overlap_px')}px"
                )
        if sink_rows:
            detail = "; ".join(sink_rows)

    if rule_name == GLASS_RULE and isinstance(per_role, dict):
        glass_rows = []
        for role, role_details in per_role.items():
            if not isinstance(role_details, dict):
                continue
            if role_details.get("skipped"):
                glass_rows.append(
                    f"{role}: cleanup не выполнен; {role_details.get('reason')}"
                )
                continue
            hits = role_details.get("hits") or []
            if not hits:
                glass_rows.append(f"{role}: норма")
                continue
            for hit in hits:
                glass_rows.append(
                    f"{role}: glass #{hit.get('glass_index')} -> CLEANUP; "
                    f"platform {hit.get('platform_overlap_px')}px; "
                    f"pin {hit.get('pin_overlap_px')}px; "
                    f"ring {hit.get('ring_overlap_px')}px; "
                    f"union {hit.get('cleanup_overlap_px')}px"
                )
        if glass_rows:
            detail = "; ".join(glass_rows)

    if rule_name == GLASS_BAD_RULE and isinstance(per_role, dict):
        bad_rows = []
        for role, role_details in per_role.items():
            if not isinstance(role_details, dict):
                continue
            reason = role_details.get("reason")
            if reason:
                bad_rows.append(f"{role}: glass context invalid ({reason})")
                continue
            pairs = role_details.get("pairs") or []
            if not pairs:
                bad_rows.append(f"{role}: норма")
                continue
            for pair in pairs:
                bad_rows.append(
                    f"{role}: glass #{pair.get('glass_index')} -> "
                    f"contact #{pair.get('contact_index')}; "
                    f"overlap {pair.get('overlap_pixels')}px -> BAD"
                )
        if bad_rows:
            detail = "; ".join(bad_rows)

    if rule_name == PLATFORM_OVERLAP_RULE and isinstance(per_role, dict):
        overflow_rows = []
        for role, role_details in per_role.items():
            if not isinstance(role_details, dict):
                continue
            reason = role_details.get("reason")
            if reason == "no_valid_platform":
                overflow_rows.append(f"{role}: нет valid platform mask")
                continue
            if reason == "invalid_platform_orientation":
                overflow_rows.append(f"{role}: не построена orientation platform")
                continue
            if reason == "contact_boundary_not_built":
                groups = role_details.get("contact_groups") or {}
                group_text = "/".join(
                    f"{side}{int(groups.get(side) or 0)}"
                    for side in ("L", "R", "T", "B")
                )
                overflow_rows.append(
                    f"{role}: область по контактам не построена "
                    f"({group_text})"
                )
                continue
            overflow_rows.append(
                f"{role}: boundary "
                f"{float(role_details.get('boundary_width_px') or 0):g}x"
                f"{float(role_details.get('boundary_height_px') or 0):g}px; "
                f"component min "
                f"{int(role_details.get('excess_component_min_px') or 0)}px"
            )
            overflow_rows.append(
                f"{role}: largest component "
                f"{int(role_details.get('largest_component_pixels') or 0)}px; "
                f"confirmed {int(role_details.get('excess_pixels') or 0)}px"
            )
        if overflow_rows:
            detail = "; ".join(overflow_rows)

    if rule_name == PART_PRESENCE_RULE:
        detail = (
            "ДЕТАЛЬ НЕ ОБНАРУЖЕНА"
            if details.get("empty_tray")
            else "Деталь обнаружена"
        )
    if not detail:
        detail = "Сработало" if result.triggered else "Норма"
    status = "TRIGGERED" if result.triggered else ("SKIPPED" if skipped else "OK")
    reported_details = details if rule_name in DETAIL_RULES else {}
    if rule_name in OMISSION_RULES and isinstance(per_role, dict):
        public_keys = (
            "triggered", "valid", "reason",
            "allowed_thickness_px", "excess_component_min_px",
            "top_line_max_residual_px", "top_line_actual_max_residual_px",
            "largest_component_pixels", "excess_pixels",
            "max_excess_depth_px",
        )
        reported_details = {
            "per_role": {
                role: {
                    key: role_details.get(key)
                    for key in public_keys
                    if key in role_details
                }
                for role, role_details in per_role.items()
                if isinstance(role_details, dict)
            }
        }
    if rule_name == "top_platform" and isinstance(per_role, dict):
        public_keys = (
            "triggered", "reason", "rect_width_px", "rect_height_px",
            "angle_deg", "fits", "placement", "shift_distance_px",
        )
        reported_details = {
            "per_role": {
                role: {
                    key: role_details.get(key)
                    for key in public_keys
                    if key in role_details
                }
                for role, role_details in per_role.items()
                if isinstance(role_details, dict)
            }
        }
    if rule_name == GLASS_BAD_RULE and isinstance(per_role, dict):
        public_keys = (
            "triggered", "reason", "reference_fail", "hits", "pairs",
            "invalid_glass_indices", "valid_contacts",
            "contact_group_counts", "pins_found", "invalid_pin_indices",
            "case_found", "case_central_found",
        )
        reported_details = {
            "per_role": {
                role: {
                    key: role_details.get(key)
                    for key in public_keys
                    if key in role_details
                }
                for role, role_details in per_role.items()
                if isinstance(role_details, dict)
            }
        }
    if rule_name == GLASS_RULE and isinstance(per_role, dict):
        public_keys = (
            "triggered", "skipped", "reason", "cleanup_hits", "hits",
        )
        reported_details = {
            "per_role": {
                role: {
                    key: role_details.get(key)
                    for key in public_keys
                    if key in role_details
                }
                for role, role_details in per_role.items()
                if isinstance(role_details, dict)
            }
        }
    if rule_name == TOP_SINKS_RULE and isinstance(per_role, dict):
        public_keys = (
            "triggered", "reason", "defect_sinks", "hits",
            "invalid_sink_indices", "case_central_found",
            "valid_contacts", "contact_group_counts",
        )
        reported_details = {
            "per_role": {
                role: {
                    key: role_details.get(key)
                    for key in public_keys
                    if key in role_details
                }
                for role, role_details in per_role.items()
                if isinstance(role_details, dict)
            }
        }
    if rule_name == PLATFORM_OVERLAP_RULE and isinstance(per_role, dict):
        public_keys = (
            "triggered", "reason", "boundary_width_px",
            "boundary_height_px", "excess_component_min_px",
            "largest_component_pixels", "excess_pixels",
            "used_contacts", "contact_groups",
        )
        reported_details = {
            "per_role": {
                role: {
                    key: role_details.get(key)
                    for key in public_keys
                    if key in role_details
                }
                for role, role_details in per_role.items()
                if isinstance(role_details, dict)
            }
        }
    return {
        "name": result.rule_name,
        "status": status,
        "triggered": bool(result.triggered),
        "detail": str(detail),
        "details": reported_details,
        "defect": getattr(result, "defect", None),
        "required_roles": sorted(required_roles),
    }


def _detection_area_statistics(item: dict) -> dict:
    bbox = item.get("bbox")
    bbox_area_px2 = None
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        bbox_area_px2 = abs(
            (float(bbox[2]) - float(bbox[0]))
            * (float(bbox[3]) - float(bbox[1]))
        )

    mask = item.get("mask")
    mask_area_px2 = None
    mask_error = None
    if not isinstance(mask, (list, tuple)) or len(mask) < 3:
        mask_error = "missing_mask"
    else:
        try:
            points = np.asarray(mask, dtype=np.float32)
            valid = (
                points.ndim == 2
                and points.shape[1] == 2
                and len(points) >= 3
                and np.isfinite(points).all()
            )
            if valid:
                area = float(abs(cv2.contourArea(points)))
                if np.isfinite(area) and area > 0.0:
                    mask_area_px2 = area
                else:
                    mask_error = "zero_mask_area"
            else:
                mask_error = "invalid_mask"
        except (TypeError, ValueError, cv2.error):
            mask_error = "invalid_mask"

    return {
        "mask_area_px2": (
            round(mask_area_px2, 3) if mask_area_px2 is not None else None
        ),
        "bbox_area_px2": (
            round(bbox_area_px2, 3) if bbox_area_px2 is not None else None
        ),
        "mask_area_error": mask_error,
    }


def summarize_detections(detections: dict) -> dict:
    summary = {}
    for role, items in detections.items():
        rows = []
        for item in items:
            bbox = item.get("bbox")
            rows.append({
                "class": item.get("class"),
                "confidence": round(float(item.get("confidence", 0.0)), 6),
                "bbox": [round(float(value), 3) for value in bbox] if bbox else None,
                "mask_points": len(item.get("mask") or []),
                **_detection_area_statistics(item),
                "model": item.get("model_path"),
            })
        summary[role] = {
            "count": len(rows),
            "items": rows,
        }
    return summary


def write_image(path: Path, frame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise RuntimeError(f"Не удалось закодировать JPEG: {path}")
    encoded.tofile(path)


def run_rule_checks(decision, vision_results, frames, provided_roles):
    from domain.defect_rules import InputPartPresenceRule

    runnable, skipped = partition_rules(decision.rules, provided_roles)
    rule_results = []
    rule_rows = []
    errors = []
    input_is_empty = False

    input_present = set(INPUT_ROLES).issubset(provided_roles)
    if input_present:
        presence = InputPartPresenceRule(decision.thresholds)
        if not presence.enabled:
            skipped.append({
                "name": "part_presence",
                "required_roles": list(INPUT_ROLES),
                "missing_roles": [],
                "reason": "Правило отключено в thresholds.json",
            })
        else:
            try:
                result = presence.check(vision_results)
                rule_results.append(result)
                rule_rows.append(rule_report_row(result, INPUT_ROLES))
                input_is_empty = bool(result.details.get("empty_tray"))
            except Exception as exc:
                errors.append(f"part_presence: {type(exc).__name__}: {exc}")
                rule_rows.append({
                    "name": "part_presence",
                    "status": "ERROR",
                    "triggered": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                    "defect": None,
                    "required_roles": list(INPUT_ROLES),
                })
    elif set(INPUT_ROLES) & set(provided_roles):
        skipped.append({
            "name": "part_presence",
            "required_roles": list(INPUT_ROLES),
            "missing_roles": sorted(set(INPUT_ROLES) - set(provided_roles)),
            "reason": "Для проверки наличия детали нужны обе входные камеры",
        })

    for rule in runnable:
        required = set(getattr(rule, "ROLES", ()))
        if input_is_empty and required.issubset(INPUT_ROLES):
            skipped.append({
                "name": rule.name,
                "required_roles": sorted(required),
                "missing_roles": [],
                "reason": "Не выполнено: входной лоток пуст",
            })
            continue
        try:
            result = decision.evaluate_rules_detailed(
                [rule], vision_results, frames=frames,
            )[0]
            rule_results.append(result)
            row = rule_report_row(result, required)
            row["missing_roles"] = sorted(required - set(provided_roles))
            row["partial"] = bool(row["missing_roles"])
            rule_rows.append(row)
        except Exception as exc:
            errors.append(f"{rule.name}: {type(exc).__name__}: {exc}")
            rule_rows.append({
                "name": rule.name,
                "status": "ERROR",
                "triggered": False,
                "detail": f"{type(exc).__name__}: {exc}",
                "defect": None,
                "required_roles": sorted(required),
                "missing_roles": sorted(required - set(provided_roles)),
                "partial": bool(required - set(provided_roles)),
            })

    return rule_results, rule_rows, skipped, errors, input_is_empty


def compute_category(rule_results, provided_roles, input_is_empty, decision):
    from domain.part import Part

    if input_is_empty:
        return "EMPTY", []
    provided = set(provided_roles)
    input_complete = set(INPUT_ROLES).issubset(provided)
    spider_complete = set(SPIDER_ROLES).issubset(provided)
    defects = [result.defect for result in rule_results if result.triggered and result.defect]
    if not (input_complete and spider_complete):
        return "UNKNOWN", defects

    rule_roles = {
        rule.name: set(getattr(rule, "ROLES", ()))
        for rule in decision.rules
    }
    part = Part(part_id=1, step_created=0)
    for result in rule_results:
        if not result.triggered or not result.defect or result.rule_name == "part_presence":
            continue
        required = rule_roles.get(result.rule_name, set())
        if required and required.issubset(INPUT_ROLES):
            part.add_input_defect(result.defect)
        else:
            part.add_spider_defect(result.defect)
    part.mark_input_done()
    part.mark_spider_done()
    return part.route_category, defects


def analyze_frames_in_memory(frames, image_health, vision, decision) -> tuple[dict, dict, dict]:
    """Выполнить полный анализ уже загруженного набора кадров без записи файлов."""
    started = time.perf_counter()
    report = {
        "sample": "interactive",
        "ok": False,
        "roles": list(frames),
        "missing_roles": [role for role in ROLES if role not in frames],
        "images": dict(image_health),
        "models": [],
        "detections": {},
        "rules": [],
        "skipped_rules": [],
        "rule_errors": [],
        "defects": [],
        "category": "UNKNOWN",
        "input_is_empty": False,
        "elapsed_ms": None,
        "error": None,
    }
    model_overlays = {}
    rule_overlays = {}
    try:
        vision_results = vision.process_all(frames)
        report["models"] = [dict(item) for item in vision.last_health]
        report["detections"] = summarize_detections(vision_results)
        rule_results, rule_rows, skipped, rule_errors, input_is_empty = run_rule_checks(
            decision, vision_results, frames, set(frames),
        )
        report["rules"] = rule_rows
        report["skipped_rules"] = skipped
        report["rule_errors"] = rule_errors
        report["input_is_empty"] = input_is_empty
        category, defects = compute_category(
            rule_results, set(frames), input_is_empty, decision,
        )
        report["category"] = category
        report["defects"] = defects

        from vision.overlay.debug_overlay import DebugOverlay
        from vision.overlay.raw_overlay import RawOverlay

        for role, frame in frames.items():
            model_overlays[role] = RawOverlay.render(
                frame, vision_results.get(role, []),
            )
            rule_overlays[role] = DebugOverlay.render_frame(
                frame, role, rule_results,
            )
        report["ok"] = not rule_errors
    except Exception as exc:
        report["models"] = [dict(item) for item in getattr(vision, "last_health", [])]
        report["error"] = f"{type(exc).__name__}: {exc}"
        model_overlays = {role: frame.copy() for role, frame in frames.items()}
        rule_overlays = {role: frame.copy() for role, frame in frames.items()}
    report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return report, model_overlays, rule_overlays


def analyze_sample(
    sample: SampleSpec,
    output_root: Path,
    vision,
    decision,
    *,
    allow_size_mismatch: bool,
    allow_near_black: bool,
) -> dict:
    started = time.perf_counter()
    sample_output = output_root / safe_name(sample.name)
    raw_dir = sample_output / "original"
    models_dir = sample_output / "models"
    rules_dir = sample_output / "rules"
    sample_output.mkdir(parents=True, exist_ok=True)

    frames = {}
    image_health = {}
    for role in ROLES:
        path = sample.images.get(role)
        if path is None:
            continue
        frame = read_image(path)
        image_health[role] = {
            "path": str(path),
            **inspect_frame_health(
                frame,
                allow_size_mismatch=allow_size_mismatch,
                allow_near_black=allow_near_black,
            ),
        }
        frames[role] = frame
        write_image(raw_dir / f"{role}.jpg", frame)

    if not frames:
        raise RuntimeError(f"В комплекте {sample.name} нет поддерживаемых ролей")

    report = {
        "sample": sample.name,
        "ok": False,
        "roles": list(frames),
        "missing_roles": [role for role in ROLES if role not in frames],
        "images": image_health,
        "models": [],
        "detections": {},
        "rules": [],
        "skipped_rules": [],
        "rule_errors": [],
        "defects": [],
        "category": "UNKNOWN",
        "input_is_empty": False,
        "elapsed_ms": None,
        "error": None,
    }

    try:
        vision_results = vision.process_all(frames)
        report["models"] = [dict(item) for item in vision.last_health]
        report["detections"] = summarize_detections(vision_results)

        rule_results, rule_rows, skipped, rule_errors, input_is_empty = run_rule_checks(
            decision, vision_results, frames, set(frames),
        )
        report["rules"] = rule_rows
        report["skipped_rules"] = skipped
        report["rule_errors"] = rule_errors
        report["input_is_empty"] = input_is_empty
        category, defects = compute_category(
            rule_results, set(frames), input_is_empty, decision,
        )
        report["category"] = category
        report["defects"] = defects

        from vision.overlay.debug_overlay import DebugOverlay
        from vision.overlay.raw_overlay import RawOverlay

        for role, frame in frames.items():
            model_overlay = RawOverlay.render(frame, vision_results.get(role, []))
            rule_overlay = DebugOverlay.render_frame(frame, role, rule_results)
            write_image(models_dir / f"{role}.jpg", model_overlay)
            write_image(rules_dir / f"{role}.jpg", rule_overlay)

        report["ok"] = not rule_errors
    except Exception as exc:
        report["models"] = [dict(item) for item in getattr(vision, "last_health", [])]
        report["error"] = f"{type(exc).__name__}: {exc}"
        for role, frame in frames.items():
            write_image(models_dir / f"{role}.jpg", frame)
            write_image(rules_dir / f"{role}.jpg", frame)
    finally:
        report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        (sample_output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        write_sample_html(sample_output, report)
    return report


def preferred_analysis_mode(report: dict) -> str:
    """При сработавшем правиле сразу показать RULES, а не нейтральный RAW."""
    return (
        "RULES"
        if any(row.get("triggered") for row in report.get("rules", []))
        else "RAW"
    )


def rule_status_for_role(rule_row: dict, role: str) -> str:
    """Локальный статус камеры, а не общий OR по парному правилу."""
    status = str(rule_row.get("status", "—"))
    if status == "ERROR":
        return status
    per_role = (rule_row.get("details") or {}).get("per_role") or {}
    role_details = per_role.get(role)
    if not isinstance(role_details, dict):
        return status
    if role_details.get("skipped"):
        return "SKIPPED"
    return "TRIGGERED" if role_details.get("triggered") else "OK"


def _status_text(report: dict) -> str:
    if report.get("error"):
        return "ОШИБКА"
    if report.get("rule_errors"):
        return "ОШИБКА ПРАВИЛ"
    if report.get("input_is_empty"):
        return "ПУСТО"
    if report.get("defects"):
        return "ОБНАРУЖЕНЫ ДЕФЕКТЫ"
    if report.get("skipped_rules"):
        return "ЧАСТИЧНАЯ ПРОВЕРКА"
    return "НОРМА"


def collect_omission_calibration(reports: Iterable[dict]) -> dict:
    """Собрать только согласованные calibration-метрики omission."""
    rows = []
    for report in reports:
        for rule_row in report.get("rules", []):
            rule_name = rule_row.get("name")
            if rule_name not in OMISSION_RULES:
                continue
            per_role = (rule_row.get("details") or {}).get("per_role") or {}
            for role, details in per_role.items():
                if not isinstance(details, dict):
                    continue
                image_info = report.get("images", {}).get(role, {})
                rows.append({
                    "sample": str(report.get("sample", "sample")),
                    "image": str(image_info.get("path", "")),
                    "role": str(role),
                    "rule": str(rule_name),
                    "triggered": bool(details.get("triggered")),
                    "valid": bool(details.get("valid")),
                    "reason": str(details.get("reason") or ""),
                    "allowed_thickness_px": float(
                        details.get("allowed_thickness_px") or 0.0
                    ),
                    "excess_component_min_px": int(
                        details.get("excess_component_min_px") or 0
                    ),
                    "top_line_max_residual_px": (
                        float(details["top_line_max_residual_px"])
                        if details.get("top_line_max_residual_px") is not None
                        else None
                    ),
                    "top_line_actual_max_residual_px": (
                        float(details["top_line_actual_max_residual_px"])
                        if details.get("top_line_actual_max_residual_px") is not None
                        else None
                    ),
                    "largest_component_pixels": int(
                        details.get("largest_component_pixels") or 0
                    ),
                    "excess_pixels": (
                        int(details["excess_pixels"])
                        if details.get("excess_pixels") is not None else None
                    ),
                    "max_excess_depth_px": (
                        float(details["max_excess_depth_px"])
                        if details.get("max_excess_depth_px") is not None else None
                    ),
                })

    summaries = []
    grouped = {}
    for row in rows:
        grouped.setdefault((row["rule"], row["role"]), []).append(row)
    for (rule_name, role), group_rows in sorted(grouped.items()):
        valid_rows = [row for row in group_rows if row["valid"]]
        confirmed = [
            row["excess_pixels"] for row in valid_rows
            if row["excess_pixels"] is not None
        ]
        depths = [
            row["max_excess_depth_px"] for row in valid_rows
            if row["max_excess_depth_px"] is not None
        ]
        residuals = [
            row["top_line_actual_max_residual_px"] for row in valid_rows
            if row["top_line_actual_max_residual_px"] is not None
        ]
        largest = [row["largest_component_pixels"] for row in valid_rows]
        summaries.append({
            "rule": rule_name,
            "role": role,
            "samples": len(group_rows),
            "valid_samples": len(valid_rows),
            "invalid_samples": len(group_rows) - len(valid_rows),
            "allowed_thickness_px": group_rows[0]["allowed_thickness_px"],
            "excess_component_min_px": group_rows[0][
                "excess_component_min_px"
            ],
            "residual_limit_px": group_rows[0]["top_line_max_residual_px"],
            "max_actual_residual_px": max(residuals) if residuals else None,
            "largest_component_max_px": max(largest) if largest else None,
            "confirmed_median_px": (
                float(np.median(confirmed)) if confirmed else None
            ),
            "confirmed_max_px": max(confirmed) if confirmed else None,
            "max_depth_px": max(depths) if depths else None,
        })
    return {"rows": rows, "summaries": summaries}


def write_omission_calibration(output_root: Path, reports: list[dict]) -> dict:
    calibration = collect_omission_calibration(reports)
    rows = calibration["rows"]
    if not rows:
        return calibration

    (output_root / "omission_areas.json").write_text(
        json.dumps(calibration, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    fields = (
        "sample", "image", "role", "rule", "triggered", "valid", "reason",
        "allowed_thickness_px", "excess_component_min_px",
        "top_line_max_residual_px", "top_line_actual_max_residual_px",
        "largest_component_pixels", "excess_pixels", "max_excess_depth_px",
    )
    with (output_root / "omission_areas.csv").open(
        "w", encoding="utf-8-sig", newline="",
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    esc = html.escape

    def fmt(value, decimals=1):
        return "—" if value is None else f"{float(value):.{decimals}f}"

    summary_html = "".join(
        "<tr>"
        f"<td>{esc(row['role'])}</td><td>{esc(row['rule'])}</td>"
        f"<td>{row['samples']}</td><td>{row['valid_samples']}</td>"
        f"<td>{row['invalid_samples']}</td>"
        f"<td>{row['allowed_thickness_px']:.1f}</td>"
        f"<td>{row['excess_component_min_px']}</td>"
        f"<td>{fmt(row['max_actual_residual_px'])}/"
        f"{fmt(row['residual_limit_px'])}</td>"
        f"<td>{fmt(row['largest_component_max_px'], 0)}</td>"
        f"<td>{fmt(row['confirmed_median_px'], 0)}</td>"
        f"<td>{fmt(row['confirmed_max_px'], 0)}</td>"
        f"<td>{fmt(row['max_depth_px'])}</td>"
        "</tr>"
        for row in calibration["summaries"]
    )
    detail_html = "".join(
        "<tr>"
        f"<td><a href=\"{esc(safe_name(row['sample']))}/report.html\">"
        f"{esc(row['sample'])}</a></td>"
        f"<td>{esc(row['role'])}</td>"
        f"<td>{'ДА' if row['valid'] else 'НЕТ'}</td>"
        f"<td>{esc(row['reason'] or '—')}</td>"
        f"<td>{row['allowed_thickness_px']:.1f}</td>"
        f"<td>{row['excess_component_min_px']}</td>"
        f"<td>{fmt(row['top_line_actual_max_residual_px'])}/"
        f"{fmt(row['top_line_max_residual_px'])}</td>"
        f"<td>{row['largest_component_pixels']}</td>"
        f"<td>{fmt(row['excess_pixels'], 0)}</td>"
        f"<td>{fmt(row['max_excess_depth_px'])}</td>"
        f"<td>{'БРАК' if row['triggered'] else 'НОРМА'}</td>"
        "</tr>"
        for row in rows
    )
    document = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Калибровка omission boundary</title>
<style>:root{{--bg:#0f1317;--panel:#182027;--border:#44515c;--text:#e0e5e9;--dim:#9aa5ad}}*{{box-sizing:border-box}}body{{margin:0;padding:20px;background:var(--bg);color:var(--text);font:14px 'Segoe UI',Arial,sans-serif}}.block{{margin-bottom:14px;padding:14px;background:var(--panel);border:1px solid var(--border);overflow:auto}}table{{width:100%;border-collapse:collapse;font-size:12px;white-space:nowrap}}th,td{{padding:7px;border-bottom:1px solid var(--border);text-align:left}}th{{color:var(--dim)}}a{{color:#9fc0cc}}</style></head><body>
<h1>КАЛИБРОВКА ГРАНИЦЫ OMISSION</h1>
<div class="block"><h2>СВОДКА</h2><table><thead><tr><th>Роль</th><th>Правило</th><th>Кадров</th><th>Валидно</th><th>Ошибка</th><th>Толщина</th><th>Min comp</th><th>Residual max/limit</th><th>Largest comp</th><th>Confirmed median</th><th>Confirmed max</th><th>Max depth</th></tr></thead><tbody>{summary_html}</tbody></table></div>
<div class="block"><h2>КАЖДЫЙ КАДР</h2><table><thead><tr><th>Кадр</th><th>Роль</th><th>Валидно</th><th>Причина</th><th>Толщина</th><th>Min comp</th><th>Residual/limit</th><th>Largest comp</th><th>Confirmed px</th><th>Max depth</th><th>Результат</th></tr></thead><tbody>{detail_html}</tbody></table></div>
</body></html>"""
    (output_root / "omission_areas.html").write_text(document, encoding="utf-8")
    return calibration


def write_sample_html(folder: Path, report: dict) -> None:
    esc = html.escape
    roles = report.get("roles", [])
    camera_sections = []
    for role in roles:
        camera_sections.append(f"""
        <section class="camera">
          <h2>{esc(role)}</h2>
          <div class="images">
            <figure><figcaption>ОРИГИНАЛ</figcaption><img src="original/{esc(role)}.jpg"></figure>
            <figure><figcaption>МОДЕЛИ</figcaption><img src="models/{esc(role)}.jpg"></figure>
            <figure><figcaption>ПРАВИЛА</figcaption><img src="rules/{esc(role)}.jpg"></figure>
          </div>
        </section>""")

    model_rows = "".join(
        "<tr>"
        f"<td>{esc(str(row.get('role', '—')))}</td>"
        f"<td>{esc(Path(str(row.get('model', '—'))).name)}</td>"
        f"<td>{float(row.get('elapsed_ms') or 0):.0f} мс</td>"
        f"<td>{int(row.get('detections') or 0)}</td>"
        f"<td>{'НОРМА' if row.get('ok') else 'ОШИБКА'}</td>"
        "</tr>"
        for row in report.get("models", [])
    ) or '<tr><td colspan="5">Нет результатов моделей</td></tr>'

    rule_rows = "".join(
        "<tr>"
        f"<td>{esc(str(row.get('name', '—')))}</td>"
        f"<td>{esc(str(row.get('status', '—')))}</td>"
        f"<td>{esc(str(row.get('detail', '—')))}</td>"
        "</tr>"
        for row in report.get("rules", [])
    )
    rule_rows += "".join(
        "<tr class=" + '"skipped"' + ">"
        f"<td>{esc(str(row.get('name', '—')))}</td>"
        "<td>НЕ ВЫПОЛНЕНО</td>"
        f"<td>{esc(str(row.get('reason', '—')))}; "
        f"нет: {esc(', '.join(row.get('missing_roles', [])) or '—')}</td>"
        "</tr>"
        for row in report.get("skipped_rules", [])
    )
    if not rule_rows:
        rule_rows = '<tr><td colspan="3">Нет применимых правил</td></tr>'

    omission_data = collect_omission_calibration([report])
    def fmt(source, name, decimals=1):
        value = source.get(name)
        return "—" if value is None else f"{float(value):.{decimals}f}"

    omission_rows_list = []
    for row in omission_data["rows"]:
        omission_rows_list.append(
            "<tr>"
            f"<td>{esc(row['role'])}</td>"
            f"<td>{'ДА' if row['valid'] else 'НЕТ'}</td>"
            f"<td>{esc(row['reason'] or '—')}</td>"
            f"<td>{row['allowed_thickness_px']:.1f}</td>"
            f"<td>{row['excess_component_min_px']}</td>"
            f"<td>{fmt(row, 'top_line_actual_max_residual_px')}/"
            f"{fmt(row, 'top_line_max_residual_px')}</td>"
            f"<td>{row['largest_component_pixels']}</td>"
            f"<td>{fmt(row, 'excess_pixels', 0)}</td>"
            f"<td>{fmt(row, 'max_excess_depth_px')}</td>"
            f"<td>{'БРАК' if row['triggered'] else 'НОРМА'}</td>"
            "</tr>"
        )
    omission_rows = "".join(omission_rows_list)
    omission_block = ""
    if omission_rows:
        omission_block = (
            '<div class="block"><h2>ГРАНИЦА OMISSION</h2>'
            '<table><thead><tr><th>Камера</th><th>Валидно</th>'
            '<th>Причина</th><th>Толщина</th><th>Min comp</th>'
            '<th>Residual/limit</th><th>Largest comp</th>'
            '<th>Confirmed px</th><th>Max depth</th><th>Результат</th>'
            f'</tr></thead><tbody>{omission_rows}</tbody></table></div>'
        )

    error = report.get("error")
    error_html = f'<div class="error">{esc(error)}</div>' if error else ""
    document = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>{esc(report['sample'])}</title>
<style>
:root{{--bg:#0f1317;--panel:#182027;--border:#44515c;--text:#e0e5e9;--dim:#9aa5ad;--ok:#72a37e;--bad:#c97676;--warn:#c1a66a}}
*{{box-sizing:border-box}} body{{margin:0;padding:18px;background:var(--bg);color:var(--text);font:14px 'Segoe UI',Arial,sans-serif}}
header,.camera,.block{{margin-bottom:12px;padding:12px;background:var(--panel);border:1px solid var(--border);border-radius:4px}}
h1,h2,h3{{margin:0 0 8px}} h1{{font-size:20px}} h2{{font-size:13px;color:var(--dim)}}
.meta{{display:flex;gap:18px;flex-wrap:wrap;color:var(--dim)}} .status{{color:var(--ok);font-weight:700}} .error{{padding:10px;border:1px solid var(--bad);color:var(--bad)}}
.images{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}} figure{{margin:0}} figcaption{{margin-bottom:4px;color:var(--dim);font-size:11px}} img{{display:block;width:100%;background:#080b0e;border:1px solid var(--border)}}
table{{width:100%;border-collapse:collapse;font-size:12px}} th,td{{padding:6px;border-bottom:1px solid var(--border);text-align:left}} th{{color:var(--dim)}} .skipped{{color:var(--warn)}} .note{{color:var(--dim);line-height:1.4}}
</style></head><body>
<header><h1>{esc(report['sample'])}</h1><div class="meta"><span class="status">{esc(_status_text(report))}</span><span>Категория: {esc(report.get('category','UNKNOWN'))}</span><span>Роли: {esc(', '.join(roles))}</span><span>{report.get('elapsed_ms',0):.0f} мс</span></div>{error_html}</header>
<div class="block"><h2>МОДЕЛИ</h2><table><thead><tr><th>Камера</th><th>Модель</th><th>Время</th><th>Объекты</th><th>Статус</th></tr></thead><tbody>{model_rows}</tbody></table></div>
<div class="block"><h2>ПРАВИЛА</h2><table><thead><tr><th>Правило</th><th>Статус</th><th>Подробности</th></tr></thead><tbody>{rule_rows}</tbody></table></div>
{omission_block}
{''.join(camera_sections)}
</body></html>"""
    (folder / "report.html").write_text(document, encoding="utf-8")


def write_index(output_root: Path, reports: list[dict]) -> None:
    calibration = write_omission_calibration(output_root, reports)
    rows = "".join(
        f'<li><a href="{html.escape(safe_name(report["sample"]))}/report.html">'
        f'{html.escape(report["sample"])}</a> — {html.escape(_status_text(report))}</li>'
        for report in reports
    )
    calibration_link = ""
    if calibration["rows"]:
        calibration_link = (
            '<section><h2>ГРАНИЦА OMISSION</h2>'
            '<p><a href="omission_areas.html">Открыть сводку для подбора порога</a></p>'
            '<p>Табличные данные: <a href="omission_areas.csv">CSV</a> · '
            '<a href="omission_areas.json">JSON</a></p></section>'
        )
    document = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Проверка сохранённых кадров</title>
<style>body{{max-width:1000px;margin:30px auto;background:#0f1317;color:#e0e5e9;font:15px 'Segoe UI',Arial,sans-serif}}section{{margin:14px 0;padding:14px;background:#182027;border:1px solid #44515c}}a{{color:#9fc0cc}}li{{margin:8px 0}}</style></head>
<body><h1>Проверка сохранённых кадров</h1>{calibration_link}<section><h2>КАДРЫ</h2><ul>{rows}</ul></section></body></html>"""
    (output_root / "index.html").write_text(document, encoding="utf-8")


def list_folder_images(folder: Path) -> list[Path]:
    return sorted(
        path.resolve()
        for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )


def discover_folder_as_role(folder: Path, role: str) -> list[SampleSpec]:
    """Каждый файл папки становится отдельным кадром одной camera role."""
    folder = folder.expanduser().resolve()
    if not folder.is_dir():
        raise NotADirectoryError(f"Папка не найдена: {folder}")
    if role not in ROLES:
        raise ValueError(f"Неизвестная роль: {role}")
    paths = sorted(
        path.resolve()
        for path in folder.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )
    if not paths:
        raise RuntimeError(f"В папке нет поддерживаемых изображений: {folder}")

    samples = []
    used_names = set()
    for path in paths:
        relative = path.relative_to(folder)
        base_name = safe_name(str(relative.with_suffix("")))
        name = base_name
        suffix = 2
        while name in used_names:
            name = f"{base_name}_{suffix}"
            suffix += 1
        used_names.add(name)
        samples.append(SampleSpec(name=name, images={role: path}))
    return samples


class InteractiveAnalyzer:
    BG = "#0f1317"
    PANEL = "#182027"
    RAISED = "#1c252d"
    BORDER = "#44515c"
    TEXT = "#e0e5e9"
    DIM = "#9aa5ad"
    MUTED = "#69747c"
    ACCENT = "#7899a5"
    OK = "#72a37e"
    BAD = "#c97676"
    WARN = "#c1a66a"

    def __init__(self, root, folder: Path, args):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.folder = folder.resolve()
        self.args = args
        self.paths: list[Path] = []
        self.assignments: dict[Path, str] = {}
        self.frame_cache: dict[Path, np.ndarray] = {}
        self.analysis_role_to_path: dict[str, Path] = {}
        self.report: dict | None = None
        self.model_overlays: dict[str, np.ndarray] = {}
        self.rule_overlays: dict[str, np.ndarray] = {}
        self.mode = "RAW"
        self.engine_ready = False
        self.analysis_busy = False
        self.vision = None
        self.decision = None
        self.photo = None
        self.resize_job = None
        self._setting_role = False

        self.status_var = tk.StringVar(value="Загрузка production-моделей...")
        self.image_title_var = tk.StringVar(value="Изображение не выбрано")
        self.mode_var = tk.StringVar(value="ИСХОДНОЕ ИЗОБРАЖЕНИЕ")
        self.role_var = tk.StringVar(value=UNASSIGNED_LABEL)

        self._configure_window()
        self._build_ui()
        self._load_folder(self.folder)
        self._bind_keys()
        threading.Thread(target=self._load_engine, daemon=True).start()

    def _configure_window(self):
        self.root.title("ПРОВЕРКА СОХРАНЁННЫХ ИЗОБРАЖЕНИЙ")
        self.root.geometry("1280x720")
        self.root.minsize(1100, 650)
        self.root.configure(bg=self.BG)

        style = self.ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except self.tk.TclError:
            pass
        style.configure("App.TFrame", background=self.BG)
        style.configure("Panel.TFrame", background=self.PANEL)
        style.configure(
            "App.TButton", background=self.RAISED, foreground=self.TEXT,
            bordercolor=self.BORDER, padding=(12, 7), font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "App.TButton",
            background=[("active", "#222d36"), ("disabled", self.PANEL)],
            foreground=[("disabled", self.MUTED)],
        )
        style.configure(
            "Role.TCombobox", fieldbackground=self.RAISED, background=self.RAISED,
            foreground=self.TEXT, arrowcolor=self.TEXT, bordercolor=self.BORDER,
            padding=5,
        )

    def _build_ui(self):
        top = self.ttk.Frame(self.root, style="Panel.TFrame", padding=(12, 8))
        top.grid(row=0, column=0, columnspan=3, sticky="nsew")
        top.columnconfigure(1, weight=1)

        title = self.tk.Label(
            top, text="ПРОВЕРКА СОХРАНЁННЫХ ИЗОБРАЖЕНИЙ",
            bg=self.PANEL, fg=self.TEXT, font=("Segoe UI", 13, "bold"),
        )
        title.grid(row=0, column=0, sticky="w")
        self.folder_label = self.tk.Label(
            top, text=str(self.folder), anchor="w",
            bg=self.PANEL, fg=self.DIM, font=("Cascadia Mono", 9),
        )
        self.folder_label.grid(row=0, column=1, padx=16, sticky="ew")
        self.open_button = self.ttk.Button(
            top, text="ОТКРЫТЬ ПАПКУ", style="App.TButton",
            command=self.choose_folder,
        )
        self.open_button.grid(row=0, column=2, padx=(0, 6))
        self.analyze_button = self.ttk.Button(
            top, text="АНАЛИЗИРОВАТЬ", style="App.TButton",
            command=self.start_analysis, state="disabled",
        )
        self.analyze_button.grid(row=0, column=3, padx=(0, 6))
        self.save_button = self.ttk.Button(
            top, text="СОХРАНИТЬ ОТЧЁТ", style="App.TButton",
            command=self.save_current_report, state="disabled",
        )
        self.save_button.grid(row=0, column=4)

        left = self.ttk.Frame(self.root, style="Panel.TFrame", padding=10)
        left.grid(row=1, column=0, padx=(12, 6), pady=12, sticky="nsew")
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)
        self._section_label(left, "ИЗОБРАЖЕНИЯ").grid(row=0, column=0, sticky="w", pady=(0, 6))
        list_frame = self.ttk.Frame(left, style="Panel.TFrame")
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        self.image_list = self.tk.Listbox(
            list_frame, bg=self.BG, fg=self.TEXT, selectbackground="#31414b",
            selectforeground=self.TEXT, borderwidth=1, relief="solid",
            highlightthickness=0, activestyle="none", exportselection=False,
            font=("Cascadia Mono", 9),
        )
        self.image_list.grid(row=0, column=0, sticky="nsew")
        list_scroll = self.ttk.Scrollbar(
            list_frame, orient="vertical", command=self.image_list.yview,
        )
        list_scroll.grid(row=0, column=1, sticky="ns")
        self.image_list.configure(yscrollcommand=list_scroll.set)
        self.image_list.bind("<<ListboxSelect>>", self._on_image_selected)

        self._section_label(left, "КЛАСС КАМЕРЫ").grid(
            row=2, column=0, sticky="w", pady=(10, 5),
        )
        self.role_combo = self.ttk.Combobox(
            left, textvariable=self.role_var, values=ROLE_CHOICES,
            state="readonly", style="Role.TCombobox",
        )
        self.role_combo.grid(row=3, column=0, sticky="ew")
        self.role_combo.bind("<<ComboboxSelected>>", self._on_role_changed)
        help_text = self.tk.Label(
            left,
            text=(
                "Выберите изображение и назначьте класс камеры.\n"
                "ENTER — запустить анализ назначенного набора.\n"
                "TAB — переключить RAW / ПРАВИЛА."
            ),
            justify="left", anchor="w", wraplength=225,
            bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 9),
        )
        help_text.grid(row=4, column=0, sticky="ew", pady=(10, 0))

        center = self.ttk.Frame(self.root, style="Panel.TFrame", padding=8)
        center.grid(row=1, column=1, padx=6, pady=12, sticky="nsew")
        center.rowconfigure(1, weight=1)
        center.columnconfigure(0, weight=1)
        center_head = self.ttk.Frame(center, style="Panel.TFrame")
        center_head.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        center_head.columnconfigure(0, weight=1)
        self.image_title = self.tk.Label(
            center_head, textvariable=self.image_title_var, anchor="w",
            bg=self.PANEL, fg=self.TEXT, font=("Segoe UI", 10, "bold"),
        )
        self.image_title.grid(row=0, column=0, sticky="ew")
        mode_switch = self.tk.Frame(center_head, bg=self.PANEL)
        mode_switch.grid(row=0, column=1, padx=(8, 6), sticky="e")
        self.raw_button = self.tk.Button(
            mode_switch, text="RAW", command=lambda: self._set_mode("RAW"),
            bg=self.RAISED, fg=self.MUTED, activebackground="#222d36",
            activeforeground=self.TEXT, disabledforeground=self.MUTED,
            relief="solid", bd=1, padx=9, pady=2,
            font=("Cascadia Mono", 9, "bold"), state="disabled",
        )
        self.raw_button.pack(side="left")
        self.rules_button = self.tk.Button(
            mode_switch, text="ПРАВИЛА", command=lambda: self._set_mode("RULES"),
            bg=self.RAISED, fg=self.MUTED, activebackground="#222d36",
            activeforeground=self.TEXT, disabledforeground=self.MUTED,
            relief="solid", bd=1, padx=9, pady=2,
            font=("Cascadia Mono", 9, "bold"), state="disabled",
        )
        self.rules_button.pack(side="left", padx=(3, 0))
        self.mode_label = self.tk.Label(
            center_head, textvariable=self.mode_var,
            bg=self.RAISED, fg=self.ACCENT, padx=8, pady=3,
            font=("Cascadia Mono", 9, "bold"),
        )
        self.mode_label.grid(row=0, column=2, sticky="e")
        self.image_panel = self.tk.Label(
            center, bg="#080b0e", fg=self.MUTED,
            text="Выберите изображение", compound="center",
        )
        self.image_panel.grid(row=1, column=0, sticky="nsew")
        self.image_panel.bind("<Configure>", self._schedule_image_render)

        right = self.ttk.Frame(self.root, style="Panel.TFrame", padding=10)
        right.grid(row=1, column=2, padx=(6, 12), pady=12, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        self._section_label(right, "СТАТИСТИКА И РЕЗУЛЬТАТЫ").grid(
            row=0, column=0, sticky="w", pady=(0, 6),
        )
        stats_frame = self.ttk.Frame(right, style="Panel.TFrame")
        stats_frame.grid(row=1, column=0, sticky="nsew")
        stats_frame.rowconfigure(0, weight=1)
        stats_frame.columnconfigure(0, weight=1)
        self.stats_text = self.tk.Text(
            stats_frame, wrap="word", bg=self.BG, fg=self.TEXT,
            insertbackground=self.TEXT, relief="solid", borderwidth=1,
            font=("Cascadia Mono", 9), padx=8, pady=8, state="disabled",
            spacing1=1, spacing3=2,
        )
        self.stats_text.grid(row=0, column=0, sticky="nsew")
        stats_scroll = self.ttk.Scrollbar(
            stats_frame, orient="vertical", command=self.stats_text.yview,
        )
        stats_scroll.grid(row=0, column=1, sticky="ns")
        self.stats_text.configure(yscrollcommand=stats_scroll.set)
        self.stats_text.tag_configure("head", foreground=self.ACCENT, font=("Segoe UI", 9, "bold"))
        self.stats_text.tag_configure("good", foreground=self.OK)
        self.stats_text.tag_configure("bad", foreground=self.BAD)
        self.stats_text.tag_configure("warn", foreground=self.WARN)
        self.stats_text.tag_configure("dim", foreground=self.DIM)

        status = self.tk.Label(
            self.root, textvariable=self.status_var, anchor="w",
            bg=self.PANEL, fg=self.DIM, padx=12,
            font=("Cascadia Mono", 9),
        )
        status.grid(row=2, column=0, columnspan=3, sticky="ew")

        self.root.rowconfigure(1, weight=1)
        self.root.columnconfigure(0, minsize=260)
        self.root.columnconfigure(1, weight=1, minsize=480)
        self.root.columnconfigure(2, minsize=390)
        self._render_stats()

    def _section_label(self, parent, text):
        return self.tk.Label(
            parent, text=text, bg=self.PANEL, fg=self.DIM,
            font=("Segoe UI", 9, "bold"),
        )

    def _bind_keys(self):
        priority_tag = "OfflineAnalyzerGlobal"
        self.root.bind_class(priority_tag, "<Return>", self._on_enter)
        self.root.bind_class(priority_tag, "<Tab>", self._on_tab)
        self.root.bind_class(
            priority_tag,
            "<Escape>",
            lambda _event: self.root.destroy() or "break",
        )
        self.root.bind_class(
            priority_tag,
            "<Control-o>",
            lambda _event: self.choose_folder() or "break",
        )

        def prepend_bindtag(widget):
            tags = widget.bindtags()
            if priority_tag not in tags:
                widget.bindtags((priority_tag, *tags))
            for child in widget.winfo_children():
                prepend_bindtag(child)

        prepend_bindtag(self.root)

    def _load_engine(self):
        try:
            from core.decision_engine import DecisionEngine
            from domain.threshold_loader import ThresholdLoader
            from vision.vision_cluster import VisionCluster

            vision = VisionCluster(device=self.args.device)
            if not self.args.skip_warmup:
                vision.warmup()
            decision = DecisionEngine(
                thresholds=ThresholdLoader(ROOT / "thresholds.json").get_all()
            )
        except Exception as exc:
            self.root.after(0, self._engine_failed, exc)
            return
        self.root.after(0, self._engine_loaded, vision, decision)

    def _engine_loaded(self, vision, decision):
        self.vision = vision
        self.decision = decision
        self.engine_ready = True
        self.analyze_button.configure(state="normal")
        self.status_var.set(
            "Модели готовы. Назначьте классы камер и нажмите ENTER."
        )

    def _engine_failed(self, exc):
        from tkinter import messagebox

        self.status_var.set(f"Ошибка загрузки моделей: {exc}")
        messagebox.showerror("Ошибка загрузки моделей", f"{type(exc).__name__}: {exc}")

    def choose_folder(self):
        from tkinter import filedialog

        selected = filedialog.askdirectory(initialdir=str(self.folder))
        if selected:
            self._load_folder(Path(selected))

    def _load_folder(self, folder: Path):
        from tkinter import messagebox

        folder = folder.resolve()
        paths = list_folder_images(folder)
        if not paths:
            messagebox.showwarning("Папка пуста", "В выбранной папке нет поддерживаемых изображений")
            return
        self.folder = folder
        self.paths = paths
        self.assignments.clear()
        self.frame_cache.clear()
        self._clear_analysis()
        self.folder_label.configure(text=str(folder))
        self._refresh_image_list(select_index=0)
        self.status_var.set(f"Найдено изображений: {len(paths)}")

    def _refresh_image_list(self, select_index=None):
        current = self._current_index() if select_index is None else select_index
        self.image_list.delete(0, self.tk.END)
        for path in self.paths:
            role = self.assignments.get(path)
            suffix = f"  [{role}]" if role else ""
            self.image_list.insert(self.tk.END, f"{path.name}{suffix}")
        if self.paths:
            current = max(0, min(current, len(self.paths) - 1))
            self.image_list.selection_set(current)
            self.image_list.activate(current)
            self.image_list.see(current)
            self._select_path(self.paths[current])

    def _current_index(self):
        selection = self.image_list.curselection()
        return int(selection[0]) if selection else 0

    def _current_path(self):
        if not self.paths:
            return None
        index = self._current_index()
        return self.paths[index] if 0 <= index < len(self.paths) else None

    def _on_image_selected(self, _event=None):
        path = self._current_path()
        if path is not None:
            self._select_path(path)

    def _select_path(self, path: Path):
        self._setting_role = True
        role = self.assignments.get(path)
        self.role_var.set(ROLE_LABELS[role] if role else UNASSIGNED_LABEL)
        self._setting_role = False
        self.image_title_var.set(path.name)
        self._update_mode_buttons()
        self._render_image()
        self._render_stats()

    def _on_role_changed(self, _event=None):
        if self._setting_role:
            return
        path = self._current_path()
        if path is None:
            return
        selected = self.role_var.get()
        role = LABEL_TO_ROLE.get(selected)
        if role is None:
            self.assignments.pop(path, None)
        else:
            for other_path, other_role in list(self.assignments.items()):
                if other_role == role and other_path != path:
                    del self.assignments[other_path]
            self.assignments[path] = role
        self._clear_analysis()
        self._refresh_image_list(select_index=self._current_index())
        self.status_var.set("Назначения изменены. Нажмите ENTER для анализа.")

    def _clear_analysis(self):
        self.report = None
        self.analysis_role_to_path.clear()
        self.model_overlays.clear()
        self.rule_overlays.clear()
        self.mode = "RAW"
        self.mode_var.set("ИСХОДНОЕ ИЗОБРАЖЕНИЕ")
        if hasattr(self, "save_button"):
            self.save_button.configure(state="disabled")
        self._update_mode_buttons()

    def _on_enter(self, _event=None):
        self.start_analysis()
        return "break"

    def _is_current_analyzed(self):
        path = self._current_path()
        if path is None or self.report is None:
            return False
        role = self.assignments.get(path)
        return role is not None and self.analysis_role_to_path.get(role) == path

    def _set_mode(self, mode: str):
        if mode not in ("RAW", "RULES"):
            return
        if not self._is_current_analyzed():
            self.status_var.set("Сначала нажмите ENTER и дождитесь анализа этого изображения")
            self._update_mode_buttons()
            return
        self.mode = mode
        self.mode_var.set("ПРАВИЛА" if mode == "RULES" else "RAW · МОДЕЛИ")
        self._update_mode_buttons()
        self._render_image()

    def _update_mode_buttons(self):
        if not hasattr(self, "raw_button"):
            return
        enabled = self._is_current_analyzed()
        state = "normal" if enabled else "disabled"
        self.raw_button.configure(state=state)
        self.rules_button.configure(state=state)
        if not enabled:
            self.raw_button.configure(bg=self.RAISED, fg=self.MUTED)
            self.rules_button.configure(bg=self.RAISED, fg=self.MUTED)
            return
        self.raw_button.configure(
            bg=self.ACCENT if self.mode == "RAW" else self.RAISED,
            fg=self.BG if self.mode == "RAW" else self.DIM,
        )
        self.rules_button.configure(
            bg=self.ACCENT if self.mode == "RULES" else self.RAISED,
            fg=self.BG if self.mode == "RULES" else self.DIM,
        )

    def _on_tab(self, _event=None):
        if not self._is_current_analyzed():
            self.status_var.set("Сначала нажмите ENTER и дождитесь анализа")
            return "break"
        self._set_mode("RULES" if self.mode == "RAW" else "RAW")
        return "break"

    def start_analysis(self):
        from tkinter import messagebox

        if self.analysis_busy:
            return
        if not self.engine_ready:
            self.status_var.set("Модели ещё загружаются")
            return
        if not self.assignments:
            messagebox.showwarning(
                "Нет назначений",
                "Выберите изображение и назначьте хотя бы один класс камеры.",
            )
            return

        role_to_path = {role: path for path, role in self.assignments.items()}
        self.analysis_busy = True
        self.analyze_button.configure(state="disabled")
        self.open_button.configure(state="disabled")
        self.save_button.configure(state="disabled")
        self.status_var.set("Выполняются модели и применимые правила...")
        threading.Thread(
            target=self._analysis_worker,
            args=(role_to_path,),
            daemon=True,
        ).start()

    def _analysis_worker(self, role_to_path):
        try:
            frames = {}
            health = {}
            for role in ROLES:
                path = role_to_path.get(role)
                if path is None:
                    continue
                frame = self._get_frame(path)
                health[role] = {
                    "path": str(path),
                    **inspect_frame_health(
                        frame,
                        allow_size_mismatch=self.args.allow_size_mismatch,
                        allow_near_black=self.args.allow_near_black,
                    ),
                }
                frames[role] = frame
            report, model_overlays, rule_overlays = analyze_frames_in_memory(
                frames, health, self.vision, self.decision,
            )
        except Exception as exc:
            self.root.after(0, self._analysis_failed, exc)
            return
        self.root.after(
            0,
            self._analysis_completed,
            role_to_path,
            report,
            model_overlays,
            rule_overlays,
        )

    def _analysis_completed(self, role_to_path, report, model_overlays, rule_overlays):
        self.analysis_busy = False
        self.analysis_role_to_path = dict(role_to_path)
        self.report = report
        self.model_overlays = model_overlays
        self.rule_overlays = rule_overlays
        self.mode = preferred_analysis_mode(report)
        self.mode_var.set(
            "ПРАВИЛА" if self.mode == "RULES" else "RAW · МОДЕЛИ"
        )
        self.analyze_button.configure(state="normal")
        self.open_button.configure(state="normal")
        self.save_button.configure(state="normal")
        self._update_mode_buttons()
        self.status_var.set(
            f"{_status_text(report)} · {report['elapsed_ms']:.0f} мс · "
            "TAB переключает RAW / ПРАВИЛА"
        )
        self._render_image()
        self._render_stats()

    def _analysis_failed(self, exc):
        from tkinter import messagebox

        self.analysis_busy = False
        self.analyze_button.configure(state="normal")
        self.open_button.configure(state="normal")
        self.status_var.set(f"Ошибка анализа: {exc}")
        messagebox.showerror("Ошибка анализа", f"{type(exc).__name__}: {exc}")

    def _get_frame(self, path: Path):
        frame = self.frame_cache.get(path)
        if frame is None:
            frame = read_image(path)
            self.frame_cache[path] = frame
        return frame

    def _schedule_image_render(self, _event=None):
        if self.resize_job is not None:
            self.root.after_cancel(self.resize_job)
        self.resize_job = self.root.after(80, self._render_image)

    def _render_image(self):
        path = self._current_path()
        if path is None:
            return
        try:
            frame = self._get_frame(path)
        except Exception as exc:
            self.image_panel.configure(text=f"Ошибка изображения:\n{exc}", image="")
            return
        role = self.assignments.get(path)
        analyzed = role is not None and self.analysis_role_to_path.get(role) == path
        if analyzed and self.report is not None:
            overlays = self.rule_overlays if self.mode == "RULES" else self.model_overlays
            frame = overlays.get(role, frame)
        else:
            self.mode_var.set("ИСХОДНОЕ ИЗОБРАЖЕНИЕ")

        max_width = max(320, self.image_panel.winfo_width() - 8)
        max_height = max(240, self.image_panel.winfo_height() - 8)
        height, width = frame.shape[:2]
        scale = min(max_width / width, max_height / height)
        out_width = max(1, int(width * scale))
        out_height = max(1, int(height * scale))
        interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        resized = cv2.resize(frame, (out_width, out_height), interpolation=interpolation)
        ok, encoded = cv2.imencode(".png", resized)
        if not ok:
            return
        payload = base64.b64encode(encoded.tobytes()).decode("ascii")
        self.photo = self.tk.PhotoImage(data=payload)
        self.image_panel.configure(image=self.photo, text="")

    def _render_stats(self):
        path = self._current_path()
        role = self.assignments.get(path) if path else None
        self.stats_text.configure(state="normal")
        self.stats_text.delete("1.0", self.tk.END)

        def line(text="", tag=None):
            self.stats_text.insert(self.tk.END, text + "\n", tag)

        if path is None:
            line("Изображение не выбрано", "dim")
        else:
            line("ТЕКУЩЕЕ ИЗОБРАЖЕНИЕ", "head")
            line(path.name)
            line(f"Класс: {ROLE_LABELS.get(role, 'не назначен')}", "dim")

        if self.report is None:
            line()
            line("ПОРЯДОК РАБОТЫ", "head")
            line("1. Выберите изображение.")
            line("2. Назначьте класс камеры.")
            line("3. Нажмите ENTER.")
            line("4. Переключайте RAW / ПРАВИЛА клавишей TAB.")
            self.stats_text.configure(state="disabled")
            return

        status = _status_text(self.report)
        status_tag = "bad" if self.report.get("error") or self.report.get("defects") else "good"
        if self.report.get("skipped_rules") and status_tag == "good":
            status_tag = "warn"
        line()
        line("ОБЩИЙ РЕЗУЛЬТАТ", "head")
        line(status, status_tag)
        line(f"Категория: {self.report.get('category', 'UNKNOWN')}")
        line(f"Время: {self.report.get('elapsed_ms', 0):.0f} мс", "dim")
        if self.report.get("defects"):
            line("Дефекты: " + ", ".join(self.report["defects"]), "bad")
        if self.report.get("error"):
            line(self.report["error"], "bad")

        if role is None or self.analysis_role_to_path.get(role) != path:
            line()
            line("Для текущего изображения нет результата анализа", "warn")
            self.stats_text.configure(state="disabled")
            return

        image_info = self.report.get("images", {}).get(role, {})
        line()
        line("ИЗОБРАЖЕНИЕ", "head")
        line(f"Разрешение: {image_info.get('width', '—')} × {image_info.get('height', '—')}")
        line(f"Средняя яркость: {image_info.get('mean_luminance', '—')}")
        line(f"P99 яркости: {image_info.get('p99_luminance', '—')}")

        model_rows = [row for row in self.report.get("models", []) if row.get("role") == role]
        line()
        line(f"МОДЕЛИ · {len(model_rows)}", "head")
        if not model_rows:
            line("Нет результатов моделей", "dim")
        for row in model_rows:
            model_name = Path(str(row.get("model", "—"))).name
            tag = "good" if row.get("ok") else "bad"
            line(model_name, tag)
            line(
                f"  {float(row.get('elapsed_ms') or 0):.0f} мс · "
                f"объектов: {int(row.get('detections') or 0)}",
                "dim",
            )
            if row.get("error"):
                line(f"  {row['error']}", "bad")

        detection_report = self.report.get("detections", {}).get(role, {})
        detections = detection_report.get("items", [])
        grouped = {}
        for detection in detections:
            grouped.setdefault(str(detection.get("class", "unknown")), []).append(detection)
        line()
        line(f"КЛАССЫ ОБЪЕКТОВ · {len(grouped)}", "head")
        if not grouped:
            line("Объекты не обнаружены", "dim")
        for class_name, items in sorted(grouped.items()):
            confidences = [float(item.get("confidence") or 0) for item in items]
            mask_areas = [
                float(item["mask_area_px2"])
                for item in items
                if item.get("mask_area_px2") is not None
            ]
            line(
                f"{class_name}: {len(items)} · "
                f"conf {min(confidences):.3f}…{max(confidences):.3f} · "
                f"среднее {sum(confidences) / len(confidences):.3f}"
            )
            if mask_areas:
                line(
                    f"  mask area: {min(mask_areas):.1f}…{max(mask_areas):.1f} px² · "
                    f"среднее {sum(mask_areas) / len(mask_areas):.1f}",
                    "dim",
                )

        line()
        line(f"ВСЕ ОБЪЕКТЫ · {len(detections)}", "head")
        if not detections:
            line("Нет детекций", "dim")
        for index, item in enumerate(detections, start=1):
            bbox = item.get("bbox")
            bbox_text = (
                ", ".join(f"{value:.1f}" for value in bbox)
                if bbox else "без bbox"
            )
            line(
                f"#{index:02d} {item.get('class')} · "
                f"{float(item.get('confidence') or 0):.3f}",
                "good",
            )
            line(f"  bbox: {bbox_text} · mask: {item.get('mask_points', 0)} точек", "dim")
            if item.get("mask_area_px2") is not None:
                line(
                    f"  площадь mask: {float(item['mask_area_px2']):.1f} px² · "
                    f"площадь bbox: {float(item.get('bbox_area_px2') or 0):.1f} px²",
                    "dim",
                )
            else:
                line(
                    f"  площадь mask: НЕТ ИЗМЕРЕНИЯ ({item.get('mask_area_error', '—')})",
                    "warn",
                )
            if item.get("model"):
                line(f"  модель: {Path(str(item['model'])).name}", "dim")

        geometry_measurement = None
        for rule_row in self.report.get("rules", []):
            if rule_row.get("name") != WINDOW_GEOMETRY_RULE:
                continue
            per_role = (rule_row.get("details") or {}).get("per_role") or {}
            role_details = per_role.get(role)
            if isinstance(role_details, dict):
                geometry_measurement = role_details
                break
        if geometry_measurement is not None:
            top_limits = geometry_measurement.get("top_limits_px") or [0.0, 0.0]
            bottom_limits = geometry_measurement.get("bottom_limits_px") or [0.0, 0.0]
            line()
            line("ГЕОМЕТРИЯ ОКОН · ПИКСЕЛИ", "head")
            line(
                f"Порог T: {float(top_limits[0]):.1f}…{float(top_limits[1]):.1f} px"
            )
            line(
                f"Порог B: {float(bottom_limits[0]):.1f}…{float(bottom_limits[1]):.1f} px"
            )
            reason = geometry_measurement.get("reason")
            if reason:
                line(
                    f"Найдено окон: {int(geometry_measurement.get('found') or 0)}/"
                    f"{int(geometry_measurement.get('expected_count') or 0)}",
                    "bad",
                )
            else:
                ignored = int(geometry_measurement.get("ignored") or 0)
                if ignored:
                    line(f"Лишних detections показано серым: {ignored}", "dim")
                items = geometry_measurement.get("items") or []
                for item in items:
                    index = int(item.get("index") or 0)
                    if not item.get("valid"):
                        line(f"Окно #{index}: нет измерения T/B", "bad")
                        continue
                    failed = bool(item.get("top_fail") or item.get("bottom_fail"))
                    line(
                        f"Окно #{index}: "
                        f"T={float(item.get('top_px') or 0):.1f}px · "
                        f"B={float(item.get('bottom_px') or 0):.1f}px",
                        "bad" if failed else "good",
                    )

        omission_measurement = None
        omission_rule_name = None
        for rule_row in self.report.get("rules", []):
            if rule_row.get("name") not in OMISSION_RULES:
                continue
            per_role = (rule_row.get("details") or {}).get("per_role") or {}
            role_details = per_role.get(role)
            if isinstance(role_details, dict):
                omission_measurement = role_details
                omission_rule_name = rule_row.get("name")
                break
        if omission_measurement is not None:
            line()
            line(f"ГРАНИЦА OMISSION · {omission_rule_name}", "head")
            reason = omission_measurement.get("reason")
            if reason:
                line(f"Нет valid omission reference: {reason}", "bad")
            else:
                allowed = float(
                    omission_measurement.get("allowed_thickness_px") or 0.0
                )
                component_min = int(
                    omission_measurement.get("excess_component_min_px") or 0
                )
                residual = float(
                    omission_measurement.get(
                        "top_line_actual_max_residual_px"
                    ) or 0.0
                )
                residual_limit = float(
                    omission_measurement.get("top_line_max_residual_px") or 0.0
                )
                excess = int(
                    omission_measurement.get("excess_pixels") or 0
                )
                largest = int(
                    omission_measurement.get("largest_component_pixels") or 0
                )
                max_depth = float(
                    omission_measurement.get("max_excess_depth_px") or 0.0
                )
                triggered = bool(omission_measurement.get("triggered"))
                line(f"Допустимая толщина: {allowed:.1f} px")
                line(f"Минимальная компонента: {component_min} px")
                line(
                    f"Residual верхней линии: {residual:.1f}/"
                    f"{residual_limit:.1f} px"
                )
                line(
                    f"Самая большая компонента: {largest} px",
                    "bad" if triggered else "good",
                )
                line(
                    f"Подтверждённые пиксели: {excess} px",
                    "bad" if triggered else "good",
                )
                line(
                    f"Максимальный выход: {max_depth:.1f} px",
                    "bad" if triggered else "good",
                )

        relevant_rules = []
        for row in self.report.get("rules", []):
            required = set(row.get("required_roles", []))
            if not required or role in required:
                relevant_rules.append(row)
        skipped_rules = [
            row for row in self.report.get("skipped_rules", [])
            if role in set(row.get("required_roles", []))
        ]
        line()
        line(f"ПРАВИЛА · {len(relevant_rules) + len(skipped_rules)}", "head")
        if not relevant_rules and not skipped_rules:
            line("Для этого класса камеры правила не назначены", "dim")
        local_triggered_rules = []
        for row in relevant_rules:
            status = rule_status_for_role(row, role)
            if status == "TRIGGERED":
                local_triggered_rules.append(str(row.get("name")))
            if status in ("ERROR", "TRIGGERED"):
                tag = "bad"
            elif status == "SKIPPED":
                tag = "warn"
            else:
                tag = "good"
            status_text = {
                "OK": "НОРМА",
                "TRIGGERED": "СРАБОТАЛО",
                "ERROR": "ОШИБКА",
                "SKIPPED": "НЕ ВЫПОЛНЕНО",
            }.get(status, status)
            line(f"{row.get('name')}: {status_text}", tag)
            line(f"  {row.get('detail', '—')}", "dim")
            if row.get("partial"):
                missing = ", ".join(row.get("missing_roles", [])) or "—"
                line(f"  Частичный результат; нет: {missing}", "warn")
        for row in skipped_rules:
            line(f"{row.get('name')}: НЕ ВЫПОЛНЕНО", "warn")
            missing = ", ".join(row.get("missing_roles", [])) or "—"
            line(f"  {row.get('reason')}; нет: {missing}", "dim")
        if self.report.get("defects") and not local_triggered_rules:
            line()
            line(
                "Общий комплект забракован по другой назначенной камере; "
                "на текущем изображении локальных срабатываний нет.",
                "warn",
            )

        self.stats_text.configure(state="disabled")

    def save_current_report(self):
        from tkinter import messagebox

        if self.report is None:
            return
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        folder = Path(self.args.output).expanduser().resolve() / f"interactive_{timestamp}"
        folder.mkdir(parents=True, exist_ok=True)
        report = dict(self.report)
        report["sample"] = folder.name
        for role, path in self.analysis_role_to_path.items():
            frame = self._get_frame(path)
            write_image(folder / "original" / f"{role}.jpg", frame)
            write_image(folder / "models" / f"{role}.jpg", self.model_overlays.get(role, frame))
            write_image(folder / "rules" / f"{role}.jpg", self.rule_overlays.get(role, frame))
        (folder / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        write_sample_html(folder, report)
        self.status_var.set(f"Отчёт сохранён: {folder}")
        messagebox.showinfo("Отчёт сохранён", str(folder / "report.html"))


def run_interactive(args) -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise RuntimeError("Tkinter недоступен в этой установке Python") from exc

    if args.input:
        folder = Path(args.input).expanduser().resolve()
        if not folder.is_dir():
            raise NotADirectoryError(f"Папка не найдена: {folder}")
    else:
        picker = tk.Tk()
        picker.withdraw()
        selected = filedialog.askdirectory(title="Выберите папку с изображениями")
        picker.destroy()
        if not selected:
            return 0
        folder = Path(selected).resolve()

    previous_cwd = Path.cwd()
    os.chdir(ROOT)
    try:
        root = tk.Tk()
        InteractiveAnalyzer(root, folder, args)
        root.mainloop()
    finally:
        os.chdir(previous_cwd)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Проверить сохранённые изображения production-моделями и rules без оборудования."
    )
    parser.add_argument("input", nargs="?", help="Папка с изображениями или manifest JSON")
    parser.add_argument(
        "--batch", action="store_true",
        help="Пакетный режим без графического окна",
    )
    parser.add_argument(
        "--image", action="append", default=[], metavar="ROLE=PATH",
        help="Явно указать изображение роли; параметр можно повторять",
    )
    parser.add_argument(
        "--folder-role",
        choices=ROLES,
        help=(
            "Считать каждый файл входной папки отдельным кадром указанной роли; "
            "используется с --batch для подбора порогов"
        ),
    )
    parser.add_argument("-o", "--output", default=str(DEFAULT_OUTPUT), help="Папка результатов")
    parser.add_argument("--device", default="cpu", help="Устройство Ultralytics: cpu, 0, cuda:0")
    parser.add_argument("--skip-warmup", action="store_true", help="Не прогревать модели")
    parser.add_argument("--allow-size-mismatch", action="store_true", help="Разрешить изображения не 1280x720")
    parser.add_argument("--allow-near-black", action="store_true", help="Разрешить почти чёрные изображения")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    input_is_manifest = bool(
        args.input and Path(args.input).suffix.lower() == ".json"
    )
    if args.folder_role:
        if not args.batch:
            parser.error("--folder-role используется вместе с --batch")
        if args.image:
            parser.error("--folder-role нельзя совмещать с --image")
        if not args.input or input_is_manifest:
            parser.error("Для --folder-role укажите папку с изображениями")
    if not args.batch and not args.image and not input_is_manifest:
        return run_interactive(args)

    if args.folder_role:
        samples = discover_folder_as_role(Path(args.input), args.folder_role)
    else:
        samples = resolve_samples(args.input, args.image)
    output_root = Path(args.output).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    print(f"Найдено комплектов: {len(samples)}")
    for sample in samples:
        print(f"  {sample.name}: {', '.join(sample.images)}")

    previous_cwd = Path.cwd()
    os.chdir(ROOT)
    try:
        from core.decision_engine import DecisionEngine
        from domain.threshold_loader import ThresholdLoader
        from vision.vision_cluster import VisionCluster

        print(f"Загрузка моделей на устройстве {args.device}...")
        vision = VisionCluster(device=args.device)
        if not args.skip_warmup:
            vision.warmup()
        thresholds = ThresholdLoader(ROOT / "thresholds.json").get_all()
        decision = DecisionEngine(thresholds=thresholds)

        reports = []
        for index, sample in enumerate(samples, start=1):
            print(f"[{index}/{len(samples)}] {sample.name}")
            try:
                report = analyze_sample(
                    sample,
                    output_root,
                    vision,
                    decision,
                    allow_size_mismatch=args.allow_size_mismatch,
                    allow_near_black=args.allow_near_black,
                )
            except Exception as exc:
                report = {
                    "sample": sample.name,
                    "ok": False,
                    "roles": [],
                    "models": [],
                    "rules": [],
                    "skipped_rules": [],
                    "rule_errors": [],
                    "defects": [],
                    "category": "UNKNOWN",
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_ms": 0,
                }
                sample_output = output_root / safe_name(sample.name)
                sample_output.mkdir(parents=True, exist_ok=True)
                (sample_output / "report.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                write_sample_html(sample_output, report)
            reports.append(report)
            print(f"  {_status_text(report)}; report: {safe_name(sample.name)}/report.html")
        write_index(output_root, reports)
    finally:
        os.chdir(previous_cwd)

    failed = [report for report in reports if not report.get("ok")]
    print(f"Общий отчёт: {output_root / 'index.html'}")
    if failed:
        print(f"Комплектов с ошибками: {len(failed)}")
        return 1
    print("Проверка завершена без ошибок выполнения")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
