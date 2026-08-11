"""Загрузчики конфигурации: строгая валидация VCS-файлов как контрактов.

calibration.json, thresholds.json и camera_mapping.json — граница между
редактируемым человеком файлом и кодом: ослабленная валидация пропускает
опечатку прямо в механику, поэтому каждое правило валидации покрыто тестом.
"""

import json
import shutil

import pytest

from config.calibration_loader import load_calibration, DEFAULTS
from config.camera_mapping import (
    load_camera_mapping,
    validate_camera_mapping,
    REQUIRED_ROLES,
)
from domain.threshold_loader import ThresholdLoader

from conftest import REPO_ROOT

CALIBRATION_FILE = REPO_ROOT / "calibration.json"
THRESHOLDS_FILE = REPO_ROOT / "thresholds.json"
CAMERA_MAPPING_FILE = REPO_ROOT / "camera_mapping.json"


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _fresh_thresholds(tmp_path):
    """Копия штатного thresholds.json во временном каталоге."""
    target = tmp_path / "thresholds.json"
    shutil.copy(THRESHOLDS_FILE, target)
    return target


def _load_raw(path):
    return json.loads(path.read_text(encoding="utf-8"))


class TestCalibration:
    def test_штатный_файл_загружается(self):
        data = load_calibration(str(CALIBRATION_FILE))
        for key in DEFAULTS:
            assert key in data

    def test_лишний_ключ_отвергается(self, tmp_path):
        data = _load_raw(CALIBRATION_FILE)
        data["unknown_knob"] = 1
        path = tmp_path / "calibration.json"
        _write_json(path, data)
        with pytest.raises(ValueError, match="extra"):
            load_calibration(str(path))

    def test_пропавший_ключ_отвергается(self, tmp_path):
        data = _load_raw(CALIBRATION_FILE)
        del data["conveyor_speed"]
        path = tmp_path / "calibration.json"
        _write_json(path, data)
        with pytest.raises(ValueError, match="missing"):
            load_calibration(str(path))

    def test_bad_и_cleanup_позиции_обязаны_различаться(self, tmp_path):
        data = _load_raw(CALIBRATION_FILE)
        data["dist2_bad_position"] = data["dist2_cleanup_position"]
        path = tmp_path / "calibration.json"
        _write_json(path, data)
        with pytest.raises(ValueError, match="различаться"):
            load_calibration(str(path))

    def test_jog_hold_steps_имеет_безопасный_диапазон(self, tmp_path):
        for value in (9_999, 10_000_001):
            data = _load_raw(CALIBRATION_FILE)
            data["jog_hold_steps"] = value
            path = tmp_path / "calibration.json"
            _write_json(path, data)
            with pytest.raises(ValueError, match="jog_hold_steps"):
                load_calibration(str(path))

    def test_числа_не_могут_быть_строками(self, tmp_path):
        data = _load_raw(CALIBRATION_FILE)
        data["micro_steps"] = "500"
        path = tmp_path / "calibration.json"
        _write_json(path, data)
        with pytest.raises(ValueError):
            load_calibration(str(path))

    def test_пропавший_файл_это_runtime_error(self, tmp_path):
        with pytest.raises(RuntimeError, match="не найден"):
            load_calibration(str(tmp_path / "absent.json"))


