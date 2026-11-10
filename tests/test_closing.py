"""月结/季结状态机 + 期间锁 + 快照测试（P0-3A）。

覆盖 `docs/CONTRACTS_P0_3.md` 第 2.6 节：
状态机（未结账→已结账→反结账→未结账）、未来期间拒绝、重复结账报错、结账前检查（含工资表状态）、
损益结转凭证借贷平衡且科目正确、按期间过滤、快照可复现与篡改检测、反结账删除结转凭证、
季度结账要求 3 个月已结账、期间锁在 income/expense/journal/rd/ingest.confirm_match 生效、
锁不影响未结账期间、`list_periods` 派生数据正确。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import config
import ledger
from ledger import employees, expense_ledger, income_ledger, journal, locks, payroll
from ledger.store import read_log, table

import closing
from closing import checks as checks_module

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ingest"


# =============================================================================
# 测试辅助
# =============================================================================
def _activity(month: str = "2026-01", amount: float = 20600.0) -> tuple[dict, dict]:
    """在指定月份造两笔业务并登记凭证（收入 + 办公费）。"""
    record = income_ledger.add_income(f"{month}-10", "云图科技", amount, category="技术服务")
    journal.post_entry(journal.build_income_entry(record))
    expense = expense_ledger.add_expense(f"{month}-15", "办公耗材", 500.0, category="办公")
    journal.post_entry(journal.build_expense_entry(expense))
    return record, expense


def _mark_period(period: str, status: str = "已结账", period_type: str | None = None) -> dict:
    """直接写 periods 表设置期间状态（用于构造锁定/重复结账场景）。"""
    period_table = table("periods")
    existing = next((row for row in period_table.read() if str(row.get("period")) == period), None)
    kind = period_type or ("季" if "Q" in period else "月")
    if existing:
        return period_table.update(existing["id"], {"status": status, "period_type": kind})
    return period_table.append({"period": period, "period_type": kind, "status": status})


def _add_unbalanced_lines(month: str = "2026-01") -> None:
    """直接写一张借贷不平衡的凭证（绕过 post_entry 校验，模拟坏账）。"""
    table("journal_entries").append(
        {
            "date": f"{month}-20",
            "entry_no": f"记-{month.replace('-', '')}-9999",
            "description": "测试不平衡凭证",
            "total_amount": 100.0,
            "source_ref": "",
        }
    )
    table("journal_lines").append(
        {
            "entry_no": f"记-{month.replace('-', '')}-9999",
            "date": f"{month}-20",
            "description": "测试不平衡凭证",
            "account": "管理费用—办公费",
            "direction": "借",
            "amount": 100.0,
            "line_no": 1,
        }
    )


def _close_months(*months: str) -> None:
    for month in months:
        closing.close_period(month)


def _period_row(period: str) -> dict:
    return closing.find_period_row(period) or {}


# =============================================================================
# 1. 期间状态与派生数据
# =============================================================================
class TestPeriods:
    def test_period_type_of(self):
        assert closing.period_type_of("2026-01") == "月"
        assert closing.period_type_of("2026Q1") == "季"

    def test_normalize_period(self):
        assert closing.normalize_period("2026-1") == "2026-01"
        assert closing.normalize_period("2026Q1") == "2026Q1"
        assert closing.normalize_period("2026年3月") == "2026-03"
        with pytest.raises(ValueError):
            closing.normalize_period("abc")

    def test_current_period(self):
        expected = f"{config.today().year}-{config.today().month:02d}"
        assert closing.current_period() == expected

    def test_ensure_period_creates_and_idempotent(self):
        first = closing.ensure_period("2026-01")
        second = closing.ensure_period("2026-01")
        assert first["id"] == second["id"]
        assert first["status"] == "未结账"
        assert first["period_type"] == "月"
        assert table("periods").count() == 1
        assert closing.ensure_period("2026Q1")["period_type"] == "季"

    def test_get_period_none_before_register(self):
        assert closing.get_period("2026-01") is None
        closing.ensure_period("2026-01")
        period = closing.get_period("2026-01")
        assert period["period"] == "2026-01"
        assert period["period_label"] == "2026年1月"

    def test_list_periods_derived_data(self):
        _activity("2026-01", 20600.0)
        periods = closing.list_periods()
        item = next(period for period in periods if period["period"] == "2026-01")
        assert item["income_inclusive"] == 20600.0
        assert item["income_exclusive"] == 20396.04
        assert item["expense"] == 500.0
        assert item["profit"] == 19896.04
        assert item["vat"] == 0.0
        assert item["counts"]["income"] == 1
        assert item["counts"]["expense"] == 1
        assert item["counts"]["journal_entries"] == 2
        assert item["locked"] is False
        assert item["start"] == "2026-01-01" and item["end"] == "2026-01-31"

    def test_list_periods_kind_filter_and_limit(self):
        _activity("2026-01")
        closing.ensure_period("2026Q1")
        months = closing.list_periods(kind="月")
        assert months and all(period["period_type"] == "月" for period in months)
        quarters = closing.list_periods(kind="季")
        assert [period["period"] for period in quarters] == ["2026Q1"]
        assert len(closing.list_periods(limit=2)) == 2
        with pytest.raises(ValueError):
            closing.list_periods(kind="年")

    def test_period_overview_checklist(self):
        _activity("2026-01")
        overview = closing.period_overview("2026-01")
        assert overview["period"] == "2026-01"
        assert overview["status"] == "未结账"
        checklist = overview["checklist"]
        assert set(checklist) >= {"ok", "level", "total", "pass", "warning", "error"}
        assert checklist["total"] == 10


# =============================================================================
# 2. 结账前检查
# =============================================================================
class TestChecks:
    def test_all_checks_present_and_pass(self):
        _activity("2026-01")
        report = closing.pre_close_checks("2026-01")
        assert [check["key"] for check in report["checks"]] == list(checks_module.CHECK_KEYS)
        assert report["level"] == "PASS"
        assert report["ok"] is True
        assert report["blocking"] == [] and report["warnings"] == []
        for check in report["checks"]:
            assert check["status"] in ("PASS", "WARNING", "ERROR")
            assert check["name"] and isinstance(check["fix_hint"], str)

    def test_future_period_error(self):
        report = closing.pre_close_checks("2027-01")
        future = next(check for check in report["checks"] if check["key"] == "period_in_future")
        assert future["status"] == "ERROR"
        assert report["ok"] is False
        assert report["level"] == "ERROR"
        assert any(check["key"] == "period_in_future" for check in report["blocking"])

    def test_duplicate_close_error(self):
        _mark_period("2026-01", "已结账")
        report = closing.pre_close_checks("2026-01")
        duplicate = next(check for check in report["checks"] if check["key"] == "status_not_closed")
        assert duplicate["status"] == "ERROR"
        assert "请勿重复结账" in duplicate["message"]

    def test_unbalanced_journal_error(self):
        _add_unbalanced_lines("2026-01")
        report = closing.pre_close_checks("2026-01")
        balanced = next(check for check in report["checks"] if check["key"] == "journal_balanced")
        assert balanced["status"] == "ERROR"
        assert "不平衡" in balanced["message"]
        assert report["ok"] is False

    def test_zero_activity_warning(self):
        report = closing.pre_close_checks("2026-05")
        activity = next(check for check in report["checks"] if check["key"] == "has_activity")
        assert activity["status"] == "WARNING"
        assert report["level"] == "WARNING"
        assert report["ok"] is True

    def test_zero_voucher_warning(self):
        table("journal_entries").append(
            {
                "date": "2026-05-10",
                "entry_no": "记-202605-0001",
                "description": "0 元凭证",
                "total_amount": 0.0,
                "source_ref": "",
            }
        )
        report = closing.pre_close_checks("2026-05")
        zero = next(check for check in report["checks"] if check["key"] == "no_zero_vouchers")
        assert zero["status"] == "WARNING"
        assert "0 元凭证" in zero["message"]

    def test_unmatched_ingest_warning(self):
        table("invoices").append(
            {
                "invoice_no": "INV-UNMATCHED",
                "invoice_date": "2026-01-15",
                "direction": "销项",
                "invoice_type": "普票",
                "seller": "本公司",
                "buyer": "某客户",
                "total_inclusive": 1000.0,
                "status": "待匹配",
            }
        )
        report = closing.pre_close_checks("2026-01")
        unmatched = next(check for check in report["checks"] if check["key"] == "no_unmatched_ingest")
        assert unmatched["status"] == "WARNING"
        assert "待匹配发票 1 张" in unmatched["message"]

    def test_duplicate_invoice_error(self):
        for code, date in (("DUP-001", "2026-01-10"), ("DUP-001", "2026-01-20")):
            table("invoices").append(
                {
                    "invoice_no": "DUP-001",
                    "invoice_code": code,
                    "invoice_date": date,
                    "direction": "销项",
                    "invoice_type": "普票",
                    "seller": "本公司",
                    "buyer": "某客户",
                    "total_inclusive": 1000.0,
                    "status": "待匹配",
                }
            )
        report = closing.pre_close_checks("2026-01")
        duplicates = next(check for check in report["checks"] if check["key"] == "invoice_duplicates")
        assert duplicates["status"] == "ERROR"
        assert "DUP-001" in duplicates["message"]

    def test_previous_period_warning(self):
        _activity("2026-01")
        report = closing.pre_close_checks("2026-02")
        previous = next(check for check in report["checks"] if check["key"] == "previous_period_closed")
        assert previous["status"] == "WARNING"
        assert "2026-01" in previous["message"]

    def test_previous_period_first_period_pass(self):
        report = closing.pre_close_checks("2025-12")
        previous = next(check for check in report["checks"] if check["key"] == "previous_period_closed")
        assert previous["status"] == "PASS"

    def test_policy_period_warning(self):
        report = closing.pre_close_checks("2028-01")
        policy = next(check for check in report["checks"] if check["key"] == "policy_period")
        assert policy["status"] == "WARNING"
        assert "不在已验证政策区间" in policy["message"]


class TestPayrollStatusCheck:
    """closing.checks 增量：payroll_status（契约 P1-B §4）。"""

    def test_pass_when_no_employees(self):
        report = closing.pre_close_checks("2026-05")
        check = next(item for item in report["checks"] if item["key"] == "payroll_status")
        assert check["status"] == "PASS"
        assert "无在职员工" in check["message"]

    def test_warning_when_active_employees_without_payroll(self):
        employees.add_employee({"name": "张三", "hire_date": "2026-01-01"})
        report = closing.pre_close_checks("2026-05")
        check = next(item for item in report["checks"] if item["key"] == "payroll_status")
        assert check["status"] == "WARNING"
        assert "无工资表" in check["message"]
        assert "零申报" in check["fix_hint"]

    def test_warning_when_draft_payroll_rows(self):
        employee = employees.add_employee({"name": "李四", "hire_date": "2026-01-01"})
        payroll.build_payroll("2026-05", employee_ids=[employee["id"]])
        report = closing.pre_close_checks("2026-05")
        check = next(item for item in report["checks"] if item["key"] == "payroll_status")
        assert check["status"] == "WARNING"
        assert "草稿工资" in check["message"]

    def test_pass_when_payroll_confirmed_or_declared(self):
        employee = employees.add_employee({"name": "王五", "hire_date": "2026-01-01"})
        payroll.build_payroll("2026-05", employee_ids=[employee["id"]])
        row = payroll.list_payroll("2026-05")[0]
        payroll.update_payroll_row(row["id"], {"gross_salary": 10000.0})
        payroll.recalc_period("2026-05")
        payroll.confirm_period("2026-05")
        report = closing.pre_close_checks("2026-05")
        check = next(item for item in report["checks"] if item["key"] == "payroll_status")
        assert check["status"] == "PASS"
        assert "已全部确认/已申报" in check["message"]

    def test_quarter_payroll_status_aggregates_months(self):
        employee = employees.add_employee({"name": "赵六", "hire_date": "2026-01-01"})
        for month in ("2026-01", "2026-02", "2026-03"):
            payroll.build_payroll(month, employee_ids=[employee["id"]])
        report = closing.pre_close_checks("2026Q1")
        check = next(item for item in report["checks"] if item["key"] == "payroll_status")
        assert check["status"] == "WARNING"
        assert "草稿工资" in check["message"]


# =============================================================================
# 3. 损益结转凭证
# =============================================================================
class TestClosingEntries:
    def test_closing_entries_preview_does_not_write(self):
        _activity("2026-01")
        before = len(journal.list_entries())
        entries = closing.closing_entries("2026-01")
        assert len(journal.list_entries()) == before
        assert len(entries) == 1
        entry = entries[0]
        assert entry["source_ref"] == "CLOSE-2026-01"
        assert entry["voucher_type"] == "结转"
        assert entry["date"] == "2026-01-31"
        debit = sum(line["amount"] for line in entry["lines"] if line["direction"] == "借")
        credit = sum(line["amount"] for line in entry["lines"] if line["direction"] == "贷")
        assert round(debit, 2) == round(credit, 2)

    def test_closing_entries_accounts_income_expense(self):
        _activity("2026-01", 20600.0)
        entry = closing.closing_entries("2026-01")[0]
        debits = {line["account"]: line["amount"] for line in entry["lines"] if line["direction"] == "借"}
        credits = {line["account"]: line["amount"] for line in entry["lines"] if line["direction"] == "贷"}
        assert debits["主营业务收入"] == 20396.04
        assert debits["本年利润"] == 500.0
        assert credits["本年利润"] == 20396.04
        assert credits["管理费用—办公费"] == 500.0

    def test_closing_entries_includes_rd_accounts(self):
        record = income_ledger.add_income("2026-01-12", "云图科技", 10100.0)
        journal.post_entry(journal.build_income_entry(record))
        rd_expense = expense_ledger.add_expense(
            "2026-01-20", "研发人员工资", 20000.0, category="工资", is_rd_related=True
        )
        journal.post_entry(journal.build_expense_entry(rd_expense))
        entry = closing.closing_entries("2026-01")[0]
        assert any(
            line["account"].startswith("研发支出") and line["direction"] == "贷" for line in entry["lines"]
        )

    def test_closing_entries_empty_when_no_activity(self):
        assert closing.closing_entries("2026-05") == []

    def test_closing_entries_excludes_existing_closing_voucher(self):
        _activity("2026-01")
        closing.close_period("2026-01")
        assert closing.closing_entries("2026-01") == []

    def test_close_period_filters_current_period_only(self):
        _activity("2026-01", 20600.0)
        _activity("2026-02", 10100.0)
        result = closing.close_period("2026-01")
        entry = result["entries"][0]
        debit_income = sum(
            line["amount"] for line in entry["lines"] if line["account"] == "主营业务收入"
        )
        assert debit_income == 20396.04  # 仅 1 月收入
        closing_entry = journal.list_entries(start="2026-01-01", end="2026-01-31")[-1]
        assert closing_entry["source_ref"] == "CLOSE-2026-01"
        feb_entries = journal.list_entries(start="2026-02-01", end="2026-02-28")
        assert feb_entries and all(item["source_ref"] != "CLOSE-2026-01" for item in feb_entries)


# =============================================================================
# 4. 月度结账/反结账状态机
# =============================================================================
class TestCloseStateMachine:
    def test_close_period_success(self):
        _activity("2026-01")
        result = closing.close_period("2026-01", operator="tester", note="月结")
        assert result["ok"] is True
        assert result["period"] == "2026-01"
        assert result["status"] == "已结账"
        assert len(result["entries"]) == 1
        assert result["snapshot_hash"] and len(result["snapshot_hash"]) == 64
        row = _period_row("2026-01")
        assert row["status"] == "已结账"
        assert row["closed_by"] == "tester"
        assert row["closed_at"]
        assert row["snapshot_hash"] == result["snapshot_hash"]
        assert row["snapshot_json"]["hash"] == result["snapshot_hash"]
        assert closing.is_closed("2026-01") is True
        assert "期间结账" in [item["action"] for item in read_log("user_action_log")]

    def test_close_period_duplicate_raises(self):
        _activity("2026-01")
        closing.close_period("2026-01")
        with pytest.raises(ValueError, match="重复结账"):
            closing.close_period("2026-01")

    def test_close_future_period_raises(self):
        with pytest.raises(ValueError, match="无法结账"):
            closing.close_period("2027-01")

    def test_close_unbalanced_raises_even_with_force(self):
        _add_unbalanced_lines("2026-01")
        with pytest.raises(ValueError, match="不平衡"):
            closing.close_period("2026-01")
        with pytest.raises(ValueError):
            closing.close_period("2026-01", force=True)

    def test_close_zero_activity_period_ok(self):
        result = closing.close_period("2026-05")
        assert result["ok"] is True
        assert result["entries"] == []
        assert closing.is_closed("2026-05") is True
        assert _period_row("2026-05")["status"] == "已结账"

    def test_close_quarter_period_via_close_period_raises(self):
        with pytest.raises(ValueError, match="close_quarter"):
            closing.close_period("2026Q1")

    def test_reopen_period_restores_state(self):
        _activity("2026-01")
        normal_entries = len(journal.list_entries())
        closing.close_period("2026-01", operator="tester")
        result = closing.reopen_period("2026-01", operator="tester", reason="调整")
        assert result["ok"] is True
        assert result["status"] == "未结账"
        assert len(result["removed_entries"]) == 1
        assert result["snapshot_hash"] == ""
        assert len(journal.list_entries()) == normal_entries  # 仅删除结转凭证
        row = _period_row("2026-01")
        assert row["status"] == "未结账"
        assert row["reopened_at"]
        assert row["snapshot_hash"] == ""
        assert closing.is_closed("2026-01") is False
        assert "期间反结账" in [item["action"] for item in read_log("user_action_log")]

    def test_reopen_unclosed_raises(self):
        with pytest.raises(ValueError, match="未结账"):
            closing.reopen_period("2026-01")

    def test_reopen_only_latest_period(self):
        _activity("2026-01")
        _activity("2026-02")
        closing.close_period("2026-01")
        closing.close_period("2026-02")
        with pytest.raises(ValueError, match="最近一个已结账期间"):
            closing.reopen_period("2026-01")
        closing.reopen_period("2026-02")
        assert closing.reopen_period("2026-01")["status"] == "未结账"

    def test_reopen_month_in_closed_quarter_raises(self):
        for month in ("2026-01", "2026-02", "2026-03"):
            _activity(month)
        _close_months("2026-01", "2026-02", "2026-03")
        closing.close_quarter("2026Q1")
        with pytest.raises(ValueError, match="所属季度"):
            closing.reopen_period("2026-03")

    def test_is_closed_transitions(self):
        assert closing.is_closed("2026-01") is False
        _mark_period("2026-01", "已结账")
        assert closing.is_closed("2026-01") is True


# =============================================================================
# 5. 季度结账/反结账
# =============================================================================
class TestQuarter:
    def test_close_quarter_requires_three_months(self):
        _activity("2026-01")
        closing.close_period("2026-01")
        with pytest.raises(ValueError, match="3 个月均已结账"):
            closing.close_quarter("2026Q1")

    def test_close_quarter_success(self):
        for month in ("2026-01", "2026-02", "2026-03"):
            _activity(month)
        _close_months("2026-01", "2026-02", "2026-03")
        entries_before = len(journal.list_entries())
        result = closing.close_quarter("2026Q1", operator="tester")
        assert result["ok"] is True
        assert result["status"] == "已结账"
        assert result["period_type"] == "季"
        assert result["months"] == {"2026-01": "已结账", "2026-02": "已结账", "2026-03": "已结账"}
        assert result["snapshot_hash"]
        # 季度结账不重复生成结转凭证
        assert len(journal.list_entries()) == entries_before
        assert closing.is_closed("2026Q1") is True
        assert _period_row("2026Q1")["period_type"] == "季"
        assert "季度结账" in [item["action"] for item in read_log("user_action_log")]

    def test_close_quarter_duplicate_raises(self):
        for month in ("2026-01", "2026-02", "2026-03"):
            _activity(month)
        _close_months("2026-01", "2026-02", "2026-03")
        closing.close_quarter("2026Q1")
        with pytest.raises(ValueError, match="请勿重复结账"):
            closing.close_quarter("2026Q1")

    def test_close_quarter_rejects_month_period(self):
        with pytest.raises(ValueError, match="不是季度期间"):
            closing.close_quarter("2026-01")

    def test_reopen_quarter(self):
        for month in ("2026-01", "2026-02", "2026-03"):
            _activity(month)
        _close_months("2026-01", "2026-02", "2026-03")
        closing.close_quarter("2026Q1")
        result = closing.reopen_quarter("2026Q1", reason="调整")
        assert result["status"] == "未结账"
        assert result["months_unchanged"] is True
        assert _period_row("2026Q1")["status"] == "未结账"
        assert closing.is_closed("2026Q1") is False
        # 月度状态不变
        assert _period_row("2026-01")["status"] == "已结账"
        assert closing.is_closed("2026-01") is True

    def test_reopen_quarter_unclosed_raises(self):
        with pytest.raises(ValueError, match="无需反结账"):
            closing.reopen_quarter("2026Q1")

    def test_reopen_quarter_only_latest(self):
        for month in ("2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"):
            _activity(month)
        _close_months("2026-01", "2026-02", "2026-03")
        closing.close_quarter("2026Q1")
        _close_months("2026-04", "2026-05", "2026-06")
        closing.close_quarter("2026Q2")
        with pytest.raises(ValueError, match="最近一个已结账季度"):
            closing.reopen_quarter("2026Q1")


# =============================================================================
# 6. 期间快照
# =============================================================================
class TestSnapshot:
    def test_period_snapshot_structure(self):
        _activity("2026-01")
        snapshot = closing.period_snapshot("2026-01")
        assert set(snapshot) >= {
            "period",
            "period_type",
            "generated_at",
            "start",
            "end",
            "totals",
            "counts",
            "balances",
            "hash",
            "disclaimer",
        }
        assert snapshot["period"] == "2026-01"
        assert snapshot["period_type"] == "月"
        assert len(snapshot["hash"]) == 64
        assert set(snapshot["totals"]) == {
            "income_inclusive",
            "income_exclusive",
            "vat",
            "expense",
            "profit",
            "rd_cost",
            "rd_deduction",
            "surtax",
        }
        assert snapshot["totals"]["income_inclusive"] == 20600.0
        assert snapshot["counts"]["income"] == 1
        assert snapshot["counts"]["journal_entries"] == 2
        assert isinstance(snapshot["balances"], dict)

    def test_snapshot_hash_reproducible(self):
        _activity("2026-01")
        first = closing.period_snapshot("2026-01")
        second = closing.period_snapshot("2026-01")
        assert first["hash"] == second["hash"]
        # 哈希只覆盖业务数据：可手工复算
        payload = {key: value for key, value in first.items() if key not in ("hash", "generated_at", "disclaimer")}
        expected = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        assert first["hash"] == expected
        # 金额统一 2 位小数
        for value in first["totals"].values():
            assert round(value, 2) == value

    def test_verify_snapshot_ok_after_close(self):
        _activity("2026-01")
        closing.close_period("2026-01")
        result = closing.verify_snapshot("2026-01")
        assert result["ok"] is True
        assert result["expected_hash"] == result["current_hash"]
        assert result["changes"] == []

    def test_verify_snapshot_before_close(self):
        _activity("2026-01")
        result = closing.verify_snapshot("2026-01")
        assert result["ok"] is False
        assert result["expected_hash"] == ""
        assert any("尚未结账" in change for change in result["changes"])

    def test_verify_snapshot_detects_tamper(self):
        _activity("2026-01")
        closing.close_period("2026-01")
        income_table = table("income")
        first = income_table.read()[0]
        income_table.update(first["id"], {"inclusive_amount": 999999.0})
        result = closing.verify_snapshot("2026-01")
        assert result["ok"] is False
        assert result["expected_hash"] != result["current_hash"]
        assert any("收入含税合计" in change for change in result["changes"])

    def test_snapshot_balances_include_closing_accounts(self):
        _activity("2026-01")
        closing.close_period("2026-01")
        stored = _period_row("2026-01")["snapshot_json"]
        balances = stored["balances"]
        assert "主营业务收入" in balances
        assert "管理费用—办公费" in balances
        assert "本年利润" in balances
        # 结账后收入/费用科目已结转平
        assert balances["主营业务收入"] == 0.0
        assert balances["管理费用—办公费"] == 0.0

    def test_quarter_snapshot(self):
        for month in ("2026-01", "2026-02", "2026-03"):
            _activity(month)
        snapshot = closing.period_snapshot("2026Q1")
        assert snapshot["period_type"] == "季"
        assert snapshot["start"] == "2026-01-01" and snapshot["end"] == "2026-03-31"
        assert snapshot["counts"]["income"] == 3
        assert len(snapshot["hash"]) == 64
        assert snapshot["hash"] == closing.period_snapshot("2026Q1")["hash"]


# =============================================================================
# 7. 期间锁接入
# =============================================================================
class TestPeriodLocks:
    def test_income_write_paths_locked(self, tmp_path):
        _activity("2026-01")
        record = income_ledger.list_income()[0]
        closing.close_period("2026-01")
        with pytest.raises(locks.PeriodLockedError):
            income_ledger.add_income("2026-01-20", "新客户", 100.0)
        with pytest.raises(locks.PeriodLockedError):
            income_ledger.update_income(record["id"], {"note": "修改"})
        with pytest.raises(locks.PeriodLockedError):
            income_ledger.delete_income(record["id"])
        csv_path = tmp_path / "income.csv"
        csv_path.write_text("收款日期,客户名称,含税金额\n2026-01-20,某客户,1000.00\n", encoding="utf-8-sig")
        with pytest.raises(locks.PeriodLockedError):
            income_ledger.import_income_csv(csv_path)
        # 未结账期间可正常写入
        assert income_ledger.add_income("2026-02-10", "新客户", 100.0)["date"] == "2026-02-10"

    def test_expense_write_paths_locked(self, tmp_path):
        _activity("2026-01")
        record = expense_ledger.list_expenses()[0]
        closing.close_period("2026-01")
        with pytest.raises(locks.PeriodLockedError):
            expense_ledger.add_expense("2026-01-20", "办公耗材", 100.0, category="办公")
        with pytest.raises(locks.PeriodLockedError):
            expense_ledger.update_expense(record["id"], {"amount": 600.0})
        with pytest.raises(locks.PeriodLockedError):
            expense_ledger.delete_expense(record["id"])
        csv_path = tmp_path / "expense.csv"
        csv_path.write_text("发生日期,费用描述,金额,费用类别\n2026-01-20,办公耗材,100.00,办公\n", encoding="utf-8-sig")
        with pytest.raises(locks.PeriodLockedError):
            expense_ledger.import_expense_csv(csv_path)

    def test_journal_locked_and_bypass(self):
        _activity("2026-01")
        closing.close_period("2026-01")
        entry = {
            "date": "2026-01-25",
            "description": "测试锁定凭证",
            "voucher_type": "其他",
            "lines": [
                {"account": "库存现金", "direction": "借", "amount": 100.0},
                {"account": "其他收益", "direction": "贷", "amount": 100.0},
            ],
        }
        with pytest.raises(locks.PeriodLockedError):
            journal.post_entry(entry)
        with pytest.raises(locks.PeriodLockedError):
            journal.post_entries([entry])
        # 结账内部绕过锁（check_lock=False）可写入
        posted = journal.post_entry(entry, check_lock=False)
        assert posted["entry_no"]

    def test_rd_write_paths_locked(self):
        record = ledger.rd_auxiliary.add_rd_record("研发项目", "2026Q1", personnel_cost=1000.0)
        _mark_period("2026-01", "已结账")
        with pytest.raises(locks.PeriodLockedError):
            ledger.rd_auxiliary.add_rd_record("研发项目", "2026Q1", personnel_cost=2000.0)
        with pytest.raises(locks.PeriodLockedError):
            ledger.rd_auxiliary.update_rd_record(record["id"], {"personnel_cost": 3000.0})
        with pytest.raises(locks.PeriodLockedError):
            ledger.rd_auxiliary.delete_rd_record(record["id"])

    def test_quarter_locked_blocks_its_months(self):
        _mark_period("2026Q1", "已结账")
        with pytest.raises(locks.PeriodLockedError):
            income_ledger.add_income("2026-02-10", "客户", 100.0)
        assert income_ledger.add_income("2026-04-10", "客户", 100.0)["date"] == "2026-04-10"

    def test_ingest_confirm_locked(self):
        from ingest import bank_parser, invoice_parser, matcher

        invoices = invoice_parser.parse_invoice_file(FIXTURES / "invoice_sales.xml")
        transactions = bank_parser.parse_bank_file(FIXTURES / "bank_cmb.csv")
        invoice_parser.save_invoices(invoices["invoices"])
        bank_parser.save_transactions(transactions["transactions"])
        suggestions = matcher.suggest_matches()["created"]
        assert suggestions
        match = suggestions[0]
        _mark_period("2026-01", "已结账")
        with pytest.raises(locks.PeriodLockedError):
            matcher.confirm_match(match["id"])
        assert table("matches").get(match["id"])["status"] == "建议"
        assert income_ledger.summarize_income()["count"] == 0

    def test_default_unlocked_without_periods(self):
        _activity("2026-01")
        assert income_ledger.summarize_income()["count"] == 1
        assert expense_ledger.summarize_expenses()["count"] == 1
        assert journal.trial_balance()["balanced"] is True
        assert closing.is_closed("2026-01") is False

    def test_locked_error_attributes(self):
        _mark_period("2026-01", "已结账")
        with pytest.raises(locks.PeriodLockedError) as excinfo:
            income_ledger.add_income("2026-01-20", "客户", 100.0)
        error = excinfo.value
        assert error.period == "2026-01"
        assert error.action == "新增收入"
        assert error.status == "已结账"
