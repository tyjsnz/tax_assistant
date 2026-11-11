"""员工档案 + 月度工资表 + 累计预扣法 + 工资凭证/零申报测试（P1-B）。

覆盖 `docs/CONTRACTS_P1_B.md` 第 1/2 节：
员工 CRUD/脱敏/离职保护；工资表生成/编辑/删除/期间锁；累计口径（已申报月份、跨月收入、
已预扣）；recalc 多员工、税率跳档、专项附加、一次性扣除；确认生成两张凭证借贷平衡且幂等；
标记已申报；零申报；汇总；累计预扣结果与 `engine.calc_iit_withholding` 一致。
"""

from __future__ import annotations

import pytest

import config
from engine import calc_iit_withholding
from ledger import employees, locks, payroll
from ledger.store import read_log, table


# =============================================================================
# 测试辅助
# =============================================================================
def _add_employee(payload: dict | None = None, **overrides) -> dict:
    base = {
        "name": "张三",
        "employee_no": "E001",
        "department": "综合部",
        "position": "会计",
        "hire_date": "2026-01-01",
        "social_insurance_base": 8000.0,
        "housing_fund_base": 8000.0,
        "special_deduction_json": {},
    }
    base.update(payload or {})
    base.update(overrides)
    return employees.add_employee(base)


def _draft(month: str, employee: dict, gross: float = 15000.0, **patch) -> dict:
    """生成当月工资行并写入应发金额（不重算）。"""
    payroll.build_payroll(month, employee_ids=[employee["id"]])
    row = next(item for item in payroll.list_payroll(month) if item["employee_id"] == employee["id"])
    changes = {"gross_salary": gross}
    changes.update(patch)
    return payroll.update_payroll_row(row["id"], changes)


def _recalc_row(month: str, employee: dict, gross: float = 15000.0, **patch) -> dict:
    """生成并重算当月工资行，返回重算后的记录。"""
    _draft(month, employee, gross, **patch)
    rows = payroll.recalc_period(month)["rows"]
    return next(item for item in rows if item["employee_id"] == employee["id"])


def _close_month(period: str) -> None:
    """直接标记期间已结账（构造期间锁场景）。"""
    table("periods").append({"period": period, "period_type": "月", "status": "已结账"})


def _income(row: dict) -> float:
    return round(
        float(row["gross_salary"]) + float(row["bonus"]) + float(row["allowance"]), 2
    )


