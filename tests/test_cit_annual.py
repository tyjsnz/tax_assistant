"""企业所得税年度申报辅助表测试（P1-C §2）：bundle 结构、主表行次、纳税调整明细、
亏损弥补明细、checks、guide 与 Excel 4 个 sheet。
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook

import config
from engine import get_settlement_sources
from ledger import expense_ledger, income_ledger, journal, rd_auxiliary, settlement

import filing.cit_annual as cit_annual


# =============================================================================
# 测试辅助
# =============================================================================
def _seed_year(year: str = "2026") -> None:
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
    rd_auxiliary.add_rd_record("研发项目A", f"{year}Q1", personnel_cost=5000.0)


def _save_filing(period: str, amount: float) -> None:
    from filing.progress import save_filing

    save_filing(
        {
            "form_id": "CIT_PREPAY",
            "form_name": "企业所得税月（季）度预缴纳税申报表（A类）",
            "period": period,
            "totals": {"prepay_payable": amount},
            "summary": {"level": "PASS", "text": ""},
        },
        status="已确认",
    )


def _line_map(bundle: dict, title: str) -> dict[str, dict]:
    section = next(item for item in bundle["sections"] if item["title"] == title)
    return {str(line["line_no"]): line for line in section["lines"]}


# =============================================================================
# 1. bundle 结构与主表
# =============================================================================
class TestBundle:
    def test_bundle_structure(self):
        _seed_year()
        bundle = cit_annual.build_cit_annual_return("2026")
        required = {
            "form_id",
            "form_name",
            "category",
            "period",
            "period_label",
            "frequency",
            "generated_at",
            "status",
            "sections",
            "totals",
            "calc",
            "checks",
            "summary",
            "guide",
            "data",
            "disclaimer",
        }
        assert required <= set(bundle)
        assert bundle["form_id"] == "CIT_ANNUAL"
        assert bundle["form_name"] == cit_annual.FORM_NAME
        assert bundle["category"] == "企业所得税"
        assert bundle["period"] == "2026"
        assert bundle["period_label"] == "2026年度"
        assert bundle["frequency"] == "annual"
        assert bundle["status"] == "草稿"
        assert bundle["disclaimer"] == config.DISCLAIMER
        assert [section["title"] for section in bundle["sections"]] == [
            "主表（A 类关键行次）",
            "纳税调整明细",
            "亏损弥补明细",
            "政策依据与资料留存",
        ]

    def test_main_table_lines(self):
        _seed_year()
        bundle = cit_annual.build_cit_annual_return("2026")
        lines = _line_map(bundle, "主表（A 类关键行次）")
        assert len(lines) == 12
        assert lines["1"]["value"] == 20504.95
        assert lines["2"]["value"] == 0.0
        assert lines["3"]["value"] == 0.0
        assert lines["4"]["value"] == 7000.0
        assert lines["5"]["value"] == 0.0
        assert lines["6"]["value"] == 13504.95
        assert lines["7"]["value"] == 0.05
        assert lines["7"]["value_text"] == "5.00%"
        assert lines["8"]["value"] == 675.25
        assert lines["9"]["value"] == 2700.99
        assert lines["10"]["value"] == 675.25
        assert lines["11"]["value"] == 0.0
        assert lines["12"]["value"] == 675.25
        assert lines["12"]["payable_label"] == "应补税额"
        assert lines["1"]["highlight"] is True and lines["6"]["highlight"] is True

    def test_totals(self):
        _seed_year()
        bundle = cit_annual.build_cit_annual_return("2026")
        totals = bundle["totals"]
        for key in (
            "accounting_profit",
            "adjustment_add",
            "adjustment_reduce",
            "rd_deduction",
            "loss_used",
            "taxable_income",
            "corporate_tax",
            "prepaid_tax",
            "payable",
            "refund",
        ):
            assert key in totals
        assert totals["accounting_profit"] == 20504.95
        assert totals["rd_deduction"] == 7000.0
        assert totals["taxable_income"] == 13504.95
        assert totals["corporate_tax"] == 675.25
        assert totals["payable"] == 675.25
        assert totals["refund"] == 0.0

    def test_settlement_parameter_passthrough(self):
        _seed_year()
        result = settlement.compute_settlement("2026")
        bundle = cit_annual.build_cit_annual_return("2026", settlement=result)
        assert bundle["totals"]["corporate_tax"] == result["corporate_tax"]
        assert bundle["calc"]["taxable_income"] == result["taxable_income"]

    def test_invalid_year(self):
        try:
            cit_annual.build_cit_annual_return("abc")
        except ValueError as exc:
            assert "年度格式" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("应抛出 ValueError")

    def test_status_from_annual_inputs(self):
        _seed_year()
        settlement.save_annual_inputs("2026", {"prepaid_tax": 100})
        settlement.mark_settlement_status("2026", "已确认")
        bundle = cit_annual.build_cit_annual_return("2026")
        assert bundle["status"] == "已确认"


# =============================================================================
# 2. 明细分区
# =============================================================================
class TestDetailSections:
    def test_adjustment_detail(self):
        _seed_year()
        settlement.save_annual_inputs(
            "2026", {"entertainment_actual": 5000.0, "advertising_actual": 10000.0}
        )
        settlement.add_adjustment("2026", "税收滞纳金", "调增", 10000.0, basis="不得税前扣除")
        bundle = cit_annual.build_cit_annual_return("2026")
        section = next(item for item in bundle["sections"] if item["title"] == "纳税调整明细")
        lines = section["lines"]
        # 5 项限额类（工资总额相关三项实际发生额为 0）+ 1 项手工 + 合计
        assert len(lines) == 7
        names = [line["name"] for line in lines]
        assert any("业务招待费" in name for name in names)
        assert any("广告费" in name for name in names)
        assert any("税收滞纳金" in name for name in names)
        manual = next(line for line in lines if "税收滞纳金" in line["name"])
        assert manual["direction"] == "调增"
        assert manual["adjustment_amount"] == 10000.0
        assert manual["basis"] == "不得税前扣除"
        total_line = lines[-1]
        assert total_line["line_no"] == "合计"
        assert total_line["value"] == bundle["totals"]["adjustment_add"]
        assert total_line["value"] == round(4752.48 + 2574.26 + 10000.0, 2)

    def test_loss_detail(self):
        _seed_year()
        settlement.add_loss("2022", 5000.0, used_amount=1000.0)
        bundle = cit_annual.build_cit_annual_return("2026")
        section = next(item for item in bundle["sections"] if item["title"] == "亏损弥补明细")
        lines = section["lines"]
        assert len(lines) == 2  # 1 条亏损 + 合计
        loss_line = lines[0]
        assert loss_line["loss_year"] == "2022"
        assert loss_line["loss_amount"] == 5000.0
        assert loss_line["used_amount"] == 1000.0
        assert loss_line["used_this_year"] == 4000.0
        assert loss_line["remaining"] == 0.0
        assert loss_line["expire_year"] == "2027"
        assert loss_line["status"] == "已弥补"
        assert lines[-1]["value"] == 4000.0

    def test_loss_detail_none(self):
        _seed_year()
        bundle = cit_annual.build_cit_annual_return("2026")
        section = next(item for item in bundle["sections"] if item["title"] == "亏损弥补明细")
        names = [line["name"] for line in section["lines"]]
        assert any("无以前年度可弥补亏损" in name for name in names)

    def test_policy_section(self):
        _seed_year()
        bundle = cit_annual.build_cit_annual_return("2026")
        section = next(item for item in bundle["sections"] if item["title"] == "政策依据与资料留存")
        assert len(section["lines"]) == len(get_settlement_sources()) + len(cit_annual.MATERIALS)
        names = [line["name"] for line in section["lines"]]
        assert any("留存备查" in name for name in names)
        assert bundle["policy_sources"]


# =============================================================================
# 3. checks / summary / guide
# =============================================================================
class TestChecks:
    def test_basic_checks(self):
        _seed_year()
        bundle = cit_annual.build_cit_annual_return("2026")
        fields = [check["field"] for check in bundle["checks"]]
        assert fields == [
            "预缴与应纳所得税额",
            "亏损弥补期限",
            "纳税调整项平衡",
            "利润与台账一致性",
            "小型微利企业条件",
            "研发加计资料",
            "零申报提示",
        ]
        checks = {check["field"]: check for check in bundle["checks"]}
        assert checks["预缴与应纳所得税额"]["status"] == "WARNING"  # 未预缴
        assert checks["纳税调整项平衡"]["status"] == "PASS"
        assert checks["利润与台账一致性"]["status"] == "PASS"
        assert checks["小型微利企业条件"]["status"] == "PASS"
        assert checks["研发加计资料"]["status"] == "PASS"
        assert checks["零申报提示"]["status"] == "PASS"
        assert bundle["summary"]["level"] == "WARNING"

    def test_pass_when_prepaid_matches(self):
        _seed_year()
        result = settlement.compute_settlement("2026")
        _save_filing("2026Q1", result["corporate_tax"])
        bundle = cit_annual.build_cit_annual_return("2026")
        checks = {check["field"]: check for check in bundle["checks"]}
        assert checks["预缴与应纳所得税额"]["status"] == "PASS"
        assert bundle["summary"]["level"] == "PASS"
        assert bundle["totals"]["payable"] == 0.0

    def test_adjustment_balance_error(self):
        _seed_year()
        result = settlement.compute_settlement("2026")
        tampered = dict(result)
        tampered["manual_adjustment_add"] = float(result["manual_adjustment_add"]) + 100.0
        bundle = cit_annual.build_cit_annual_return("2026", settlement=tampered)
        check = next(item for item in bundle["checks"] if item["field"] == "纳税调整项平衡")
        assert check["status"] == "ERROR"
        assert bundle["summary"]["level"] == "ERROR"

    def test_accounting_profit_override_warning(self):
        _seed_year()
        settlement.save_annual_inputs("2026", {"accounting_profit": 100000.0})
        bundle = cit_annual.build_cit_annual_return("2026")
        check = next(item for item in bundle["checks"] if item["field"] == "利润与台账一致性")
        assert check["status"] == "WARNING"
        assert "手工覆盖" in check["message"]

    def test_zero_year_warning(self):
        bundle = cit_annual.build_cit_annual_return("2025")
        check = next(item for item in bundle["checks"] if item["field"] == "零申报提示")
        assert check["status"] == "WARNING"

    def test_guide(self):
        _seed_year()
        bundle = cit_annual.build_cit_annual_return("2026")
        guide = bundle["guide"]
        assert any("A100000" in step for step in guide)
        assert any("A105000" in step for step in guide)
        assert any("5 月 31 日" in step for step in guide)
        assert any("不代替" in step for step in guide)
        assert guide[-1] == config.DISCLAIMER

    def test_data_and_checklist(self):
        _seed_year()
        bundle = cit_annual.build_cit_annual_return("2026")
        data = bundle["data"]
        assert data["year"] == "2026"
        assert data["annual"]["revenue_exclusive"] == 49504.95
        assert isinstance(data["inputs"], dict)
        assert isinstance(data["checklist"], list) and data["checklist"]
        assert bundle["checklist"] == data["checklist"]



# =============================================================================
# 4. Excel 导出（4 个 sheet）
# =============================================================================
class TestExport:
    def test_export_default_path_and_sheets(self):
        _seed_year()
        settlement.add_loss("2022", 5000.0)
        bundle = cit_annual.build_cit_annual_return("2026")
        path = cit_annual.export_cit_annual_excel(bundle)
        assert isinstance(path, Path) and path.exists()
        assert path.parent == Path(config.EXPORT_DIR)
        workbook = load_workbook(path)
        assert workbook.sheetnames == ["主表", "纳税调整明细", "亏损弥补明细", "政策依据与操作步骤"]
        assert workbook["主表"]["A1"].value == cit_annual.FORM_NAME
        assert workbook["纳税调整明细"]["A3"].value == "序号"
        assert workbook["亏损弥补明细"]["A3"].value == "序号"
        assert workbook["政策依据与操作步骤"]["A1"].value == "政策依据、资料留存与操作步骤"

    def test_export_explicit_path(self, tmp_path):
        _seed_year()
        bundle = cit_annual.build_cit_annual_return("2026")
        target = tmp_path / "cit_annual.xlsx"
        path = cit_annual.export_cit_annual_excel(bundle, target)
        assert path == target and target.exists()
        workbook = load_workbook(target)
        main_values = [cell.value for row in workbook["主表"].iter_rows() for cell in row if cell.value]
        assert "利润总额" in main_values
        policy_text = " ".join(
            str(cell.value) for row in workbook["政策依据与操作步骤"].iter_rows() for cell in row if cell.value
        )
        assert "留存备查" in policy_text
        assert "电子税务局" in policy_text
