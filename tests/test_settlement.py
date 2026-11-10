"""企业所得税汇算清缴后端测试（P1-C §1）：年度数据汇总、年度输入、纳税调整、
亏损弥补、汇算测算与检查清单。
"""

from __future__ import annotations

import pytest

from engine import calc_settlement
from ledger import expense_ledger, income_ledger, journal, rd_auxiliary, settlement
from ledger.store import read_log


# =============================================================================
# 测试辅助
# =============================================================================
def _seed_year(year: str = "2026") -> None:
    """造一套年度基础数据：收入 5 万（含税）、费用 2.9 万（含研发材料）、研发手工归集 5000。"""
    first = income_ledger.add_income(f"{year}-03-10", "云图科技", 30000.0)
    journal.post_entry(journal.build_income_entry(first))
    second = income_ledger.add_income(f"{year}-08-10", "滇南数据", 20000.0)
    journal.post_entry(journal.build_income_entry(second))
    for date, amount, category, rd in (
        (f"{year}-02-10", 24000.0, "工资", False),
        (f"{year}-06-10", 3000.0, "办公", False),
        (f"{year}-07-10", 2000.0, "研发材料", True),
    ):
        expense = expense_ledger.add_expense(date, f"{category}支出", amount, category=category, is_rd_related=rd)
        journal.post_entry(journal.build_expense_entry(expense))
    rd_auxiliary.add_rd_record("研发项目A", f"{year}Q1", personnel_cost=5000.0, note="手工归集")


def _seed_filing(period: str, amount: float, form_id: str = "CIT_PREPAY") -> dict:
    from filing.progress import save_filing

    bundle = {
        "form_id": form_id,
        "form_name": "企业所得税月（季）度预缴纳税申报表（A类）",
        "period": period,
        "totals": {"prepay_payable": amount},
        "summary": {"level": "PASS", "text": ""},
    }
    return save_filing(bundle, status="已确认")


# =============================================================================
# 1. 年度台账数据
# =============================================================================
class TestAnnualLedgerData:
    def test_basic_totals(self):
        _seed_year()
        data = settlement.annual_ledger_data("2026")
        assert data["year"] == "2026"
        assert data["start"] == "2026-01-01" and data["end"] == "2026-12-31"
        assert data["revenue_inclusive"] == 50000.0
        assert data["revenue_exclusive"] == 49504.95
        assert data["expense_total"] == 29000.0
        assert data["profit"] == 20504.95
        assert data["salary_total"] == 24000.0
        assert data["expense_by_category"]["工资"] == 24000.0
        assert data["counts"]["income"] == 2
        assert data["counts"]["expense"] == 3
        assert data["counts"]["journal_entries"] == 5
        assert len(data["monthly"]) == 12
        assert data["monthly"][2]["month"] == "2026-03"
        assert data["monthly"][2]["income_exclusive"] == 29702.97
        assert data["monthly"][1]["expense"] == 24000.0
        assert data["monthly"][11]["income_exclusive"] == 0.0

    def test_rd_cost_and_deduction(self):
        _seed_year()
        data = settlement.annual_ledger_data("2026")
        assert data["rd_cost"] == 7000.0  # 研发材料 2000 + 手工人员人工 5000
        assert data["rd_deduction"] == 7000.0
        assert data["rd_detail"]["manual_count"] == 1

    def test_prepaid_from_filings(self):
        _seed_filing("2026Q1", 1234.56)
        _seed_filing("2026Q2", 567.89)
        data = settlement.annual_ledger_data("2026")
        assert data["prepaid_tax"] == 1802.45
        assert data["prepaid_source"] == "filings"
        assert data["counts"]["journal_entries"] == 0

    def test_prepaid_fallback_to_inputs(self):
        settlement.save_annual_inputs("2026", {"prepaid_tax": 999.99})
        data = settlement.annual_ledger_data("2026")
        assert data["prepaid_tax"] == 999.99
        assert data["prepaid_source"] == "inputs"

    def test_empty_year(self):
        data = settlement.annual_ledger_data("2025")
        assert data["revenue_exclusive"] == 0.0
        assert data["expense_total"] == 0.0
        assert data["profit"] == 0.0
        assert data["prepaid_tax"] == 0.0
        assert data["warnings"]

    def test_invalid_year(self):
        with pytest.raises(ValueError, match="年度格式"):
            settlement.annual_ledger_data("abc")
        with pytest.raises(ValueError):
            settlement.normalize_year("20")


