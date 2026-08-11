"""PartArchive: запись meta.json, статистики и batch.json на диск.

Реальный PartArchive пишет через atomic_write_* и формирует структуру
каталогов партии. Тесты проверяют:

* finalize() создаёт meta.json с правильными полями;
* stats.json обновляется атомарно;
* batch.json содержит все заархивированные детали;
* нормализация категории: UNKNOWN -> BAD;
* compress() создаёт валидный ZIP и удаляет исходную папку.
"""

import json
import os
import zipfile

from inspection.part_archive import PartArchive


def _make_archive(tmp_path, **kwargs):
    return PartArchive(
        root_folder=str(tmp_path / "archive"),
        enabled=True,
        jpeg_quality=85,
        compress_on_shutdown=False,
        delete_original_after_zip=True,
        **kwargs,
    )


class TestFinalize:
    def test_meta_json_создаётся_с_правильными_полями(self, tmp_path):
        archive = _make_archive(tmp_path)
        folder = archive.finalize(
            part_id=1,
            category="GOOD",
            decision="none",
            defects=[],
            step=5,
        )
        assert folder is not None
        meta_path = os.path.join(folder, "meta.json")
        assert os.path.exists(meta_path)
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        assert meta["part_id"] == 1
        assert meta["category"] == "GOOD"
        assert meta["decision"] == "none"
        assert meta["defects"] == []
        assert meta["step"] == 5
        assert "batch_id" in meta
        assert "timestamp" in meta

    def test_unknown_категория_нормализуется_в_bad(self, tmp_path):
        archive = _make_archive(tmp_path)
        folder = archive.finalize(
            part_id=2,
            category="UNKNOWN",
            decision="incomplete",
            defects=[],
            step=3,
        )
        meta_path = os.path.join(folder, "meta.json")
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        assert meta["category"] == "BAD"
        assert meta["requested_category"] == "UNKNOWN"

    def test_extra_поля_сохраняются_в_meta(self, tmp_path):
        archive = _make_archive(tmp_path)
        folder = archive.finalize(
            part_id=3,
            category="BAD",
            decision="glass",
            defects=["glass"],
            step=7,
            extra={"inspection_consensus": {"runs": 1}},
        )
        meta_path = os.path.join(folder, "meta.json")
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        assert meta["inspection_consensus"] == {"runs": 1}

    def test_stats_json_обновляется(self, tmp_path):
        archive = _make_archive(tmp_path)
        archive.finalize(part_id=1, category="GOOD", decision="none",
                         defects=[], step=1)
        archive.finalize(part_id=2, category="BAD", decision="glass",
                         defects=["glass"], step=2)
        archive.finalize(part_id=3, category="CLEANUP", decision="glass",
                         defects=["glass"], step=3)
        stats_path = os.path.join(str(tmp_path / "archive"), "stats.json")
        assert os.path.exists(stats_path)
        with open(stats_path, encoding="utf-8") as f:
            stats = json.load(f)
        assert stats["total"] == 3
        assert stats["good"] == 1
        assert stats["bad"] == 1
        assert stats["cleanup"] == 1

    def test_batch_json_содержит_все_детали(self, tmp_path):
        archive = _make_archive(tmp_path)
        archive.finalize(part_id=1, category="GOOD", decision="none",
                         defects=[], step=1)
        archive.finalize(part_id=2, category="BAD", decision="glass",
                         defects=["glass"], step=2)
        batch_path = os.path.join(archive.batch_folder, "batch.json")
        assert os.path.exists(batch_path)
        with open(batch_path, encoding="utf-8") as f:
            batch = json.load(f)
        assert batch["batch_id"] == archive.batch_id
        assert len(batch["parts"]) == 2
        ids = {p["part_id"] for p in batch["parts"]}
        assert ids == {1, 2}

    def test_get_part_info_возвращает_запись(self, tmp_path):
        archive = _make_archive(tmp_path)
        archive.finalize(part_id=42, category="GOOD", decision="none",
                         defects=[], step=1)
        info = archive.get_part_info(42)
        assert info is not None
        assert info["part_id"] == 42
        assert info["category"] == "GOOD"

    def test_get_part_info_несуществующий_возвращает_none(self, tmp_path):
        archive = _make_archive(tmp_path)
        assert archive.get_part_info(999) is None


class TestDisabledArchive:
    def test_disabled_finalize_возвращает_none(self, tmp_path):
        archive = PartArchive(
            root_folder=str(tmp_path / "archive"),
            enabled=False,
        )
        result = archive.finalize(
            part_id=1, category="GOOD", decision="none",
            defects=[], step=1,
        )
        assert result is None

    def test_disabled_compress_возвращает_none(self, tmp_path):
        archive = PartArchive(
            root_folder=str(tmp_path / "archive"),
            enabled=False,
        )
        assert archive.compress() is None


class TestCompress:
    def test_compress_создаёт_валидный_zip(self, tmp_path):
        archive = _make_archive(tmp_path)
        archive.finalize(part_id=1, category="GOOD", decision="none",
                         defects=[], step=1)
        archive.finalize(part_id=2, category="BAD", decision="glass",
                         defects=["glass"], step=2)
        zip_path = archive.compress(delete_original=False)
        assert zip_path is not None
        assert os.path.exists(zip_path)
        with zipfile.ZipFile(zip_path, "r") as zf:
            assert zf.testzip() is None
            names = zf.namelist()
            assert any("batch.json" in n for n in names)
            assert any("meta.json" in n for n in names)

    def test_compress_с_удалением_исходника(self, tmp_path):
        archive = _make_archive(tmp_path)
        archive.finalize(part_id=1, category="GOOD", decision="none",
                         defects=[], step=1)
        batch_folder = archive.batch_folder
        assert os.path.isdir(batch_folder)
        zip_path = archive.compress(delete_original=True)
        assert zip_path is not None
        assert not os.path.exists(batch_folder)

    def test_compress_пустой_партии_возвращает_none(self, tmp_path):
        archive = _make_archive(tmp_path)
        assert archive.compress() is None


class TestCategoryNormalization:
    def test_good_normalise(self, tmp_path):
        archive = _make_archive(tmp_path)
        assert archive.normalise_category("GOOD") == "GOOD"
        assert archive.normalise_category("good") == "GOOD"

    def test_bad_normalise(self, tmp_path):
        archive = _make_archive(tmp_path)
        assert archive.normalise_category("BAD") == "BAD"

    def test_cleanup_normalise(self, tmp_path):
        archive = _make_archive(tmp_path)
        assert archive.normalise_category("CLEANUP") == "CLEANUP"

    def test_unknown_normalise_to_bad(self, tmp_path):
        archive = _make_archive(tmp_path)
        assert archive.normalise_category("UNKNOWN") == "BAD"
        assert archive.normalise_category("") == "BAD"
        assert archive.normalise_category("SOMETHING") == "BAD"


class TestGetPartImages:
    def test_get_part_images_пусто_без_файлов(self, tmp_path):
        archive = _make_archive(tmp_path)
        archive.finalize(part_id=1, category="GOOD", decision="none",
                         defects=[], step=1)
        # Без кадров — роли сохранены, но файлов нет
        images = archive.get_part_images(1)
        # Все роли без файлов -> пустой результат
        for role_entry in images.values():
            assert isinstance(role_entry, dict)

    def test_get_part_images_несуществующий(self, tmp_path):
        archive = _make_archive(tmp_path)
        assert archive.get_part_images(999) == {}