# =============================================================================
# 1. 员工档案
# =============================================================================
class TestEmployees:
    def test_add_employee_defaults(self):
        employee = employees.add_employee({"name": "李四"})
        assert employee["name"] == "李四"
        assert employee["status"] == "在职"
        assert employee["hire_date"] == config.today().isoformat()
        assert employee["leave_date"] == ""
        assert employee["deduction_choice"] == "累计预扣"
        assert employee["is_deduction_qualified"] is True
        assert employee["special_deduction_json"] == {}
        assert employee["id_card_masked"] == ""

    def test_employee_validations(self):
        with pytest.raises(ValueError):
            employees.add_employee({"name": "  "})
        with pytest.raises(ValueError):
            employees.add_employee({"name": "某人", "status": "休假"})
        with pytest.raises(ValueError):
            employees.add_employee({"name": "某人", "deduction_choice": "随便"})
        with pytest.raises(ValueError):
            employees.add_employee({"name": "某人", "social_insurance_base": -1})
        with pytest.raises(ValueError):
            employees.add_employee({"name": "某人", "special_deduction_json": "不是字典"})
        assert employees.list_employees() == []

    def test_mask_id_card(self):
        assert employees.mask_id_card("530128199001011234") == "530128****1234"
        assert employees.mask_id_card("530128****1234") == "530128****1234"
        assert employees.mask_id_card("") == ""
        masked_short = employees.mask_id_card("12345")
        assert "*" in masked_short and len(masked_short) == 5
        employee = employees.add_employee(
            {"name": "王五", "id_card": "530128199001011234"}
        )
        assert employee["id_card_masked"] == "530128****1234"
        stored = employees.get_employee(employee["id"])
        assert stored is not None
        assert stored["id_card_masked"] == "530128****1234"
        assert "530128199001011234" not in str(stored)

    def test_update_status_requires_leave_date(self):
        employee = _add_employee()
        with pytest.raises(ValueError, match="离职日期"):
            employees.update_employee(employee["id"], {"status": "离职"})

        left = employees.update_employee(employee["id"], {"status": "离职", "leave_date": "2026-01-31"})
        assert left["status"] == "离职"
        assert left["leave_date"] == "2026-01-31"

        active_again = employees.update_employee(employee["id"], {"status": "在职"})
        assert active_again["status"] == "在职"
        assert active_again["leave_date"] == ""

    def test_list_employee_filters(self):
        active = _add_employee({"name": "在职A"})
        left = _add_employee({"name": "离职B"})
        employees.update_employee(left["id"], {"status": "离职", "leave_date": "2026-01-31"})
        assert [item["name"] for item in employees.list_employees()] == ["在职A"]
        assert [item["name"] for item in employees.list_employees("离职")] == ["离职B"]
        assert len(employees.list_employees(None)) == 2
        assert len(employees.list_employees(include_left=True)) == 2
        assert employees.get_employee(active["id"]) is not None
        with pytest.raises(ValueError):
            employees.list_employees("休假")

    def test_delete_employee_with_payroll_forbidden(self):
        employee = _add_employee()
        payroll.build_payroll("2026-01", employee_ids=[employee["id"]])
        with pytest.raises(ValueError, match="工资记录"):
            employees.delete_employee(employee["id"])
        other = _add_employee({"name": "无工资"})
        assert employees.delete_employee(other["id"]) is True
        assert employees.get_employee(other["id"]) is None
        assert employees.delete_employee("EMP999999") is False

    def test_special_deduction_monthly(self):
        assert employees.special_deduction_monthly({}) == 0.0
        one_child = _add_employee(
            {"name": "有子女", "special_deduction_json": {"children_education_children": 1}}
        )
        assert employees.special_deduction_monthly(one_child) == 2000.0
        two_child_loan = _add_employee(
            {
                "name": "两孩房贷",
                "special_deduction_json": {
                    "children_education_children": 2,
                    "housing_loan_interest": True,
                },
            }
        )
        assert employees.special_deduction_monthly(two_child_loan) == 5000.0

    def test_summarize_employees(self):
        _add_employee({"name": "甲", "department": "研发部", "social_insurance_base": 10000, "housing_fund_base": 5000})
        _add_employee({"name": "乙", "department": "研发部", "social_insurance_base": 8000, "housing_fund_base": 4000})
        left = _add_employee({"name": "丙", "department": "综合部", "social_insurance_base": 6000})
        employees.update_employee(left["id"], {"status": "离职", "leave_date": "2026-01-31"})
        summary = employees.summarize_employees()
        assert summary["total"] == 3
        assert summary["active"] == 2
        assert summary["left"] == 1
        assert summary["by_department"] == {"研发部": 2, "综合部": 1}
        assert summary["total_social_base"] == 18000.0
        assert summary["total_housing_fund_base"] == 9000.0

    def test_active_employees_for_month(self):
        _add_employee({"name": "月初入职", "hire_date": "2026-01-15", "employee_no": "A"})
        _add_employee({"name": "次月入职", "hire_date": "2026-02-01", "employee_no": "B"})
        early_leave = _add_employee({"name": "月初离职", "hire_date": "2025-01-01", "employee_no": "C"})
        employees.update_employee(early_leave["id"], {"status": "离职", "leave_date": "2026-01-01"})
        mid_leave = _add_employee({"name": "月中离职", "hire_date": "2025-06-01", "employee_no": "D"})
        employees.update_employee(mid_leave["id"], {"status": "离职", "leave_date": "2026-01-20"})
        names = {item["name"] for item in employees.active_employees_for_month("2026-01")}
        assert "月初入职" in names
        assert "次月入职" not in names
        assert "月初离职" not in names
        assert "月中离职" in names

    def test_employee_audit_logs(self):
        employee = _add_employee()
        employees.update_employee(employee["id"], {"position": "主管"})
        employees.delete_employee(employee["id"])
        actions = [row["action"] for row in read_log("user_action_log")]
        assert "新增员工" in actions and "修改员工" in actions and "删除员工" in actions


