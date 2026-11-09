"""算税引擎单元测试（确定性代码，不依赖网络与 LLM）。

覆盖：增值税免税/专票/超限/边界、企业所得税小微优惠与超限、季度预缴、
研发费用加计扣除（含其他相关费用限额、形成无形资产）、印花税、申报日历与提醒、
金额四舍五入与免责声明。
"""

from __future__ import annotations

from datetime import date

import pytest

import config
from engine import (
    aggregate_rd_expenses,
    calc_cit_prepay,
    calc_corp_tax,
    calc_rd_deduction,
    calc_stamp_tax,
    calc_vat,
    current_quarter,
    deadline_status,
    filing_deadlines,
    get_params,
    get_rate_table,
    next_deadline,
    pct,
    prev_quarter,
    quarter_end,
    quarter_of,
    quarter_range,
    quarter_start,
    reminder_text,
    reminders,
    round2,
    shift_to_workday,
    upcoming_deadlines,
    yuan,
)

DISCLAIMER = config.DISCLAIMER


# =============================================================================
# 金额工具
# =============================================================================
class TestMoney:
    def test_round_half_up(self):
        assert round2(1019.805) == 1019.81
        assert round2("1019.804") == 1019.80
        assert round2(None) == 0.0
        assert round2("1,234.567") == 1234.57

    def test_yuan_format(self):
        assert yuan(101980.2) == "101,980.20"
        assert yuan(0) == "0.00"

    def test_pct(self):
        assert pct(0.01) == "1.00%"
        assert pct(0.25) == "25.00%"


