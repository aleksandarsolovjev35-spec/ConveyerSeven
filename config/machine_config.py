"""Developer-owned, versioned physical machine configuration."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path


SCHEMA_VERSION = 1
ROLES = (
    "INPUT_LEFT", "INPUT_RIGHT", "SPIDER_LEFT", "SPIDER_RIGHT",
    "SPIDER_IN", "SPIDER_OUT", "TOP",
)


class MachineConfigError(ValueError):
    pass


def canonical_sha256(data: dict) -> str:
    encoded = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_machine_config(path: str = "machine_config.json") -> tuple[dict, str]:
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise MachineConfigError(f"cannot read machine config: {exc}") from exc
    validate_machine_config(data)
    return data, canonical_sha256(data)


def validate_machine_config(data: dict):
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise MachineConfigError("unsupported machine_config schema")
    cameras = data.get("cameras")
    if not isinstance(cameras, dict) or set(cameras) != set(ROLES):
        raise MachineConfigError("machine_config must define all seven camera roles")
    serials = []
    indices = []
    for role in ROLES:
        spec = cameras[role]
        if not isinstance(spec, dict) or not isinstance(spec.get("serial"), str) or not spec["serial"].strip():
            raise MachineConfigError(f"{role}: stable camera serial is required")
        serials.append(spec["serial"].strip())
        index = spec.get("index", spec.get("device_index"))
        if type(index) is not int or index < 0:
            raise MachineConfigError(f"{role}: camera index must be a non-negative int")
        indices.append(index)
        transform = spec.get("transform", {})
        if not isinstance(transform, dict):
            raise MachineConfigError(f"{role}: transform must be an object")
        rotation = transform.get("rotate", 0)
        if rotation not in (0, 90, 180, 270):
            raise MachineConfigError(f"{role}: invalid rotation")
    if len(serials) != len(set(serials)):
        raise MachineConfigError("camera serials must be unique")
    if len(indices) != len(set(indices)):
        raise MachineConfigError("camera indices must be unique")
    motion = data.get("conveyor")
    if not isinstance(motion, dict):
        raise MachineConfigError("conveyor config is required")
    for key in ("steps_per_division", "divisions_per_movement", "production_target"):
        if type(motion.get(key)) is not int or motion[key] <= 0:
            raise MachineConfigError(f"conveyor.{key} must be a positive int")
    if motion["production_target"] != motion["steps_per_division"] * motion["divisions_per_movement"]:
        raise MachineConfigError("production target does not match geometry")
    distributor = data.get("distributor")
    if not isinstance(distributor, dict):
        raise MachineConfigError("distributor config is required")
    for key in ("dist1_to_dist2", "dist2_bad", "dist2_cleanup"):
        if type(distributor.get(key)) is not int or distributor[key] < 0:
            raise MachineConfigError(f"distributor.{key} must be a non-negative int")