# =============================================================================
# 2. 工资表生成 / 编辑 / 期间锁
# =============================================================================
class TestPayrollCrud:
    def test_build_payroll_and_skip_duplicates(self):
        employee = _add_employee()
        first = payroll.build_payroll("2026-01")
        assert first["created"] == 1
        assert first["rows"][0]["status"] == "草稿"
        assert first["rows"][0]["period"] == "2026-01"
        assert first["rows"][0]["employee_name"] == "张三"

        second = payroll.build_payroll("2026-01")
        assert second["created"] == 0
        assert second["skipped"][0]["employee_id"] == employee["id"]
        assert len(payroll.list_payroll("2026-01")) == 1

    def test_build_payroll_employment_window_and_filter(self):
        jan = _add_employee({"name": "一月在职", "employee_no": "A"})
        feb = _add_employee({"name": "二月入职", "employee_no": "B", "hire_date": "2026-02-01"})
        result = payroll.build_payroll("2026-01", employee_ids=[jan["id"], feb["id"]])
        assert result["created"] == 1
        assert result["rows"][0]["employee_id"] == jan["id"]

    def test_update_and_delete_draft(self):
        employee = _add_employee()
        row = _draft("2026-01", employee, gross=12000.0, bonus=1000.0, allowance=500.0)
        assert _income(row) == 13500.0
        with pytest.raises(ValueError):
            payroll.update_payroll_row(row["id"], {"gross_salary": -1})
        assert payroll.delete_payroll_row(row["id"]) is True
        assert payroll.get_payroll_row(row["id"]) is None
        assert payroll.delete_payroll_row("PAY999999") is False

    def test_confirmed_row_cannot_edit_or_delete(self):
        employee = _add_employee()
        row = _recalc_row("2026-01", employee, gross=15000.0)
        payroll.confirm_period("2026-01")
        confirmed = payroll.get_payroll_row(row["id"])
        assert confirmed["status"] == "已确认"
        with pytest.raises(ValueError, match="不能修改"):
            payroll.update_payroll_row(row["id"], {"gross_salary": 20000.0})
        with pytest.raises(ValueError, match="不能删除"):
            payroll.delete_payroll_row(row["id"])

    def test_period_lock_blocks_payroll_writes(self):
        employee = _add_employee({"name": "张三", "hire_date": "2025-01-01"})
        row = _draft("2025-12", employee, gross=10000.0)
        _close_month("2025-12")
        with pytest.raises(locks.PeriodLockedError):
            payroll.build_payroll("2025-12")
        with pytest.raises(locks.PeriodLockedError):
            payroll.update_payroll_row(row["id"], {"gross_salary": 12000.0})
        with pytest.raises(locks.PeriodLockedError):
            payroll.delete_payroll_row(row["id"])
        assert payroll.list_payroll("2025-12")

    def test_normalize_month(self):
        assert payroll.normalize_month("2026-1") == "2026-01"
        assert payroll.normalize_month("2026年3月") == "2026-03"
        with pytest.raises(ValueError, match="按月"):
            payroll.normalize_month("2026Q1")
        with pytest.raises(ValueError):
            payroll.normalize_month("abc")


