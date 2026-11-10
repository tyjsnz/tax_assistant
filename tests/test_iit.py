"""个人所得税累计预扣法引擎测试（确定性代码，不依赖网络/LLM）。"""

from __future__ import annotations

import pytest

import config
from engine import (
    BASIC_DEDUCTION_PER_MONTH,
    IIT_RATE_TABLE,
    calc_iit_withholding,
    calc_special_deduction,
    get_iit_rate_table,
    get_iit_sources,
    get_special_deduction_standards,
)


class TestWithholding:
    def test_first_month_basic(self):
        """月薪 15,000：减除 5,000 → 应纳税所得 10,000 × 3% = 300。"""
        result = calc_iit_withholding(15_000, months_declared=1, period="2026-01")
        assert result["cumulative_basic_deduction"] == pytest.approx(BASIC_DEDUCTION_PER_MONTH)
        assert result["cumulative_taxable_income"] == pytest.approx(10_000.0)
        assert result["applicable_rate"] == pytest.approx(0.03)
        assert result["cumulative_tax"] == pytest.approx(300.0)
        assert result["tax_payable"] == pytest.approx(300.0)
        assert result["period"] == "2026-01"

    def test_second_month_cumulative(self):
        """累计两个月：30,000 − 10,000 = 20,000 × 3% = 600，已预扣 300 → 本期 300。"""
        result = calc_iit_withholding(
            30_000, months_declared=2, cumulative_tax_withheld=300, period="2026-02"
        )
        assert result["cumulative_taxable_income"] == pytest.approx(20_000.0)
        assert result["cumulative_tax"] == pytest.approx(600.0)
        assert result["tax_payable"] == pytest.approx(300.0)

    def test_rate_bracket_jump(self):
        """累计应纳税所得 90,000：10% − 2,520 = 6,480。"""
        # 3 个月 × 30,000 = 90,000 收入，减除 15,000 → 75,000 → 10% - 2520 = 4,980
        result = calc_iit_withholding(90_000, months_declared=3)
        assert result["cumulative_taxable_income"] == pytest.approx(75_000.0)
        assert result["applicable_rate"] == pytest.approx(0.10)
        assert result["quick_deduction"] == pytest.approx(2_520.0)
        assert result["cumulative_tax"] == pytest.approx(4_980.0)

    def test_special_deduction_reduces_tax(self):
        result = calc_iit_withholding(
            30_000,
            months_declared=2,
            cumulative_social_insurance=2_000,
            cumulative_housing_fund=1_000,
            cumulative_special_additional=4_000,
            cumulative_other_deduction=500,
        )
        # 30,000 − 10,000 − 2,000 − 1,000 − 4,000 − 500 = 12,500 → 3% = 375
        assert result["cumulative_taxable_income"] == pytest.approx(12_500.0)
        assert result["cumulative_tax"] == pytest.approx(375.0)

    def test_income_below_threshold(self):
        result = calc_iit_withholding(4_000, months_declared=1)
        assert result["cumulative_taxable_income"] == 0
        assert result["tax_payable"] == 0
        assert result["cumulative_tax"] == 0

    def test_zero_months_deduction_warning(self):
        """2026 年扣缴端规则：未申报月份减除费用为 0，并提示漏做零申报。"""
        result = calc_iit_withholding(10_000, months_declared=0)
        assert result["cumulative_basic_deduction"] == 0
        assert result["cumulative_taxable_income"] == pytest.approx(10_000.0)
        assert result["cumulative_tax"] == pytest.approx(300.0)
        assert any("零申报" in warning for warning in result["warnings"])
        assert "零申报" in result["zero_declaration_note"]

    def test_one_time_60000_option(self):
        result = calc_iit_withholding(100_000, months_declared=3, one_time_basic_deduction=True)
        assert result["cumulative_basic_deduction"] == pytest.approx(60_000.0)
        assert result["cumulative_taxable_income"] == pytest.approx(40_000.0)
        assert result["applicable_rate"] == pytest.approx(0.10)

    def test_over_withheld_payable_zero(self):
        result = calc_iit_withholding(10_000, months_declared=1, cumulative_tax_withheld=500)
        assert result["cumulative_tax"] == pytest.approx(150.0)
        assert result["tax_payable"] == 0
        assert any("多缴" in warning for warning in result["warnings"])

    def test_negative_income_handled(self):
        result = calc_iit_withholding(-100, months_declared=1)
        assert result["cumulative_income"] == 0
        assert any("负数" in warning for warning in result["warnings"])

    def test_months_over_12_clamped(self):
        result = calc_iit_withholding(200_000, months_declared=15)
        assert result["months_declared"] == 12
        assert any("12" in warning for warning in result["warnings"])

    def test_high_income_top_bracket(self):
        result = calc_iit_withholding(2_000_000, months_declared=12)
        # 2,000,000 − 60,000 = 1,940,000 → 45% - 181,920 = 691,080
        assert result["applicable_rate"] == pytest.approx(0.45)
        assert result["cumulative_tax"] == pytest.approx(691_080.0)

    def test_disclaimer_and_sources(self):
        result = calc_iit_withholding(10_000, months_declared=1)
        assert result["disclaimer"] == config.DISCLAIMER
        assert result["policy_sources"] and result["policy_sources"][0]["source"]
        assert result["last_verified"]


