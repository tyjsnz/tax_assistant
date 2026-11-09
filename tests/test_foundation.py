"""基础层测试：数据模型规范化 + CSV 存储层（原子读写、ID、过滤、日志、备份）。

这些是 ledger/filing/ui 共同依赖的基础设施，任何回归都会影响全体模块。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import config
from ledger import models, store


class TestModels:
    def test_parse_bool(self):
        for value in ("是", "true", "TRUE", "1", True, 1):
            assert models.parse_bool(value) is True
        for value in ("否", "false", "0", False, 0, "", None, "随便"):
            assert models.parse_bool(value) is False
        assert models.bool_text(True) == "是"
        assert models.bool_text(False) == "否"

    def test_normalize_date(self):
        assert models.normalize_date("2026-01-05") == "2026-01-05"
        assert models.normalize_date("2026/01/05") == "2026-01-05"
        assert models.normalize_date("2026年1月5日") == "2026-01-05"
        assert models.normalize_date("20260105") == "2026-01-05"
        assert models.normalize_date("")  # 默认今天

    def test_normalize_period(self):
        assert models.normalize_period("2026Q1") == "2026Q1"
        assert models.normalize_period("2026-q1") == "2026Q1"
        assert models.normalize_period("2026年第一季度") == "2026Q1"
        assert models.normalize_period("2026-03") == "2026-03"
        assert models.normalize_period("2026-3") == "2026-03"

    def test_derive_exclusive(self):
        exclusive, vat = models.derive_exclusive(103000)
        assert exclusive == pytest.approx(101980.20)
        assert vat == pytest.approx(1019.80)
        assert models.derive_exclusive(0) == (0.0, 0.0)

    def test_normalize_income_derives_amounts(self):
        record = models.normalize_income(
            {"date": "2026-01-05", "client": "云图科技", "inclusive_amount": 20600, "invoice_type": "普票"}
        )
        assert record["exclusive_amount"] == pytest.approx(20396.04)
        assert record["vat_amount"] == pytest.approx(203.96)
        assert record["category"] == "技术服务"

    def test_normalize_income_rejects_bad_enum(self):
        with pytest.raises(ValueError):
            models.normalize_income({"inclusive_amount": 100, "invoice_type": "外星发票"})

    def test_normalize_income_rejects_negative(self):
        with pytest.raises(ValueError):
            models.normalize_income({"inclusive_amount": -100})

    def test_normalize_expense(self):
        record = models.normalize_expense(
            {"date": "2026-01-06", "description": "房租", "amount": "8,000.00", "category": "房租", "is_rd_related": "否"}
        )
        assert record["amount"] == pytest.approx(8000.0)
        assert record["is_rd_related"] is False

    def test_normalize_rd_computes_total(self):
        record = models.normalize_rd(
            {"project_name": "智慧税务", "period": "2026Q1", "personnel_cost": 20000, "material_cost": 3000}
        )
        assert record["total"] == pytest.approx(23000.0)
        assert record["deduction_amount"] == pytest.approx(23000.0)

    def test_normalize_journal_line(self):
        line = models.normalize_journal_line({"account": "银行存款", "direction": "借", "amount": 100})
        assert line["direction"] == "借"
        with pytest.raises(ValueError):
            models.normalize_journal_line({"account": "现金", "direction": "中"})

    def test_normalize_contract(self):
        record = models.normalize_contract({"contract_type": "技术合同", "amount": 100000})
        assert record["contract_type"] == "技术合同"

    def test_normalize_filing(self):
        record = models.normalize_filing(
            {"period": "2026q1", "form_id": "VAT_QUARTERLY", "form_name": "增值税申报表", "data_json": {"a": 1}}
        )
        assert record["period"] == "2026Q1"
        assert record["status"] == "草稿"
        assert record["data_json"] == {"a": 1}
        with pytest.raises(ValueError):
            models.normalize_filing({"data_json": "not-a-dict"})


class TestCSVTable:
    def test_append_and_read(self):
        table = store.table("income")
        row = table.append(
            {
                "date": "2026-01-05",
                "client": "云图科技",
                "description": "技术服务费",
                "category": "技术服务",
                "invoice_type": "普票",
                "inclusive_amount": 20600,
                "exclusive_amount": 20396.04,
                "vat_amount": 203.96,
                "is_rd_related": False,
            }
        )
        assert row["id"].startswith("INC")
        rows = table.read()
        assert len(rows) == 1
        assert rows[0]["inclusive_amount"] == pytest.approx(20600.0)
        assert rows[0]["is_rd_related"] is False
        assert isinstance(rows[0]["exclusive_amount"], float)

    def test_ids_increment(self):
        table = store.table("expense")
        first = table.append({"date": "2026-01-01", "description": "办公", "amount": 10})
        second = table.append({"date": "2026-01-02", "description": "差旅", "amount": 20})
        assert first["id"] == "EXP000001"
        assert second["id"] == "EXP000002"

    def test_update_and_delete(self):
        table = store.table("income")
        row = table.append({"date": "2026-01-05", "client": "A", "inclusive_amount": 100})
        updated = table.update(row["id"], {"client": "B", "inclusive_amount": 200})
        assert updated["client"] == "B"
        assert updated["inclusive_amount"] == pytest.approx(200.0)
        assert table.delete(row["id"]) is True
        assert table.delete(row["id"]) is False
        assert table.read() == []

    def test_update_missing_raises(self):
        with pytest.raises(KeyError):
            store.table("income").update("INC999999", {"client": "X"})

    def test_filter_by_date_and_field(self):
        table = store.table("income")
        table.append({"date": "2026-01-05", "client": "A", "invoice_type": "普票", "inclusive_amount": 100})
        table.append({"date": "2026-04-05", "client": "B", "invoice_type": "专票", "inclusive_amount": 200})
        table.append({"date": "2026-04-06", "client": "B", "invoice_type": "普票", "inclusive_amount": 300})
        assert len(table.filter(start="2026-04-01", end="2026-04-30")) == 2
        assert len(table.filter(start="2026-04-01", end="2026-04-30", invoice_type="专票")) == 1
        assert len(table.filter(client="B")) == 2

    def test_json_and_bool_roundtrip(self):
        table = store.table("filings")
        row = table.append(
            {
                "period": "2026Q1",
                "form_id": "VAT_QUARTERLY",
                "form_name": "增值税申报表",
                "status": "草稿",
                "data_json": {"total": 101980.2, "lines": [1, 2]},
            }
        )
        read_back = table.get(row["id"])
        assert read_back["data_json"] == {"total": 101980.2, "lines": [1, 2]}
        assert read_back["status"] == "草稿"

    def test_replace_all(self):
        table = store.table("contracts")
        table.append({"date": "2026-01-01", "contract_type": "技术合同", "amount": 100})
        count = table.replace_all(
            [
                {"date": "2026-02-01", "contract_type": "买卖合同", "amount": 200},
                {"date": "2026-02-02", "contract_type": "技术合同", "amount": 300},
            ]
        )
        assert count == 2
        rows = table.read()
        assert len(rows) == 2
        assert rows[0]["id"].startswith("CT")
        assert len({row["id"] for row in rows}) == 2

    def test_backup(self):
        table = store.table("income")
        table.append({"date": "2026-01-05", "client": "A", "inclusive_amount": 100})
        backup = table.backup(tag="test")
        assert backup.exists()
        assert backup.parent == Path(config.BACKUP_DIR)
        assert "income_ledger" in backup.name

    def test_next_id(self):
        table = store.table("rd")
        assert table.next_id() == "RD000001"
        table.append({"project_name": "P", "period": "2026Q1"})
        assert table.next_id() == "RD000002"

    def test_storage_stats_and_init(self):
        store.init_storage()
        stats = store.storage_stats()
        assert set(stats) == set(models.TABLE_NAMES)
        assert all(isinstance(value, int) for value in stats.values())

    def test_missing_file_creates_header(self):
        table = store.table("journal_entries")
        assert table.read() == []
        assert table.path.exists()
        header = table.path.read_text(encoding="utf-8-sig").strip().split(",")
        assert header[:3] == ["id", "date", "entry_no"]


class TestAuditLog:
    def test_append_and_read_log(self):
        store.append_log(
            "calculation_log",
            {"calc_type": "vat", "period": "2026Q1", "inputs": {"total": 1}, "result": {"vat": 0}},
        )
        rows = store.read_log("calculation_log")
        assert len(rows) == 1
        assert rows[0]["calc_type"] == "vat"
        assert rows[0]["timestamp"]
        assert json.loads(rows[0]["inputs"]) == {"total": 1}

    def test_newest_first(self):
        for index in range(3):
            store.append_log("user_action_log", {"action": f"act{index}", "detail": ""})
        rows = store.read_log("user_action_log")
        assert [row["action"] for row in rows] == ["act2", "act1", "act0"]

    def test_log_limit(self):
        for index in range(5):
            store.append_log("journal_log", {"entry_no": f"记-{index}"})
        assert len(store.read_log("journal_log", limit=2)) == 2

    def test_read_missing_log(self):
        assert store.read_log("not_exist_log") == []
