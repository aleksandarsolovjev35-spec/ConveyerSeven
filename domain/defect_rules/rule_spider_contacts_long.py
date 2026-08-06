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


class SpiderContactsLongRule(BaseRule):
    """Длинные контакты: собственная геометрия + наклон к omission-long."""

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
            line_dev = self._get("spider_contacts_long_line_deviation_ratio", 0.35, role=role)
            max_level_slope = self._get(
                "spider_contacts_long_max_level_slope", 0.10, role=role,
            )
            rect_width_px = self._get(
                "spider_contacts_long_inscribed_rect_width_px", 11.5, role=role,
            )
            rect_height_px = self._get(
                "spider_contacts_long_inscribed_rect_height_px", 8.6, role=role,
            )
            y_filter = self._get("spider_contacts_long_y_filter_ratio", 3.0, role=role)
            omission_min_conf = self._get(
                "spider_long_omission_min_confidence", 0.3, role=role,
            )
            omission_tilt_ratio_max = self._get(
                "spider_contacts_long_omission_tilt_ratio_max", 0.20,
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
                role, candidates, omissions, expected, line_dev,
                rect_width_px, rect_height_px, y_filter,
                omission_tilt_ratio_max, max_level_slope, drawings,
            )

            if role_result["triggered"]:
                triggered = True
            details_per_role[role] = role_result

        return RuleResult(
            self.name, triggered,
            details={"per_role": details_per_role},
            drawings=drawings,
        )

    def _check_role(self, role, candidates, omissions, expected_count,
                    line_dev_ratio, rect_width_px, rect_height_px,
                    y_filter_ratio, omission_tilt_ratio_max,
                    max_level_slope, drawings):
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

        xs = np.array([p["center_x"] for p in params], dtype=np.float64)
        ys_top = np.array([p["top_y"] for p in params], dtype=np.float64)
        ys_bot = np.array([p["bottom_y"] for p in params], dtype=np.float64)
        heights = np.array([p["height"] for p in params], dtype=np.float64)

        median_h = max(1.0, float(np.median(heights)))
        line_tol = median_h * line_dev_ratio

        line_top = self._fit_line(xs, ys_top)
        line_bot = self._fit_line(xs, ys_bot)

        devs_top = np.abs(ys_top - self._eval_line(line_top, xs))
        devs_bot = np.abs(ys_bot - self._eval_line(line_bot, xs))

        max_dev_top = float(np.max(devs_top)) if len(devs_top) else 0
        max_dev_bot = float(np.max(devs_bot)) if len(devs_bot) else 0
        line_fail = max_dev_top > line_tol or max_dev_bot > line_tol

        omission_tilt_check = self._check_omission_tilt(
            omissions=omissions,
            contacts=sorted_dets,
            contact_params=params,
            median_contact_height=median_h,
            ratio_max=omission_tilt_ratio_max,
        )
        omission_reference_fail = omission_tilt_check["status"] == "error"
        omission_tilt_fail = omission_tilt_check["status"] == "fail"
        omission_fail = omission_reference_fail or omission_tilt_fail

        inscribe_check, inscribe_results, _fail_indices = self._run_inscribe(
            sorted_dets, rect_width_px, rect_height_px,
        )
        inscribe_fail = inscribe_check["status"] in ("fail", "error")

        # После подтверждения формы уровень оценивается по центрам
        # вписанных эталонных прямоугольников, а не по краям mask.
        rect_centers = [res.get("center") for res in inscribe_results]
        if all(center is not None for center in rect_centers):
            center_xs = np.array([center[0] for center in rect_centers], dtype=np.float64)
            center_ys = np.array([center[1] for center in rect_centers], dtype=np.float64)
            line_center = self._fit_line(center_xs, center_ys)
            center_devs = np.abs(center_ys - self._eval_line(line_center, center_xs))
            center_slope = float(line_center[0])
            slope_fail = abs(center_slope) > max_level_slope
            line_fail = bool(np.max(center_devs) > line_tol or slope_fail)
        else:
            center_xs = center_ys = center_devs = None
            line_center = None
            center_slope = None
            slope_fail = False
        role_triggered = line_fail or omission_fail or inscribe_fail
        omission_distances = {
            int(contact["index"]): float(contact["distance_px"])
            for contact in omission_tilt_check.get("contacts", [])
        }
        items = []
        for i, (det, _p) in enumerate(
            zip(sorted_dets, params, strict=True),
            start=1,
        ):
            array_index = i - 1
            top_failed = bool(
                (center_devs[array_index] if center_devs is not None else devs_top[array_index])
                > line_tol
            )
            bottom_failed = False
            rect_fits = bool(
                inscribe_results
                and inscribe_results[array_index]["fits"]
            )
            failures = []
            if top_failed:
                failures.append("line_T")
            if bottom_failed:
                failures.append("line_B")
            if not rect_fits:
                failures.append("size")
            item_triggered = bool(failures)

            drawings.append({
                "type": "contacts_long_item", "role": role,
                "bbox": det["bbox"], "mask": det.get("mask"),
                "index": i,
                "dev_top": round(float(devs_top[array_index]), 3),
                "dev_bottom": round(float(devs_bot[array_index]), 3),
                "failures": failures, "triggered": item_triggered,
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
                        "triggered": bool(line_fail),
                    })
                drawings.append({
                    "type": "contacts_long_inscribed_rect", "role": role,
                    "points": inscribe_results[array_index]["points"],
                    "fits": inscribe_results[array_index]["fits"],
                    "index": i,
                })
            items.append({
                "index": i,
                "dev_top_px": round(float(
                    center_devs[array_index]
                    if center_devs is not None else devs_top[array_index]
                ), 3),
                "dev_bottom_px": 0.0,
                "top_fail": top_failed,
                "bottom_fail": bottom_failed,
                "rect_fits": rect_fits,
                "omission_distance_px": (
                    round(omission_distances[i], 3)
                    if i in omission_distances else None
                ),
                "failures": failures,
            })

        # Визуализируем ровно ту же линию, которая участвует в решении:
        # линию центров вписанных прямоугольников. Старые линии по верхнему
        # и нижнему краям segmentation mask больше не показываем.
        if line_center is not None:
            drawings.append({
                "type": "contacts_long_fit_line", "role": role,
                "x_start": int(np.min(center_xs) - 40),
                "x_end": int(np.max(center_xs) + 40),
                "y_start": int(self._eval_line(line_center, np.array([np.min(center_xs) - 40]))[0]),
                "y_end": int(self._eval_line(line_center, np.array([np.max(center_xs) + 40]))[0]),
                "tolerance": int(line_tol), "label": "center",
                "triggered": line_fail,
                "slope": center_slope,
                "max_slope": max_level_slope,
            })

        if omission_tilt_check["status"] != "error":
            drawings.append({
                "type": "contacts_long_omission_line",
                "role": role,
                "x_start": omission_tilt_check["x_start"],
                "y_start": omission_tilt_check["y_start"],
                "x_end": omission_tilt_check["x_end"],
                "y_end": omission_tilt_check["y_end"],
                "triggered": omission_tilt_fail,
            })
            for contact in omission_tilt_check["contacts"]:
                drawings.append({
                    "type": "contacts_long_omission_distance",
                    "role": role,
                    "contact_point": contact["point"],
                    "projection_point": contact["projection"],
                    "distance_px": contact["distance_px"],
                    "triggered": omission_tilt_fail,
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
            "reason": None,
            "found": found,
            "found_raw": found_raw,
            "ignored": len(ignored),
            "filter_note": filter_note,
            "median_contact_height_px": round(median_h, 3),
            "line_tolerance_px": round(line_tol, 3),
            "level_slope": (
                round(float(center_slope), 6)
                if center_slope is not None else None
            ),
            "max_level_slope": round(float(max_level_slope), 6),
            "slope_fail": slope_fail,
            "max_dev_top": round(max_dev_top, 3),
            "max_dev_bottom": round(max_dev_bot, 3),
            "line_fail": line_fail,
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

    @staticmethod
    def _check_omission_tilt(
        *,
        omissions,
        contacts,
        contact_params,
        median_contact_height,
        ratio_max,
    ):
        x_start = min(float(detection["bbox"][0]) for detection in contacts)
        x_end = max(float(detection["bbox"][2]) for detection in contacts)
        reference = fit_omission_top_line(
            omissions,
            x_start=x_start,
            x_end=x_end,
        )
        if reference is None:
            return {
                "status": "error",
                "reason": "no_valid_omission_top_line",
                "distance_trend_ratio": None,
                "ratio_max": ratio_max,
            }

        slope, intercept = reference["line"]
        measured_contacts = []
        xs = []
        distances = []
        for index, parameters in enumerate(contact_params, start=1):
            point = (
                float(parameters["center_x"]),
                float(parameters["top_y"]),
            )
            distance, projection = signed_distance_and_projection(
                point, slope, intercept,
            )
            xs.append(point[0])
            distances.append(distance)
            measured_contacts.append({
                "index": index,
                "point": [round(point[0], 3), round(point[1], 3)],
                "projection": [
                    round(projection[0], 3), round(projection[1], 3),
                ],
                "distance_px": round(float(distance), 3),
            })

        if len({round(value, 6) for value in xs}) < 2:
            return {
                "status": "error",
                "reason": "contact_span_too_small",
                "distance_trend_ratio": None,
                "ratio_max": ratio_max,
            }
        distance_slope, distance_intercept = fit_theil_sen_line(xs, distances)
        left_x = min(xs)
        right_x = max(xs)
        predicted_left = distance_slope * left_x + distance_intercept
        predicted_right = distance_slope * right_x + distance_intercept
        trend_delta_px = predicted_right - predicted_left
        ratio = abs(trend_delta_px) / max(1.0, float(median_contact_height))
        failed = ratio > ratio_max
        return {
            "status": "fail" if failed else "ok",
            "reason": None,
            "valid_points": reference["valid_points"],
            "sample_points": reference["sample_points"],
            "slope": round(float(slope), 8),
            "intercept": round(float(intercept), 3),
            "angle_deg": round(float(np.degrees(np.arctan(slope))), 3),
            "x_start": int(round(reference["x_start"])),
            "y_start": int(round(slope * reference["x_start"] + intercept)),
            "x_end": int(round(reference["x_end"])),
            "y_end": int(round(slope * reference["x_end"] + intercept)),
            "contacts": measured_contacts,
            "distance_trend_slope": round(float(distance_slope), 8),
            "predicted_left_distance_px": round(float(predicted_left), 3),
            "predicted_right_distance_px": round(float(predicted_right), 3),
            "distance_trend_delta_px": round(float(trend_delta_px), 3),
            "median_contact_height_px": round(float(median_contact_height), 3),
            "distance_trend_ratio": round(float(ratio), 6),
            "ratio_max": round(float(ratio_max), 6),
        }

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

    @staticmethod
    def _try_inscribe(det, expected_short_px, expected_long_px, common_angle):
        mask = det.get("mask")
        if not mask or len(mask) < 3:
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

    @staticmethod
    def _bbox_center_x(bbox):
        return (bbox[0] + bbox[2]) / 2.0

    @staticmethod
    def _fit_line(xs, ys):
        if len(xs) < 2:
            return 0.0, float(ys[0]) if len(ys) else 0.0
        a, b = np.polyfit(xs, ys, 1)
        return float(a), float(b)

    @staticmethod
    def _eval_line(line, xs):
        return line[0] * xs + line[1]

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