# =============================================================================
# 增值税（规范文档测试用例 + 扩展）
# =============================================================================
class TestVAT:
    def test_full_exemption(self):
        """全普票+未开票，不超30万 → 全免。"""
        result = calc_vat(quarterly_total_inclusive=101000, special_invoice_inclusive=0, period="2026Q1")
        assert result["vat_payable"] == 0
        assert result["within_exemption_limit"] is True
        assert result["vat_exempt"] > 0
        assert result["surtax"] == 0
        assert result["total_tax"] == 0

    def test_with_special_invoice(self):
        """含专票 → 专票部分不免，普票部分免征。"""
        result = calc_vat(quarterly_total_inclusive=101000, special_invoice_inclusive=10100, period="2026Q1")
        assert result["vat_payable"] > 0
        assert result["vat_exempt"] > 0
        assert result["within_exemption_limit"] is True
        # 专票不含税 10,000 × 1% = 100.00
        assert result["vat_payable"] == pytest.approx(100.0)
        assert result["ordinary_exclusive"] == pytest.approx(90000.0)

    def test_exceed_limit(self):
        """超过30万 → 全额按 1%。"""
        result = calc_vat(quarterly_total_inclusive=404000, special_invoice_inclusive=0, period="2026Q1")
        assert result["within_exemption_limit"] is False
        assert result["vat_payable"] > 0
        assert result["vat_exempt"] == 0
        assert result["total_exclusive"] == pytest.approx(400000.0)
        assert result["vat_payable"] == pytest.approx(4000.0)

    def test_exact_boundary(self):
        """边界值：恰好 30 万不含税（303,000 / 1.01 = 300,000）→ 免。"""
        result = calc_vat(quarterly_total_inclusive=303000, special_invoice_inclusive=0, period="2026Q1")
        assert result["within_exemption_limit"] is True
        assert result["vat_payable"] == 0

    def test_boundary_plus_one_cent(self):
        """超过边界 0.01 元 → 不再免税。"""
        result = calc_vat(quarterly_total_inclusive=303000.01, special_invoice_inclusive=0, period="2026Q1")
        assert result["within_exemption_limit"] is False
        assert result["vat_payable"] > 0

    def test_zero_sales(self):
        """零申报。"""
        result = calc_vat(quarterly_total_inclusive=0, special_invoice_inclusive=0, period="2026Q1")
        assert result["vat_payable"] == 0
        assert result["surtax"] == 0
        assert "零申报" in result["note"]

    def test_spec_example_output(self):
        """规范文档示例：103,000 含税 → 不含税 101,980.20，免征 1,019.80。"""
        result = calc_vat(103000, 0, "2026Q1")
        assert result["total_exclusive"] == pytest.approx(101980.20)
        assert result["vat_exempt"] == pytest.approx(1019.80)
        assert result["quarter"] == "2026Q1"

    def test_return_lines_taxable(self):
        """超限场景的申报表栏次勾稽：第18栏=第1栏×3%，第22栏=第1栏×2%，第24栏=第18-22。"""
        result = calc_vat(404000, 0, "2026Q1")
        lines = result["return_lines"]
        assert lines["line_1_taxable_sales"] == pytest.approx(400000.0)
        assert lines["line_18_tax_amount"] == pytest.approx(12000.0)
        assert lines["line_22_reduction"] == pytest.approx(8000.0)
        assert lines["line_24_payable"] == pytest.approx(4000.0)
        assert lines["line_18_tax_amount"] - lines["line_22_reduction"] == pytest.approx(lines["line_24_payable"])

    def test_return_lines_exempt(self):
        """免税场景：第10栏免税销售额 = 全部销售额，第19栏免税额 = 销售额×3%。"""
        result = calc_vat(103000, 0, "2026Q1")
        lines = result["return_lines"]
        assert lines["line_10_exempt_sales"] == pytest.approx(101980.20)
        assert lines["line_18_tax_amount"] == 0
        assert lines["line_24_payable"] == 0
        assert lines["line_19_tax_exempt_amount"] == pytest.approx(3059.41)

    def test_ordinary_limit_basis_option(self):
        """limit_basis="ordinary" 时仅以普票+未开票销售额判断免税。"""
        total = 404000
        special = 101000  # 专票不含税 100,000，普票不含税 300,000
        result = calc_vat(total, special, "2026Q1", limit_basis="ordinary")
        assert result["within_exemption_limit"] is True
        assert result["vat_payable"] == pytest.approx(1000.0)
        result_total = calc_vat(total, special, "2026Q1", limit_basis="total")
        assert result_total["within_exemption_limit"] is False
        assert result_total["vat_payable"] == pytest.approx(4000.0)

    def test_special_larger_than_total_is_capped(self):
        """专票金额大于总销售额时封顶并告警。"""
        result = calc_vat(10000, 50000, "2026Q1")
        assert result["special_inclusive"] == pytest.approx(10000.0)
        assert any("封顶" in warning for warning in result["warnings"])

    def test_negative_input_rejected(self):
        with pytest.raises(ValueError):
            calc_vat(-1, 0)

    def test_disclaimer_and_breakdown_present(self):
        result = calc_vat(103000, 0, "2026Q1")
        assert result["disclaimer"] == DISCLAIMER
        assert isinstance(result["breakdown"], list) and len(result["breakdown"]) >= 6
        assert result["policy_version"]
        assert result["last_verified"]

    def test_surtax_halved_on_vat_payable(self):
        """附加税费 = 增值税 × 12% × 50%。"""
        result = calc_vat(404000, 0, "2026Q1")
        assert result["vat_payable"] == pytest.approx(4000.0)
        assert result["surtax_full"] == pytest.approx(480.0)
        assert result["surtax"] == pytest.approx(240.0)


