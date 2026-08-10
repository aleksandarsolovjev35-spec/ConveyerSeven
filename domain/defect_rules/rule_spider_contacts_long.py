import cv2
import numpy as np
from domain.defect_rules.base import BaseRule, RuleResult
from domain.defect_rules.omission_reference import (
    fit_omission_top_line,
    fit_theil_sen_line,
    signed_distance_and_projection,
)

TOP_PERCENTILE = 10.0
BOTTOM_PERCENTILE = 90.0

# Опорная линия omission должна покрывать не меньше этой доли размаха
# ряда контактов. Иначе линия экстраполируется за пределы фит-участка и
# заслонка меряется по недостоверной опорной.
MIN_REFERENCE_COVERAGE = 0.5


class SpiderContactsLongRule(BaseRule):
    """Длинные контакты: ровность ряда по схеме «дымоход-заслонка».

    Опорная линия строится по верху полосы omission (контакты всегда под
    ней). От неё опускаются перпендикуляры («стены») до центров вписанных
    в контакты эталонных прямоугольников. Линия через эти центры —
    «заслонка». Ряд ровный, когда заслонка параллельна опорной (перепад
    заслонки на размахе ряда в px) и длины стен одинаковые (разброс
    расстояний в px). Наклон всей детали на вердикт не влияет: дымоход
    наклоняется вместе с деталью, а заслонка остаётся закрытой.
    """

    name = "contacts_long"
    ROLES = ("SPIDER_LEFT", "SPIDER_RIGHT")
    TARGET_CLASS = "contacts-long"
    OMISSION_CLASS = "omission-long"

    def check(self, vision_results, **kwargs):
        if not self.enabled:
            return self._make_skip(self.name)

        drawings = []
        triggered = False
        details_per_role = {}

        for role in self.ROLES:
            if role not in vision_results:
                continue

            min_conf = self._get("spider_contacts_long_min_confidence", 0.3, role=role)
            expected = self._get("spider_contacts_long_expected_count", 5, role=role)
            if type(expected) is not int or expected < 2:
                raise ValueError(
                    f"{role}.spider_contacts_long_expected_count "
                    "должен быть целым числом >= 2"
                )
            rect_width_px = self._get(
                "spider_contacts_long_inscribed_rect_width_px", 38.0, role=role,
            )
            rect_height_px = self._get(
                "spider_contacts_long_inscribed_rect_height_px", 18.0, role=role,
            )
            y_filter = self._get("spider_contacts_long_y_filter_ratio", 3.0, role=role)
            omission_min_conf = self._get(
                "spider_long_omission_min_confidence", 0.3, role=role,
            )
            damper_open_max_px = self._read_positive_px(
                role, "spider_contacts_long_damper_open_max_px",
            )
            gap_dev_max_px = self._read_positive_px(
                role, "spider_contacts_long_gap_dev_max_px",
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
                role, candidates, omissions, expected,
                rect_width_px, rect_height_px, y_filter,
                damper_open_max_px, gap_dev_max_px, drawings,
            )

            if role_result["triggered"]:
                triggered = True
            details_per_role[role] = role_result

        return RuleResult(
            self.name, triggered,
            details={"per_role": details_per_role},
            drawings=drawings,
        )

    def _read_positive_px(self, role, name):
        value = self._get(name, None, role=role)
        if (
            type(value) not in (int, float)
            or not np.isfinite(float(value))
            or float(value) <= 0.0
        ):
            raise ValueError(f"{role}.{name} должен быть числом > 0")
        return float(value)

    def _check_role(self, role, candidates, omissions, expected_count,
                    rect_width_px, rect_height_px,
                    y_filter_ratio, damper_open_max_px, gap_dev_max_px,
                    drawings):
        found_raw = len(candidates)

        selected, ignored, filter_note = self._select_contacts(
            candidates, expected_count, y_filter_ratio,
        )
        found = len(selected)

        for det in ignored:
            drawings.append({
                "type": "contacts_long_ignored", "role": role,
                "bbox": det["bbox"], "mask": det.get("mask"),
                "triggered": False,
            })

        if found != expected_count:
            ordered_found = sorted(
                selected,
                key=lambda detection: self._bbox_center_x(detection["bbox"]),
            )
            for index, det in enumerate(ordered_found, start=1):
                drawings.append({
                    "type": "contacts_long_count_item", "role": role,
                    "bbox": det["bbox"], "mask": det.get("mask"),
                    "index": index, "triggered": True,
                })
            drawings.append({
                "type": "construction_error",
                "role": role,
                "bbox": self._combined_bbox(ordered_found),
                "message": f"CONTACTS {found}/{expected_count}",
                "triggered": True,
            })
            return {
                "triggered": True,
                "reason": f"wrong_count: {found}/{expected_count}",
                "found": found, "found_raw": found_raw,
                "ignored": len(ignored), "filter_note": filter_note,
                "items": [],
            }

        sorted_dets = sorted(selected, key=lambda d: self._bbox_center_x(d["bbox"]))
        invalid_mask_indices = [
            index
            for index, detection in enumerate(sorted_dets, start=1)
            if self._mask_points(detection) is None
        ]
        if invalid_mask_indices:
            for index in invalid_mask_indices:
                detection = sorted_dets[index - 1]
                drawings.append({
                    "type": "contacts_long_invalid_mask",
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
                "items": [],
            }

        params = [self._extract_params(d) for d in sorted_dets]
        heights = np.array([p["height"] for p in params], dtype=np.float64)
        median_h = max(1.0, float(np.median(heights)))

        inscribe_check, inscribe_results, _fail_indices = self._run_inscribe(
            sorted_dets, rect_width_px, rect_height_px,
        )
        inscribe_fail = inscribe_check["status"] in ("fail", "error")
        rect_centers = [res.get("center") for res in inscribe_results]

        # ── Дымоход-заслонка ─────────────────────────────────────────
        damper = self._build_damper_check(
            omissions=omissions,
            rect_centers=rect_centers,
            contact_span=(
                min(float(d["bbox"][0]) for d in sorted_dets),
                max(float(d["bbox"][2]) for d in sorted_dets),
            ),
            damper_open_max_px=damper_open_max_px,
            gap_dev_max_px=gap_dev_max_px,
        )
        reference_missing = damper["status"] == "error"
        damper_fail = damper.get("damper_fail", False)
        gap_fail = damper.get("gap_fail", False)
        measure_fail = damper_fail or gap_fail
        role_triggered = reference_missing or measure_fail or inscribe_fail

        gap_by_index = {
            int(gap["index"]): gap for gap in damper.get("gaps", [])
        }
        straight_by_index = damper.get("straight_devs", {})

        items = []
        for i, det in enumerate(sorted_dets, start=1):
            array_index = i - 1
            rect_fits = bool(
                inscribe_results
                and inscribe_results[array_index]["fits"]
            )
            gap = gap_by_index.get(i)
            failures = []
            if not rect_fits:
                failures.append("size")
            if gap is not None and gap["fail"]:
                failures.append("gap")
            item_triggered = bool(failures)

            drawings.append({
                "type": "contacts_long_item", "role": role,
                "bbox": det["bbox"], "mask": det.get("mask"),
                "index": i,
                "failures": failures,
                "triggered": item_triggered,
            })
            if (
                inscribe_results
                and inscribe_results[array_index].get("points") is not None
            ):
                if inscribe_results[array_index].get("center") is not None:
                    drawings.append({
                        "type": "contacts_long_level_center",
                        "role": role,
                        "center": inscribe_results[array_index]["center"],
                        "triggered": item_triggered,
                    })
                drawings.append({
                    "type": "contacts_long_inscribed_rect", "role": role,
                    "points": inscribe_results[array_index]["points"],
                    "fits": inscribe_results[array_index]["fits"],
                    "index": i,
                })
            items.append({
                "index": i,
                "rect_fits": rect_fits,
                "gap_fail": bool(gap and gap["fail"]),
                "gap_deviation_px": (
                    round(gap["deviation_px"], 3) if gap else None
                ),
                "omission_distance_px": (
                    round(gap["distance_px"], 3) if gap else None
                ),
                "straight_dev_px": (
                    round(straight_by_index[i], 3)
                    if i in straight_by_index else None
                ),
                "failures": failures,
            })

        if not reference_missing:
            # Заслонка — устойчивая линия через центры эталонов.
            x_start = damper["damper_x_start"] - 40
            x_end = damper["damper_x_end"] + 40
            slope_c = damper["damper_line"][0]
            intercept_c = damper["damper_line"][1]
            drawings.append({
                "type": "contacts_long_fit_line", "role": role,
                "x_start": int(x_start),
                "x_end": int(x_end),
                "y_start": int(round(slope_c * x_start + intercept_c)),
                "y_end": int(round(slope_c * x_end + intercept_c)),
                "tolerance": 0,
                "label": "damper",
                "triggered": damper_fail,
            })
            # Крыша — опорная линия omission.
            drawings.append({
                "type": "contacts_long_omission_line",
                "role": role,
                "x_start": damper["x_start"],
                "y_start": damper["y_start"],
                "x_end": damper["x_end"],
                "y_end": damper["y_end"],
                "triggered": measure_fail,
            })
            # Стены — перпендикуляры от центров к опорной.
            for gap in damper["gaps"]:
                drawings.append({
                    "type": "contacts_long_omission_distance",
                    "role": role,
                    "contact_point": gap["point"],
                    "projection_point": gap["projection"],
                    "distance_px": gap["distance_px"],
                    "triggered": bool(gap["fail"]),
                })
        else:
            missing_bbox = self._combined_bbox(sorted_dets)
            drawings.append({
                "type": "contacts_long_omission_missing",
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
            "reason": damper.get("reason") if reference_missing else None,
            "found": found,
            "found_raw": found_raw,
            "ignored": len(ignored),
            "filter_note": filter_note,
            "median_contact_height_px": round(median_h, 3),
            "damper_open_px": damper.get("damper_open_px"),
            "damper_open_max_px": damper_open_max_px,
            "damper_fail": damper_fail,
            "gap_dev_px": damper.get("gap_dev_px"),
            "gap_dev_max_px": gap_dev_max_px,
            "gap_fail": gap_fail,
            "gap_median_px": damper.get("gap_median_px"),
            "damper_slope": damper.get("damper_slope"),
            "omission_slope": damper.get("omission_slope"),
            "reference_coverage": damper.get("reference_coverage"),
            "straight_dev_max_px": damper.get("straight_dev_max_px"),
            "omission_reference": damper.get("omission_reference"),
            "gaps": damper.get("gaps", []),
            # Общий флаг проверок уровня — для согласованности отчётов.
            "omission_fail": bool(measure_fail or reference_missing),
            "inscribe_fail": inscribe_fail,
            "inscribe_check": inscribe_check,
            "rect_width_px": rect_width_px,
            "rect_height_px": rect_height_px,
            "items": items,
        }

    # ─── Дымоход-заслонка ────────────────────────────────────────────

    @classmethod
    def _build_damper_check(
        cls,
        *,
        omissions,
        rect_centers,
        contact_span,
        damper_open_max_px,
        gap_dev_max_px,
    ):
        """Опорная линия omission + стены + соседние заслонки.

        Строит пять стен (перпендикуляры от центров эталонов к опорной
        линии omission). Для каждой соседней пары контактов «заслонка» —
        разница длин двух стен (|d_{i+1} − d_i|), как на короткой стороне.
        Брак, если хотя бы одна из четырёх соседних заслонок открыта больше
        ``damper_open_max_px`` — локальный скачок ряда. Общий наклон детали
        на вердикт не влияет: он вычитается опорной линией.

        Возвращает ``status``: ok / fail (заслонка открыта или стены
        разъехались) / error (опорную не построить: нет omission линии
        или она покрывает слишком малую долю размаха ряда).
        """
        x_start = float(contact_span[0])
        x_end = float(contact_span[1])
        reference = fit_omission_top_line(
            omissions, x_start=x_start, x_end=x_end,
        )
        if reference is None:
            return {"status": "error", "reason": "no_valid_omission_top_line"}

        all_points = (
            reference.get("all_sample_points") or reference["sample_points"]
        )
        coverage = 0.0
        if all_points:
            sample_xs = [float(point[0]) for point in all_points]
            span = max(1.0, x_end - x_start)
            coverage = (max(sample_xs) - min(sample_xs)) / span
        if coverage < MIN_REFERENCE_COVERAGE:
            return {
                "status": "error",
                "reason": "omission_reference_too_short",
                "reference_coverage": round(float(coverage), 3),
            }

        slope_o, intercept = reference["line"]
        centers = [
            (float(center[0]), float(center[1]))
            for center in rect_centers
            if center is not None
        ]
        xs = [center[0] for center in centers]
        ys = [center[1] for center in centers]

        gaps = []
        for index, point in enumerate(centers, start=1):
            distance, projection = signed_distance_and_projection(
                point, slope_o, intercept,
            )
            gaps.append({
                "index": index,
                "point": [round(point[0], 3), round(point[1], 3)],
                "projection": [
                    round(projection[0], 3), round(projection[1], 3),
                ],
                "distance_px": round(float(distance), 3),
            })

        # ── Соседние заслонки: |d_{i+1} − d_i| для каждой пары соседей ──
        # Как на короткой стороне, где пара — две стены и одна заслонка;
        # здесь контактов пять, поэтому соседних заслонок четыре.
        damper_open = 0.0
        for left, right in zip(gaps, gaps[1:], strict=False):
            damper_open = max(
                damper_open,
                abs(
                    float(left["distance_px"])
                    - float(right["distance_px"])
                ),
            )
        damper_fail = damper_open > damper_open_max_px

        # Отклонение каждой стены от медианы — оставлено информационно
        # (какой именно контакт виновен), на вердикт не влияет.
        distances = [gap["distance_px"] for gap in gaps]
        median_distance = float(np.median(distances)) if distances else 0.0
        for gap in gaps:
            deviation = gap["distance_px"] - median_distance
            gap["deviation_px"] = round(float(deviation), 3)
            gap["fail"] = bool(damper_fail)
        deviations = [abs(gap["deviation_px"]) for gap in gaps]
        gap_dev = max(deviations) if deviations else 0.0
        gap_fail = damper_fail

        center_x_start = min(xs) if xs else x_start
        center_x_end = max(xs) if xs else x_end
        span = max(1.0, center_x_end - center_x_start)
        slope_c, intercept_c = fit_theil_sen_line(
            np.asarray(xs, dtype=np.float64),
            np.asarray(ys, dtype=np.float64),
        )

        straight = np.abs(
            np.asarray(ys, dtype=np.float64)
            - (slope_c * np.asarray(xs, dtype=np.float64) + intercept_c)
        ) if xs else np.asarray([])
        straight_devs = {
            index: round(float(deviation), 3)
            for index, deviation in enumerate(straight, start=1)
        }

        return {
            "status": "fail" if damper_fail else "ok",
            "reason": None,
            "damper_fail": bool(damper_fail),
            "gap_fail": bool(gap_fail),
            "damper_open_px": round(float(damper_open), 3),
            "gap_dev_px": round(float(gap_dev), 3),
            "gap_median_px": round(float(median_distance), 3),
            "damper_slope": round(float(slope_c), 8),
            "omission_slope": round(float(slope_o), 8),
            "reference_coverage": round(float(coverage), 3),
            "damper_line": (float(slope_c), float(intercept_c)),
            "damper_x_start": float(center_x_start),
            "damper_x_end": float(center_x_end),
            "straight_dev_max_px": (
                round(float(np.max(straight)), 3) if len(straight) else None
            ),
            "straight_devs": straight_devs,
            "gaps": gaps,
            "omission_reference": {
                "angle_deg": round(
                    float(np.degrees(np.arctan(slope_o))), 3,
                ),
                "valid_points": reference["valid_points"],
            },
            "x_start": int(round(reference["x_start"])),
            "y_start": int(round(slope_o * reference["x_start"] + intercept)),
            "x_end": int(round(reference["x_end"])),
            "y_end": int(round(slope_o * reference["x_end"] + intercept)),
        }

    # ─── Вписывание эталона (без изменений) ──────────────────────────

    def _run_inscribe(self, sorted_dets, width_px, height_px):
        expected_height_px = float(height_px)
        expected_width_px = float(width_px)
        results = []
        fail_indices = []

        for i, det in enumerate(sorted_dets):
            res = self._try_inscribe(
                det, expected_height_px, expected_width_px, 0.0,
            )
            results.append({
                "index": i,
                "fits": res["fits"],
                "points": res.get("points"),
                "center": res.get("center"),
            })
            if not res["fits"]:
                fail_indices.append(i)

        check = {
            "status": "ok" if not fail_indices else "fail",
            "rect_width_px": round(expected_width_px, 1),
            "rect_height_px": round(expected_height_px, 1),
            "fails": len(fail_indices),
        }
        return check, results, fail_indices

    @staticmethod
    def _try_inscribe(det, expected_short_px, expected_long_px, common_angle):
        mask = det.get("mask")
        if mask is None or len(mask) < 3:
            return {"fits": False}

        pts = np.array(mask, dtype=np.float32)
        x_min, x_max = float(pts[:, 0].min()), float(pts[:, 0].max())
        y_min, y_max = float(pts[:, 1].min()), float(pts[:, 1].max())
        cx, cy = (x_min + x_max) / 2, (y_min + y_max) / 2

        max_dim = max(x_max - x_min, y_max - y_min, expected_long_px)
        pad = int(max_dim * 0.6) + 20
        cs = int(max_dim) + 2 * pad

        pl = pts - np.array([cx, cy], dtype=np.float32)
        pl[:, 0] += cs / 2
        pl[:, 1] += cs / 2

        canvas = np.zeros((cs, cs), dtype=np.uint8)
        cv2.fillPoly(canvas, [pl.astype(np.int32)], 255)

        if abs(common_angle) > 0.01:
            M = cv2.getRotationMatrix2D((cs / 2, cs / 2), common_angle, 1.0)
            rotated = cv2.warpAffine(canvas, M, (cs, cs),
                                     flags=cv2.INTER_NEAREST, borderValue=0)
        else:
            rotated = canvas

        kh = max(1, int(round(expected_short_px)))
        kw = max(1, int(round(expected_long_px)))
        tcx, tcy = cs / 2.0, cs / 2.0

        y0 = int(round(tcy - kh // 2))
        y1 = int(round(tcy + kh - kh // 2))
        x0 = int(round(tcx - kw // 2))
        x1 = int(round(tcx + kw - kw // 2))

        fits_center = False
        if y0 >= 0 and x0 >= 0 and y1 <= rotated.shape[0] and x1 <= rotated.shape[1]:
            fits_center = bool(np.all(rotated[y0:y1, x0:x1] == 255))

        if fits_center:
            fits, fcx, fcy = True, tcx, tcy
        elif kh > rotated.shape[0] or kw > rotated.shape[1]:
            fits, fcx, fcy = False, tcx, tcy
        else:
            kernel = np.ones((kh, kw), dtype=np.uint8)
            eroded = cv2.erode(rotated, kernel, iterations=1)
            ye, xe = np.where(eroded > 0)
            if len(xe) > 0:
                dx = xe.astype(np.float32) - tcx
                dy = ye.astype(np.float32) - tcy
                bi = int(np.argmin(dx * dx + dy * dy))
                fits, fcx, fcy = True, float(xe[bi]), float(ye[bi])
            else:
                fits, fcx, fcy = False, tcx, tcy

        hw, hh = expected_long_px / 2, expected_short_px / 2
        cr = np.array([
            [fcx - hw, fcy - hh], [fcx + hw, fcy - hh],
            [fcx + hw, fcy + hh], [fcx - hw, fcy + hh],
        ], dtype=np.float32)

        if abs(common_angle) > 0.01:
            Mi = cv2.getRotationMatrix2D((cs / 2, cs / 2), -common_angle, 1.0)
            cl = (Mi @ np.hstack([cr, np.ones((4, 1), dtype=np.float32)]).T).T
        else:
            cl = cr

        cl[:, 0] -= cs / 2
        cl[:, 1] -= cs / 2
        cl[:, 0] += cx
        cl[:, 1] += cy

        return {
            "fits": fits,
            "points": cl.astype(np.int32).tolist(),
            "center": (
                float(np.mean(cl[:, 0])),
                float(np.mean(cl[:, 1])),
            ),
        }

    # ─── Отбор кандидатов (без изменений) ────────────────────────────

    @classmethod
    def _select_contacts(cls, candidates, expected, y_filter_ratio):
        n = len(candidates)
        if n == 0:
            return [], [], "no detections"
        if n <= expected:
            return list(candidates), [], "no filtering needed"

        params = [cls._extract_params_basic(d) for d in candidates]
        center_ys = np.array([p["center_y"] for p in params])
        heights = np.array([p["height"] for p in params])
        median_y = float(np.median(center_ys))
        y_tol = float(np.median(heights)) * y_filter_ratio

        y_kept = [i for i in range(n) if abs(center_ys[i] - median_y) <= y_tol]
        y_drop = [i for i in range(n) if i not in y_kept]

        if len(y_kept) < expected:
            s = [candidates[i] for i in y_kept]
            g = [candidates[i] for i in y_drop]
            return s, g, f"y-filter left only {len(y_kept)}"
        if len(y_kept) == expected:
            s = [candidates[i] for i in y_kept]
            g = [candidates[i] for i in y_drop]
            return s, g, f"y-filter dropped {len(y_drop)}"

        kept_sorted = sorted(y_kept, key=lambda i: params[i]["center_x"])
        best_window, best_score = None, float("inf")
        for start in range(len(kept_sorted) - expected + 1):
            widx = kept_sorted[start:start + expected]
            wxs = [params[i]["center_x"] for i in widx]
            sp = np.diff(wxs)
            if len(sp) < 1:
                continue
            med = float(np.median(sp))
            if med <= 0:
                continue
            sc = float(np.max(np.abs(sp - med))) / med
            if sc < best_score:
                best_score, best_window = sc, widx

        if best_window is None:
            s = [candidates[i] for i in kept_sorted[:expected]]
            g = [c for i, c in enumerate(candidates) if i not in kept_sorted[:expected]]
            return s, g, "fallback: first N"

        s = [candidates[i] for i in best_window]
        g = [c for i, c in enumerate(candidates) if i not in best_window]
        note = f"y-drop={len(y_drop)} x-drop={len(kept_sorted) - expected} score={best_score:.2f}"
        return s, g, note

    # ─── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _bbox_center_x(bbox):
        return (bbox[0] + bbox[2]) / 2.0

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
        if mask is None or len(mask) < 3:
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
    def _extract_params_basic(det):
        b = det["bbox"]
        return {
            "center_x": (b[0] + b[2]) / 2,
            "center_y": (b[1] + b[3]) / 2,
            "height": abs(b[3] - b[1]),
        }

    @classmethod
    def _extract_params(cls, det):
        b = det["bbox"]
        x1, _y1, x2, _y2 = b
        w = abs(x2 - x1)
        points = cls._mask_points(det)
        if points is None:
            raise ValueError("valid contact segmentation mask required")
        ys = points[:, 1]
        ty = float(np.percentile(ys, TOP_PERCENTILE))
        by = float(np.percentile(ys, BOTTOM_PERCENTILE))
        h = max(1.0, by - ty)
        return {
            "top_y": ty, "bottom_y": by,
            "center_y": (ty + by) / 2, "center_x": (x1 + x2) / 2,
            "height": h, "width": w,
        }
