"""公司账套交接包测试（v1.28）。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from archiving.handover import build_handover_package
from ledger import store


def test_handover_package_structure(tmp_path):
    # 写入一条收入记录，确保 CSV 有内容
    store.table("income").append({
        "date": "2026-01-05", "customer": "测试客户", "amount_inclusive": 10300,
        "amount_exclusive": 10000, "tax": 300, "invoice_type": "普票", "category": "服务",
        "description": "交接测试", "invoice_no": "TEST001", "note": "",
    })
    target = tmp_path / "handover.zip"
    result = build_handover_package(path=target)
    assert result["path"] == target
    assert target.exists() and target.stat().st_size > 1000
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        assert "README.md" in names
        assert "company.json" in names
        assert "manifest.json" in names
        assert "账套汇总.xlsx" in names
        assert any(name.startswith("data/") and name.endswith(".csv") for name in names)
        assert not any("user_settings" in name for name in names)
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        assert manifest["company"]
        assert manifest["files"]
        assert manifest["tables"]


def test_handover_excludes_secrets(tmp_path):
    # 模拟密钥文件，确认不会被打包
    from pathlib import Path as _Path

    import config

    secret = _Path(config.DATA_DIR) / "user_settings.json"
    secret.write_text('{"api_key": "sk-secret"}', encoding="utf-8")
    target = tmp_path / "handover2.zip"
    build_handover_package(path=target)
    with zipfile.ZipFile(target) as archive:
        assert not any("user_settings" in name for name in archive.namelist())
