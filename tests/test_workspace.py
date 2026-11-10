"""P1-A 多公司 / 地区参数 / schema 迁移测试（Lead 维护）。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import config
import workspace
from ledger import income_ledger, store


def _reset_paths() -> None:
    root = Path(config.DATA_ROOT)
    config.DATA_DIR = root
    config.BACKUP_DIR = root / "backups"
    config.UPLOAD_DIR = root / "uploads"
    store.reset_cache()


class TestRegistry:
    def test_ensure_default_creates_and_activates(self):
        record = workspace.ensure_default()
        assert record["id"] == workspace.DEFAULT_COMPANY_ID
        assert workspace.active_company_id() == workspace.DEFAULT_COMPANY_ID
        assert Path(config.DATA_DIR) == workspace.company_dir(workspace.DEFAULT_COMPANY_ID)
        assert workspace.get_company() is not None
        assert workspace.registry_path().exists()
        assert workspace.schema_version() == workspace.SCHEMA_VERSION

    def test_add_and_switch_company_data_isolation(self):
        default_id = workspace.active_company_id()
        income_ledger.add_income("2026-01-05", "客户A", 10100, invoice_type="普票", description="A 公司收入")
        assert store.table("income").count() == 1

        record = workspace.add_company(
            {
                "name": "第二测试有限公司",
                "short_name": "第二公司",
                "unified_social_credit_code": "91530000000000000X",
                "region_id": "yunnan-county-town",
            }
        )
        assert record["id"] != default_id
        assert not record["id"].startswith("..")

        workspace.activate(record["id"])
        assert workspace.active_company_id() == record["id"]
        assert Path(config.DATA_DIR) == workspace.company_dir(record["id"])
        assert store.table("income").count() == 0  # 新公司账套独立

        income_ledger.add_income("2026-02-05", "客户B", 20200, invoice_type="普票", description="B 公司收入")
        assert store.table("income").count() == 1

        workspace.activate(default_id)
        assert store.table("income").count() == 1
        assert store.table("income").read()[0]["client"] == "客户A"

    def test_update_company_and_region(self):
        active = workspace.active_company_id()
        updated = workspace.update_company(active, {"region_id": "yunnan-county-town", "short_name": "云睿（县城）"})
        assert updated["region_id"] == "yunnan-county-town"
        assert updated["city_maintenance_tax_rate"] == pytest.approx(0.05)
        region = config.get_region()
        assert region["city_maintenance_tax_rate"] == pytest.approx(0.05)
        assert "县城" in region["label"] or region["label"]
        # 算税随之变化：4000 增值税在 5% 城建税下附加税费 = 4000×10%×50% = 200
        from engine import calc_vat

        result = calc_vat(404000, 0, "2026Q1")
        assert result["surtax"] == pytest.approx(200.0)
        assert result["city_maintenance_tax_rate"] == pytest.approx(0.05)

    def test_duplicate_company_id_rejected(self):
        active = workspace.active_company_id()
        with pytest.raises(ValueError):
            workspace.add_company({"name": "重复公司"}, company_id=active)

    def test_delete_rules(self):
        active = workspace.active_company_id()
        with pytest.raises(ValueError):
            workspace.delete_company(active)  # 当前公司不能删
        record = workspace.add_company({"name": "待删除公司"})
        result = workspace.delete_company(record["id"])
        assert result["action"] == "archived"
        assert workspace.get_company(record["id"]) is None
        assert (workspace.companies_root() / "_deleted").exists()

    def test_cannot_delete_last_company(self):
        # 删除非当前公司后只剩当前公司，再尝试删除当前公司应报错
        record = workspace.add_company({"name": "临时公司"})
        workspace.delete_company(record["id"])
        active = workspace.active_company_id()
        with pytest.raises(ValueError):
            workspace.delete_company(active)

    def test_list_companies_metadata(self):
        workspace.add_company({"name": "列表测试有限公司", "short_name": "列表"})
        rows = workspace.list_companies()
        assert len(rows) >= 2
        active_rows = [row for row in rows if row["is_active"]]
        assert len(active_rows) == 1
        assert all("data_dir" in row and "has_data" in row for row in rows)


class TestLegacyMigration:
    def _prepare_legacy_flat_layout(self) -> Path:
        root = Path(config.DATA_ROOT)
        shutil.rmtree(workspace.companies_root(), ignore_errors=True)
        workspace.registry_path().unlink(missing_ok=True)
        workspace.schema_path().unlink(missing_ok=True)
        _reset_paths()
        config.save_user_settings(
            {"company": {"name": "旧版网络科技有限公司", "short_name": "旧版", "county": "某县"}}
        )
        income_ledger.add_income("2026-01-05", "旧版客户", 10100, invoice_type="普票", description="迁移前收入")
        return root

    def test_flat_layout_migrated_to_company(self):
        root = self._prepare_legacy_flat_layout()
        legacy_files = list(root.glob("*.csv"))
        assert legacy_files, "测试前置：根目录应存在旧版 CSV"

        record = workspace.ensure_default()

        assert record["id"] == workspace.DEFAULT_COMPANY_ID
        assert workspace.schema_version() == workspace.SCHEMA_VERSION
        target = workspace.company_dir(workspace.DEFAULT_COMPANY_ID)
        assert (target / "income_ledger.csv").exists()
        assert not list(root.glob("*.csv")), "旧版根目录 CSV 应已迁移"
        assert Path(config.DATA_DIR) == target
        rows = store.table("income").read()
        assert len(rows) == 1
        assert rows[0]["client"] == "旧版客户"
        # 旧版 user_settings 中的企业信息被承接，并清理旧字段
        company = config.get_company()
        assert company["name"] == "旧版网络科技有限公司"
        assert company["county"] == "某县"
        assert not (config.load_user_settings().get("company") or {})

    def test_schema_file_written(self):
        workspace.ensure_schema()
        data = json.loads(workspace.schema_path().read_text(encoding="utf-8"))
        assert data["version"] == workspace.SCHEMA_VERSION
        assert data["app_version"]


class TestReset:
    def test_reset_all_restores_single_company(self):
        workspace.add_company({"name": "临时公司2"})
        assert len(workspace.list_companies()) >= 2
        result = workspace.reset_all()
        assert result["company_id"] == workspace.DEFAULT_COMPANY_ID
        assert len(workspace.list_companies()) == 1
        assert store.table("income").count() == 0

    def test_workspace_summary(self):
        summary = workspace.workspace_summary()
        assert summary["data_root"]
        assert summary["schema_version"] == workspace.SCHEMA_VERSION
        assert summary["active_company_id"] == workspace.active_company_id()
        assert summary["companies"]
