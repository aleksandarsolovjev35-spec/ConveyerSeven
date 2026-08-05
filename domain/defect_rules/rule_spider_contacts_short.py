import cv2
import numpy as np
from itertools import combinations

from domain.defect_rules.base import BaseRule, RuleResult
from domain.defect_rules.omission_reference import (
    fit_omission_top_line,
    signed_distance_and_projection,
)


TOP_PERCENTILE = 10.0
BOTTOM_PERCENTILE = 90.0


class SpiderContactsShortRule(BaseRule):
    """Короткие контакты: собственная геометрия + наклон к omission-short."""

    name = "contacts_short"
    ROLES = ("SPIDER_IN", "SPIDER_OUT")
    TARGET_CLASS = "flatness_short"
    OMISSION_CLASS = "omission-short"

    def check(self, vision_results: dict, **kwargs) -> RuleResult:
        if not self.enabled:
            return self._make_skip(self.name)

        drawings = []
        triggered = False
        details_per_role = {}

        for role in self.ROLES:
            if role not in vision_results:
                continue

            min_conf = self._get("spider_contacts_short_min_confidence", 0.3, role=role)
            expected_count = self._get("spider_contacts_short_expected_count", 2, role=role)
            # Алгоритм сравнивает строго пару p_a/p_b; другое количество
            # нельзя трактовать как настраиваемый допуск.
            if type(expected_count) is not int or expected_count != 2:
                raise ValueError(
                    f"{role}.spider_contacts_short_expected_count "
                    "должен быть равен 2 (пара контактов)"
                )
            level_dev_ratio = self._get("spider_contacts_short_level_deviation_ratio", 0.20, role=role)
            rect_width_px = self._get(
                "spider_contacts_short_inscribed_rect_width_px", 25.2, role=role,
            )
            rect_height_px = self._get(
                "spider_contacts_short_inscribed_rect_height_px", 9.6, role=role,
            )
            area_absolute_min = self._get("spider_contacts_short_area_absolute_min", 400, role=role)
            y_filter_ratio = self._get("spider_contacts_short_y_filter_ratio", 3.0, role=role)
            omission_min_conf = self._get(
                "spider_short_omission_min_confidence", 0.3, role=role,
            )
            omission_tilt_ratio_max = self._get(
                "spider_contacts_short_omission_tilt_ratio_max", 0.20,
                role=role,
            )

            candidates = [
                d for d in vision_results[role]
                if d["class"] == self.TARGET_CLASS and d["confidence"] >= min_conf
            ]
            omissions = [
                d for d in vision_results[role]
                if d["class"] == self.OMISSION_CLASS
                and d["confidence"] >= omission_min_conf
            ]

            role_result = self._check_role(
                role=role, candidates=candidates, omissions=omissions,
                expected_count=expected_count, level_dev_ratio=level_dev_ratio,
                rect_width_px=rect_width_px, rect_height_px=rect_height_px,
                area_absolute_min=area_absolute_min,
                y_filter_ratio=y_filter_ratio,
                omission_tilt_ratio_max=omission_tilt_ratio_max,
                drawings=drawings,
            )

            if role_result["triggered"]:
                triggered = True
            details_per_role[role] = role_result

        return RuleResult(self.name, triggered,
            details={"per_role": details_per_role}, drawings=drawings)

    def _check_role(self, role, candidates, omissions, expected_count,
                    level_dev_ratio, rect_width_px, rect_height_px,
                    area_absolute_min, y_filter_ratio,
                    omission_tilt_ratio_max, drawings):
        found_raw = len(candidates)
        selected, ignored, filter_note = self._select_pair(
            candidates, expected_count, area_absolute_min, y_filter_ratio)
        found = len(selected)

        for det in ignored:
            drawings.append({"type": "contacts_short_ignored", "role": role,
                "bbox": det["bbox"], "mask": det.get("mask"), "triggered": False})

        if found != expected_count:
            ordered_found = sorted(
                selected,
                key=lambda detection: self._bbox_center_x(detection["bbox"]),
            )
            for index, detection in enumerate(ordered_found, start=1):
                drawings.append({
                    "type": "contacts_short_count_item",
                    "role": role,
                    "bbox": detection["bbox"],
                    "mask": detection.get("mask"),
                    "index": index,
                    "triggered": True,
                })
            ordered_candidates = sorted(
                candidates,
                key=lambda detection: self._bbox_center_x(detection["bbox"]),
            )
            invalid_candidates = [
                (index, detection)
                for index, detection in enumerate(ordered_candidates, start=1)
                if self._mask_points(detection) is None
            ]
            for index, detection in invalid_candidates:
                drawings.append({
                    "type": "contacts_short_invalid_mask",
                    "role": role,
                    "bbox": detection["bbox"],
                    "mask": detection.get("mask"),
                    "index": index,
                    "triggered": True,
                })
                drawings.append({
                    "type": "construction_error",
                    "role": role,
                    "bbox": detection["bbox"],
                    "message": f"NO CONTACT MASK #{index}",
                    "triggered": True,
                })
            drawings.append({
                "type": "construction_error",
                "role": role,
                "bbox": self._combined_bbox(candidates),
                "message": f"CONTACTS {found}/{expected_count}",
                "slot": 1 if invalid_candidates else 0,
                "triggered": True,
            })
            return {
                "triggered": True,
                "reason": f"wrong_count: {found}/{expected_count}",
                "found": found,
                "found_raw": found_raw,
                "ignored": len(ignored),
                "filter_note": filter_note,
                "area_absolute_min_px2": area_absolute_min,
                "invalid_mask_indices": [
                    index for index, _detection in invalid_candidates
                ],
                "items": [],
            }

        candidates_sorted = sorted(
            selected,
            key=lambda detection: self._bbox_center_x(detection["bbox"]),
        )
        invalid_mask_indices = [
            index
            for index, detection in enumerate(candidates_sorted, start=1)
            if self._mask_points(detection) is None
        ]
        if invalid_mask_indices:
            for index in invalid_mask_indices:
                detection = candidates_sorted[index - 1]
                drawings.append({
                    "type": "contacts_short_invalid_mask",
                    "role": role,
                    "bbox": detection["bbox"],
                    "mask": detection.get("mask"),
                    "index": index,
                    "triggered": True,
                })
                drawings.append({
                    "type": "construction_error",
                    "role": role,
                    "bbox": detection["bbox"],
                    "message": f"NO CONTACT MASK #{index}",
                    "triggered": True,
                })
            return {
                "triggered": True,
                "reason": "invalid_contact_masks",
                "invalid_mask_indices": invalid_mask_indices,
                "found": found,
                "found_raw": found_raw,
                "ignored": len(ignored),
                "filter_note": filter_note,
                "area_absolute_min_px2": area_absolute_min,
                "items": [],
            }

        params = [self._extract_params(d) for d in candidates_sorted]
        p_a, p_b = params[0], params[1]

        median_height = max(1.0, float(np.median([p_a["height"], p_b["height"]])))
        tolerance = median_height * level_dev_ratio

        # Проверка 1: ровность между собой
        delta_top = abs(p_a["top_y"] - p_b["top_y"])
        delta_bottom = abs(p_a["bottom_y"] - p_b["bottom_y"])
        delta_height = abs(p_a["height"] - p_b["height"])
        top_fail = delta_top > tolerance
        bottom_fail = delta_bottom > tolerance
        height_fail = delta_height > tolerance
        level_fail = top_fail or bottom_fail or height_fail

        # Проверка 2: наклон относительно верхней линии omission
        omission_tilt_check = self._check_omission_tilt(
            omissions=omissions,
            contact_a=p_a,
            contact_b=p_b,
            median_contact_height=median_height,
            ratio_max=omission_tilt_ratio_max,
        )
        omission_reference_fail = omission_tilt_check["status"] == "error"
        omission_tilt_fail = omission_tilt_check["status"] == "fail"
        omission_fail = omission_reference_fail or omission_tilt_fail

        # Проверка 3: вписываемость эталонного прямоугольника,
        # размер задан непосредственно в пикселях.
        expected_height_px = float(rect_height_px)
        expected_width_px = float(rect_width_px)
        inscribe_fail_indices = []
        inscribe_results = []

        for i, det in enumerate(candidates_sorted):
            res = self._try_inscribe_in_contact(
                det, expected_height_px, expected_width_px, 0.0,
            )
            inscribe_results.append({"index": i, "fits": res["fits"], "points": res.get("points"), "center": res.get("center")})
            if not res["fits"]:
                inscribe_fail_indices.append(i)

        inscribe_check = {
            "status": "ok" if not inscribe_fail_indices else "fail",
            "rect_width_px": round(expected_width_px, 1),
            "rect_height_px": round(expected_height_px, 1),
            "fails": len(inscribe_fail_indices),
        }

        inscribe_fail = inscribe_check["status"] == "fail"

        # Уровень определяется центрами вписанных эталонных прямоугольников,
        # а не границами исходной segmentation mask. Размер/форма контакта
        # остаются отдельной проверкой inscribe_fail.
        rect_centers = [res.get("center") for res in inscribe_results]
        if all(center is not None for center in rect_centers):
            rect_center_delta_y = abs(
                float(rect_centers[0][1]) - float(rect_centers[1][1])
            )
            level_tolerance = tolerance
            level_fail = rect_center_delta_y > level_tolerance
        else:
            rect_center_delta_y = None
            level_tolerance = tolerance
        role_triggered = level_fail or omission_fail or inscribe_fail

        # Отрисовка
        omission_distances = {}
        if omission_tilt_check.get("status") != "error":
            omission_distances = {
                1: float(omission_tilt_check["contact_a"]["distance_px"]),
                2: float(omission_tilt_check["contact_b"]["distance_px"]),
            }
        items = []
        for i, (det, parameters) in enumerate(
            zip(candidates_sorted, params, strict=True),
            start=1,
        ):
            array_index = i - 1
            rect_fits = bool(
                inscribe_results
                and inscribe_results[array_index]["fits"]
            )
            failures = [] if rect_fits else ["size"]
            drawings.append({
                "type": "contacts_short_item",
                "role": role,
                "bbox": det["bbox"],
                "mask": det.get("mask"),
                "index": i,
                "top_y": int(parameters["top_y"]),
                "bottom_y": int(parameters["bottom_y"]),
                "failures": failures,
                "triggered": bool(failures),
            })
            drawings.append({
                "type": "contacts_short_height_segment",
                "role": role,
                "x": int(parameters["center_x"]),
                "y_top": int(parameters["top_y"]),
                "y_bottom": int(parameters["bottom_y"]),
                "triggered": height_fail,
            })
            if (
                inscribe_results
                and inscribe_results[array_index].get("points") is not None
            ):
                if inscribe_results[array_index].get("center") is not None:
                    drawings.append({
                        "type": "contacts_short_level_center",
                        "role": role,
                        "center": inscribe_results[array_index]["center"],
                        "triggered": bool(level_fail),
                    })
                drawings.append({
                    "type": "contacts_short_inscribed_rect",
                    "role": role,
                    "points": inscribe_results[array_index]["points"],
                    "fits": inscribe_results[array_index]["fits"],
                    "index": i,
                })
            items.append({
                "index": i,
                "top_y": round(float(parameters["top_y"]), 3),
                "bottom_y": round(float(parameters["bottom_y"]), 3),
                "height_px": round(float(parameters["height"]), 3),
                "rect_fits": rect_fits,
                "omission_distance_px": (
                    round(omission_distances[i], 3)
                    if i in omission_distances else None
                ),
                "failures": failures,
            })

        # Линии
        x_left = min(p_a["bbox_x1"], p_b["bbox_x1"]) - 40
        x_right = max(p_a["bbox_x2"], p_b["bbox_x2"]) + 40

        # Показываем ту же геометрию, по которой принимается решение:
        # линию между центрами вписанных прямоугольников.
        if all(center is not None for center in rect_centers):
            center_a, center_b = rect_centers
            drawings.append({
                "type": "contacts_short_level_line", "role": role,
                "x_start": int(x_left), "x_end": int(x_right),
                "y_a": int(round(center_a[1])),
                "y_b": int(round(center_b[1])),
                "x_a": int(round(center_a[0])),
                "x_b": int(round(center_b[0])),
                "label": "C",
                "delta": round(float(rect_center_delta_y or 0), 1),
                "tolerance": round(float(level_tolerance), 1),
                "triggered": level_fail,
            })

        # Опорная линия omission и расстояния контактов
        if omission_tilt_check["status"] != "error":
            drawings.append({
                "type": "contacts_short_omission_line",
                "role": role,
                "x_start": omission_tilt_check["x_start"],
                "y_start": omission_tilt_check["y_start"],
                "x_end": omission_tilt_check["x_end"],
                "y_end": omission_tilt_check["y_end"],
                "triggered": omission_tilt_fail,
            })
            for contact in (
                omission_tilt_check["contact_a"],
                omission_tilt_check["contact_b"],
            ):
                drawings.append({
                    "type": "contacts_short_omission_distance",
                    "role": role,
                    "contact_point": contact["point"],
                    "projection_point": contact["projection"],
                    "distance_px": contact["distance_px"],
                    "triggered": omission_tilt_fail,
                })
        else:
            missing_bbox = self._combined_bbox(candidates_sorted)
            drawings.append({
                "type": "contacts_short_omission_missing",
                "role": role,
                "bbox": missing_bbox,
                "triggered": True,
            })
            drawings.append({
                "type": "construction_error",
                "role": role,
                "bbox": missing_bbox,
                "message": "NO OMISSION",
                "triggered": True,
            })

        return {
            "triggered": role_triggered,
            "reason": None,
            "found": found,
            "found_raw": found_raw,
            "ignored": len(ignored),
            "filter_note": filter_note,
            "area_absolute_min_px2": area_absolute_min,
            "median_contact_height_px": round(median_height, 3),
            "delta_top": round(delta_top, 3),
            "delta_bottom": round(delta_bottom, 3),
            "delta_height": round(delta_height, 3),
            "tolerance": round(tolerance, 3),
            "top_fail": top_fail, "bottom_fail": bottom_fail,
            "height_fail": height_fail, "level_fail": level_fail,
            "rect_center_delta_y": (
                round(float(rect_center_delta_y), 3)
                if rect_center_delta_y is not None else None
            ),
            "rect_center_level_tolerance": round(float(level_tolerance), 3),
            "omission_fail": omission_fail,
            "omission_reference_fail": omission_reference_fail,
            "omission_tilt_fail": omission_tilt_fail,
            "omission_tilt_ratio_max": omission_tilt_ratio_max,
            "omission_tilt_check": omission_tilt_check,
            "inscribe_fail": inscribe_fail,
            "inscribe_check": inscribe_check,
            "rect_width_px": rect_width_px,
            "rect_height_px": rect_height_px,
            "items": items,
        }

    # Верхняя опорная линия omission

    @classmethod
    def _check_omission_tilt(
        cls,
        *,
        omissions,
        contact_a,
        contact_b,
        median_contact_height,
        ratio_max,
    ):
        reference = fit_omission_top_line(
            omissions,
            x_start=min(contact_a["bbox_x1"], contact_b["bbox_x1"]),
            x_end=max(contact_a["bbox_x2"], contact_b["bbox_x2"]),
        )
        if reference is None:
            return {
                "status": "error",
                "reason": "no_valid_omission_top_line",
                "distance_delta_ratio": None,
                "ratio_max": ratio_max,
            }

        slope, intercept = reference["line"]
        point_a = (float(contact_a["center_x"]), float(contact_a["top_y"]))
        point_b = (float(contact_b["center_x"]), float(contact_b["top_y"]))
        distance_a, projection_a = signed_distance_and_projection(
            point_a, slope, intercept,
        )
        distance_b, projection_b = signed_distance_and_projection(
            point_b, slope, intercept,
        )
        delta_px = abs(distance_a - distance_b)
        ratio = delta_px / max(1.0, float(median_contact_height))
        failed = ratio > ratio_max
        x_line_start = float(reference["x_start"])
        x_line_end = float(reference["x_end"])
        return {
            "status": "fail" if failed else "ok",
            "reason": None,
            "valid_points": reference["valid_points"],
            "sample_points": reference["sample_points"],
            "slope": round(float(slope), 8),
            "intercept": round(float(intercept), 3),
            "angle_deg": round(float(np.degrees(np.arctan(slope))), 3),
            "x_start": int(round(x_line_start)),
            "y_start": int(round(slope * x_line_start + intercept)),
            "x_end": int(round(x_line_end)),
            "y_end": int(round(slope * x_line_end + intercept)),
            "contact_a": {
                "point": [round(point_a[0], 3), round(point_a[1], 3)],
                "projection": [
                    round(projection_a[0], 3), round(projection_a[1], 3),
                ],
                "distance_px": round(float(distance_a), 3),
            },
            "contact_b": {
                "point": [round(point_b[0], 3), round(point_b[1], 3)],
                "projection": [
                    round(projection_b[0], 3), round(projection_b[1], 3),
                ],
                "distance_px": round(float(distance_b), 3),
            },
            "distance_delta_px": round(float(delta_px), 3),
            "median_contact_height_px": round(float(median_contact_height), 3),
            "distance_delta_ratio": round(float(ratio), 6),
            "ratio_max": round(float(ratio_max), 6),
        }

    # Вписываемость

    @staticmethod
    def _try_inscribe_in_contact(det, expected_short_px, expected_long_px, common_angle):
        mask = det.get("mask")
        if not mask or len(mask) < 3: return {"fits": False}

        pts = np.array(mask, dtype=np.float32)
        x_min, x_max = float(pts[:,0].min()), float(pts[:,0].max())
        y_min, y_max = float(pts[:,1].min()), float(pts[:,1].max())
        cx, cy = (x_min+x_max)/2, (y_min+y_max)/2

        max_dim = max(x_max-x_min, y_max-y_min, expected_long_px)
        pad = int(max_dim*0.6)+20; cs = int(max_dim)+2*pad

        pl = pts - np.array([cx, cy], dtype=np.float32)
        pl[:,0] += cs/2; pl[:,1] += cs/2
        canvas = np.zeros((cs, cs), dtype=np.uint8)
        cv2.fillPoly(canvas, [pl.astype(np.int32)], 255)

        if abs(common_angle) > 0.01:
            M = cv2.getRotationMatrix2D((cs/2, cs/2), common_angle, 1.0)
            rotated = cv2.warpAffine(canvas, M, (cs, cs), flags=cv2.INTER_NEAREST, borderValue=0)
        else:
            rotated = canvas

        kh, kw = max(1, int(round(expected_short_px))), max(1, int(round(expected_long_px)))
        tcx, tcy = cs/2.0, cs/2.0

        hkh_lo, hkh_hi = kh//2, kh-kh//2
        hkw_lo, hkw_hi = kw//2, kw-kw//2
        y0, y1 = int(round(tcy-hkh_lo)), int(round(tcy+hkh_hi))
        x0, x1 = int(round(tcx-hkw_lo)), int(round(tcx+hkw_hi))

        fits_center = False
        if not (y0<0 or x0<0 or y1>rotated.shape[0] or x1>rotated.shape[1]):
            fits_center = bool(np.all(rotated[y0:y1, x0:x1] == 255))

        if fits_center:
            fits, fcx, fcy = True, tcx, tcy
        else:
            if kh > rotated.shape[0] or kw > rotated.shape[1]:
                fits, fcx, fcy = False, tcx, tcy
            else:
                kernel = np.ones((kh, kw), dtype=np.uint8)
                eroded = cv2.erode(rotated, kernel, iterations=1)
                ye, xe = np.where(eroded > 0)
                if len(xe) > 0:
                    dx = xe.astype(np.float32)-tcx; dy = ye.astype(np.float32)-tcy
                    bi = int(np.argmin(dx*dx+dy*dy))
                    fits, fcx, fcy = True, float(xe[bi]), float(ye[bi])
                else:
                    fits, fcx, fcy = False, tcx, tcy

        hw, hh = expected_long_px/2, expected_short_px/2
        cr = np.array([[fcx-hw,fcy-hh],[fcx+hw,fcy-hh],[fcx+hw,fcy+hh],[fcx-hw,fcy+hh]], dtype=np.float32)

        if abs(common_angle) > 0.01:
            Mi = cv2.getRotationMatrix2D((cs/2, cs/2), -common_angle, 1.0)
            cl = (Mi @ np.hstack([cr, np.ones((4,1),dtype=np.float32)]).T).T
        else:
            cl = cr
        cl[:,0] -= cs/2; cl[:,1] -= cs/2; cl[:,0] += cx; cl[:,1] += cy

        return {
            "fits": fits,
            "points": cl.astype(np.int32).tolist(),
            # Центр фактически вписанного прямоугольника, включая возможный
            # сдвиг, найденный через erosion.
            "center": (
                float(np.mean(cl[:, 0])),
                float(np.mean(cl[:, 1])),
            ),
        }

    # Отбор пары

    @classmethod
    def _select_pair(cls, candidates, expected, area_absolute_min, y_filter_ratio):
        n = len(candidates)
        if n == 0:
            return [], [], "no detections"

        pa = [cls._extract_params_basic(d) for d in candidates]
        kept = [i for i in range(n) if pa[i]["area"] >= area_absolute_min]
        dropped = [i for i in range(n) if i not in kept]

        if len(kept) <= expected:
            return (
                [candidates[i] for i in kept],
                [candidates[i] for i in dropped],
                f"area-filter kept {len(kept)}/{n}",
            )

        cys = np.array([pa[i]["center_y"] for i in kept])
        hs = np.array([pa[i]["height"] for i in kept])
        my, mh = float(np.median(cys)), float(np.median(hs))
        yt = mh * y_filter_ratio

        yk = [i for i in kept if abs(pa[i]["center_y"]-my) <= yt]
        yd = [i for i in kept if i not in yk]

        if len(yk) < expected:
            return [candidates[i] for i in yk], [candidates[i] for i in range(n) if i not in yk], f"y-filter left only {len(yk)}"
        if len(yk) == expected:
            return [candidates[i] for i in yk], [candidates[i] for i in range(n) if i not in yk], f"filters dropped {len(dropped)+len(yd)}"

        best_pair, best_score = None, float("inf")
        for i, j in combinations(yk, 2):
            pi, pj = pa[i], pa[j]
            hm = max(1.0, (pi["height"]+pj["height"])/2)
            am = max(pi["area"], pj["area"])
            if am <= 0: continue
            sc = abs(pi["center_y"]-pj["center_y"])/hm + abs(pi["area"]-pj["area"])/am
            if sc < best_score: best_score, best_pair = sc, (i, j)

        if best_pair is None:
            s = [candidates[i] for i in yk[:expected]]
            return s, [c for i,c in enumerate(candidates) if i not in yk[:expected]], "fallback: first N"

        s = [candidates[i] for i in best_pair]
        g = [c for i,c in enumerate(candidates) if i not in best_pair]
        return s, g, f"area-drop={len(dropped)} y-drop={len(yd)} score={best_score:.3f}"

    # Helpers

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

    @staticmethod
    def _mask_points(det):
        mask = det.get("mask")
        if not mask or len(mask) < 3:
            return None
        points = np.asarray(mask, dtype=np.float32)
        if (
            points.ndim != 2
            or points.shape[1] != 2
            or len(points) < 3
            or not np.isfinite(points).all()
            or abs(float(cv2.contourArea(points))) <= 0.0
        ):
            return None
        return points

    @staticmethod
    def _bbox_center_x(bbox):
        return (bbox[0] + bbox[2]) / 2

    @classmethod
    def _extract_params_basic(cls, det):
        x1, y1, x2, y2 = det["bbox"]
        points = cls._mask_points(det)
        area = (
            abs(float(cv2.contourArea(points)))
            if points is not None else 0.0
        )
        return {
            "center_x": (x1 + x2) / 2,
            "center_y": (y1 + y2) / 2,
            "height": abs(y2 - y1),
            "area": area,
        }

    @classmethod
    def _extract_params(cls, det):
        x1, _y1, x2, _y2 = det["bbox"]
        width = abs(x2 - x1)
        points = cls._mask_points(det)
        if points is None:
            raise ValueError("valid contact segmentation mask required")
        ys = points[:, 1]
        top_y = float(np.percentile(ys, TOP_PERCENTILE))
        bottom_y = float(np.percentile(ys, BOTTOM_PERCENTILE))
        height = max(1.0, bottom_y-top_y)
        return {
            "top_y": top_y,
            "bottom_y": bottom_y,
            "center_y": (top_y+bottom_y)/2,
            "center_x": (x1+x2)/2,
            "height": height,
            "width": width,
            "bbox_x1": float(x1),
            "bbox_x2": float(x2),
        }
