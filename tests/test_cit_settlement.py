"""企业所得税汇算清缴辅助引擎测试（确定性代码）。"""

from __future__ import annotations

import pytest

import config
from engine import (
    LOSS_CARRYFORWARD_YEARS,
    calc_adjustment_limits,
    calc_loss_carryforward,
    calc_settlement,
    get_settlement_sources,
)


class TestAdjustmentLimits:
    def test_entertainment_60pct_vs_5permille(self):
        # 发生额 10,000：60% = 6,000；营业收入 1,000,000 × 5‰ = 5,000 → 限额 5,000，调增 5,000
        result = calc_adjustment_limits(revenue_exclusive=1_000_000, entertainment_actual=10_000)
        item = result["items"][0]
        assert item["limit"] == pytest.approx(5_000.0)
        assert item["deductible"] == pytest.approx(5_000.0)
        assert item["adjustment_add"] == pytest.approx(5_000.0)
        assert result["total_adjustment_add"] == pytest.approx(5_000.0)

    def test_entertainment_below_limit(self):
        # 发生额 4,000：60% = 2,400 < 收入 5‰ = 5,000 → 可扣 2,400，调增 1,600
        item = calc_adjustment_limits(revenue_exclusive=1_000_000, entertainment_actual=4_000)["items"][0]
        assert item["limit"] == pytest.approx(2_400.0)
        assert item["adjustment_add"] == pytest.approx(1_600.0)

    def test_advertising_15pct(self):
        item = calc_adjustment_limits(revenue_exclusive=1_000_000, advertising_actual=200_000)["items"][0]
        assert item["limit"] == pytest.approx(150_000.0)
        assert item["adjustment_add"] == pytest.approx(50_000.0)

    def test_salary_related_limits(self):
        result = calc_adjustment_limits(
            revenue_exclusive=1_000_000,
            salary_total=500_000,
            welfare_actual=80_000,
            education_actual=50_000,
            union_actual=12_000,
        )
        by_key = {item["key"]: item for item in result["items"]}
        assert by_key["welfare"]["limit"] == pytest.approx(70_000.0)
        assert by_key["welfare"]["adjustment_add"] == pytest.approx(10_000.0)
        assert by_key["education"]["limit"] == pytest.approx(40_000.0)
        assert by_key["education"]["adjustment_add"] == pytest.approx(10_000.0)
        assert by_key["union"]["limit"] == pytest.approx(10_000.0)
        assert by_key["union"]["adjustment_add"] == pytest.approx(2_000.0)
        # 合计调增 22,000
        assert result["total_adjustment_add"] == pytest.approx(22_000.0)

    def test_no_actuals_no_items(self):
        result = calc_adjustment_limits(revenue_exclusive=1_000_000)
        assert result["items"] == []
        assert result["total_adjustment_add"] == 0

    def test_sources_and_disclaimer(self):
        result = calc_adjustment_limits(revenue_exclusive=1_000_000, entertainment_actual=1_000)
        assert result["policy_sources"]
        assert result["disclaimer"] == config.DISCLAIMER


class TestLossCarryforward:
    def test_fifo_by_expire_year(self):
        losses = [
            {"year": 2021, "loss_amount": 100_000},
            {"year": 2024, "loss_amount": 50_000},
        ]
        result = calc_loss_carryforward(losses, 120_000, year=2026)
        assert result["total_used"] == pytest.approx(120_000.0)
        assert result["remaining_loss"] == pytest.approx(30_000.0)
        lines = {item["year"]: item for item in result["lines"]}
        assert lines["2021"]["status"] == "已弥补"
        assert lines["2024"]["status"] == "部分弥补"
        assert lines["2024"]["remaining_after"] == pytest.approx(30_000.0)

    def test_expired_loss_not_used(self):
        # 2019 年亏损，2026 年已超过 5 年弥补期（到期 2024）
        result = calc_loss_carryforward([{"year": 2019, "loss_amount": 100_000}], 200_000, year=2026)
        assert result["total_used"] == 0
        assert result["lines"][0]["status"] == "已过期"
        assert any("超过" in warning for warning in result["warnings"])

    def test_used_amount_respected(self):
        result = calc_loss_carryforward(
            [{"year": 2023, "loss_amount": 100_000, "used_amount": 60_000}],
            100_000,
            year=2026,
        )
        assert result["total_used"] == pytest.approx(40_000.0)

    def test_no_income_no_use(self):
        result = calc_loss_carryforward([{"year": 2023, "loss_amount": 100_000}], -50_000, year=2026)
        assert result["total_used"] == 0
        assert result["remaining_loss"] == pytest.approx(100_000.0)

    def test_custom_carryforward_years(self):
        result = calc_loss_carryforward([{"year": 2021, "loss_amount": 100_000}], 100_000, year=2026, years=10)
        assert result["total_used"] == pytest.approx(100_000.0)

    def test_default_years(self):
        assert LOSS_CARRYFORWARD_YEARS == 5


