from __future__ import annotations

import math
from itertools import combinations

import numpy as np

from domain.defect_rules.base import BaseRule, RuleResult
from domain.defect_rules.top_geometry import (
    largest_valid_mask,
    mask_points,
    try_inscribe_center_then_nearest,
)


class TopContactsRule(BaseRule):
    name = "top_contacts"
    ROLES = ("TOP",)
    TARGET_CLASS = "contacts"
    PLATFORM_CLASS = "platform"
    EXPECTED_GROUPS = {"L": 5, "R": 5, "T": 2, "B": 2}

    def check(self, vision_results: dict, **kwargs) -> RuleResult:
        if not self.enabled:
            return self._make_skip(self.name)
        drawings = []
        per_role = {}
        triggered = False
        for role in self.ROLES:
            if role not in vision_results:
                continue
            min_conf = self._get("top_contacts_min_confidence", 0.3, role=role)
            expected_count = self._get(
                "top_contacts_expected_count", 14, role=role,
            )
            # Раскладка дальше по коду фиксирована: 5L + 5R + 2T + 2B.
            # Не принимаем иное значение как будто правило умеет динамически
            # перераспределить группы.
            if type(expected_count) is not int or expected_count != 14:
                raise ValueError(
                    f"{role}.top_contacts_expected_count должен быть равен 14 "
                    "(5L+5R+2T+2B)"
                )
            platform_min_conf = self._get(
                "top_contacts_platform_min_confidence", 0.3, role=role,
            )
            distance_ratio_max = self._get(
                "top_contacts_edge_distance_deviation_ratio", 0.4,
                role=role,
            )
            side_rect = (
                float(self._get(
                    "top_contacts_side_rect_width_px", 28, role=role,
                )),
                float(self._get(
                    "top_contacts_side_rect_height_px", 35, role=role,
                )),
            )
            edge_rect = (
                float(self._get(
                    "top_contacts_edge_rect_width_px", 30, role=role,
                )),
                float(self._get(
                    "top_contacts_edge_rect_height_px", 28, role=role,
                )),
            )
            detections = vision_results[role]
            contacts = [
                detection for detection in detections
                if detection.get("class") == self.TARGET_CLASS
                and float(detection.get("confidence", 0.0)) >= float(min_conf)
            ]
            platforms = [
                detection for detection in detections
                if detection.get("class") == self.PLATFORM_CLASS
                and float(detection.get("confidence", 0.0))
                >= float(platform_min_conf)
            ]
            result = self._check_role(
                role=role,
                contacts=contacts,
                platforms=platforms,
                expected_count=int(expected_count),
                distance_ratio_max=float(distance_ratio_max),
                side_rect=side_rect,
                edge_rect=edge_rect,
                drawings=drawings,
            )
            per_role[role] = result
            triggered = triggered or result["triggered"]
        return RuleResult(
            self.name,
            triggered,
            details={"per_role": per_role},
            drawings=drawings,
        )

    @classmethod
    def _check_role(
        cls,
        *,
        role,
        contacts,
        platforms,
        expected_count,
        distance_ratio_max,
        side_rect,
        edge_rect,
        drawings,
    ):
        found_raw = len(contacts)
        if found_raw < expected_count:
            cls._draw_count_items(contacts, role, drawings)
            drawings.append({
                "type": "construction_error",
                "role": role,
                "bbox": cls._combined_bbox(contacts),
                "message": f"CONTACTS {found_raw}/{expected_count}",
                "triggered": True,
            })
            return {
                "triggered": True,
                "reason": f"wrong_count: {found_raw}/{expected_count}",
                "found": found_raw,
                "found_raw": found_raw,
                "expected_count": expected_count,
                "selected": 0,
                "ignored": 0,
                "items": [],
            }

        valid_contacts = [
            detection for detection in contacts
            if mask_points(detection) is not None
        ]
        invalid_contacts = [
            detection for detection in contacts
            if mask_points(detection) is None
        ]
        if len(valid_contacts) < expected_count:
            cls._draw_count_items(valid_contacts, role, drawings)
            ordered_raw = sorted(contacts, key=cls._raw_sort_key)
            invalid_indices = []
            for raw_index, detection in enumerate(ordered_raw, start=1):
                if mask_points(detection) is not None:
                    continue
                invalid_indices.append(raw_index)
                drawings.append({
                    "type": "top_contacts_invalid_mask",
                    "role": role,
                    "bbox": detection.get("bbox") or [0, 0, 0, 0],
                    "mask": detection.get("mask"),
                    "index": raw_index,
                    "triggered": True,
                })
                drawings.append({
                    "type": "construction_error",
                    "role": role,
                    "bbox": detection.get("bbox") or [0, 0, 0, 0],
                    "message": f"NO CONTACT MASK #{raw_index}",
                    "triggered": True,
                })
            drawings.append({
                "type": "construction_error",
                "role": role,
                "bbox": cls._combined_bbox(contacts),
                "message": (
                    f"CONTACTS {len(valid_contacts)}/{expected_count}"
                ),
                "slot": 1 if invalid_indices else 0,
                "triggered": True,
            })
            return {
                "triggered": True,
                "reason": "insufficient_valid_contact_masks",
                "found": len(valid_contacts),
                "found_raw": found_raw,
                "expected_count": expected_count,
                "invalid_mask_indices": invalid_indices,
                "selected": 0,
                "ignored": len(invalid_contacts),
                "items": [],
            }

        platform = largest_valid_mask(platforms)
        if platform is None:
            cls._draw_count_items(valid_contacts, role, drawings)
            drawings.append({
                "type": "construction_error",
                "role": role,
                "bbox": cls._combined_bbox(valid_contacts),
                "message": "NO PLATFORM",
                "triggered": True,
            })
            return {
                "triggered": True,
                "reason": "no_valid_platform",
                "found": len(valid_contacts),
                "found_raw": found_raw,
                "expected_count": expected_count,
                "selected": 0,
                "ignored": len(invalid_contacts),
                "items": [],
            }
        platform_bbox = platform.get("bbox")
        if not _valid_bbox(platform_bbox):
            cls._draw_count_items(valid_contacts, role, drawings)
            drawings.append({
                "type": "construction_error",
                "role": role,
                "bbox": cls._combined_bbox(valid_contacts),
                "message": "NO PLATFORM BBOX",
                "triggered": True,
            })
            return {
                "triggered": True,
                "reason": "invalid_platform_bbox",
                "found": len(valid_contacts),
                "found_raw": found_raw,
                "expected_count": expected_count,
                "selected": 0,
                "ignored": len(invalid_contacts),
                "items": [],
            }

        drawings.append({
            "type": "top_contacts_platform_bbox",
            "role": role,
            "bbox": platform_bbox,
            "triggered": False,
        })
        grouped_candidates, unassigned = cls._group_candidates(
            valid_contacts,
            platform_bbox,
        )
        group_counts_raw = {
            group: len(grouped_candidates[group])
            for group in cls.EXPECTED_GROUPS
        }
        insufficient_groups = [
            group
            for group, expected in cls.EXPECTED_GROUPS.items()
            if len(grouped_candidates[group]) < expected
        ]
        if insufficient_groups:
            cls._draw_count_items(valid_contacts, role, drawings)
            counts_text = " ".join(
                f"{group}{group_counts_raw[group]}/{expected}"
                for group, expected in cls.EXPECTED_GROUPS.items()
            )
            drawings.append({
                "type": "construction_error",
                "role": role,
                "bbox": cls._combined_bbox(valid_contacts),
                "message": f"LAYOUT {counts_text}",
                "triggered": True,
            })
            return {
                "triggered": True,
                "reason": "layout_groups_failed",
                "found": len(valid_contacts),
                "found_raw": found_raw,
                "expected_count": expected_count,
                "group_counts": group_counts_raw,
                "insufficient_groups": insufficient_groups,
                "selected": 0,
                "ignored": len(invalid_contacts) + len(unassigned),
                "items": [],
            }

        selected_groups = {}
        non_selected = list(unassigned)
        for group, expected in cls.EXPECTED_GROUPS.items():
            selected, extras = cls._select_consistent_group(
                grouped_candidates[group],
                expected,
                group,
                platform_bbox,
            )
            selected_groups[group] = selected
            non_selected.extend(extras)
        non_selected.extend(invalid_contacts)
        selected_contacts = []
        for group in ("L", "R", "T", "B"):
            selected_contacts.extend(cls._sort_group(selected_groups[group], group))
        selected_ids = {id(detection) for detection in selected_contacts}
        ignored_unique = []
        seen = set()
        for detection in non_selected:
            marker = id(detection)
            if marker in selected_ids or marker in seen:
                continue
            seen.add(marker)
            ignored_unique.append(detection)
        for detection in ignored_unique:
            drawings.append({
                "type": "top_contacts_ignored",
                "role": role,
                "bbox": detection.get("bbox") or [0, 0, 0, 0],
                "mask": detection.get("mask"),
                "triggered": False,
            })

        group_checks = {}
        failed_groups = []
        item_rows = []
        item_index_by_id = {
            id(detection): index
            for index, detection in enumerate(selected_contacts, start=1)
        }
        distance_fail_ids = set()
        distance_data = {}
        for group in ("L", "R", "T", "B"):
            detections = cls._sort_group(selected_groups[group], group)
            group_parameters = [
                cls._contact_parameters(item) for item in detections
            ]
            distances = [
                cls._distance_to_side(item, group, platform_bbox)
                for item in group_parameters
            ]
            scale_key = "width" if group in ("L", "R") else "height"
            scale_values = [item[scale_key] for item in group_parameters]
            median_distance = float(np.median(distances))
            scale = max(1.0, float(np.median(scale_values)))
            allowed_deviation = scale * distance_ratio_max
            deviations = [
                abs(float(distance) - median_distance)
                for distance in distances
            ]
            failed = any(
                deviation > allowed_deviation for deviation in deviations
            )
            if failed:
                failed_groups.append(group)
            group_checks[group] = {
                "indices": [item_index_by_id[id(item)] for item in detections],
                "median_distance_px": round(median_distance, 3),
                "max_deviation_px": round(max(deviations, default=0.0), 3),
                "allowed_deviation_px": round(allowed_deviation, 3),
                "deviation_ratio_max": distance_ratio_max,
                "failed": failed,
            }
            drawings.append({
                "type": "top_contacts_group_reference",
                "role": role,
                "group": group,
                "line": cls._reference_line(
                    group,
                    platform_bbox,
                    median_distance,
                ),
                "triggered": failed,
            })
            for detection, item_parameters, distance, deviation in zip(
                detections,
                group_parameters,
                distances,
                deviations,
                strict=True,
            ):
                index = item_index_by_id[id(detection)]
                distance_failed = deviation > allowed_deviation
                if distance_failed:
                    distance_fail_ids.add(id(detection))
                start, end = cls._distance_segment(
                    item_parameters,
                    group,
                    platform_bbox,
                )
                drawings.append({
                    "type": "top_contacts_distance",
                    "role": role,
                    "group": group,
                    "index": index,
                    "start": start,
                    "end": end,
                    "triggered": distance_failed,
                })
                distance_data[id(detection)] = {
                    "distance_px": float(distance),
                    "deviation_px": float(deviation),
                    "allowed_deviation_px": float(allowed_deviation),
                    "distance_fail": distance_failed,
                }

        rectangle_fail_indices = []
        rectangle_results = {}
        for detection in selected_contacts:
            group = cls._group_for_detection(detection, selected_groups)
            width, height = side_rect if group in ("L", "R") else edge_rect
            fit = try_inscribe_center_then_nearest(
                detection,
                width_px=width,
                height_px=height,
                angle_deg=0.0,
            )
            index = item_index_by_id[id(detection)]
            rectangle_results[id(detection)] = {
                "width_px": width,
                "height_px": height,
                "fits": bool(fit["fits"]),
                "points": fit.get("points"),
            }
            if not fit["fits"]:
                rectangle_fail_indices.append(index)

        for detection in selected_contacts:
            marker = id(detection)
            index = item_index_by_id[marker]
            group = cls._group_for_detection(detection, selected_groups)
            rectangle = rectangle_results[marker]
            failures = []
            if marker in distance_fail_ids:
                failures.append("distance")
            if not rectangle["fits"]:
                failures.append("rectangle")
            drawings.append({
                "type": "top_contacts_item",
                "role": role,
                "bbox": detection.get("bbox") or [0, 0, 0, 0],
                "mask": detection.get("mask"),
                "index": index,
                "group": group,
                "failures": failures,
                "triggered": bool(failures),
            })
            if rectangle["points"] is not None:
                drawings.append({
                    "type": "top_contact_inscribed_rect",
                    "role": role,
                    "points": rectangle["points"],
                    "fits": rectangle["fits"],
                    "index": index,
                    "group": group,
                })
            item_rows.append({
                "index": index,
                "group": group,
                **{
                    key: round(value, 3) if isinstance(value, float) else value
                    for key, value in distance_data[marker].items()
                },
                "rect_width_px": rectangle["width_px"],
                "rect_height_px": rectangle["height_px"],
                "rect_fits": rectangle["fits"],
                "failures": failures,
            })

        item_rows.sort(key=lambda item: item["index"])
        triggered = bool(failed_groups or rectangle_fail_indices)
        return {
            "triggered": triggered,
            "reason": None,
            "found": len(valid_contacts),
            "found_raw": found_raw,
            "expected_count": expected_count,
            "selected": len(selected_contacts),
            "ignored": len(ignored_unique),
            "group_counts": {
                group: len(selected_groups[group])
                for group in cls.EXPECTED_GROUPS
            },
            "group_counts_raw": group_counts_raw,
            "group_checks": group_checks,
            "failed_groups": failed_groups,
            "rectangle_fail_indices": rectangle_fail_indices,
            "side_rect_px": [side_rect[0], side_rect[1]],
            "edge_rect_px": [edge_rect[0], edge_rect[1]],
            "items": item_rows,
            "ignored_platforms": max(0, len(platforms) - 1),
        }

    @classmethod
    def _group_candidates(cls, detections, platform_bbox):
        groups = {group: [] for group in cls.EXPECTED_GROUPS}
        unassigned = []
        for detection in detections:
            parameters = cls._contact_parameters(detection)
            candidates = cls._side_candidates(parameters, platform_bbox)
            if not candidates:
                unassigned.append(detection)
                continue
            _distance, group = min(candidates, key=lambda item: item[0])
            groups[group].append(detection)
        return groups, unassigned

    @classmethod
    def _select_consistent_group(
        cls,
        detections,
        expected,
        group,
        platform_bbox,
    ):
        if len(detections) == expected:
            return list(detections), []
        scored = []
        for subset in combinations(detections, expected):
            parameters = [cls._contact_parameters(item) for item in subset]
            distances = [
                cls._distance_to_side(item, group, platform_bbox)
                for item in parameters
            ]
            scale_values = [
                item["width" if group in ("L", "R") else "height"]
                for item in parameters
            ]
            median_distance = float(np.median(distances))
            scale = max(1.0, float(np.median(scale_values)))
            deviation_ratio = max(
                abs(float(distance) - median_distance)
                for distance in distances
            ) / scale
            mean_confidence = float(np.mean([
                float(item.get("confidence", 0.0)) for item in subset
            ]))
            scored.append((deviation_ratio, -mean_confidence, subset))
        _ratio, _negative_confidence, best = min(
            scored,
            key=lambda item: (item[0], item[1]),
        )
        selected_ids = {id(item) for item in best}
        extras = [item for item in detections if id(item) not in selected_ids]
        return list(best), extras

    @staticmethod
    def _sort_group(detections, group):
        axis = 1 if group in ("L", "R") else 0
        return sorted(
            detections,
            key=lambda detection: (
                float(detection["bbox"][axis])
                + float(detection["bbox"][axis + 2])
            ) / 2.0,
        )

    @staticmethod
    def _group_for_detection(detection, selected_groups):
        marker = id(detection)
        for group, detections in selected_groups.items():
            if any(id(item) == marker for item in detections):
                return group
        raise RuntimeError("selected contact has no group")

    @staticmethod
    def _side_candidates(parameters, platform_bbox):
        x1, y1, x2, y2 = map(float, platform_bbox)
        cx = parameters["center_x"]
        cy = parameters["center_y"]
        candidates = []
        if cx < x1:
            candidates.append((x1 - cx, "L"))
        if cx > x2:
            candidates.append((cx - x2, "R"))
        if cy < y1:
            candidates.append((y1 - cy, "T"))
        if cy > y2:
            candidates.append((cy - y2, "B"))
        return candidates

    @classmethod
    def _distance_to_side(cls, parameters, group, platform_bbox):
        candidates = {
            side: distance
            for distance, side in cls._side_candidates(parameters, platform_bbox)
        }
        return float(candidates[group])

    @staticmethod
    def _distance_segment(parameters, group, platform_bbox):
        x1, y1, x2, y2 = map(float, platform_bbox)
        cx = float(parameters["center_x"])
        cy = float(parameters["center_y"])
        if group == "L":
            return [cx, cy], [x1, cy]
        if group == "R":
            return [cx, cy], [x2, cy]
        if group == "T":
            return [cx, cy], [cx, y1]
        return [cx, cy], [cx, y2]

    @staticmethod
    def _reference_line(group, platform_bbox, median_distance):
        x1, y1, x2, y2 = map(float, platform_bbox)
        if group == "L":
            return [x1-median_distance, y1, x1-median_distance, y2]
        if group == "R":
            return [x2+median_distance, y1, x2+median_distance, y2]
        if group == "T":
            return [x1, y1-median_distance, x2, y1-median_distance]
        return [x1, y2+median_distance, x2, y2+median_distance]

    @staticmethod
    def _draw_count_items(contacts, role, drawings):
        for index, contact in enumerate(
            sorted(contacts, key=TopContactsRule._raw_sort_key),
            start=1,
        ):
            drawings.append({
                "type": "top_contacts_count_item",
                "role": role,
                "bbox": contact.get("bbox") or [0, 0, 0, 0],
                "mask": contact.get("mask"),
                "index": index,
                "triggered": True,
            })

    @staticmethod
    def _raw_sort_key(detection):
        bbox = detection.get("bbox") or [0, 0, 0, 0]
        return (
            (float(bbox[1]) + float(bbox[3])) / 2.0,
            (float(bbox[0]) + float(bbox[2])) / 2.0,
        )

    @staticmethod
    def _contact_parameters(contact):
        bbox = contact["bbox"]
        x1, y1, x2, y2 = map(float, bbox)
        return {
            "center_x": (x1+x2)/2.0,
            "center_y": (y1+y2)/2.0,
            "width": abs(x2-x1),
            "height": abs(y2-y1),
        }

    @staticmethod
    def _combined_bbox(detections):
        boxes = [detection.get("bbox") for detection in detections]
        boxes = [box for box in boxes if box and len(box) == 4]
        if not boxes:
            return [0, 0, 0, 0]
        return [
            min(float(box[0]) for box in boxes),
            min(float(box[1]) for box in boxes),
            max(float(box[2]) for box in boxes),
            max(float(box[3]) for box in boxes),
        ]


def _valid_bbox(bbox):
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return False
    if any(
        type(value) not in (int, float) or not math.isfinite(float(value))
        for value in bbox
    ):
        return False
    return float(bbox[2]) > float(bbox[0]) and float(bbox[3]) > float(bbox[1])
