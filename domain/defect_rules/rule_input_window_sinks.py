import cv2
import numpy as np

from domain.defect_rules.base import BaseRule, RuleResult
from domain.defect_rules.rule_input_window_geometry import InputWindowGeometryRule


class InputWindowSinksRule(BaseRule):
    """Раковины objects внутри тех же семи flatness, что и window_geometry."""

    name = "window_sinks"
    ROLES = ("INPUT_LEFT", "INPUT_RIGHT")
    SINK_CLASS = "objects"
    WINDOW_CLASS = "flatness"

    def check(self, vision_results: dict, **kwargs) -> RuleResult:
        if not self.enabled:
            return self._make_skip(self.name)

        frames = kwargs.get("frames") or {}
        drawings = []
        triggered = False
        details_per_role = {}
        for role in self.ROLES:
            if role not in vision_results:
                continue
            sink_min_conf = self._get(
                "input_window_sinks_min_confidence", 0.4, role=role,
            )
            window_min_conf = self._get(
                "input_window_sinks_window_min_confidence", 0.15, role=role,
            )
            overlap_min_px = self._get(
                "input_window_sinks_overlap_min_px", 5, role=role,
            )
            if type(overlap_min_px) is not int or overlap_min_px < 1:
                raise ValueError(
                    f"{role}.input_window_sinks_overlap_min_px "
                    "должен быть целым числом >= 1"
                )
            expected_count = self._get(
                "input_window_geometry_expected_count", 7, role=role,
            )
            detections = vision_results[role]
            sinks = sorted(
                (
                    detection for detection in detections
                    if detection.get("class") == self.SINK_CLASS
                    and float(detection.get("confidence", 0.0))
                    >= float(sink_min_conf)
                ),
                key=self._detection_sort_key,
            )
            windows = [
                detection for detection in detections
                if detection.get("class") == self.WINDOW_CLASS
                and float(detection.get("confidence", 0.0))
                >= float(window_min_conf)
            ]
            role_result = self._check_role(
                role=role,
                frame=frames.get(role),
                sinks=sinks,
                windows=windows,
                expected_count=int(expected_count),
                overlap_min_px=overlap_min_px,
                drawings=drawings,
            )
            details_per_role[role] = role_result
            triggered = triggered or role_result["triggered"]

        return RuleResult(
            self.name,
            triggered,
            details={"per_role": details_per_role},
            drawings=drawings,
        )

    @classmethod
    def _check_role(
        cls,
        *,
        role,
        frame,
        sinks,
        windows,
        expected_count,
        overlap_min_px,
        drawings,
    ):
        # Нет objects — нет дефекта, reference windows не требуются.
        if not sinks:
            return {
                "triggered": False,
                "reason": None,
                "sinks_total": 0,
                "selected_windows": 0,
                "ignored_windows": 0,
                "confirmed_sinks": 0,
                "hits": [],
                "overlap_min_px": overlap_min_px,
            }

        selected, ignored, selection_note = InputWindowGeometryRule._select_row(
            windows,
            expected_count,
        )
        selected = sorted(selected, key=cls._detection_sort_key)
        if len(selected) != expected_count:
            for detection in selected:
                drawings.append({
                    "type": "window_sink_reference_count_item",
                    "role": role,
                    "bbox": detection.get("bbox") or [0, 0, 0, 0],
                    "mask": detection.get("mask"),
                    "triggered": True,
                })
            drawings.append({
                "type": "construction_error",
                "role": role,
                "bbox": cls._combined_bbox(selected),
                "message": f"WINDOWS REF {len(selected)}/{expected_count}",
                "triggered": True,
            })
            return {
                "triggered": True,
                "reason": (
                    f"invalid_window_reference_count: "
                    f"{len(selected)}/{expected_count}"
                ),
                "sinks_total": len(sinks),
                "windows_found": len(windows),
                "selected_windows": len(selected),
                "ignored_windows": len(ignored),
                "selection_note": selection_note,
                "confirmed_sinks": 0,
                "hits": [],
                "overlap_min_px": overlap_min_px,
            }

        invalid_windows = [
            index
            for index, detection in enumerate(selected, start=1)
            if cls._mask_points(detection) is None
        ]
        if invalid_windows:
            for index in invalid_windows:
                detection = selected[index - 1]
                drawings.append({
                    "type": "window_sink_invalid_reference",
                    "role": role,
                    "bbox": detection.get("bbox") or [0, 0, 0, 0],
                    "mask": detection.get("mask"),
                    "reference": "window",
                    "index": index,
                    "triggered": True,
                })
                drawings.append({
                    "type": "construction_error",
                    "role": role,
                    "bbox": detection.get("bbox") or [0, 0, 0, 0],
                    "message": f"NO WINDOW MASK #{index}",
                    "triggered": True,
                })
            return {
                "triggered": True,
                "reason": "invalid_window_masks",
                "invalid_window_indices": invalid_windows,
                "sinks_total": len(sinks),
                "selected_windows": expected_count,
                "ignored_windows": len(ignored),
                "confirmed_sinks": 0,
                "hits": [],
                "overlap_min_px": overlap_min_px,
            }

        invalid_sinks = [
            index
            for index, detection in enumerate(sinks, start=1)
            if cls._mask_points(detection) is None
        ]
        if invalid_sinks:
            for index in invalid_sinks:
                detection = sinks[index - 1]
                drawings.append({
                    "type": "window_sink_invalid_reference",
                    "role": role,
                    "bbox": detection.get("bbox") or [0, 0, 0, 0],
                    "mask": detection.get("mask"),
                    "reference": "sink",
                    "index": index,
                    "triggered": True,
                })
                drawings.append({
                    "type": "construction_error",
                    "role": role,
                    "bbox": detection.get("bbox") or [0, 0, 0, 0],
                    "message": f"NO SINK MASK #{index}",
                    "triggered": True,
                })
            return {
                "triggered": True,
                "reason": "invalid_sink_masks",
                "invalid_sink_indices": invalid_sinks,
                "sinks_total": len(sinks),
                "selected_windows": expected_count,
                "ignored_windows": len(ignored),
                "confirmed_sinks": 0,
                "hits": [],
                "overlap_min_px": overlap_min_px,
            }

        shape = cls._infer_frame_shape(frame, sinks, selected)
        window_rasters = [
            cls._rasterize_mask(detection, shape)
            for detection in selected
        ]
        sink_rasters = [
            cls._rasterize_mask(detection, shape)
            for detection in sinks
        ]

        hits = []
        confirmed_sinks = set()
        for sink_index, (sink, sink_raster) in enumerate(
            zip(sinks, sink_rasters, strict=True),
            start=1,
        ):
            for window_index, (window, window_raster) in enumerate(
                zip(selected, window_rasters, strict=True),
                start=1,
            ):
                overlap = cv2.bitwise_and(sink_raster, window_raster)
                overlap_px = int(np.count_nonzero(overlap))
                if overlap_px < overlap_min_px:
                    continue
                confirmed_sinks.add(sink_index)
                hit = {
                    "sink_index": sink_index,
                    "window_index": window_index,
                    "overlap_px": overlap_px,
                }
                hits.append(hit)
                contours, _hierarchy = cv2.findContours(
                    overlap,
                    cv2.RETR_EXTERNAL,
                    cv2.CHAIN_APPROX_SIMPLE,
                )
                drawings.append({
                    "type": "window_sink_overlap",
                    "role": role,
                    "sink_index": sink_index,
                    "window_index": window_index,
                    "sink_bbox": sink.get("bbox") or [0, 0, 0, 0],
                    "sink_mask": sink.get("mask"),
                    "window_bbox": window.get("bbox") or [0, 0, 0, 0],
                    "window_mask": window.get("mask"),
                    "overlap_px": overlap_px,
                    "overlap_raster": overlap,
                    "overlap_contours": [
                        contour.reshape(-1, 2).astype(np.int32).tolist()
                        for contour in contours
                        if len(contour) >= 1
                    ],
                    "triggered": True,
                })

        return {
            "triggered": bool(hits),
            "reason": None,
            "sinks_total": len(sinks),
            "selected_windows": expected_count,
            "ignored_windows": len(ignored),
            "selection_note": selection_note,
            "confirmed_sinks": len(confirmed_sinks),
            "hits": hits,
            "overlap_min_px": overlap_min_px,
        }

    @staticmethod
    def _mask_points(detection):
        mask = detection.get("mask")
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

    @classmethod
    def _rasterize_mask(cls, detection, shape):
        points = cls._mask_points(detection)
        if points is None:
            raise ValueError("segmentation mask required")
        canvas = np.zeros(shape, dtype=np.uint8)
        cv2.fillPoly(canvas, [points.astype(np.int32)], 255)
        return canvas

    @staticmethod
    def _infer_frame_shape(frame, *detection_lists):
        if frame is not None:
            return frame.shape[:2]
        max_x = 1
        max_y = 1
        for detections in detection_lists:
            for detection in detections:
                bbox = detection.get("bbox")
                if bbox and len(bbox) == 4:
                    max_x = max(max_x, int(np.ceil(float(bbox[2]))) + 2)
                    max_y = max(max_y, int(np.ceil(float(bbox[3]))) + 2)
                points = InputWindowSinksRule._mask_points(detection)
                if points is not None:
                    max_x = max(max_x, int(np.ceil(points[:, 0].max())) + 2)
                    max_y = max(max_y, int(np.ceil(points[:, 1].max())) + 2)
        return max_y, max_x

    @staticmethod
    def _combined_bbox(detections):
        boxes = [
            detection.get("bbox")
            for detection in detections
            if detection.get("bbox") and len(detection.get("bbox")) == 4
        ]
        if not boxes:
            return [0, 0, 0, 0]
        return [
            min(float(box[0]) for box in boxes),
            min(float(box[1]) for box in boxes),
            max(float(box[2]) for box in boxes),
            max(float(box[3]) for box in boxes),
        ]

    @staticmethod
    def _detection_sort_key(detection):
        bbox = detection.get("bbox") or [0, 0, 0, 0]
        return (
            (float(bbox[0]) + float(bbox[2])) / 2.0,
            (float(bbox[1]) + float(bbox[3])) / 2.0,
        )
