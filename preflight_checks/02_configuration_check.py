from __future__ import annotations

from common import ROOT, print_header, require

from config.calibration_loader import load_calibration
from config.camera_mapping import REQUIRED_ROLES, load_camera_mapping
from domain.threshold_loader import ThresholdLoader
from inspection.consensus import CONSENSUS_MIN_VOTES, INSPECTION_RUNS
from vision.model_config import MODEL_GROUPS, ROLE_TO_GROUP


def main() -> int:
    print_header("02 — CONFIGURATION CHECK")
    calibration = load_calibration(ROOT / "calibration.json")
    mapping = load_camera_mapping(ROOT / "camera_mapping.json")
    thresholds = ThresholdLoader(ROOT / "thresholds.json").get_all()

    require(set(mapping) == REQUIRED_ROLES, "Camera role set mismatch")
    require(set(ROLE_TO_GROUP) == REQUIRED_ROLES, "Model role set mismatch")
    unique_models = set()
    references = 0
    for role in sorted(REQUIRED_ROLES):
        group_name = ROLE_TO_GROUP[role]
        require(group_name in MODEL_GROUPS, f"Missing model group for {role}")
        entries = MODEL_GROUPS[group_name]
        require(entries, f"Empty model group for {role}")
        for entry in entries:
            references += 1
            path = entry.get("path")
            confidence = entry.get("conf")
            classes = entry.get("classes")
            require(isinstance(path, str) and path, f"Invalid model path for {role}")
            require(
                type(confidence) in (int, float) and 0 <= confidence <= 1,
                f"Invalid confidence for {role}/{path}",
            )
            require(
                isinstance(classes, (list, tuple))
                and classes
                and all(isinstance(name, str) and name for name in classes)
                and len(classes) == len(set(classes)),
                f"Invalid expected classes for {role}/{path}: {classes!r}",
            )
            unique_models.add(path)

    print(f"camera_roles={len(mapping)}")
    print(f"model_references={references}")
    print(f"unique_model_files={len(unique_models)}")
    print(f"threshold_keys={len(thresholds)}")
    print(
        f"inspection_consensus={CONSENSUS_MIN_VOTES}/{INSPECTION_RUNS} "
        "fresh frame runs"
    )
    require(INSPECTION_RUNS == 3, "Production inspection must use exactly 3 runs")
    require(CONSENSUS_MIN_VOTES == 2, "Production consensus must require 2 votes")
    print(f"jog_hold_steps={calibration['jog_hold_steps']}")
    print(
        "distributor="
        f"DIST1_OPEN:{calibration['dist1_open_position']} "
        f"DIST2_BAD:{calibration['dist2_bad_position']} "
        f"DIST2_CLEANUP:{calibration['dist2_cleanup_position']}"
    )
    require(len(mapping) == 7, "Exactly seven camera roles are required")
    require(references == 18, f"Expected 18 model-role references, got {references}")
    require(len(unique_models) == 12, f"Expected 12 model files, got {len(unique_models)}")
    print("CONFIGURATION CHECK PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
