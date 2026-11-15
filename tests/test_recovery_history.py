"""历史申报补录与完整性检查测试（v1.38）。"""

from __future__ import annotations

import pytest

from recovery import history


def test_add_list_delete():
    row = history.add_filing("2026-03", "增值税及附加", filed_date="2026-04-15", tax_amount=123.45, status="已申报", note="测试")
    assert row["period"] == "2026-03"
    assert any(item["id"] == row["id"] for item in history.list_filings())
    history.delete_filing(row["id"])
    assert all(item["id"] != row["id"] for item in history.list_filings())


def test_duplicate_rejected():
    history.add_filing("2026Q1", "印花税", status="零申报")
    with pytest.raises(ValueError):
        history.add_filing("2026Q1", "印花税", status="已申报")


def test_template_and_import():
    data = history.export_template()
    assert "所属期" in data.decode("utf-8-sig")
    csv_data = "所属期,税种,申报日期,税额,状态,备注\n2026-01,个人所得税扣缴,2026-02-15,88.00,已申报,导入\n".encode("utf-8-sig")
    result = history.import_csv(csv_data)
    assert result["added"] == 1
    assert not result["skipped"]


def test_completeness_shape():
    result = history.completeness()
    assert result["total"] >= 5
    assert 0 <= result["score"] <= 100
    assert all({"key", "label", "status", "detail"} <= set(item) for item in result["items"])