# =============================================================================
# 2. 年度输入（annual_settlements）
# =============================================================================
class TestAnnualInputs:
    def test_defaults(self):
        inputs = settlement.get_annual_inputs("2026")
        for key in settlement.INPUT_KEYS:
            assert key in inputs
        assert inputs["employees"] is None
        assert inputs["accounting_profit"] is None
        assert inputs["rd_deduction"] is None
        assert inputs["is_restricted_industry"] is False
        assert inputs["entertainment_actual"] == 0.0

    def test_save_whitelist_and_upsert(self):
        first = settlement.save_annual_inputs(
            "2026",
            {
                "entertainment_actual": 1234.567,
                "employees": "12",
                "total_assets": 5000000,
                "is_restricted_industry": "否",
                "unknown_key": "忽略",
                "note": "测试",
            },
        )
        assert first["year"] == "2026"
        assert first["status"] == "草稿"
        data = settlement.get_annual_inputs("2026")
        assert data["entertainment_actual"] == 1234.57
        assert data["employees"] == 12
        assert data["total_assets"] == 5000000.0
        assert data["is_restricted_industry"] is False
        assert "unknown_key" not in data

        second = settlement.save_annual_inputs("2026", {"advertising_actual": 8000})
        assert second["id"] == first["id"]  # upsert by year
        assert settlement.get_annual_inputs("2026")["advertising_actual"] == 8000.0
        actions = [row["action"] for row in read_log("user_action_log")]
        assert "保存年度汇算输入" in actions

    def test_save_validations(self):
        with pytest.raises(ValueError, match="不能为负数"):
            settlement.save_annual_inputs("2026", {"entertainment_actual": -1})
        with pytest.raises(ValueError, match="从业人数"):
            settlement.save_annual_inputs("2026", {"employees": "abc"})
        with pytest.raises(ValueError, match="从业人数不能为负数"):
            settlement.save_annual_inputs("2026", {"employees": -2})
        with pytest.raises(ValueError):
            settlement.save_annual_inputs("abc", {})

    def test_save_returns_flat_inputs(self):
        """save_annual_inputs 返回行字段 + 扁平输入，API/CLI 可直接读取。"""
        saved = settlement.save_annual_inputs("2026", {"entertainment_actual": 12000.0, "employees": 5})
        assert saved["entertainment_actual"] == 12000.0
        assert saved["employees"] == 5
        assert saved["year"] == "2026"
        assert saved["status"] == "草稿"
        assert saved["data_json"]["entertainment_actual"] == 12000.0

    def test_partial_save_keeps_ledger_rd_deduction(self):
        """只保存部分输入时，研发加计扣除仍应使用台账汇总值而不是 0。"""
        _seed_year()
        annual = settlement.annual_ledger_data("2026")
        assert annual["rd_deduction"] == 7000.0
        settlement.save_annual_inputs("2026", {"entertainment_actual": 10000.0})
        inputs = settlement.get_annual_inputs("2026")
        assert inputs["rd_deduction"] is None
        assert inputs["accounting_profit"] is None
        result = settlement.compute_settlement("2026")
        assert result["research_deduction"] == annual["rd_deduction"]
        assert result["research_deduction"] == 7000.0
        assert result["accounting_profit"] == annual["profit"]

    def test_mark_status(self):
        settlement.save_annual_inputs("2026", {"prepaid_tax": 100})
        updated = settlement.mark_settlement_status("2026", "已确认")
        assert updated["status"] == "已确认"
        assert settlement.get_settlement_record("2026")["status"] == "已确认"
        with pytest.raises(ValueError, match="不合法"):
            settlement.mark_settlement_status("2026", "已申报")
        created = settlement.mark_settlement_status("2027", "已确认")
        assert created["year"] == "2027"
        actions = [row["action"] for row in read_log("user_action_log")]
        assert "年度汇算状态变更" in actions