class TestSpecialDeduction:
    def test_children_and_elderly(self):
        result = calc_special_deduction({"children_education_children": 2, "elderly_care": "独生"})
        # 2 孩 × 2,000 + 独生 3,000 = 7,000
        assert result["monthly_total"] == pytest.approx(7_000.0)
        labels = [item["label"] for item in result["items"]]
        assert "子女教育" in labels and "赡养老人" in labels

    def test_infant_and_continuing_education(self):
        result = calc_special_deduction({"infant_care_children": 1, "continuing_education": "学历"})
        assert result["monthly_total"] == pytest.approx(2_400.0)

    def test_housing_exclusive(self):
        result = calc_special_deduction(
            {"housing_loan_interest": True, "housing_rent": "直辖市/省会/计划单列市"}
        )
        assert result["monthly_total"] == pytest.approx(1_500.0)
        assert any("不可同时" in warning for warning in result["warnings"])

    def test_elderly_non_only_child_capped(self):
        result = calc_special_deduction({"elderly_care": 2_000})
        assert result["monthly_total"] == pytest.approx(1_500.0)

    def test_serious_illness_annual_only(self):
        result = calc_special_deduction({"serious_illness_annual": 100_000})
        assert result["monthly_total"] == 0
        assert result["annual_only_total"] == pytest.approx(80_000.0)
        assert any("汇算" in warning for warning in result["warnings"])

    def test_empty_breakdown(self):
        result = calc_special_deduction()
        assert result["monthly_total"] == 0
        assert result["items"] == []


class TestPolicyInfo:
    def test_rate_table(self):
        table = get_iit_rate_table()
        assert len(table) == 7
        assert table[0]["预扣率"] == "3.00%"
        assert table[-1]["预扣率"] == "45.00%"
        assert IIT_RATE_TABLE[-1][2] == pytest.approx(181_920.0)

    def test_sources(self):
        sources = get_iit_sources()
        assert any("2018年第61号" in item["source"] for item in sources)
        assert any("2023〕13号" in item["source"] or "2023 年" in item["title"] for item in sources)
        assert all(item.get("effective_date") for item in sources)

    def test_standards(self):
        standards = get_special_deduction_standards()
        assert standards["children_education"]["monthly_per_child"] == pytest.approx(2_000.0)
        assert standards["elderly_care"]["monthly_only_child"] == pytest.approx(3_000.0)
        assert standards["serious_illness"]["annual_limit"] == pytest.approx(80_000.0)
