"""个税扣缴申报辅助表测试（P1-B §3）：bundle 结构、主表行次、员工明细、
校验项、操作步骤与 Excel 导出。
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook

import config
from engine import calc_iit_withholding
from ledger import employees, payroll
from ledger.store import read_log

import filing.iit_return as iit


# =============================================================================
# 测试辅助
# =============================================================================
def _employee(**overrides) -> dict:
    payload = {
        "name": "张三",
        "employee_no": "E001",
        "department": "综合部",
        "hire_date": "2026-01-01",
        "special_deduction_json": {},
    }
    payload.update(overrides)
    return employees.add_employee(payload)


def _prepared(month: str = "2026-01", gross: float = 15000.0, employee: dict | None = None, **patch) -> dict:
    """生成工资行 → 写入应发 → 重算个税，返回工资行。"""
    employee = employee or _employee()
    payroll.build_payroll(month, employee_ids=[employee["id"]])
    row = next(item for item in payroll.list_payroll(month) if item["employee_id"] == employee["id"])
    changes = {"gross_salary": gross}
    changes.update(patch)
    payroll.update_payroll_row(row["id"], changes)
    rows = payroll.recalc_period(month)["rows"]
    return next(item for item in rows if item["employee_id"] == employee["id"])


def _line_values(bundle: dict, section_title: str) -> dict[str, dict]:
    section = next(item for item in bundle["sections"] if item["title"] == section_title)
    return {str(line["line_no"]): line for line in section["lines"]}


def _check_map(bundle: dict) -> dict[str, dict]:
    return {str(check["field"]): check for check in bundle["checks"]}


# =============================================================================
# 1. bundle 结构与主表
# =============================================================================
class TestBundle:
    def test_bundle_structure(self):
        _prepared()
        payroll.confirm_period("2026-01")
        bundle = iit.build_iit_return("2026-01")
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
        assert bundle["form_id"] == "IIT_WITHHOLDING"
        assert bundle["form_name"] == iit.FORM_NAME
        assert bundle["category"] == "个人所得税"
        assert bundle["period"] == "2026-01"
        assert bundle["period_label"] == "2026年1月"
        assert bundle["frequency"] == "monthly"
        assert bundle["disclaimer"] == config.DISCLAIMER
        assert bundle["status"] == "已确认"
        assert [section["title"] for section in bundle["sections"]] == [
            "主表（本期汇总）",
            "员工明细",
            "政策依据与预扣率表",
        ]

    def test_main_table_lines(self):
        _prepared()
        payroll.confirm_period("2026-01")
        bundle = iit.build_iit_return("2026-01")
        lines = _line_values(bundle, "主表（本期汇总）")
        assert len(lines) == 11
        assert lines["1"]["value"] == 15000.0
        assert lines["2"]["value"] == 0.0
        assert lines["3"]["value"] == 5000.0
        assert lines["4"]["value"] == 0.0
        assert lines["5"]["value"] == 0.0
        assert lines["6"]["value"] == 0.0
        assert lines["7"]["value"] == 10000.0
        assert lines["8"]["value"] == 300.0
        assert lines["9"]["value"] == 0.0
        assert lines["10"]["value"] == 300.0
        assert lines["11"]["value"] == 1.0
        assert lines["11"]["value_text"] == "1 人"
        # 勾稽：7 = 1-2-3-4-5-6；10 = 8-9
        assert lines["7"]["value"] == round(
            lines["1"]["value"] - lines["2"]["value"] - lines["3"]["value"]
            - lines["4"]["value"] - lines["5"]["value"] - lines["6"]["value"], 2
        )
        assert lines["10"]["value"] == round(lines["8"]["value"] - lines["9"]["value"], 2)
        assert lines["10"]["highlight"] is True

    def test_employee_detail_section(self):
        _prepared()
        payroll.confirm_period("2026-01")
        bundle = iit.build_iit_return("2026-01")
        section = next(item for item in bundle["sections"] if item["title"] == "员工明细")
        assert len(section["lines"]) == 1
        line = section["lines"][0]
        assert line["employee_name"] == "张三"
        assert line["income"] == 15000.0
        assert line["cumulative_taxable"] == 10000.0
        assert line["tax_withheld"] == 300.0
        assert line["net_pay"] == 14700.0
        assert line["months_declared"] == 1

    def test_policy_section(self):
        _prepared()
        payroll.confirm_period("2026-01")
        bundle = iit.build_iit_return("2026-01")
        section = next(item for item in bundle["sections"] if item["title"] == "政策依据与预扣率表")
        names = [line["name"] for line in section["lines"]]
        assert any("累计预扣率" in name for name in names)
        sources = iit.get_iit_sources()
        assert len(section["lines"]) == len(sources) + len(iit.get_iit_rate_table())
        assert any("扣缴" in line["source"] for line in section["lines"])
        assert bundle["policy_sources"]

    def test_calc_matches_engine(self):
        _prepared()
        payroll.confirm_period("2026-01")
        bundle = iit.build_iit_return("2026-01")
        calc = bundle["calc"]
        assert len(calc["rate_table"]) == 7
        assert calc["policy_sources"]
        cumulative = payroll.cumulative_for_employee(
            payroll.list_payroll("2026-01")[0]["employee_id"], "2026-01"
        )
        expected = calc_iit_withholding(
            cumulative["cumulative_income"], months_declared=cumulative["months_declared"]
        )
        assert calc["rows"][0]["tax_payable"] == expected["tax_payable"]
        assert calc["rows"][0]["cumulative_taxable_income"] == expected["cumulative_taxable_income"]
        assert bundle["totals"]["total_tax"] == expected["tax_payable"]

    def test_totals(self):
        _prepared()
        payroll.confirm_period("2026-01")
        bundle = iit.build_iit_return("2026-01")
        totals = bundle["totals"]
        assert totals["headcount"] == 1
        assert totals["total_income"] == 15000.0
        assert totals["total_basic_deduction"] == 5000.0
        assert totals["total_social"] == 0.0
        assert totals["total_special"] == 0.0
        assert totals["total_taxable"] == 10000.0
        assert totals["total_cumulative_tax"] == 300.0
        assert totals["total_tax"] == 300.0
        assert totals["total_net"] == 14700.0
        assert totals["declared"] is False

    def test_status_transitions(self):
        row = _prepared()
        draft = iit.build_iit_return("2026-01")
        assert draft["status"] == "草稿"
        payroll.confirm_period("2026-01")
        confirmed = iit.build_iit_return("2026-01")
        assert confirmed["status"] == "已确认"
        payroll.mark_period_declared("2026-01")
        declared = iit.build_iit_return("2026-01")
        assert declared["status"] == "已申报"
        assert declared["totals"]["declared"] is True
        assert row["id"]

    def test_rows_parameter_override(self):
        _prepared()
        rows = payroll.list_payroll("2026-01")
        bundle = iit.build_iit_return("2026-01", rows=rows)
        assert bundle["totals"]["headcount"] == 1
        empty = iit.build_iit_return("2026-01", rows=[])
        assert empty["totals"]["headcount"] == 0

    def test_calculation_audit_log(self):
        _prepared()
        iit.build_iit_return("2026-01")
        logs = read_log("calculation_log")
        assert any(row["calc_type"] == "个人所得税扣缴" for row in logs)


# =============================================================================
# 2. 校验项
# =============================================================================
class TestChecks:
    def test_all_pass_when_confirmed(self):
        _prepared()
        payroll.confirm_period("2026-01")
        bundle = iit.build_iit_return("2026-01")
        assert len(bundle["checks"]) == 6
        assert all(check["status"] == "PASS" for check in bundle["checks"])
        assert bundle["summary"]["level"] == "PASS"

    def test_draft_payroll_warning(self):
        _prepared()  # 仅重算，未确认
        bundle = iit.build_iit_return("2026-01")
        checks = _check_map(bundle)
        assert checks["工资表状态"]["status"] == "WARNING"
        assert "草稿" in checks["工资表状态"]["message"]
        assert bundle["summary"]["level"] == "WARNING"

    def test_no_employee_warning(self):
        bundle = iit.build_iit_return("2026-02")
        checks = _check_map(bundle)
        assert checks["零申报"]["status"] == "WARNING"
        assert checks["员工与工资表"]["status"] == "WARNING"
        assert bundle["totals"]["headcount"] == 0
        assert bundle["summary"]["level"] == "WARNING"

    def test_zero_declaration_bundle(self):
        payroll.mark_zero_declaration("2026-05")
        bundle = iit.build_iit_return("2026-05")
        assert bundle["status"] == "已申报"
        assert bundle["totals"]["headcount"] == 0
        assert bundle["totals"]["zero_declaration"] is True
        checks = _check_map(bundle)
        assert checks["零申报"]["status"] == "PASS"
        assert checks["员工与工资表"]["status"] == "PASS"
        assert bundle["summary"]["level"] == "PASS"

    def test_social_insurance_over_income_warning(self):
        _prepared(gross=100.0, social_insurance=200.0)
        bundle = iit.build_iit_return("2026-01")
        checks = _check_map(bundle)
        assert checks["专项扣除（社保+公积金）≤ 本期收入"]["status"] == "WARNING"
        assert "社保+公积金超过本期收入" in checks["专项扣除（社保+公积金）≤ 本期收入"]["message"]

    def test_special_deduction_over_income_warning(self):
        _prepared(gross=100.0, special_deduction=500.0)
        bundle = iit.build_iit_return("2026-01")
        checks = _check_map(bundle)
        assert checks["专项附加扣除 ≤ 本期收入"]["status"] == "WARNING"

    def test_2026_declared_months_deficit_warning(self):
        employee = _employee()
        _prepared("2026-03", employee=employee)  # 只做了 3 月工资，1/2 月未申报
        bundle = iit.build_iit_return("2026-03")
        checks = _check_map(bundle)
        assert checks["2026 扣缴端减除费用口径"]["status"] == "WARNING"
        assert "减除费用" in checks["2026 扣缴端减除费用口径"]["message"]

    def test_new_hire_no_false_deficit(self):
        employee = _employee(hire_date="2026-03-01")
        _prepared("2026-03", employee=employee)
        bundle = iit.build_iit_return("2026-03")
        checks = _check_map(bundle)
        assert checks["2026 扣缴端减除费用口径"]["status"] == "PASS"

    def test_checks_structure(self):
        _prepared()
        bundle = iit.build_iit_return("2026-01")
        fields = [check["field"] for check in bundle["checks"]]
        assert fields == [
            "专项扣除（社保+公积金）≤ 本期收入",
            "专项附加扣除 ≤ 本期收入",
            "工资表状态",
            "零申报",
            "员工与工资表",
            "2026 扣缴端减除费用口径",
        ]
        assert all({"field", "status", "message"} <= set(check) for check in bundle["checks"])
        assert bundle["summary"]["text"]


# =============================================================================
# 3. 操作指引与数据快照
# =============================================================================
class TestGuideAndData:
    def test_guide_steps(self):
        _prepared()
        bundle = iit.build_iit_return("2026-01")
        guide = bundle["guide"]
        assert len(guide) >= 5
        assert any("扣缴端" in step for step in guide)
        assert any("零申报" in step for step in guide)
        assert any("申报" in step and "标记已申报" in step for step in guide)
        assert guide[-1] == config.DISCLAIMER

    def test_data_rows(self):
        _prepared()
        payroll.confirm_period("2026-01")
        bundle = iit.build_iit_return("2026-01")
        rows = bundle["data"]["rows"]
        assert len(rows) == 1
        row = rows[0]
        assert row["employee_name"] == "张三"
        assert row["income"] == 15000.0
        assert row["social_insurance"] == 0.0
        assert row["cumulative_taxable"] == 10000.0
        assert row["months_declared"] == 1
        assert row["status"] == "已确认"


# =============================================================================
# 4. Excel 导出
# =============================================================================
class TestExport:
    def test_export_default_path_and_sheets(self):
        _prepared()
        payroll.confirm_period("2026-01")
        bundle = iit.build_iit_return("2026-01")
        path = iit.export_iit_excel(bundle)
        assert isinstance(path, Path) and path.exists()
        assert path.parent == Path(config.EXPORT_DIR)
        workbook = load_workbook(path)
        assert workbook.sheetnames == ["主表", "员工明细", "政策依据与操作步骤"]
        main_sheet = workbook["主表"]
        assert main_sheet["A1"].value == iit.FORM_NAME
        assert main_sheet["A5"].value == "行次"
        detail_sheet = workbook["员工明细"]
        assert detail_sheet["A3"].value == "序号"
        assert detail_sheet["B3"].value == "姓名"
        policy_sheet = workbook["政策依据与操作步骤"]
        assert policy_sheet["A1"].value == "政策依据与操作步骤"

    def test_export_explicit_path_and_content(self, tmp_path):
        _prepared()
        payroll.confirm_period("2026-01")
        bundle = iit.build_iit_return("2026-01")
        target = tmp_path / "iit.xlsx"
        path = iit.export_iit_excel(bundle, target)
        assert path == target and target.exists()
        workbook = load_workbook(target)
        detail_sheet = workbook["员工明细"]
        values = [cell.value for cell in detail_sheet[4]]
        assert "张三" in values
        policy_text = " ".join(str(cell.value) for row in workbook["政策依据与操作步骤"].iter_rows() for cell in row if cell.value)
        assert "政策依据" in policy_text
        assert "扣缴端" in policy_text
