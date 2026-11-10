"""残保金测算/申报辅助表测试（P1-D）。"""

from __future__ import annotations

from pathlib import Path

import pytest

import config


def test_defaults_from_ledger(demo_data):
    from filing import disability_fund

    bundle = disability_fund.build_disability_fund("2026")
    assert bundle["form_id"] == disability_fund.FORM_ID
    assert bundle["totals"]["employees"] >= 0
    assert "payable" in bundle["totals"]
    assert bundle["disclaimer"] == config.DISCLAIMER


def test_exempt_thirty_employees(demo_data):
    from filing import disability_fund

    bundle = disability_fund.build_disability_fund("2026", employees=30, average_wage=80_000)
    assert bundle["totals"]["exempt"] is True
    assert bundle["totals"]["payable"] == 0


def test_manual_ninety_percent(demo_data):
    from filing import disability_fund

    bundle = disability_fund.build_disability_fund("2026", employees=31, average_wage=80_000)
    assert bundle["totals"]["payable"] == pytest.approx(33_480.0)
    assert bundle["totals"]["discount_rate"] == pytest.approx(0.9)
    assert "应缴" in bundle["summary"]["text"]


def test_reached_ratio(demo_data):
    from filing import disability_fund

    bundle = disability_fund.build_disability_fund(
        "2026", employees=100, average_wage=80_000, disabled_employed=2
    )
    assert bundle["totals"]["payable"] == 0
    assert bundle["totals"]["actual_ratio"] == pytest.approx(0.02)


def test_guide_and_export(demo_data, tmp_path):
    from filing import disability_fund

    bundle = disability_fund.build_disability_fund("2026", employees=31, average_wage=80_000)
    assert any("残联" in step for step in bundle["guide"])
    assert any("电子税务局" in step for step in bundle["guide"])
    path = disability_fund.export_disability_fund_excel(bundle, tmp_path / "disability.xlsx")
    assert Path(path).exists() and Path(path).stat().st_size > 3_000
