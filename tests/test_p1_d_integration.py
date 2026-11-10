"""P1-D 集成测试：申报日历（社保/残保金）、工商年报、残保金测算与导出。"""

from __future__ import annotations

from pathlib import Path

import pytest

import config
from engine import calc_disability_fund, filing_deadlines


class TestCalendar:
    def test_calendar_has_social_and_disability(self):
        items = filing_deadlines(2026)
        keys = {item["key"] for item in items}
        assert "social_insurance_monthly" in keys
        assert "disability_fund_annual" in keys
        social = [i for i in items if i["key"] == "social_insurance_monthly"]
        assert len(social) == 12
        assert social[0]["tax_period"].count("-") == 1
        disability = [i for i in items if i["key"] == "disability_fund_annual"][0]
        assert disability["tax_period"] == "2025"
        assert "以当地" in disability["description"] or "以当地" in str(disability)


class TestAnnualReport:
    def test_bundle(self, demo_data):
        from filing import annual_report

        bundle = annual_report.build_annual_report("2026")
        assert bundle["form_id"] == annual_report.FORM_ID
        assert bundle["sections"]
        assert bundle["totals"]["employees"] >= 0
        assert bundle["checks"]
        assert bundle["guide"]
        assert bundle["disclaimer"] == config.DISCLAIMER
        assert any("6" in step or "公示" in step or "年报" in step for step in bundle["guide"][:6])

    def test_export(self, demo_data, tmp_path):
        from filing import annual_report

        bundle = annual_report.build_annual_report("2026")
        path = annual_report.export_annual_report_excel(bundle, tmp_path / "annual_report.xlsx")
        assert Path(path).exists() and Path(path).stat().st_size > 5_000


class TestDisabilityFundBundle:
    def test_default_from_ledger(self, demo_data):
        from filing import disability_fund

        bundle = disability_fund.build_disability_fund("2026")
        assert bundle["form_id"] == disability_fund.FORM_ID
        assert bundle["sections"]
        assert "payable" in bundle["totals"]
        assert bundle["checks"]
        assert bundle["guide"]

    def test_manual_inputs(self, demo_data):
        from filing import disability_fund

        bundle = disability_fund.build_disability_fund(
            "2026", employees=31, average_wage=80_000, disabled_employed=0
        )
        assert bundle["totals"]["payable"] == pytest.approx(33_480.0)
        assert bundle["totals"]["employees"] == 31

    def test_engine_consistency(self):
        result = calc_disability_fund(employees=100, average_wage=80_000, disabled_employed=1)
        assert result["payable"] == pytest.approx(20_000.0)
        assert result["discount_rate"] == pytest.approx(0.5)

    def test_export(self, demo_data, tmp_path):
        from filing import disability_fund

        bundle = disability_fund.build_disability_fund("2026", employees=31, average_wage=80_000)
        path = disability_fund.export_disability_fund_excel(bundle, tmp_path / "disability.xlsx")
        assert Path(path).exists() and Path(path).stat().st_size > 4_000


class TestCLI:
    def test_annual_report_and_disability_commands(self, demo_data, tmp_path, capsys):
        import main

        assert main.main(["annual-report", "--year", "2026", "--out", str(tmp_path / "a.xlsx")]) == 0
        assert (tmp_path / "a.xlsx").exists()
        assert main.main([
            "disability-fund", "--year", "2026",
            "--employees", "31", "--avg-wage", "80000", "--disabled", "0",
            "--out", str(tmp_path / "d.xlsx"),
        ]) == 0
        assert (tmp_path / "d.xlsx").exists()
