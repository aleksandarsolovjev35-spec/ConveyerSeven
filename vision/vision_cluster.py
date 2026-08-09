import math
import os
import time

from ultralytics import YOLO
import numpy as np

from vision.model_config import MODEL_GROUPS, ROLE_TO_GROUP

INFERENCE_IMGSZ = 1280

AGGRESSIVE_IOU_ROLES = {"TOP", "SPIDER_LEFT", "SPIDER_RIGHT"}
DEFAULT_IOU    = 0.45
AGGRESSIVE_IOU = 0.10


class MalformedVisionResult(RuntimeError):
    """A model produced structurally invalid evidence."""


class VisionCluster:

    def __init__(
        self,
        device: str = "cpu",
        verbose: bool = True,
        *,
        worker_runner=None,
        worker_timeout: float = 30.0,
    ):
        self.device = device
        self.verbose = verbose
        self.worker_runner = worker_runner
        self.worker_timeout = float(worker_timeout)
        if self.worker_timeout <= 0:
            raise ValueError("worker_timeout must be positive")
        self.models = {}
        self.last_health = []
        self._load_all_models()

    def _load_all_models(self):
        for model_list in MODEL_GROUPS.values():
            for entry in model_list:
                path = entry["path"]
                if path not in self.models:
                    if not os.path.isfile(path):
                        raise FileNotFoundError(f"Model file not found: {path}")
                    if self.verbose:
                        print(f"[VISION] Loading {path}")
                    model = YOLO(path)
                    self._verify_model_classes(
                        path,
                        model,
                        tuple(entry.get("classes", ())),
                    )
                    self.models[path] = model
        if self.verbose:
            print(f"[VISION] Models loaded: {len(self.models)}")

    @staticmethod
    def _verify_model_classes(path: str, model, expected: tuple[str, ...]):
        if not expected:
            return
        names = getattr(model, "names", None)
        if isinstance(names, dict):
            actual = tuple(str(names[index]) for index in sorted(names))
        elif isinstance(names, (list, tuple)):
            actual = tuple(str(name) for name in names)
        else:
            raise RuntimeError(
                f"Model {path} has no readable class names; expected {expected}"
            )
        if actual != expected:
            raise RuntimeError(
                f"Model class mismatch for {path}: actual={actual}, expected={expected}"
            )

    def _predict(self, model, frame, **kwargs):
        """Run a model call directly or through an injected terminating worker."""
        if self.worker_runner is None:
            return model.predict(frame, **kwargs)
        return self.worker_runner(
            model.predict,
            frame,
            timeout=self.worker_timeout,
            **kwargs,
        )

    def warmup(self):
        dummy = np.zeros((720, 1280, 3), dtype=np.uint8)
        errors = []
        for path, model in self.models.items():
            try:
                self._predict(
                    model,
                    dummy,
                    device=self.device,
                    verbose=False,
                    imgsz=INFERENCE_IMGSZ,
                    retina_masks=True,
                )
            except Exception as e:
                errors.append(f"{path}: {type(e).__name__}: {e}")
        if errors:
            raise RuntimeError("Model warmup failed: " + "; ".join(errors))
        if self.verbose:
            print("[VISION] Warmup done")

    def process_all(self, frames: dict) -> dict:
        results = {}
        health = []
        self.last_health = []

        for role, frame in frames.items():
            group_name = ROLE_TO_GROUP.get(role)
            if not group_name:
                raise ValueError(f"Unknown camera role: {role}")
            array = np.asarray(frame)
            if array.ndim != 3 or array.shape[0] < 240 or array.shape[1] < 320:
                raise ValueError(f"Invalid frame for {role}: shape={array.shape}")

            iou = (
                AGGRESSIVE_IOU
                if role in AGGRESSIVE_IOU_ROLES
                else DEFAULT_IOU
            )

            detections = []

            for entry in MODEL_GROUPS[group_name]:
                path  = entry["path"]
                conf  = entry["conf"]
                model = self.models[path]

                started = time.perf_counter()
                try:
                    preds = self._predict(
                        model,
                        frame,
                        device=self.device,
                        conf=conf,
                        imgsz=INFERENCE_IMGSZ,
                        iou=iou,
                        retina_masks=True,
                        verbose=False,
                    )
                except Exception as e:
                    health.append({
                        "role": role,
                        "model": path,
                        "ok": False,
                        "elapsed_ms": (time.perf_counter() - started) * 1000,
                        "detections": 0,
                        "error": f"{type(e).__name__}: {e}",
                    })
                    self.last_health = health
                    raise RuntimeError(
                        f"Model inference failed for {role} / {path}: "
                        f"{type(e).__name__}: {e}"
                    ) from e

                parsed = self._parse_predictions(preds)
                health.append({
                    "role": role,
                    "model": path,
                    "ok": True,
                    "elapsed_ms": (time.perf_counter() - started) * 1000,
                    "detections": len(parsed),
                    "error": None,
                })
                for detection in parsed:
                    detection["model_path"] = path
                if self.verbose:
                    print(
                        f"[VISION] {role} | "
                        f"{path.split('/')[-1]} "
                        f"-> {len(parsed)} det"
                    )
                detections.extend(parsed)

            # Invalid evidence is a technical fault.  It must never be
            # silently removed and converted into an empty/GOOD result.
            for detection in detections:
                self._require_valid(detection, role)
            results[role] = detections

        self.last_health = health
        return results

    def _parse_predictions(self, preds) -> list:
        out = []

        for result in preds:
            names   = result.names
            boxes   = result.boxes
            masks   = result.masks

            if boxes is None:
                continue

            xyxy    = boxes.xyxy.cpu().numpy()
            confs   = boxes.conf.cpu().numpy()
            cls_ids = boxes.cls.cpu().numpy().astype(int)

            mask_polys = None
            if masks is not None:
                if masks.xy is None or len(masks.xy) < len(xyxy):
                    raise MalformedVisionResult("model returned incomplete segmentation masks")
                mask_polys = masks.xy

            for i in range(len(xyxy)):
                det = {
                    "class":      names[cls_ids[i]],
                    "confidence": float(confs[i]),
                    "bbox":       [float(v) for v in xyxy[i]],
                    "mask": (
                        [
                            [float(p[0]), float(p[1])]
                            for p in mask_polys[i]
                        ]
                        if mask_polys is not None
                        and i < len(mask_polys)
                        else None
                    ),
                }
                out.append(det)

        return out

    @staticmethod
    def _require_valid(det: dict, role: str = "unknown") -> None:
        """Raise on malformed model evidence; never repair or drop it."""
        if not isinstance(det, dict):
            raise MalformedVisionResult(f"{role}: detection is not an object")
        if not isinstance(det.get("class"), str) or not det.get("class"):
            raise MalformedVisionResult(f"{role}: detection has no class")
        confidence = det.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
        ):
            raise MalformedVisionResult(f"{role}: non-finite confidence")
        bbox = det.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            raise MalformedVisionResult(f"{role}: invalid bbox")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in bbox
        ):
            raise MalformedVisionResult(f"{role}: non-finite bbox coordinate")
        mask = det.get("mask")
        if mask is None:
            return
        if not isinstance(mask, (list, tuple)) or len(mask) < 3:
            raise MalformedVisionResult(f"{role}: invalid mask")
        for point in mask:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise MalformedVisionResult(f"{role}: invalid mask coordinate")
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in point
            ):
                raise MalformedVisionResult(f"{role}: non-finite mask coordinate")

    @staticmethod
    def _is_valid(det: dict) -> bool:
        """Compatibility predicate; production calls _require_valid."""
        try:
            VisionCluster._require_valid(det)
        except MalformedVisionResult:
            return False
        return True