class TestSettlement:
    def test_basic_small_low_profit(self):
        result = calc_settlement(
            year=2026,
            revenue_exclusive=1_000_000,
            cost_total=800_000,
            entertainment_actual=10_000,
            rd_deduction=20_000,
            prepaid_tax=5_000,
        )
        assert result["accounting_profit"] == pytest.approx(200_000.0)
        assert result["limit_adjustment_add"] == pytest.approx(5_000.0)
        assert result["taxable_income"] == pytest.approx(185_000.0)
        assert result["qualifies_small_low_profit"] is True
        assert result["applicable_rate"] == pytest.approx(0.05)
        assert result["corporate_tax"] == pytest.approx(9_250.0)
        assert result["payable"] == pytest.approx(4_250.0)
        assert result["payable_label"] == "应补税额"

    def test_manual_adjustments(self):
        result = calc_settlement(
            year=2026,
            revenue_exclusive=1_000_000,
            cost_total=800_000,
            extra_adjustments=[
                {"item_type": "税收罚款", "direction": "调增", "adjustment_amount": 10_000, "basis": "罚款不得扣除"},
                {"item_type": "免税收入", "direction": "调减", "adjustment_amount": 5_000, "basis": "国债利息免税"},
            ],
            prepaid_tax=0,
        )
        assert result["manual_adjustment_add"] == pytest.approx(10_000.0)
        assert result["manual_adjustment_reduce"] == pytest.approx(5_000.0)
        assert result["taxable_income"] == pytest.approx(205_000.0)
        assert result["corporate_tax"] == pytest.approx(10_250.0)

    def test_loss_offset(self):
        result = calc_settlement(
            year=2026,
            revenue_exclusive=500_000,
            cost_total=400_000,
            losses=[{"year": 2023, "loss_amount": 80_000}],
            prepaid_tax=0,
        )
        assert result["accounting_profit"] == pytest.approx(100_000.0)
        assert result["loss_used"] == pytest.approx(80_000.0)
        assert result["taxable_income"] == pytest.approx(20_000.0)
        assert result["corporate_tax"] == pytest.approx(1_000.0)

    def test_over_three_million_not_small(self):
        result = calc_settlement(year=2026, revenue_exclusive=10_000_000, cost_total=5_000_000, prepaid_tax=0)
        assert result["qualifies_small_low_profit"] is False
        assert result["applicable_rate"] == pytest.approx(0.25)
        assert result["tax_reduction"] == 0

    def test_loss_year_negative_profit(self):
        result = calc_settlement(year=2026, revenue_exclusive=100_000, cost_total=300_000)
        assert result["accounting_profit"] == pytest.approx(-200_000.0)
        assert result["taxable_income"] == 0
        assert result["corporate_tax"] == 0
        assert any("负数" in warning for warning in result["warnings"])

    def test_refund_when_prepaid_exceeds(self):
        result = calc_settlement(
            year=2026,
            revenue_exclusive=1_000_000,
            cost_total=900_000,
            prepaid_tax=10_000,
        )
        assert result["refund"] > 0
        assert result["payable_label"] == "应退税额"
        assert any("退税" in warning for warning in result["warnings"])

    def test_employees_threshold(self):
        result = calc_settlement(
            year=2026,
            revenue_exclusive=1_000_000,
            cost_total=800_000,
            employees=301,
            prepaid_tax=0,
        )
        assert result["qualifies_small_low_profit"] is False
        assert result["applicable_rate"] == pytest.approx(0.25)

    def test_notes_sources_disclaimer(self):
        result = calc_settlement(year=2026, revenue_exclusive=100, cost_total=0)
        assert result["note"]
        assert result["policy_sources"]
        assert result["disclaimer"] == config.DISCLAIMER
        assert result["last_verified"]

    def test_invalid_adjustment_direction(self):
        with pytest.raises(ValueError):
            calc_settlement(
                year=2026,
                revenue_exclusive=1_000,
                cost_total=0,
                extra_adjustments=[{"direction": "调平", "adjustment_amount": 100}],
            )

    def test_sources_helper(self):
        sources = get_settlement_sources()
        assert any("第四十三条" in item["source"] or "实施条例" in item["source"] for item in sources)
        assert all(item["effective_date"] for item in sources)
