import unittest
from pathlib import Path


class PreflightFolderTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.preflight = self.root / "preflight_checks"

    def test_operator_preflight_folder_has_numbered_checks_and_documentation(self):
        expected = {
            "README_RU.md",
            "00_RUN_SOFTWARE_ONLY.bat",
            "00_RUN_ALL_NO_MOTION.bat",
            "01_environment_check.py",
            "02_configuration_check.py",
            "03_model_files_check.py",
            "04_model_load_and_warmup.py",
            "05_seven_cameras_check.py",
            "06_controller_no_motion_check.py",
            "07_AUTOMATED_CODE_AND_UI_TESTS.bat",
            "08_OPERATOR_CHECKLIST_BEFORE_START_RU.md",
            "09_DISTRIBUTOR_CALIBRATION_RU.md",
            "10_CAMERA_CALIBRATION_RU.md",
            "common.py",
        }
        self.assertTrue(self.preflight.is_dir())
        self.assertTrue(expected.issubset({path.name for path in self.preflight.iterdir()}))
        readme = (self.preflight / "README_RU.md").read_text(encoding="utf-8")
        for name in sorted(expected - {"common.py", "README_RU.md"}):
            self.assertIn(name, readme)

    def test_no_motion_python_checks_contain_no_motion_commands(self):
        forbidden = ('send("G1")', 'send("G3")', 'send("G20")', 'send("G27")', 'send("G28")')
        for path in sorted(self.preflight.glob("0[1-6]_*.py")):
            source = path.read_text(encoding="utf-8")
            for command in forbidden:
                self.assertNotIn(command, source, f"{path.name} contains {command}")

    def test_top_level_test_bat_is_only_a_preflight_wrapper(self):
        content = (self.root / "test.bat").read_text(encoding="utf-8")
        self.assertIn("preflight_checks\\07_AUTOMATED_CODE_AND_UI_TESTS.bat", content)
        self.assertNotIn("unittest discover", content)

    def test_operator_checklist_covers_all_five_prestart_capabilities(self):
        checklist = (
            self.preflight / "08_OPERATOR_CHECKLIST_BEFORE_START_RU.md"
        ).read_text(encoding="utf-8")
        for term in (
            "Камеры",
            "Модели и правила",
            "Распределитель",
            "Коррекция ленты JOG",
            "Готовность START",
        ):
            self.assertIn(term, checklist)


if __name__ == "__main__":
    unittest.main()
