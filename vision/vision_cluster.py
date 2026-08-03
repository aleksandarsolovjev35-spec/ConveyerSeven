import math
import os
import time

from ultralytics import YOLO
import numpy as np

from vision.model_config import MODEL_GROUPS, ROLE_TO_GROUP
from vision.normalize import normalize_for_role

INFERENCE_IMGSZ = 1280

AGGRESSIVE_IOU_ROLES = {"TOP", "SPIDER_LEFT", "SPIDER_RIGHT"}
DEFAULT_IOU    = 0.45
AGGRESSIVE_IOU = 0.10

# Префикс env-переменных, переопределяющих conf для класса:
# VISION_CONF_<класс> (например VISION_CONF_omission-short=0.2) позволяет
# смягчить порог без правки model_config.py, когда после смены освещения
# уверенность модели просела ниже штатного порога.
CONF_OVERRIDE_PREFIX = "VISION_CONF_"


def effective_conf(entry: dict, verbose: bool = False) -> float:
    """conf модели с учётом env-переопределения по классам.

    Если для класса(ов) модели задано несколько VISION_CONF_*, берётся
    минимальное значение (самое «мягкое»): цель переопределения — не
    потерять детекции после смены условий съёмки.
    """
    base = float(entry.get("conf", 0.25))
    overrides = []
    for cls_name in entry.get("classes", ()):
        raw = os.environ.get(f"{CONF_OVERRIDE_PREFIX}{cls_name}")
        if raw is None:
            continue
        try:
            value = float(raw.strip())
        except ValueError:
            print(
                f"[VISION] {CONF_OVERRIDE_PREFIX}{cls_name}={raw!r} "
                "не число, переопределение проигнорировано"
            )
            continue
        if not 0.0 <= value <= 1.0:
            print(
                f"[VISION] {CONF_OVERRIDE_PREFIX}{cls_name}={value} "
                "вне диапазона 0..1, переопределение проигнорировано"
            )
            continue
        overrides.append(value)
    if not overrides:
        return base
    chosen = min(overrides)
    if verbose and chosen != base:
        print(
            f"[VISION] conf {entry.get('path')}: "
            f"{base} -> {chosen} (env)"
        )
    return chosen


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

            # Смена освещения на линии сдвигает яркость/контраст, и
            # уверенность YOLO падает ниже conf-порога. CLAHE-нормализация
            # (vision.normalize) частично компенсирует это; включается
            # env VISION_NORMALIZE=1, по умолчанию выключена.
            normalized = normalize_for_role(array, role)
            if normalized is not array and self.verbose:
                print(f"[VISION] {role}: кадр нормализован (CLAHE)")

            iou = (
                AGGRESSIVE_IOU
                if role in AGGRESSIVE_IOU_ROLES
                else DEFAULT_IOU
            )

            detections = []

            for entry in MODEL_GROUPS[group_name]:
                path  = entry["path"]
                conf  = effective_conf(entry, verbose=self.verbose)
                model = self.models[path]

                started = time.perf_counter()
                try:
                    preds = model.predict(
                        normalized,
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