# =============================================================================
# 企业所得税
# =============================================================================
class TestCorpTax:
    def test_small_profit_5pct(self):
        """年利润 20 万、研发 5 万 → 应纳税所得额 15 万，5%。"""
        result = calc_corp_tax(annual_profit=200000, rd_cost=50000)
        assert result["effective_rate"] == pytest.approx(0.05)
        assert result["taxable_income"] == pytest.approx(150000.0)
        assert result["corp_tax"] == pytest.approx(7500.0)
        assert result["qualifies_small_low_profit"] is True

    def test_exceed_3million(self):
        """年应纳税所得额超 300 万 → 25%。"""
        result = calc_corp_tax(annual_profit=3500000, rd_cost=0)
        assert result["effective_rate"] == pytest.approx(0.25)
        assert result["corp_tax"] == pytest.approx(875000.0)
        assert result["qualifies_small_low_profit"] is False

    def test_exact_3million_still_small(self):
        result = calc_corp_tax(annual_profit=3000000, rd_cost=0)
        assert result["qualifies_small_low_profit"] is True
        assert result["corp_tax"] == pytest.approx(150000.0)

    def test_employee_threshold(self):
        """从业人数超 300 → 不符合小微。"""
        result = calc_corp_tax(annual_profit=100000, employees=301, total_assets=1000000)
        assert result["qualifies_small_low_profit"] is False
        assert any("从业人数" in reason for reason in result["small_low_profit_check"]["reasons"])

    def test_asset_threshold(self):
        result = calc_corp_tax(annual_profit=100000, total_assets=60_000_000)
        assert result["qualifies_small_low_profit"] is False

    def test_revenue_cost_input(self):
        """可用收入-成本计算利润。"""
        result = calc_corp_tax(annual_revenue_exclusive=1000000, annual_cost=600000, rd_cost=100000)
        assert result["annual_profit"] == pytest.approx(400000.0)
        assert result["taxable_income"] == pytest.approx(300000.0)
        assert result["corp_tax"] == pytest.approx(15000.0)

    def test_loss_negative_taxable(self):
        """亏损时不缴税并提示结转。"""
        result = calc_corp_tax(annual_profit=-50000, rd_cost=0)
        assert result["corp_tax"] == 0
        assert any("亏损" in warning for warning in result["warnings"])

    def test_rd_formed_intangible(self):
        """研发形成无形资产 → 按年摊销额加计 100%。"""
        result = calc_corp_tax(annual_profit=500000, rd_cost=120000, rd_formed_intangible=True)
        # 120,000 / 10 年 = 12,000/年 摊销，加计 100% = 12,000
        assert result["rd_deduction"] == pytest.approx(12000.0)
        assert "无形资产" in result["rd_note"]

    def test_missing_inputs_raise(self):
        with pytest.raises(ValueError):
            calc_corp_tax()

    def test_disclaimer(self):
        assert calc_corp_tax(annual_profit=1000)["disclaimer"] == DISCLAIMER


class TestCITPrepay:
    def test_basic_prepay(self):
        """累计利润 15 万、研发 3 万、已预缴 5000 → 应补 1000。"""
        result = calc_cit_prepay(
            quarter_profit=50000,
            ytd_profit=150000,
            ytd_cit_paid=5000,
            rd_cost_ytd=30000,
            employees=5,
            total_assets=1000000,
            period="2026Q3",
        )
        assert result["taxable_income_ytd"] == pytest.approx(120000.0)
        assert result["cit_accrued"] == pytest.approx(6000.0)
        assert result["prepay_payable"] == pytest.approx(1000.0)

    def test_overpaid_no_negative(self):
        """已预缴超过应纳额 → 本期应补为 0 并提示。"""
        result = calc_cit_prepay(quarter_profit=10000, ytd_profit=10000, ytd_cit_paid=5000, employees=5, total_assets=1000000)
        assert result["prepay_payable"] == 0
        assert any("多缴" in warning for warning in result["warnings"])

    def test_first_prepay(self):
        result = calc_cit_prepay(quarter_profit=80000, ytd_profit=80000, period="2026Q1")
        assert result["cit_accrued"] == pytest.approx(4000.0)
        assert result["prepay_payable"] == pytest.approx(4000.0)


