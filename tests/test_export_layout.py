"""统一导出目录/索引测试（v1.54）。"""

from __future__ import annotations

from pathlib import Path

from storage import export_index, export_paths


def test_export_paths_monthly_layout(monkeypatch, tmp_path):
    import config

    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path)
    target = export_paths.export_file("测试申报包", "xlsx", period="2026-10", category="申报表")
    assert target.parent.parts[-4:] == ("main", "2026", "10", "01_申报表")
    assert target.suffix == ".xlsx"


def test_export_index_scan_and_find(monkeypatch, tmp_path):
    import config

    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path)
    target = export_paths.export_file("工资表", "xlsx", period="2026-10", category="工资个税")
    target.write_bytes(b"test")
    entries = export_index.scan("main")
    assert any(Path(item["relative_path"]).name == target.name for item in entries)
    found = export_index.find("main", year="2026", month="10", category="02_工资个税")
    assert any(item["file_name"] == target.name for item in found)