# =============================================================================
# 3. 纳税调整清单
# =============================================================================
class TestAdjustments:
    def test_add_and_list(self):
        record = settlement.add_adjustment(
            "2026", "税收滞纳金", "调增", 8000.0, accounting_amount=8000.0, tax_amount=0.0, basis="不得税前扣除", source="企业所得税法第十条"
        )
        assert record["year"] == "2026"
        assert record["direction"] == "调增"
        assert record["adjustment_amount"] == 8000.0
        assert record["basis"] == "不得税前扣除"
        assert len(settlement.list_adjustments("2026")) == 1
        assert settlement.list_adjustments("2025") == []
        assert "新增纳税调整" in [row["action"] for row in read_log("user_action_log")]

    def test_validations(self):
        with pytest.raises(ValueError, match="调整项目"):
            settlement.add_adjustment("2026", "", "调增", 100)
        with pytest.raises(ValueError, match="方向"):
            settlement.add_adjustment("2026", "罚款", "其他", 100)
        with pytest.raises(ValueError, match="不能为负数"):
            settlement.add_adjustment("2026", "罚款", "调增", -100)
        with pytest.raises(ValueError, match="会计金额"):
            settlement.add_adjustment("2026", "罚款", "调增", 100, accounting_amount=-1)

    def test_update_and_delete(self):
        record = settlement.add_adjustment("2026", "罚款", "调增", 1000.0)
        updated = settlement.update_adjustment(record["id"], {"adjustment_amount": 1500.0, "direction": "调减"})
        assert updated["adjustment_amount"] == 1500.0
        assert updated["direction"] == "调减"
        assert settlement.delete_adjustment(record["id"]) is True
        assert settlement.list_adjustments("2026") == []
        assert settlement.delete_adjustment(record["id"]) is False
        with pytest.raises(KeyError):
            settlement.update_adjustment("ADJ999999", {"adjustment_amount": 1})


# =============================================================================
# 4. 亏损弥补台账
# =============================================================================
class TestLosses:
    def test_add_loss(self):
        record = settlement.add_loss("2023", 50000.0, used_amount=10000.0, note="2023 年度亏损")
        assert record["year"] == "2023"
        assert record["loss_amount"] == 50000.0
        assert record["used_amount"] == 10000.0
        assert record["remaining"] == 40000.0
        assert record["expire_year"] == "2028"
        assert "新增亏损结转" in [row["action"] for row in read_log("user_action_log")]

    def test_loss_validations(self):
        with pytest.raises(ValueError, match="大于 0"):
            settlement.add_loss("2023", 0)
        with pytest.raises(ValueError, match="不能为负数"):
            settlement.add_loss("2023", 100, used_amount=-1)
        with pytest.raises(ValueError, match="不能超过"):
            settlement.add_loss("2023", 100, used_amount=200)

    def test_update_recalculates_remaining(self):
        record = settlement.add_loss("2023", 5000.0)
        updated = settlement.update_loss(record["id"], {"used_amount": 3000.0, "note": "已弥补"})
        assert updated["used_amount"] == 3000.0
        assert updated["remaining"] == 2000.0
        with pytest.raises(ValueError, match="不能超过"):
            settlement.update_loss(record["id"], {"used_amount": 8000.0})
        with pytest.raises(KeyError):
            settlement.update_loss("LOS999999", {"used_amount": 0})

    def test_list_filter_and_delete(self):
        first = settlement.add_loss("2022", 10000.0)
        settlement.add_loss("2023", 20000.0)
        assert len(settlement.list_losses()) == 2
        assert [row["year"] for row in settlement.list_losses("2023")] == ["2023"]
        assert settlement.delete_loss(first["id"]) is True
        assert len(settlement.list_losses()) == 1