# =============================================================================
# 3. 累计口径与重算（累计预扣法）
# =============================================================================
class TestCumulative:
    def test_first_month_basic_deduction(self):
        """1 月首次算税：months_declared=1（减除 5,000），应预扣 300。"""
        employee = _add_employee()
        row = _recalc_row("2026-01", employee, gross=15000.0)
        assert row["months_declared"] == 1
        assert row["cumulative_income"] == 15000.0
        assert row["cumulative_basic_deduction"] == 5000.0
        assert row["cumulative_taxable"] == 10000.0
        assert row["tax_withheld"] == 300.0
        assert row["net_pay"] == 14700.0
        assert row["applicable_rate"] == 0.03

    def test_recalc_matches_engine(self):
        employee = _add_employee()
        row = _recalc_row("2026-01", employee, gross=15000.0)
        cumulative = payroll.cumulative_for_employee(employee["id"], "2026-01")
        expected = calc_iit_withholding(
            cumulative["cumulative_income"],
            months_declared=cumulative["months_declared"],
            cumulative_social_insurance=cumulative["cumulative_social"],
            cumulative_housing_fund=cumulative["cumulative_housing_fund"],
            cumulative_special_additional=cumulative["cumulative_special_additional"],
            cumulative_other_deduction=cumulative["cumulative_other"],
            cumulative_tax_withheld=cumulative["cumulative_tax_withheld"],
            period="2026-01",
        )
        assert row["tax_withheld"] == expected["tax_payable"]
        assert row["cumulative_tax"] == expected["cumulative_tax"]
        assert row["cumulative_taxable"] == expected["cumulative_taxable_income"]

    def test_second_month_after_january_declared(self):
        """1 月已申报 → 2 月 months_declared=2，累计收入 30,000，本期预扣 300。"""
        employee = _add_employee()
        _recalc_row("2026-01", employee, gross=15000.0)
        payroll.confirm_period("2026-01")
        payroll.mark_period_declared("2026-01")

        row = _recalc_row("2026-02", employee, gross=15000.0)
        assert row["months_declared"] == 2
        assert row["cumulative_income"] == 30000.0
        assert row["cumulative_basic_deduction"] == 10000.0
        assert row["cumulative_taxable"] == 20000.0
        assert row["cumulative_tax"] == 600.0
        assert row["tax_withheld"] == 300.0
        assert row["cumulative_tax_withheld"] == 300.0
        assert row["net_pay"] == 14700.0

    def test_second_month_when_january_not_declared(self):
        """1 月未标记已申报 → 2 月 months_declared=1（未申报月份额度作废）。"""
        employee = _add_employee()
        _recalc_row("2026-01", employee, gross=15000.0)  # 仍为草稿
        row = _recalc_row("2026-02", employee, gross=15000.0)
        assert row["months_declared"] == 1
        assert row["cumulative_income"] == 15000.0  # 1 月草稿不计入累计
        assert row["cumulative_basic_deduction"] == 5000.0
        assert row["tax_withheld"] == 300.0

    def test_history_confirmed_counts_income_not_declared_month(self):
        """1 月仅「已确认」：累计收入计入，但已申报月份只有 0 个 → months_declared=1。"""
        employee = _add_employee()
        _recalc_row("2026-01", employee, gross=15000.0)
        payroll.confirm_period("2026-01")
        row = _recalc_row("2026-02", employee, gross=15000.0)
        assert row["months_declared"] == 1
        assert row["cumulative_income"] == 30000.0
        assert row["cumulative_basic_deduction"] == 5000.0
        assert row["cumulative_taxable"] == 25000.0
        assert row["cumulative_tax"] == 750.0
        assert row["cumulative_tax_withheld"] == 300.0
        assert row["tax_withheld"] == 450.0

    def test_no_current_row_does_not_add_month(self):
        employee = _add_employee()
        _recalc_row("2026-01", employee, gross=15000.0)
        payroll.confirm_period("2026-01")
        payroll.mark_period_declared("2026-01")
        cumulative = payroll.cumulative_for_employee(employee["id"], "2026-02")
        assert cumulative["has_current_row"] is False
        assert cumulative["months_declared"] == 1
        assert cumulative["cumulative_income"] == 15000.0

    def test_zero_declaration_preserves_month(self):
        """1 月零申报（已申报）→ 2 月 months_declared=2，保留 5,000 减除额度。"""
        employee = _add_employee()
        payroll.mark_zero_declaration("2026-01")
        row = _recalc_row("2026-02", employee, gross=15000.0)
        assert row["months_declared"] == 2
        assert row["cumulative_income"] == 15000.0
        assert row["cumulative_basic_deduction"] == 10000.0
        assert row["tax_withheld"] == 150.0

    def test_rate_bracket_jump(self):
        """累计应纳税所得 40,000 → 10% 预扣率、速算扣除数 2,520。"""
        employee = _add_employee()
        row = _recalc_row("2026-01", employee, gross=45000.0)
        assert row["cumulative_taxable"] == 40000.0
        assert row["applicable_rate"] == 0.10
        assert row["quick_deduction"] == 2520.0
        assert row["cumulative_tax"] == 1480.0
        assert row["tax_withheld"] == 1480.0

    def test_one_time_basic_deduction(self):
        employee = _add_employee({"deduction_choice": "一次性60000"})
        row = _recalc_row("2026-01", employee, gross=10000.0)
        assert row["cumulative_basic_deduction"] == 60000.0
        assert row["cumulative_taxable"] == 0.0
        assert row["tax_withheld"] == 0.0

    def test_not_qualified_employee_no_basic_deduction(self):
        employee = _add_employee({"is_deduction_qualified": False})
        row = _recalc_row("2026-01", employee, gross=15000.0)
        assert row["cumulative_basic_deduction"] == 0.0
        assert row["cumulative_taxable"] == 15000.0
        assert row["tax_withheld"] == 450.0

    def test_special_deduction_prefilled_and_reduces_tax(self):
        employee = _add_employee(
            {"special_deduction_json": {"children_education_children": 1}}
        )
        row = _recalc_row("2026-01", employee, gross=15000.0)
        assert row["special_deduction"] == 2000.0
        assert row["cumulative_taxable"] == 8000.0
        assert row["tax_withheld"] == 240.0

    def test_net_pay_with_social_and_housing(self):
        employee = _add_employee()
        row = _recalc_row(
            "2026-01", employee, gross=15000.0, social_insurance=800.0, housing_fund=800.0
        )
        assert row["cumulative_taxable"] == 8400.0
        assert row["tax_withheld"] == 252.0
        assert row["net_pay"] == 13148.0

    def test_recalc_skips_confirmed_rows(self):
        employee = _add_employee()
        row = _recalc_row("2026-01", employee, gross=15000.0)
        payroll.confirm_period("2026-01")
        result = payroll.recalc_period("2026-01")
        assert result["rows"] == []
        assert any("已确认" in warning for warning in result["warnings"])
        assert payroll.get_payroll_row(row["id"])["tax_withheld"] == 300.0

    def test_recalc_multi_employee_totals(self):
        first = _add_employee({"name": "甲", "employee_no": "A"})
        second = _add_employee({"name": "乙", "employee_no": "B"})
        _draft("2026-01", first, gross=15000.0)
        _draft("2026-01", second, gross=8000.0)
        result = payroll.recalc_period("2026-01")
        assert len(result["rows"]) == 2
        assert result["total_tax"] == round(
            sum(item["tax_withheld"] for item in result["rows"]), 2
        )
        assert result["total_tax"] == 390.0  # 300 + (8000-5000)*3%=90

    def test_recalc_locked_period_raises(self):
        employee = _add_employee({"name": "张三", "hire_date": "2025-01-01"})
        _draft("2025-12", employee, gross=10000.0)
        _close_month("2025-12")
        with pytest.raises(locks.PeriodLockedError):
            payroll.recalc_period("2025-12")


