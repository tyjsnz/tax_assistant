"""离职多申报扫描/更正方案/CLI 集成测试（P1-E）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ledger import corrections, employees, payroll


def _setup_departed_employee() -> dict:
    employee = employees.add_employee(
        {
            "name": "张三",
            "employee_no": "E001",
            "hire_date": "2026-01-01",
            "social_insurance_base": 10_000,
            "housing_fund_base": 10_000,
            "special_deduction_json": {"children_education_children": 1},
        }
    )
    for period in ("2026-01", "2026-02", "2026-03", "2026-04"):
        payroll.build_payroll(period)
        row = next(item for item in payroll.list_payroll(period) if item["employee_id"] == employee["id"])
        payroll.update_payroll_row(row["id"], {"gross_salary": 15_000, "social_insurance": 1_500, "housing_fund": 500})
        payroll.recalc_period(period)
        payroll.confirm_period(period)
    # 事后才发现 2 月已离职：登记离职日期
    employees.update_employee(employee["id"], {"status": "离职", "leave_date": "2026-02-15"})
    return employee


class TestScan:
    def test_scan_finds_departed_overpayments(self):
        _setup_departed_employee()
        result = corrections.scan_departed_overpayments()
        assert result["summary"]["employees"] == 1
        item = result["items"][0]
        assert item["employee_name"] == "张三"
        assert item["leave_month"] == "2026-02"
        assert item["periods"] == ["2026-03", "2026-04"]
        assert item["income_total"] == pytest.approx(30_000.0)
        assert item["tax_withheld"] > 0
        assert result["disclaimer"]

    def test_scan_empty_without_violation(self):
        employee = employees.add_employee({"name": "李四", "hire_date": "2026-01-01"})
        payroll.build_payroll("2026-01")
        employees.update_employee(employee["id"], {"status": "离职", "leave_date": "2026-01-31"})
        result = corrections.scan_departed_overpayments()
        assert result["items"] == []


class TestPlan:
    def test_build_and_export_plan(self, tmp_path):
        employee = _setup_departed_employee()
        plan = corrections.build_correction_plan(employee["id"])
        assert plan["employee_id"] == employee["id"]
        assert plan["removed_periods"] == ["2026-03", "2026-04"]
        assert plan["totals"]["tax_refund"] >= 0
        assert plan["steps"]
        path = corrections.export_correction_plan(plan, tmp_path / "plan.xlsx")
        assert Path(path).exists() and Path(path).stat().st_size > 4_000

    def test_scan_and_plan_all(self):
        _setup_departed_employee()
        plans = corrections.scan_and_plan_all()
        assert len(plans) == 1
        assert plans[0]["employee_name"] == "张三"


class TestCLI:
    def test_cli_scan_and_plan(self, tmp_path, capsys):
        employee = _setup_departed_employee()
        import main

        assert main.main(["payroll-scan"]) == 0
        out = capsys.readouterr().out
        assert "张三" in out and "2026-03" in out

        assert main.main(["correction-plan", "--employee", employee["id"],
                          "--out", str(tmp_path / "plan.xlsx")]) == 0
        out = capsys.readouterr().out
        assert "更正方案" in out
        assert (tmp_path / "plan.xlsx").exists()

        assert main.main(["correction-plan", "--all", "--out-dir", str(tmp_path)]) == 0
        assert list(tmp_path.glob("离职多申报更正操作单_*.xlsx"))


class TestDisputes:
    def test_dispute_crud(self):
        employee = employees.add_employee({"name": "王五", "hire_date": "2026-01-01"})
        record = corrections.add_dispute(
            employee["id"], "收入纳税明细", "2026-06-10", period="2026-03", note="员工投诉已沟通"
        )
        assert record["id"].startswith("DSP")
        assert record["employee_name"] == "王五"
        assert record["status"] == "待处理"

        rows = corrections.list_disputes()
        assert len(rows) == 1

        updated = corrections.update_dispute(record["id"], {"status": "已撤销", "resolution": "员工已撤销申诉"})
        assert updated["status"] == "已撤销"
        assert updated["withdrawn_date"]

        updated = corrections.update_dispute(record["id"], {"status": "已更正", "resolution": "已完成逐月更正"})
        assert updated["status"] == "已更正"

        assert corrections.delete_dispute(record["id"]) is True
        assert corrections.list_disputes() == []

    def test_invalid_dispute_type(self):
        employee = employees.add_employee({"name": "赵六", "hire_date": "2026-01-01"})
        with pytest.raises(ValueError):
            corrections.add_dispute(employee["id"], "不存在的类型", "2026-06-10")


class TestCorrectionProgress:
    def test_progress_checklist(self):
        employee = _setup_departed_employee()
        plan = corrections.build_correction_plan(employee["id"])
        steps = plan["steps"]
        rows = corrections.list_correction_progress(employee["id"], steps)
        assert len(rows) == len(steps)
        assert all(row["done"] is False for row in rows)

        summary = corrections.correction_progress_summary(employee["id"])
        assert summary["done"] == 0 and summary["total"] == len(steps)

        corrections.set_correction_step(employee["id"], 1, True)
        summary = corrections.correction_progress_summary(employee["id"])
        assert summary["done"] == 1
        rows = corrections.list_correction_progress(employee["id"])
        assert rows[0]["done"] is True and rows[0]["done_at"]

        corrections.set_correction_step(employee["id"], 1, False)
        assert corrections.correction_progress_summary(employee["id"])["done"] == 0

        assert corrections.reset_correction_progress(employee["id"]) == len(steps)


class TestManualCorrection:
    def test_manual_plan_for_unmarked_employee(self):
        employee = employees.add_employee({"name": "未标记离职", "hire_date": "2026-01-01"})
        for period in ("2026-01", "2026-02", "2026-03"):
            payroll.build_payroll(period)
            row = next(item for item in payroll.list_payroll(period) if item["employee_id"] == employee["id"])
            payroll.update_payroll_row(row["id"], {"gross_salary": 12_000, "social_insurance": 1_200, "housing_fund": 400})
            payroll.recalc_period(period)
        plan = corrections.build_correction_plan(employee["id"], leave_month="2026-01")
        assert plan["cutoff_source"] == "manual_leave_month"
        assert plan["removed_periods"] == ["2026-02", "2026-03"]
        assert plan["leave_date"] == ""

    def test_pending_leave_date_scan(self):
        from ledger import store

        employee = employees.add_employee({"name": "忘记填日期", "hire_date": "2026-01-01"})
        store.table("employees").update(employee["id"], {"status": "离职", "leave_date": ""})
        scan = corrections.scan_departed_overpayments()
        assert scan["summary"]["pending_leave_date"] == 1
        assert scan["pending_leave_date"][0]["employee_name"] == "忘记填日期"

    def test_cli_manual_mode(self, tmp_path):
        employee = employees.add_employee({"name": "手动模式", "hire_date": "2026-01-01"})
        for period in ("2026-01", "2026-02"):
            payroll.build_payroll(period)
            row = next(item for item in payroll.list_payroll(period) if item["employee_id"] == employee["id"])
            payroll.update_payroll_row(row["id"], {"gross_salary": 10_000, "social_insurance": 1_000, "housing_fund": 300})
            payroll.recalc_period(period)
        import main
        assert main.main(["correction-plan", "--employee", employee["id"], "--leave-month", "2026-01",
                          "--out", str(tmp_path / "manual.xlsx")]) == 0
        assert (tmp_path / "manual.xlsx").exists()


class TestDisputeFilterExport:
    def test_filter_summary_export(self, tmp_path):
        first = employees.add_employee({"name": "张三", "hire_date": "2026-01-01"})
        second = employees.add_employee({"name": "李四", "hire_date": "2026-01-01"})
        corrections.add_dispute(first["id"], "收入纳税明细", "2026-06-01", period="2026-03")
        corrections.add_dispute(second["id"], "任职受雇信息", "2026-06-02", status="已撤销")

        assert len(corrections.list_disputes(status="待处理")) == 1
        assert len(corrections.list_disputes(keyword="李四")) == 1
        assert len(corrections.list_disputes(employee_id=first["id"])) == 1
        summary = corrections.summarize_disputes()
        assert summary["total"] == 2
        assert summary["counts"].get("待处理") == 1
        assert summary["counts"].get("已撤销") == 1
        path = corrections.export_disputes_excel(corrections.list_disputes(), tmp_path / "disputes.xlsx")
        assert Path(path).exists() and Path(path).stat().st_size > 3_000