# =============================================================================
# 5. 汇算测算
# =============================================================================
class TestComputeSettlement:
    def test_basic_settlement_matches_engine(self):
        _seed_year()
        result = settlement.compute_settlement("2026")
        assert result["accounting_profit"] == 20504.95
        assert result["research_deduction"] == 7000.0
        assert result["taxable_income"] == 13504.95
        assert result["qualifies_small_low_profit"] is True
        assert result["applicable_rate"] == 0.05
        assert result["corporate_tax"] == 675.25
        assert result["prepaid_tax"] == 0.0
        assert result["payable"] == 675.25
        assert result["refund"] == 0.0
        assert isinstance(result["annual"], dict)
        assert isinstance(result["inputs"], dict)
        assert result["adjustments"] == []
        assert result["losses"] == []

        expected = calc_settlement(
            year="2026",
            revenue_exclusive=49504.95,
            cost_total=29000.0,
            salary_total=24000.0,
            rd_deduction=7000.0,
            prepaid_tax=0.0,
        )
        assert result["taxable_income"] == expected["taxable_income"]
        assert result["corporate_tax"] == expected["corporate_tax"]

    def test_limit_adjustments(self):
        _seed_year()
        settlement.save_annual_inputs(
            "2026",
            {
                "entertainment_actual": 5000.0,
                "advertising_actual": 10000.0,
                "welfare_actual": 4000.0,
                "education_actual": 3000.0,
                "union_actual": 600.0,
            },
        )
        result = settlement.compute_settlement("2026")
        keys = {item["key"]: item for item in result["limit_adjustment_items"]}
        assert keys["business_entertainment"]["adjustment_add"] == 4752.48
        assert keys["advertising"]["adjustment_add"] == 2574.26
        assert keys["welfare"]["adjustment_add"] == 640.0
        assert keys["education"]["adjustment_add"] == 1080.0
        assert keys["union"]["adjustment_add"] == 120.0
        total_add = 4752.48 + 2574.26 + 640.0 + 1080.0 + 120.0
        assert result["limit_adjustment_add"] == round(total_add, 2)
        assert result["taxable_income_before_loss"] == round(
            20504.95 + total_add - 7000.0, 2
        )

    def test_manual_adjustments_and_losses(self):
        _seed_year()
        settlement.add_adjustment("2026", "税收滞纳金", "调增", 10000.0, basis="不得税前扣除")
        settlement.add_loss("2022", 5000.0)
        result = settlement.compute_settlement("2026")
        assert result["manual_adjustment_add"] == 10000.0
        assert result["loss_used"] == 5000.0
        assert result["taxable_income"] == round(20504.95 + 10000.0 - 7000.0 - 5000.0, 2)
        assert result["loss_carryforward"]["lines"][0]["status"] == "已弥补"

    def test_expired_loss_not_used(self):
        _seed_year()
        settlement.add_loss("2019", 5000.0)  # 到期 2024，已过期
        result = settlement.compute_settlement("2026")
        assert result["loss_used"] == 0.0
        assert any("超过" in warning for warning in result["warnings"])
        line = result["loss_carryforward"]["lines"][0]
        assert line["status"] == "已过期"

    def test_accounting_profit_override(self):
        _seed_year()
        settlement.save_annual_inputs("2026", {"accounting_profit": 100000.0})
        result = settlement.compute_settlement("2026")
        assert result["accounting_profit"] == 100000.0
        assert result["taxable_income"] == 93000.0

    def test_prepaid_override(self):
        _seed_year()
        _seed_filing("2026Q1", 1802.45)
        settlement.save_annual_inputs("2026", {"prepaid_tax": 500.0})
        result = settlement.compute_settlement("2026")
        assert result["prepaid_tax"] == 500.0
        assert result["prepaid_source"] == "inputs"
        assert result["payable"] == 175.25

    def test_small_low_profit_not_qualified(self):
        _seed_year()
        settlement.save_annual_inputs("2026", {"employees": 400, "total_assets": 60000000})
        result = settlement.compute_settlement("2026")
        assert result["qualifies_small_low_profit"] is False
        assert result["applicable_rate"] == 0.25
        assert result["corporate_tax"] == round(13504.95 * 0.25, 2)
        assert result["tax_reduction"] == 0.0

    def test_calculation_audit_log(self):
        _seed_year()
        settlement.compute_settlement("2026")
        logs = read_log("calculation_log")
        assert any(row["calc_type"] == "企业所得税汇算" for row in logs)