# =============================================================================
# 4. 确认入账（两张工资凭证）、申报标记、零申报与汇总
# =============================================================================
class TestConfirmAndDeclare:
    def test_confirm_generates_two_balanced_entries(self):
        employee = _add_employee()
        row = _recalc_row(
            "2026-01", employee, gross=15000.0, social_insurance=800.0, housing_fund=800.0
        )
        result = payroll.confirm_period("2026-01", operator="tester")
        assert result["period"] == "2026-01"
        assert len(result["entries"]) == 2
        entries = {entry["description"]: entry for entry in result["entries"]}
        accrual = next(entry for desc, entry in entries.items() if "计提" in desc)
        payment = next(entry for desc, entry in entries.items() if "发放" in desc)
        for entry in (accrual, payment):
            assert entry["source_ref"] == "PAY-2026-01"
            debit = sum(line["amount"] for line in entry["lines"] if line["direction"] == "借")
            credit = sum(line["amount"] for line in entry["lines"] if line["direction"] == "贷")
            assert round(debit, 2) == round(credit, 2)

        accrual_debits = {line["account"]: line["amount"] for line in accrual["lines"] if line["direction"] == "借"}
        assert accrual_debits["管理费用—工资薪金"] == 15000.0
        payment_credits = {line["account"]: line["amount"] for line in payment["lines"] if line["direction"] == "贷"}
        assert payment_credits["银行存款"] == 13148.0
        assert payment_credits["其他应付款—社会保险费"] == 800.0
        assert payment_credits["其他应付款—住房公积金"] == 800.0
        assert payment_credits["应交税费—应交个人所得税"] == 252.0

        confirmed = payroll.get_payroll_row(row["id"])
        assert confirmed["status"] == "已确认"
        assert confirmed["journal_entry_no"] == accrual["entry_no"]
        assert table("journal_entries").count() == 2
        assert "工资表确认" in [item["action"] for item in read_log("user_action_log")]

    def test_confirm_rd_split(self):
        rd_employee = _add_employee({"name": "研发甲", "department": "研发部", "employee_no": "R1"})
        admin_employee = _add_employee({"name": "综合乙", "department": "综合部", "employee_no": "A1"})
        _draft("2026-01", rd_employee, gross=20000.0)
        _draft("2026-01", admin_employee, gross=10000.0)
        payroll.recalc_period("2026-01")
        result = payroll.confirm_period("2026-01")
        accrual = next(entry for entry in result["entries"] if "计提" in entry["description"])
        debits = {line["account"]: line["amount"] for line in accrual["lines"] if line["direction"] == "借"}
        assert debits["研发支出—人员人工"] == 20000.0
        assert debits["管理费用—工资薪金"] == 10000.0

    def test_confirm_idempotent(self):
        employee = _add_employee()
        _recalc_row("2026-01", employee, gross=15000.0)
        first = payroll.confirm_period("2026-01")
        second = payroll.confirm_period("2026-01")
        assert len(first["entries"]) == 2
        assert second["entries"] and second["entries"][0]["entry_no"] == first["entries"][0]["entry_no"]
        assert table("journal_entries").count() == 2
        assert second["warnings"]

    def test_confirm_zero_amount_rows_no_voucher(self):
        employee = _add_employee()
        payroll.build_payroll("2026-01", employee_ids=[employee["id"]])
        result = payroll.confirm_period("2026-01")
        assert result["entries"] == []
        assert result["warnings"]
        row = payroll.list_payroll("2026-01")[0]
        assert row["status"] == "已确认"
        assert table("journal_entries").count() == 0

    def test_confirm_without_journal(self):
        employee = _add_employee()
        _recalc_row("2026-01", employee, gross=15000.0)
        result = payroll.confirm_period("2026-01", post_journal=False)
        assert len(result["entries"]) == 2
        assert table("journal_entries").count() == 0
        assert payroll.list_payroll("2026-01")[0]["status"] == "已确认"

    def test_confirm_deductions_exceed_gross_raises(self):
        employee = _add_employee()
        _draft("2026-01", employee, gross=100.0, social_insurance=500.0)
        with pytest.raises(ValueError, match="超过应发工资"):
            payroll.confirm_period("2026-01")

    def test_confirm_locked_period_raises(self):
        employee = _add_employee({"name": "张三", "hire_date": "2025-01-01"})
        _draft("2025-12", employee, gross=15000.0)
        _close_month("2025-12")
        with pytest.raises(locks.PeriodLockedError):
            payroll.confirm_period("2025-12")

    def test_mark_period_declared(self):
        employee = _add_employee()
        _recalc_row("2026-01", employee, gross=15000.0)
        payroll.confirm_period("2026-01")
        result = payroll.mark_period_declared("2026-01")
        assert result["count"] == 1
        assert payroll.list_payroll("2026-01")[0]["status"] == "已申报"
        assert payroll.summarize_period("2026-01")["declared"] is True
        assert payroll.mark_period_declared("2026-01")["count"] == 0
        assert "工资个税申报标记" in [item["action"] for item in read_log("user_action_log")]

    def test_mark_zero_declaration(self):
        employee = _add_employee()
        record = payroll.mark_zero_declaration("2026-05")
        assert record["employee_id"] == "ZERO"
        assert record["employee_name"] == "（零申报）"
        assert record["zero_declaration"] is True
        assert record["status"] == "已申报"
        assert record["gross_salary"] == 0.0
        again = payroll.mark_zero_declaration("2026-05")
        assert again["id"] == record["id"]
        assert len([row for row in payroll.list_payroll("2026-05") if row["zero_declaration"]]) == 1
        summary = payroll.summarize_period("2026-05")
        assert summary["headcount"] == 0
        assert summary["declared"] is True
        assert summary["zero_declaration"] is True
        assert employees.list_employees()  # 员工仍在职，零申报不删除档案

    def test_summarize_period(self):
        first = _add_employee({"name": "甲", "employee_no": "A"})
        second = _add_employee({"name": "乙", "employee_no": "B"})
        _draft("2026-01", first, gross=15000.0, social_insurance=800.0, housing_fund=800.0)
        _draft("2026-01", second, gross=8000.0)
        payroll.recalc_period("2026-01")
        summary = payroll.summarize_period("2026-01")
        assert summary["period"] == "2026-01"
        assert summary["headcount"] == 2
        assert summary["total_gross"] == 23000.0
        assert summary["total_social"] == 800.0
        assert summary["total_housing_fund"] == 800.0
        assert summary["total_tax"] == 342.0
        assert summary["total_net"] == 21058.0  # 13148 + 7910
        assert summary["declared"] is False
        assert summary["status_counts"] == {"草稿": 2}

    def test_payroll_periods(self):
        employee = _add_employee()
        payroll.build_payroll("2026-01", employee_ids=[employee["id"]])
        payroll.build_payroll("2026-02", employee_ids=[employee["id"]])
        assert payroll.payroll_periods() == ["2026-02", "2026-01"]
        assert payroll.payroll_periods(limit=1) == ["2026-02"]
