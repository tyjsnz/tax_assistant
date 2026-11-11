"""离职经济补偿金与一次性补偿个税测算测试（P2）。"""

from __future__ import annotations

import pytest

import config
from engine.severance import (
    calc_severance,
    calc_severance_pay,
    calc_severance_tax,
    get_severance_sources,
    severance_years_factor,
)


class TestYearsFactor:
    @pytest.mark.parametrize(
        "years, months, expected",
        [
            (0, 0, 0.0),
            (0, 3, 0.5),
            (0, 6, 1.0),
            (2, 0, 2.0),
            (2, 3, 2.5),
            (3, 7, 4.0),
            (10, 11, 11.0),
        ],
    )
    def test_factor(self, years, months, expected):
        assert severance_years_factor(years, months) == pytest.approx(expected)


class TestSeverancePay:
    def test_basic(self):
        result = calc_severance_pay(10_000, 3, months=7)
        assert result["years_factor"] == pytest.approx(4.0)
        assert result["amount"] == pytest.approx(40_000.0)

    def test_high_earner_cap(self):
        # 月工资 50,000 > 当地社平 10,000 的 3 倍（30,000）；按 30,000 计
        result = calc_severance_pay(50_000, 15, local_average_wage=10_000)
        assert result["base_wage"] == pytest.approx(30_000.0)
        assert result["years_factor"] == pytest.approx(12.0)
        assert result["amount"] == pytest.approx(360_000.0)
        assert result["years_capped"] is True
        assert any("12 年" in warning for warning in result["warnings"])

    def test_negative_rejected(self):
        with pytest.raises(ValueError):
            calc_severance_pay(-1, 1)
        with pytest.raises(ValueError):
            calc_severance_pay(1_000, -1)

    def test_without_local_wage_warning(self):
        result = calc_severance_pay(10_000, 2)
        assert any("当地" in warning for warning in result["warnings"])


class TestSeveranceTax:
    def test_within_exemption(self):
        # 当地社平 10,000 → 免税额度 30,000
        result = calc_severance_tax(30_000, 10_000)
        assert result["exemption_limit"] == pytest.approx(30_000.0)
        assert result["taxable_amount"] == 0
        assert result["tax"] == 0
        assert result["net_amount"] == pytest.approx(30_000.0)

    def test_over_exemption(self):
        # 补偿 100,000，免税额 30,000，应税 70,000 → 10% 档，速算 2,520 → 4,480
        result = calc_severance_tax(100_000, 10_000)
        assert result["taxable_amount"] == pytest.approx(70_000.0)
        assert result["rate"] == pytest.approx(0.10)
        assert result["quick_deduction"] == pytest.approx(2_520.0)
        assert result["tax"] == pytest.approx(4_480.0)
        assert result["net_amount"] == pytest.approx(95_520.0)

    def test_top_bracket(self):
        result = calc_severance_tax(2_000_000, 10_000)
        assert result["rate"] == pytest.approx(0.45)
        assert result["tax"] == pytest.approx((2_000_000 - 30_000) * 0.45 - 181_920)

    def test_no_local_wage(self):
        result = calc_severance_tax(50_000, 0)
        assert result["exemption_limit"] == 0
        assert result["taxable_amount"] == pytest.approx(50_000.0)
        assert any("未提供当地" in warning for warning in result["warnings"])

    def test_negative_rejected(self):
        with pytest.raises(ValueError):
            calc_severance_tax(-1, 10_000)


class TestCombined:
    def test_combined(self):
        result = calc_severance(10_000, 3, months=7, local_average_wage=10_000)
        assert result["pay"]["amount"] == pytest.approx(40_000.0)
        assert result["tax"]["taxable_amount"] == pytest.approx(10_000.0)
        assert result["total_amount"] == pytest.approx(40_000.0)
        assert result["disclaimer"] == config.DISCLAIMER

    def test_amount_override(self):
        result = calc_severance(10_000, 3, local_average_wage=10_000, amount=50_000)
        assert result["total_amount"] == pytest.approx(50_000.0)
        assert any("与法定参考" in warning for warning in result["warnings"])

    def test_sources(self):
        assert get_severance_sources()
        sources = " ".join(item["source"] for item in get_severance_sources())
        assert "164号" in sources
        assert "劳动合同法" in sources
