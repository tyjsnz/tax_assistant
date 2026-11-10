"""P1-B 端到端集成测试：员工 → 月度工资 → 累计预扣 → 凭证 → 扣缴辅助表 → 结账检查 → 期间锁。"""

from __future__ import annotations

from pathlib import Path

import pytest

import config
from engine import calc_iit_withholding
from ledger import employees, journal, locks, payroll, store


def _seed_employees() -> list[dict]:
    first = employees.add_employee(
        {
            "name": "张三",
            "employee_no": "E001",
            "department": "研发部",
            "position": "工程师",
            "hire_date": "2026-01-01",
            "social_insurance_base": 10_000,
            "housing_fund_base": 10_000,
            "special_deduction_json": {"children_education_children": 1},
        }
    )
    second = employees.add_employee(
        {
            "name": "李四",
            "employee_no": "E002",
            "department": "运营部",
            "position": "运营",
            "hire_date": "2026-01-01",
            "social_insurance_base": 6_000,
            "housing_fund_base": 6_000,
        }
    )
    return [first, second]


def _fill_payroll(period: str, amounts: dict[str, tuple[float, float, float]]) -> None:
    """amounts: 姓名 → (应发, 个人社保, 个人公积金)。"""
    for row in payroll.list_payroll(period):
        gross, social, fund = amounts[str(row["employee_name"])]
        payroll.update_payroll_row(
            row["id"],
            {
                "gross_salary": gross,
                "social_insurance": social,
                "housing_fund": fund,
            },
        )


