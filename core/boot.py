"""BOOT/READY gate for production."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


class BootFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class BootManifest:
    app_version: str
    rules_version: str
    model_identity: MappingProxyType
    machine_config_sha256: str
    threshold_revision: str | None
    threshold_sha256: str | None
    created_at: float

    def as_dict(self) -> dict:
        return {
            "app_version": self.app_version,
            "rules_version": self.rules_version,
            "models": dict(self.model_identity),
            "machine_config_sha256": self.machine_config_sha256,
            "threshold_revision": self.threshold_revision,
            "threshold_sha256": self.threshold_sha256,
            "created_at": self.created_at,
        }


def sha256_file(path: str | os.PathLike) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class BootCoordinator:
    """Run all readiness gates before exposing production READY."""

    def __init__(self, *, marker_path: str = ".production_process.json", app_version: str = "dev"):
        self.marker_path = Path(marker_path)
        self.app_version = str(app_version)
        self.ready = False
        self.manifest: BootManifest | None = None
        self.abandoned_process = None

    def detect_previous_unclean(self) -> dict | None:
        if not self.marker_path.exists():
            return None
        try:
            with self.marker_path.open(encoding="utf-8") as stream:
                value = json.load(stream)
        except Exception as exc:
            raise BootFailure(f"process marker is unreadable: {exc}") from exc
        if not isinstance(value, dict) or value.get("status") not in {"closed", "cleanup_acknowledged"}:
            self.abandoned_process = value if isinstance(value, dict) else {"raw": value}
            return self.abandoned_process
        return None

    def acknowledge_manual_cleanup(self, confirmation: bool):
        if not confirmation:
            raise BootFailure("manual physical line cleanup is required")
        if self.abandoned_process is not None:
            previous_batch = self.abandoned_process.get("batch_id", "unknown")
            self.close_process(batch_id=previous_batch, status="cleanup_acknowledged")
        self.abandoned_process = None

    def mark_process_open(self, batch_id: str, **metadata):
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "status": "open",
            "batch_id": batch_id,
            "pid": os.getpid(),
            "started_at": time.time(),
            **metadata,
        }
        temp = self.marker_path.with_name(self.marker_path.name + ".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.marker_path)

    def close_process(self, *, batch_id: str, status: str = "closed"):
        payload = {"status": status, "batch_id": batch_id, "closed_at": time.time()}
        temp = self.marker_path.with_name(self.marker_path.name + ".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.marker_path)

    def run(
        self,
        *,
        batch_id: str,
        camera_manager,
        model_cluster,
        rules,
        hardware_gate,
        storage_gate,
        machine_config_path: str,
        threshold_path: str | None = None,
        threshold_revision: str | None = None,
        rules_version: str = "production",
    ) -> BootManifest:
        """Execute mandatory gates; any exception leaves ``ready`` false."""
        self.ready = False
        self.detect_previous_unclean()
        if self.abandoned_process is not None:
            raise BootFailure("previous process was not closed; manual cleanup required")
        try:
            self._check_cameras(camera_manager)
            self._check_models(model_cluster)
            self._check_rules(rules)
            self._call_gate(hardware_gate, "hardware")
            self._call_gate(storage_gate, "storage")
            model_identity = self._model_identity(model_cluster)
            manifest = BootManifest(
                app_version=self.app_version,
                rules_version=str(rules_version),
                model_identity=MappingProxyType(model_identity),
                machine_config_sha256=sha256_file(machine_config_path),
                threshold_revision=threshold_revision,
                threshold_sha256=(sha256_file(threshold_path) if threshold_path else None),
                created_at=time.time(),
            )
            self.manifest = manifest
            self.mark_process_open(batch_id)
            self.ready = True
            return manifest
        except BootFailure:
            raise
        except Exception as exc:
            raise BootFailure(f"BOOT gate failed: {type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _check_cameras(manager):
        mapping = getattr(manager, "mapping", {})
        required = {
            "INPUT_LEFT", "INPUT_RIGHT", "SPIDER_LEFT", "SPIDER_RIGHT",
            "SPIDER_IN", "SPIDER_OUT", "TOP",
        }
        if set(mapping) != required:
            raise BootFailure("all seven camera roles are mandatory")
        serials = getattr(manager, "serials", {})
        if serials and len(set(serials.values())) != 7:
            raise BootFailure("camera serials are not unique")
        if len(getattr(manager, "cameras", {})) != 7:
            raise BootFailure("not all seven cameras are open")

    @staticmethod
    def _check_models(cluster):
        warmup = getattr(cluster, "warmup", None)
        if not callable(warmup):
            raise BootFailure("model warmup gate is missing")
        warmup()
        if not getattr(cluster, "models", None):
            raise BootFailure("no production models loaded")

    @staticmethod
    def _check_rules(rules):
        active = getattr(rules, "rules", rules)
        if not active:
            raise BootFailure("production rule set is empty")
        if any(not getattr(rule, "enabled", True) for rule in active):
            raise BootFailure("production rules cannot be disabled")

    @staticmethod
    def _call_gate(gate, name):
        if not callable(gate):
            raise BootFailure(f"{name} health gate is missing")
        result = gate()
        if result is False:
            raise BootFailure(f"{name} health gate failed")

    @staticmethod
    def _model_identity(cluster) -> dict:
        rows = {}
        for path, model in getattr(cluster, "models", {}).items():
            try:
                rows[str(path)] = {
                    "sha256": sha256_file(path),
                    "version": str(getattr(model, "version", "unknown")),
                }
            except OSError as exc:
                raise BootFailure(f"model identity unavailable: {path}: {exc}") from exc
        return rows