# =============================================================================
# 研发费用加计扣除
# =============================================================================
class TestRDDeduction:
    def test_total_and_deduction(self):
        result = calc_rd_deduction(personnel_cost=20000, material_cost=5000, depreciation=1000, other_cost=0)
        assert result["total_collectible"] == pytest.approx(26000.0)
        assert result["deduction_amount"] == pytest.approx(26000.0)
        assert result["deduction_ratio"] == pytest.approx(1.0)

    def test_other_cost_limit(self):
        """其他相关费用限额 = 五项之和 × 10% / 90%。"""
        result = calc_rd_deduction(personnel_cost=20000, material_cost=5000, depreciation=1000, other_cost=5000)
        # 五项合计 26,000 → 限额 26,000 × 0.1 / 0.9 = 2,888.89
        assert result["other_cost_limit"] == pytest.approx(2888.89)
        assert result["other_cost_allowed"] == pytest.approx(2888.89)
        assert result["total_collectible"] == pytest.approx(28888.89)
        assert any("超过限额" in warning for warning in result["warnings"])

    def test_other_cost_within_limit(self):
        result = calc_rd_deduction(personnel_cost=20000, material_cost=5000, depreciation=1000, other_cost=500)
        assert result["other_cost_allowed"] == pytest.approx(500.0)
        assert result["total_collectible"] == pytest.approx(26500.0)

    def test_formed_intangible(self):
        result = calc_rd_deduction(personnel_cost=100000, formed_intangible=True)
        assert result["intangible_cost"] == pytest.approx(100000.0)
        assert result["annual_amortization"] == pytest.approx(10000.0)
        assert result["deduction_amount"] == pytest.approx(10000.0)

    def test_zero(self):
        result = calc_rd_deduction()
        assert result["total_collectible"] == 0
        assert result["deduction_amount"] == 0
        assert "无研发费用" in result["note"]

    def test_aggregate_from_expenses(self):
        expenses = [
            {"date": "2026-01-10", "description": "研发工资", "category": "工资", "amount": 20000, "is_rd_related": True},
            {"date": "2026-01-12", "description": "研发材料", "category": "研发材料", "amount": 3000, "is_rd_related": True},
            {"date": "2026-01-15", "description": "办公室房租", "category": "房租", "amount": 8000, "is_rd_related": True},
            {"date": "2026-01-16", "description": "非研发办公费", "category": "办公", "amount": 500, "is_rd_related": False},
        ]
        result = aggregate_rd_expenses(expenses, period="2026Q1")
        assert result["source_count"] == 2
        assert result["items"]["人员人工费用"] == pytest.approx(20000.0)
        assert result["items"]["直接投入费用"] == pytest.approx(3000.0)
        assert result["total_collectible"] == pytest.approx(23000.0)
        assert len(result["skipped"]) == 1
        assert "房租" in result["skipped"][0]["reason"]

    def test_aggregate_include_unmarked(self):
        expenses = [
            {"date": "2026-02-01", "description": "材料", "category": "研发材料", "amount": 1000, "is_rd_related": False},
        ]
        assert aggregate_rd_expenses(expenses)["source_count"] == 0
        assert aggregate_rd_expenses(expenses, include_unmarked=True)["source_count"] == 1


# =============================================================================
# 印花税
# =============================================================================
class TestStampTax:
    def test_tech_contract_halved(self):
        result = calc_stamp_tax([{"contract_type": "技术合同", "amount": 100000}], period="2026Q1")
        # 100,000 × 0.3‰ = 30 → 减半 15
        assert result["total_before_discount"] == pytest.approx(30.0)
        assert result["discount_amount"] == pytest.approx(15.0)
        assert result["total_payable"] == pytest.approx(15.0)

    def test_multiple_types(self):
        result = calc_stamp_tax(
            [
                {"contract_type": "技术合同", "amount": 100000, "counterparty": "云图科技"},
                {"contract_type": "买卖合同", "amount": 50000},
                {"contract_type": "租赁合同", "amount": 120000},
            ],
            period="2026Q1",
        )
        assert len(result["items"]) == 3
        assert result["total_before_discount"] == pytest.approx(30 + 15 + 120)
        assert result["total_payable"] == pytest.approx((30 + 15 + 120) * 0.5)

    def test_unknown_type_warns(self):
        result = calc_stamp_tax([{"contract_type": "外星合同", "amount": 1000}], period="2026Q1")
        assert result["total_payable"] == 0
        assert any("未知合同类型" in warning for warning in result["warnings"])

    def test_no_contracts(self):
        result = calc_stamp_tax([], period="2026Q1")
        assert result["total_payable"] == 0
        assert "无合同" in result["note"]

    def test_not_small_scale_no_discount(self):
        result = calc_stamp_tax([{"contract_type": "技术合同", "amount": 100000}], small_scale=False, period="2026Q1")
        assert result["total_payable"] == pytest.approx(30.0)