class TestPayrollFlow:
    def test_employee_management(self):
        created = _seed_employees()
        assert created[0]["id"].startswith("EMP")
        summary = employees.summarize_employees()
        assert summary["active"] == 2
        assert summary["total_social_base"] == pytest.approx(16_000.0)
        # 身份证号脱敏
        masked = employees.mask_id_card("530101199001011234")
        assert "199001011234" not in masked
        # 离职
        left = employees.update_employee(created[1]["id"], {"status": "离职"})
        assert left["status"] == "离职" and left["leave_date"]
        assert employees.summarize_employees()["active"] == 1

    def test_two_month_cumulative_withholding(self):
        _seed_employees()
        payroll.build_payroll("2026-01")
        _fill_payroll("2026-01", {"张三": (15_000, 1_500, 500), "李四": (8_000, 800, 400)})
        jan = payroll.recalc_period("2026-01")
        jan_rows = {row["employee_name"]: row for row in jan["rows"]}

        # 张三：15,000 − 5,000(1个月) − 2,000(社保+公积金) − 2,000(子女教育) = 6,000 → 3% = 180
        assert jan_rows["张三"]["cumulative_taxable"] == pytest.approx(6_000.0)
        assert jan_rows["张三"]["tax_withheld"] == pytest.approx(180.0)
        # 李四：8,000 − 5,000 − 1,200 = 1,800 → 54
        assert jan_rows["李四"]["tax_withheld"] == pytest.approx(54.0)

        payroll.confirm_period("2026-01")
        payroll.mark_period_declared("2026-01")

        payroll.build_payroll("2026-02")
        _fill_payroll("2026-02", {"张三": (15_000, 1_500, 500), "李四": (8_000, 800, 400)})
        feb = payroll.recalc_period("2026-02")
        feb_rows = {row["employee_name"]: row for row in feb["rows"]}

        # 张三累计：30,000 − 10,000 − 4,000 − 4,000 = 12,000 → 360，已预扣 180 → 本期 180
        assert feb_rows["张三"]["cumulative_income"] == pytest.approx(30_000.0)
        assert feb_rows["张三"]["cumulative_tax"] == pytest.approx(360.0)
        assert feb_rows["张三"]["tax_withheld"] == pytest.approx(180.0)
        assert feb_rows["张三"]["net_pay"] == pytest.approx(15_000 - 1_500 - 500 - 180.0)

        # 与引擎直接计算一致
        engine_result = calc_iit_withholding(
            30_000,
            months_declared=2,
            cumulative_social_insurance=3_000,
            cumulative_housing_fund=1_000,
            cumulative_special_additional=4_000,
            cumulative_tax_withheld=180,
        )
        assert engine_result["tax_payable"] == pytest.approx(feb_rows["张三"]["tax_withheld"])

        # 3 月尚未生成工资行：只累计 1 月已申报月份；生成 3 月行后 +1（当前月）
        employee_id = feb_rows["张三"]["employee_id"]
        assert payroll.cumulative_for_employee(employee_id, "2026-03")["months_declared"] == 1
        payroll.build_payroll("2026-03")
        assert payroll.cumulative_for_employee(employee_id, "2026-03")["months_declared"] == 2

    def test_confirm_creates_balanced_journal(self):
        _seed_employees()
        payroll.build_payroll("2026-01")
        _fill_payroll("2026-01", {"张三": (15_000, 1_500, 500), "李四": (8_000, 800, 400)})
        payroll.recalc_period("2026-01")
        result = payroll.confirm_period("2026-01")
        assert result["totals"]["gross"] == pytest.approx(23_000.0)
        assert result["totals"]["tax"] == pytest.approx(234.0)
        assert result["entries"]
        for entry in result["entries"]:
            assert entry.get("entry_no")
        assert journal.trial_balance()["balanced"] is True
        rows = payroll.list_payroll("2026-01")
        assert all(row["status"] == "已确认" for row in rows if not row.get("zero_declaration"))
        assert all(row.get("journal_entry_no") for row in rows if not row.get("zero_declaration"))
        # 幂等：重复确认不重复入账（凭证数量不变）
        count_before = len(journal.list_entries("2026-01-01", "2026-01-31"))
        again = payroll.confirm_period("2026-01")
        count_after = len(journal.list_entries("2026-01-01", "2026-01-31"))
        assert count_after == count_before
        assert again.get("idempotent") is True or again.get("entries")
        assert journal.trial_balance()["balanced"] is True

    def test_zero_declaration(self):
        payroll.mark_zero_declaration("2026-03")
        rows = payroll.list_payroll("2026-03")
        assert any(row.get("zero_declaration") for row in rows)
        summary = payroll.summarize_period("2026-03")
        assert summary["declared"] is True
        assert summary["total_gross"] == 0

    def test_iit_return_export(self, tmp_path):
        from filing import iit_return

        _seed_employees()
        payroll.build_payroll("2026-01")
        _fill_payroll("2026-01", {"张三": (15_000, 1_500, 500), "李四": (8_000, 800, 400)})
        payroll.recalc_period("2026-01")
        payroll.confirm_period("2026-01")

        bundle = iit_return.build_iit_return("2026-01")
        assert bundle["form_id"] == iit_return.FORM_ID
        assert bundle["sections"] and bundle["totals"]["total_tax"] == pytest.approx(234.0)
        assert bundle["checks"] and bundle["disclaimer"] == config.DISCLAIMER
        assert bundle["guide"]
        assert all("扣缴端" in step or "申报" in step for step in bundle["guide"][:5])

        path = iit_return.export_iit_excel(bundle, tmp_path / "iit_2026-01.xlsx")
        assert Path(path).exists() and Path(path).stat().st_size > 5_000

    def test_closing_checks_payroll_status(self):
        from closing import checks

        _seed_employees()
        payroll.build_payroll("2026-01")
        _fill_payroll("2026-01", {"张三": (15_000, 1_500, 500), "李四": (8_000, 800, 400)})
        payroll.recalc_period("2026-01")

        draft_report = checks.pre_close_checks("2026-01")
        draft_check = next((item for item in draft_report["checks"] if item["key"] == "payroll_status"), None)
        assert draft_check is not None and draft_check["status"] == "WARNING"

        payroll.confirm_period("2026-01")
        payroll.mark_period_declared("2026-01")
        done_report = checks.pre_close_checks("2026-01")
        done_check = next((item for item in done_report["checks"] if item["key"] == "payroll_status"), None)
        assert done_check is not None and done_check["status"] == "PASS"

    def test_closing_blocks_payroll_after_close(self):
        from closing import close

        _seed_employees()
        payroll.build_payroll("2026-01")
        _fill_payroll("2026-01", {"张三": (15_000, 1_500, 500), "李四": (8_000, 800, 400)})
        payroll.recalc_period("2026-01")
        # 保留草稿行：结账后该期间应被锁定，禁止再修改工资表
        close.close_period("2026-01", operator="test", force=True)
        assert locks.is_locked("2026-01-15") is True
        row = payroll.list_payroll("2026-01")[0]
        assert row["status"] == "草稿"
        with pytest.raises(ValueError):
            payroll.update_payroll_row(row["id"], {"gross_salary": 20_000})

    def test_data_isolated_between_companies(self):
        _seed_employees()
        assert store.table("employees").count() >= 2
