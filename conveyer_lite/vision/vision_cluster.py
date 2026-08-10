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


class VisionCluster:

    def __init__(self, device: str = "cpu", verbose: bool = True):
        self.device = device
        self.verbose = verbose
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

    def warmup(self):
        dummy = np.zeros((720, 1280, 3), dtype=np.uint8)
        errors = []
        for path, model in self.models.items():
            try:
                model.predict(
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
                    preds = model.predict(
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

            valid = [d for d in detections if self._is_valid(d)]
            if len(valid) != len(detections):
                print(
                    f"[VISION WARN] {role}: dropped "
                    f"{len(detections) - len(valid)} invalid detections"
                )

            results[role] = valid

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
            if masks is not None and masks.xy is not None:
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
    def _is_valid(det: dict) -> bool:
        """Проверка минимальной корректности детекции."""
        if not isinstance(det, dict):
            return False
        if "class" not in det or "confidence" not in det:
            return False
        confidence = det.get("confidence")
        if not isinstance(confidence, (int, float)) or not math.isfinite(confidence):
            return False

        bbox = det.get("bbox")
        if bbox is not None:
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                return False
            if any(
                not isinstance(v, (int, float)) or not math.isfinite(v)
                for v in bbox
            ):
                return False

        return True