# =============================================================================
# 期间与申报日历
# =============================================================================
class TestCalendar:
    def test_quarter_of(self):
        assert quarter_of("2026-01-01") == "2026Q1"
        assert quarter_of("2026-03-31") == "2026Q1"
        assert quarter_of("2026-04-01") == "2026Q2"
        assert quarter_of(date(2026, 12, 31)) == "2026Q4"
        assert current_quarter("2026-11-09") == "2026Q4"

    def test_quarter_range(self):
        assert quarter_start("2026Q1") == date(2026, 1, 1)
        assert quarter_end("2026Q1") == date(2026, 3, 31)
        assert quarter_range("2026Q2") == (date(2026, 4, 1), date(2026, 6, 30))

    def test_prev_quarter(self):
        assert prev_quarter("2026Q1") == "2025Q4"
        assert prev_quarter("2026Q3") == "2026Q2"

    def test_shift_to_workday(self):
        assert shift_to_workday(date(2026, 11, 14)) == date(2026, 11, 16)  # 周六 → 周一
        assert shift_to_workday(date(2026, 11, 16)) == date(2026, 11, 16)

    def test_filing_deadlines(self):
        items = filing_deadlines(2026)
        keys = {item["key"] for item in items}
        assert {"vat_quarterly", "corp_tax_quarterly", "personal_tax_monthly", "annual_settlement"} <= keys
        vat_items = [item for item in items if item["key"] == "vat_quarterly"]
        assert len(vat_items) == 4
        # 2026-04-15 是周三，不顺延
        april = [item for item in vat_items if item["due_date"].startswith("2026-04")]
        assert april[0]["due_date"] == "2026-04-15"
        assert april[0]["tax_period"] == "2026Q1"

    def test_upcoming_deadlines(self):
        items = upcoming_deadlines("2026-01-02", lookahead_days=30)
        assert items
        assert items[0]["due_date"] <= "2026-02-01"
        for item in items:
            assert item["days_left"] >= 0

    def test_deadline_status_levels(self):
        assert deadline_status("2026-11-12", "2026-11-09")["level"] == "urgent"
        assert deadline_status("2026-11-15", "2026-11-09")["level"] == "soon"
        assert deadline_status("2026-12-31", "2026-11-09")["level"] == "normal"
        assert deadline_status("2026-11-01", "2026-11-09")["level"] == "overdue"

    def test_next_deadline(self):
        item = next_deadline("2026-11-09")
        assert item is not None
        assert item["due_date"] >= "2026-11-09"

    def test_reminders_hit_window(self):
        hits = reminders("2026-11-09", lookahead_days=30)
        # 个税月度申报 2026-11-16 截止（15 日为周日顺延），处于 15 天提醒窗口内
        assert any(item["key"] == "personal_tax_monthly" for item in hits)

    def test_reminder_text_contains_disclaimer(self):
        text = reminder_text("2026-11-09")
        assert "申报提醒" in text
        assert config.DISCLAIMER in text


# =============================================================================
# 政策参数
# =============================================================================
class TestPolicyParams:
    def test_current_params(self):
        params = get_params("2026Q1")
        assert params["vat_levy_rate"] == pytest.approx(0.01)
        assert params["vat_exemption_limit_quarter"] == pytest.approx(300000.0)
        assert params["surtax_rate"] == pytest.approx(0.06)
        assert params["policy_version"]
        assert params["warnings"] == []

    def test_out_of_range_warns(self):
        params = get_params("2030Q1")
        assert any("不在已验证" in warning for warning in params["warnings"])

    def test_rate_table(self):
        table = get_rate_table("2026Q1")
        assert len(table) >= 6
        assert all("项目" in row and "依据" in row for row in table)

    def test_policy_sources_have_dates(self):
        for source in get_params("2026Q1")["policy_sources"]:
            assert source["source"]
            assert source["effective_date"]
            assert source["expiry_date"]