class TestThresholds:
    def test_штатный_файл_загружается_и_валидируется(self):
        loader = ThresholdLoader(str(THRESHOLDS_FILE))
        # Метки всех обязательных порогов непусты.
        assert loader.thresholds
        assert "TOP.top_contacts_expected_count" in loader.thresholds

    def test_пропавший_ключ_отвергается(self, tmp_path):
        raw = _load_raw(THRESHOLDS_FILE)
        del raw["TOP"]["top_contacts_min_confidence"]
        path = tmp_path / "thresholds.json"
        _write_json(path, raw)
        with pytest.raises(ValueError, match="Отсутствует ключ"):
            ThresholdLoader(str(path))

    def test_min_confidence_вне_диапазона(self, tmp_path):
        raw = _load_raw(THRESHOLDS_FILE)
        raw["TOP"]["top_glass_min_confidence"] = 1.5
        path = tmp_path / "thresholds.json"
        _write_json(path, raw)
        with pytest.raises(ValueError, match="0..1"):
            ThresholdLoader(str(path))

    def test_фиксированные_количества_защищены(self, tmp_path):
        # Механика рассчитана ровно на 14 контактов сверху и 2 коротких.
        for role, key, bad in (
            ("TOP", "top_contacts_expected_count", 13),
            ("SPIDER_IN", "spider_contacts_short_expected_count", 3),
        ):
            raw = _load_raw(THRESHOLDS_FILE)
            raw[role][key] = bad
            path = tmp_path / "thresholds.json"
            _write_json(path, raw)
            with pytest.raises(ValueError):
                ThresholdLoader(str(path))

    def test_part_presence_нельзя_отключать(self, tmp_path):
        raw = _load_raw(THRESHOLDS_FILE)
        raw["disabled_rules"] = ["part_presence"]
        path = tmp_path / "thresholds.json"
        _write_json(path, raw)
        with pytest.raises(ValueError, match="part_presence"):
            ThresholdLoader(str(path))

    def test_min_не_может_превышать_max_в_геометрии_окна(self, tmp_path):
        raw = _load_raw(THRESHOLDS_FILE)
        section = raw["INPUT_LEFT"]
        section["input_window_geometry_top_px_min"], section["input_window_geometry_top_px_max"] = (
            section["input_window_geometry_top_px_max"],
            section["input_window_geometry_top_px_min"],
        )
        path = tmp_path / "thresholds.json"
        _write_json(path, raw)
        with pytest.raises(ValueError, match="не может превышать"):
            ThresholdLoader(str(path))

    def test_save_file_круговой_обход_сохраняет_данные(self, tmp_path):
        loader = ThresholdLoader(str(THRESHOLDS_FILE))
        target = tmp_path / "roundtrip.json"
        ThresholdLoader.save_file(str(target), loader.thresholds, loader.labels)
        reloaded = ThresholdLoader(str(target))
        assert reloaded.thresholds == loader.thresholds

    def test_disabled_rules_валидируется_как_список_строк(self, tmp_path):
        raw = _load_raw(THRESHOLDS_FILE)
        raw["disabled_rules"] = "top_glass"
        path = tmp_path / "thresholds.json"
        _write_json(path, raw)
        with pytest.raises(ValueError):
            ThresholdLoader(str(path))


class TestCameraMapping:
    def test_штатный_файл_загружается(self):
        if not CAMERA_MAPPING_FILE.exists():
            pytest.skip("camera_mapping.json не входит в репозиторий")
        mapping = load_camera_mapping(str(CAMERA_MAPPING_FILE))
        assert set(mapping) == REQUIRED_ROLES

    def _valid(self):
        return {role: index for index, role in enumerate(sorted(REQUIRED_ROLES))}

    def test_полный_маппинг_принимается(self):
        assert validate_camera_mapping(self._valid()) == self._valid()

    def test_дубликат_id_отвергается(self):
        mapping = self._valid()
        mapping["TOP"] = mapping["INPUT_LEFT"]
        with pytest.raises(ValueError, match="уникальными"):
            validate_camera_mapping(mapping)

    def test_пропавшая_роль_отвергается(self):
        mapping = self._valid()
        del mapping["SPIDER_IN"]
        with pytest.raises(ValueError, match="missing"):
            validate_camera_mapping(mapping)

    def test_лишняя_роль_отвергается(self):
        mapping = self._valid()
        mapping["BOTTOM"] = 99
        with pytest.raises(ValueError, match="extra"):
            validate_camera_mapping(mapping)

    def test_отрицательный_или_нецелый_id_отвергается(self):
        for bad in (-1, "0", 1.5):
            mapping = self._valid()
            mapping["TOP"] = bad
            with pytest.raises(ValueError):
                validate_camera_mapping(mapping)