# =============================================================================
# 6. 检查清单
# =============================================================================
class TestChecklist:
    def test_item_coverage(self):
        _seed_year()
        result = settlement.compute_settlement("2026")
        checklist = settlement.settlement_checklist("2026", result)
        items = {item["item"]: item for item in checklist}
        for name in (
            "收入与台账一致",
            "费用凭证完整",
            "研发辅助账",
            "亏损弥补台账",
            "预缴税额核对",
            "小型微利企业条件",
            "资料留存",
            "申报期限",
            "纳税调整项完整性",
        ):
            assert name in items, name
        for item in checklist:
            assert item["status"] in ("PASS", "WARNING", "ERROR")
            assert item["message"]
            assert "action" in item

    def test_income_mismatch_error(self):
        _seed_year()
        result = settlement.compute_settlement("2026")
        tampered = {**result, "revenue_exclusive": 99999.99}
        checklist = settlement.settlement_checklist("2026", tampered)
        item = next(row for row in checklist if row["item"] == "收入与台账一致")
        assert item["status"] == "ERROR"

    def test_prepaid_warning_and_pass(self):
        _seed_year()
        result = settlement.compute_settlement("2026")
        item = next(row for row in settlement.settlement_checklist("2026", result) if row["item"] == "预缴税额核对")
        assert item["status"] == "WARNING"

        _seed_filing("2026Q1", result["prepaid_tax"] or 100.0)
        result2 = settlement.compute_settlement("2026")
        item2 = next(row for row in settlement.settlement_checklist("2026", result2) if row["item"] == "预缴税额核对")
        assert item2["status"] == "PASS"

    def test_loss_warning_when_current_year_loss(self):
        expense_ledger.add_expense("2026-03-10", "工资支出", 50000.0, category="工资")
        result = settlement.compute_settlement("2026")
        item = next(row for row in settlement.settlement_checklist("2026", result) if row["item"] == "亏损弥补台账")
        assert item["status"] == "WARNING"
        assert "会计利润为负" in item["message"]

    def test_adjustment_omission_warning(self):
        _seed_year()
        result = settlement.compute_settlement("2026")
        tampered = dict(result)
        tampered["inputs"] = {**result["inputs"], "entertainment_actual": 5000.0}
        tampered["limit_adjustment_items"] = []
        checklist = settlement.settlement_checklist("2026", tampered)
        item = next(row for row in checklist if row["item"] == "纳税调整项完整性")
        assert item["status"] == "WARNING"
        assert "业务招待费" in item["message"]

    def test_expired_loss_warning(self):
        _seed_year()
        settlement.add_loss("2019", 5000.0)
        result = settlement.compute_settlement("2026")
        item = next(row for row in settlement.settlement_checklist("2026", result) if row["item"] == "亏损弥补台账")
        assert item["status"] == "WARNING"
        assert "超过" in item["message"]

    def test_deadline_status(self):
        _seed_year()
        future = settlement.compute_settlement("2026")
        deadline_item = next(row for row in settlement.settlement_checklist("2026", future) if row["item"] == "申报期限")
        assert deadline_item["status"] == "PASS"
        assert "2027-05-31" in deadline_item["message"]

        past = settlement.compute_settlement("2024")
        past_item = next(row for row in settlement.settlement_checklist("2024", past) if row["item"] == "申报期限")
        assert past_item["status"] == "WARNING"
        assert "逾期" in past_item["message"]
