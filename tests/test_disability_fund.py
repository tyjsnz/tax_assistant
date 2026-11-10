"""残疾人就业保障金引擎测试（确定性代码）。"""

from __future__ import annotations

import pytest

import config
from engine import (
    EXEMPTION_EMPLOYEE_THRESHOLD,
    REQUIRED_DISABILITY_RATIO,
    calc_disability_fund,
    get_disability_sources,
)


class TestDisabilityFund:
    def test_thirty_or_fewer_exempt(self):
        for count in (0, 1, 30):
            result = calc_disability_fund(employees=count, average_wage=80_000)
            assert result["exempt"] is True
            assert result["payable"] == 0
            assert "免征" in result["note"]

    def test_no_disabled_ninety_percent(self):
        # 31 人、未安排：差额 0.465 人 × 80,000 = 37,200 × 90% = 33,480
        result = calc_disability_fund(employees=31, average_wage=80_000)
        assert result["exempt"] is False
        assert result["base_amount"] == pytest.approx(37_200.0)
        assert result["discount_rate"] == pytest.approx(0.9)
        assert result["payable"] == pytest.approx(33_480.0)
        assert result["actual_ratio"] == 0

    def test_one_percent_tier_fifty_percent(self):
        # 100 人、安排 1 人（1%）：差额 0.5 人 × 80,000 = 40,000 × 50% = 20,000
        result = calc_disability_fund(employees=100, average_wage=80_000, disabled_employed=1)
        assert result["actual_ratio"] == pytest.approx(0.01)
        assert result["discount_rate"] == pytest.approx(0.5)
        assert result["payable"] == pytest.approx(20_000.0)

    def test_reached_ratio_no_payment(self):
        result = calc_disability_fund(employees=100, average_wage=80_000, disabled_employed=2)
        assert result["actual_ratio"] == pytest.approx(0.02)
        assert result["payable"] == 0
        assert result["discount_rate"] == 0
        assert "无需缴纳" in result["note"]

    def test_between_one_and_one_half_percent(self):
        # 100 人、安排 1.2 人需整数；改用 200 人安排 3 人 = 1.5% 达标
        result = calc_disability_fund(employees=200, average_wage=60_000, disabled_employed=3)
        assert result["actual_ratio"] == pytest.approx(0.015)
        assert result["payable"] == 0

    def test_local_average_wage_cap_warning(self):
        result = calc_disability_fund(
            employees=50,
            average_wage=200_000,
            local_average_wage=60_000,
        )
        assert any("2 倍" in warning for warning in result["warnings"])
        assert result["wage_cap_note"]

    def test_negative_inputs_rejected(self):
        with pytest.raises(ValueError):
            calc_disability_fund(employees=-1, average_wage=80_000)
        with pytest.raises(ValueError):
            calc_disability_fund(employees=10, average_wage=-1)
        with pytest.raises(ValueError):
            calc_disability_fund(employees=10, average_wage=80_000, disabled_employed=-1)

    def test_sources_and_disclaimer(self):
        result = calc_disability_fund(employees=50, average_wage=60_000)
        assert result["disclaimer"] == config.DISCLAIMER
        assert any("2023年第8号" in item["source"] for item in result["policy_sources"])
        assert get_disability_sources()
        assert REQUIRED_DISABILITY_RATIO == pytest.approx(0.015)
        assert EXEMPTION_EMPLOYEE_THRESHOLD == 30

    def test_fractional_disabled_person(self):
        result = calc_disability_fund(employees=100, average_wage=60_000, disabled_employed=0.5)
        # 差额 1.0 人 × 60,000 = 60,000；比例 0.5% <1% → ×90% = 54,000
        assert result["shortfall"] == pytest.approx(1.0)
        assert result["payable"] == pytest.approx(54_000.0)
