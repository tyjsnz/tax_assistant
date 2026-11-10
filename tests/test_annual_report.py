"""工商年报数据准备表测试（P1-D）。"""

from __future__ import annotations

from pathlib import Path

import config


def test_bundle_structure(demo_data):
    from filing import annual_report

    bundle = annual_report.build_annual_report("2026")
    assert bundle["form_id"] == annual_report.FORM_ID
    assert bundle["frequency"] == "yearly"
    assert [section["title"] for section in bundle["sections"]][:2] == [
        "企业基本信息",
        "资产状况（按台账估算，需与财务报表核对）",
    ]
    totals = bundle["totals"]
    assert totals["revenue"] >= 0
    assert totals["assets"] >= 0
    assert totals["employees"] >= 0
    assert bundle["disclaimer"] == config.DISCLAIMER
    assert any("gsxt" in step or "公示系统" in step for step in bundle["guide"])
    assert any("6 月 30 日" in step for step in bundle["guide"])


def test_checks_and_summary(demo_data):
    from filing import annual_report

    bundle = annual_report.build_annual_report("2026")
    fields = {item["field"] for item in bundle["checks"]}
    assert {"从业人数", "资产状况", "股东及出资", "申报期限"} <= fields
    assert bundle["summary"]["level"] in {"PASS", "WARNING"}


def test_export_excel(demo_data, tmp_path):
    from filing import annual_report

    bundle = annual_report.build_annual_report("2026")
    path = annual_report.export_annual_report_excel(bundle, tmp_path / "annual.xlsx")
    assert Path(path).exists() and Path(path).stat().st_size > 4_000
