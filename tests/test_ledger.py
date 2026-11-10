"""ledger 台账模块测试：CRUD、汇总、期间取数、分录平衡、报表、示例数据、CSV 与审计日志。

测试环境由 ``tests/conftest.py`` 自动隔离（临时目录 + 每个用例清空台账），
示例数据通过 ``demo_data`` fixture 加载（企业：某科技，2026 Q1—Q4）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import config
import ledger
from engine import current_quarter, round2
from ledger import (
    audit,
    expense_ledger,
    income_ledger,
    journal,
    rd_auxiliary,
    reports,
    seed,
)
from ledger.store import read_log, table


# =============================================================================
# 收入台账
# =============================================================================
class TestIncomeLedger:
    def test_add_income_derives_exclusive_and_vat(self):
        """新增收入自动按 1% 征收率推导不含税金额与增值税额。"""
        record = income_ledger.add_income(
            "2026-01-05", "云图科技", 20600.0, invoice_type="普票", category="技术服务", description="技术服务费"
        )
        assert record["id"].startswith("INC")
        assert record["inclusive_amount"] == 20600.0
        assert record["exclusive_amount"] == 20396.04
        assert record["vat_amount"] == 203.96
        assert record["date"] == "2026-01-05"
        assert record["is_rd_related"] is False

    def test_income_crud(self):
        """收入增删改查闭环，更新含税金额后自动重算不含税金额。"""
        record = income_ledger.add_income("2026/1/8", "滇南数据", 10100.0, invoice_type="专票", category="软件开发")
        income_id = record["id"]

        fetched = income_ledger.get_income(income_id)
        assert fetched is not None and fetched["invoice_type"] == "专票"
        assert income_ledger.list_income(start="2026-01-01", end="2026-01-31")[0]["id"] == income_id

        updated = income_ledger.update_income(income_id, {"inclusive_amount": 20200.0, "note": "改价"})
        assert updated["inclusive_amount"] == 20200.0
        assert updated["exclusive_amount"] == 20000.0
        assert updated["vat_amount"] == 200.0
        assert updated["note"] == "改价"

        assert income_ledger.delete_income(income_id) is True
        assert income_ledger.get_income(income_id) is None
        assert income_ledger.delete_income("INC999999") is False

    def test_income_filters_and_summary(self):
        """按发票类型/类别过滤，并校验汇总结构。"""
        income_ledger.add_income("2026-01-05", "云图科技", 10100.0, invoice_type="专票", category="技术服务")
        income_ledger.add_income("2026-01-06", "滇南数据", 30300.0, invoice_type="普票", category="软件开发")
        income_ledger.add_income(
            "2026-01-07", "昆明智联", 5050.0, invoice_type="普票", category="运维服务", is_rd_related=True
        )
        special = income_ledger.list_income(invoice_type="专票")
        assert len(special) == 1 and special[0]["invoice_type"] == "专票"
        assert len(income_ledger.list_income(category="软件开发")) == 1
        rd_rows = income_ledger.list_income(is_rd_related=True)
        assert len(rd_rows) == 1 and rd_rows[0]["client"] == "昆明智联"

        summary = income_ledger.summarize_income()
        assert summary["count"] == 3
        assert summary["total_inclusive"] == 45450.0
        assert summary["by_invoice_type"]["专票"]["count"] == 1
        assert summary["by_invoice_type"]["普票"]["count"] == 2
        assert summary["rd_inclusive"] == 5050.0
        assert summary["max_single"] == 30300.0
        assert set(summary["by_category"]) == {"技术服务", "软件开发", "运维服务"}

    def test_period_totals_from_seed(self, demo_data):
        """示例数据的季度取数：Q1 20 万全普票、Q2 含 10.1 万专票、Q3 25 万、Q4 12 万。"""
        q1 = income_ledger.period_totals("2026Q1")
        assert q1["total_inclusive"] == 200000.0
        assert q1["special_inclusive"] == 0.0
        assert q1["ordinary_inclusive"] == 200000.0
        assert q1["count"] == 6
        assert q1["start"] == "2026-01-01" and q1["end"] == "2026-03-31"

        q2 = income_ledger.period_totals("2026Q2")
        assert q2["total_inclusive"] == 280000.0
        assert q2["special_inclusive"] == 101000.0
        assert q2["special_exclusive"] == 100000.0
        assert q2["ordinary_exclusive"] == 177227.72

        assert income_ledger.period_totals("2026Q3")["total_inclusive"] == 250000.0
        assert income_ledger.period_totals("2026Q4")["total_inclusive"] == 120000.0
        assert income_ledger.period_totals("2026-01")["total_inclusive"] == 72000.0
        assert income_ledger.period_totals("2026")["count"] == 21

    def test_invalid_input_raises(self):
        """非法发票类型与负数金额抛 ValueError。"""
        with pytest.raises(ValueError):
            income_ledger.add_income("2026-01-01", "客户", 1000.0, invoice_type="收据")
        with pytest.raises(ValueError):
            income_ledger.add_income("2026-01-01", "客户", -100.0)
        with pytest.raises(ValueError):
            income_ledger.period_totals("2026X9")


# =============================================================================
# 费用台账
# =============================================================================
class TestExpenseLedger:
    def test_expense_crud_and_summary(self):
        """费用新增、更新、删除、按类别汇总与研发标记汇总。"""
        first = expense_ledger.add_expense("2026-02-20", "研发测试材料", 3800.0, category="研发材料", is_rd_related=True)
        expense_ledger.add_expense("2026-02-21", "办公耗材", 1200.0, category="办公")
        assert first["id"].startswith("EXP")

        updated = expense_ledger.update_expense(first["id"], {"amount": 4200.0})
        assert updated["amount"] == 4200.0
        assert len(expense_ledger.list_expenses(category="办公")) == 1
        assert len(expense_ledger.list_expenses(is_rd_related=True)) == 1

        summary = expense_ledger.summarize_expenses()
        assert summary["count"] == 2
        assert summary["total"] == 5400.0
        assert summary["rd_total"] == 4200.0
        assert summary["by_category"]["研发材料"]["amount"] == 4200.0

        period = expense_ledger.period_totals("2026Q1")
        assert period["period"] == "2026Q1"
        assert period["total"] == 5400.0
        assert period["rd_total"] == 4200.0

        assert expense_ledger.delete_expense(first["id"]) is True
        assert expense_ledger.get_expense(first["id"]) is None

    def test_seed_expense_totals(self, demo_data):
        """示例数据 Q1 费用 100,530 元（含研发工资 6 万）。"""
        q1 = expense_ledger.period_totals("2026Q1")
        assert q1["count"] == 12
        assert q1["total"] == 100530.0
        assert q1["by_category"]["工资"]["amount"] == 60000.0
        assert q1["by_category"]["研发材料"]["amount"] == 3800.0


# =============================================================================
# 研发辅助账
# =============================================================================
class TestRdAuxiliary:
    def test_rd_crud(self):
        """研发归集记录增改删，total 自动等于六项费用之和。"""
        record = rd_auxiliary.add_rd_record(
            "税务数据平台", "2026Q1", personnel_cost=20000.0, material_cost=3000.0, note="测试"
        )
        assert record["total"] == 23000.0
        assert record["deduction_amount"] == 23000.0

        updated = rd_auxiliary.update_rd_record(record["id"], {"material_cost": 5000.0})
        assert updated["total"] == 25000.0
        assert updated["deduction_amount"] == 25000.0

        assert len(rd_auxiliary.list_rd_records("2026Q1")) == 1
        assert rd_auxiliary.list_rd_records(project="税务数据平台")[0]["id"] == record["id"]
        assert rd_auxiliary.delete_rd_record(record["id"]) is True

    def test_collect_from_expenses(self, demo_data):
        """从费用台账自动归集研发费用（仅 is_rd_related=True）。"""
        collected = rd_auxiliary.collect_from_expenses("2026Q1")
        assert collected["period"] == "2026Q1"
        assert collected["total_collectible"] == 64480.0
        assert collected["deduction_amount"] == 64480.0
        assert collected["items"]["人员人工费用"] == 60000.0
        assert collected["items"]["直接投入费用"] == 3800.0
        assert collected["source_count"] == 5

    def test_summarize_rd_combines_sources(self, demo_data):
        """研发汇总 = 费用台账自动归集 + 研发辅助账手工记录，并给出按项目明细。"""
        summary = rd_auxiliary.summarize_rd("2026Q1")
        assert summary["from_expenses_total"] == 64480.0
        assert summary["manual_total"] == 6000.0
        assert summary["total_collectible"] == 70480.0
        assert summary["deduction_amount"] == 70480.0
        assert summary["from_manual"]["count"] == 1
        assert len(summary["records_detail"]) == 1
        assert summary["by_project"][0]["project_name"] == "费用台账自动归集"
        assert any(item["project_name"] == "税务数据智能归集平台V2" for item in summary["by_project"])

    def test_ytd_summary(self, demo_data):
        """研发费用本年累计口径：Q3 累计大于 Q1 累计。"""
        q1 = rd_auxiliary.ytd_summary("2026Q1")
        q3 = rd_auxiliary.ytd_summary("2026Q3")
        assert q1["rd_cost"] == 70480.0
        assert q3["rd_cost"] > q1["rd_cost"]
        assert q3["quarters"] == ["2026Q1", "2026Q2", "2026Q3"]


# =============================================================================
# 凭证与试算平衡
# =============================================================================
class TestJournal:
    def test_income_entry_balanced_and_posted(self):
        """收入凭证：借银行存款、贷主营业务收入+应交税费，借贷平衡且可查询。"""
        record = income_ledger.add_income("2026-01-05", "云图科技", 20600.0)
        entry = journal.build_income_entry(record)
        assert entry["voucher_type"] == "收入"
        debit = sum(line["amount"] for line in entry["lines"] if line["direction"] == "借")
        credit = sum(line["amount"] for line in entry["lines"] if line["direction"] == "贷")
        assert round2(debit) == round2(credit) == 20600.0
        assert entry["lines"][1]["account"] == "主营业务收入"

        posted = journal.post_entry(entry)
        assert posted["entry_no"].startswith("记-202601-")
        assert journal.get_entry(posted["entry_no"])["source_ref"] == record["id"]
        assert len(journal.list_entries()) == 1
        assert journal.list_entries()[0]["lines"][0]["direction"] == "借"

        balance = journal.trial_balance()
        assert balance["balanced"] is True
        assert balance["total_debit"] == balance["total_credit"] == 20600.0

    def test_unbalanced_entry_rejected(self):
        """借贷不平衡的凭证登记时被拒绝。"""
        entry = {
            "date": "2026-01-05",
            "description": "测试不平衡",
            "voucher_type": "其他",
            "lines": [
                {"account": "银行存款", "direction": "借", "amount": 100.0},
                {"account": "主营业务收入", "direction": "贷", "amount": 90.0},
            ],
        }
        with pytest.raises(ValueError, match="借贷不平衡"):
            journal.post_entry(entry)

    def test_expense_and_salary_entries(self):
        """费用凭证与工资计提凭证均借贷平衡，工资含代扣个税与社保。"""
        expense = expense_ledger.add_expense("2026-02-20", "办公耗材", 1250.0, category="办公")
        entry = journal.build_expense_entry(expense)
        assert entry["lines"][0]["account"] == "管理费用—办公费"
        assert entry["lines"][1]["account"] == "银行存款"

        salary = journal.build_salary_entry(20000.0, date="2026-02-25", personal_tax=120.0, social_insurance=1600.0)
        debit = sum(line["amount"] for line in salary["lines"] if line["direction"] == "借")
        credit = sum(line["amount"] for line in salary["lines"] if line["direction"] == "贷")
        assert round2(debit) == round2(credit) == 20000.0
        assert any(line["account"] == "应交税费—应交个人所得税" for line in salary["lines"])

        journal.post_entries([entry, salary])
        balance = journal.trial_balance()
        assert balance["balanced"] is True
        assert len(journal.list_entries()) == 2

    def test_vat_exemption_and_payment_entries(self, demo_data):
        """Q1 免征增值税结转其他收益；Q2 专票部分缴税；Q1 无需缴税。"""
        exemption = journal.build_vat_exemption_entry("2026Q1")
        assert exemption is not None
        amount = exemption["lines"][0]["amount"]
        assert amount == 1980.21  # 按各笔收入已计提增值税合计（与凭证勾稽）
        assert exemption["lines"][0]["account"] == "应交税费—应交增值税"
        assert exemption["lines"][1]["account"] == "其他收益"

        payment = journal.build_vat_payment_entry("2026Q2")
        assert payment is not None
        assert payment["lines"][0]["amount"] == 1000.0
        assert payment["lines"][-1]["account"] == "银行存款"

        assert journal.build_vat_payment_entry("2026Q1") is None

        # 超过季度免税额度时不得生成免征增值税结转
        income_ledger.add_income("2026-02-18", "云南远卓", 204000.0)
        assert journal.build_vat_exemption_entry("2026Q1") is None
        payment = journal.build_vat_payment_entry("2026Q1")
        assert payment is not None and payment["lines"][-1]["amount"] == 4240.0

    def test_trial_balance_from_seed_is_balanced(self, demo_data):
        """示例数据生成的凭证整体试算平衡，且增值税科目结平。"""
        balance = journal.trial_balance()
        assert balance["balanced"] is True
        assert balance["total_debit"] == balance["total_credit"]
        vat_account = next(item for item in balance["accounts"] if item["account"] == "应交税费—应交增值税")
        assert vat_account["balance"] == 0.0

    def test_next_entry_no_increments(self):
        """凭证号按月顺序递增。"""
        first = journal.next_entry_no("2026-01-05")
        assert first == "记-202601-0001"
        record = income_ledger.add_income("2026-01-05", "客户", 1010.0)
        journal.post_entry(journal.build_income_entry(record))
        assert journal.next_entry_no("2026-01-20") == "记-202601-0002"
        assert journal.next_entry_no("2026-02-01") == "记-202602-0001"


# =============================================================================
# 报表
# =============================================================================
class TestReports:
    def test_monthly_summary(self, demo_data):
        """月度汇总返回 12 个月，收入/费用/利润口径一致。"""
        rows = reports.monthly_summary(2026)
        assert len(rows) == 12
        january = rows[0]
        assert january["month"] == "2026-01"
        assert january["income_inclusive"] == 72000.0
        assert january["income_exclusive"] == 71287.12
        assert january["expense"] == 32280.0
        assert january["profit"] == round2(january["income_exclusive"] - january["expense"])
        assert january["tax"] == 0.0
        limited = reports.monthly_summary(2026, months=[1, 2])
        assert [row["month"] for row in limited] == ["2026-01", "2026-02"]

    def test_quarterly_summary_and_income_expense_report(self, demo_data):
        """季度汇总与收支报表字段齐全、数据正确。"""
        summary = reports.quarterly_summary("2026Q1")
        assert summary["label"] == "2026年第一季度"
        assert summary["income_inclusive"] == 200000.0
        assert summary["income_exclusive"] == 198019.79
        assert summary["expense"] == 100530.0
        assert summary["profit"] == 97489.79
        assert summary["vat"] == 0.0
        assert summary["vat_exempt"] == 1980.2
        assert summary["rd_cost"] == 70480.0
        assert summary["within_exemption_limit"] is True

        report = reports.income_expense_report("2026Q1")
        assert report["start"] == "2026-01-01" and report["end"] == "2026-03-31"
        assert report["count_income"] == 6
        assert report["count_expense"] == 12
        assert "技术服务" in report["by_category_income"]
        assert "工资" in report["by_category_expense"]

    def test_profit_statement(self, demo_data):
        """利润表行次齐全且勾稽：营业收入 - 成本 - 税费 - 费用 = 营业利润。"""
        statement = reports.profit_statement("2026Q1")
        rows = {item["row"]: item["amount"] for item in statement}
        assert rows["一、营业收入"] == 198019.79
        assert rows["减：营业成本"] == 34800.0
        assert rows["研发费用"] == 64480.0
        total_expense = (
            rows["减：营业成本"]
            + rows["税金及附加"]
            + rows["销售费用"]
            + rows["管理费用"]
            + rows["研发费用"]
            + rows["财务费用"]
        )
        assert rows["二、营业利润"] == round2(rows["一、营业收入"] - total_expense)
        assert rows["三、利润总额"] == rows["二、营业利润"]
        assert rows["四、净利润"] == round2(rows["三、利润总额"] - rows["减：所得税费用"])

    def test_dashboard(self, demo_data):
        """驾驶舱数据字段齐全，含台账笔数、申报提醒与最近记录。"""
        data = reports.dashboard()
        assert data["period"] == current_quarter()
        assert data["counts"] == {"income": 21, "expense": 40, "rd": 4, "contracts": 5}
        assert data["ytd_income"] > 0
        assert len(data["recent_income"]) == 5
        assert len(data["recent_expense"]) == 5
        assert "next_deadline" in data and "reminders" in data
        assert data["disclaimer"] == config.DISCLAIMER

    def test_contract_crud_and_summary(self):
        """合同台账 CRUD 与按类型汇总（供印花税取数）。"""
        contract = reports.add_contract(
            "2026-01-10", "云图科技", 180000.0, contract_type="技术合同", contract_no="YR-2026-001"
        )
        reports.add_contract("2026-03-05", "昆明锐捷", 18000.0, contract_type="买卖合同")
        assert len(reports.list_contracts()) == 2
        summary = reports.summarize_contracts()
        assert summary["count"] == 2
        assert summary["total_amount"] == 198000.0
        assert summary["by_type"]["技术合同"]["amount"] == 180000.0

        updated = reports.update_contract(contract["id"], {"amount": 200000.0})
        assert updated["amount"] == 200000.0
        assert reports.delete_contract(contract["id"]) is True
        assert reports.get_contract(contract["id"]) is None


# =============================================================================
# 示例数据
# =============================================================================
class TestSeed:
    def test_seed_demo_data(self, demo_data):
        """示例数据生成成功，季度流水与企业信息符合演示要求。"""
        assert seed.is_empty() is False
        assert table("income").count() == 21
        assert table("expense").count() == 40
        assert table("rd").count() == 4
        assert table("contracts").count() == 5
        assert table("journal_entries").count() == 66
        assert table("journal_lines").count() == 154
        total_inclusive = income_ledger.summarize_income()["total_inclusive"]
        assert total_inclusive == 850000.0
        # 技术合同为主
        contracts = reports.list_contracts()
        assert sum(1 for row in contracts if row["contract_type"] == "技术合同") >= 3
        # 每季度均有研发标记费用
        assert expense_ledger.summarize_expenses()["rd_total"] > 0

    def test_seed_rejects_duplicate_without_reset(self, demo_data):
        """已有数据时未指定 reset 不允许重复生成。"""
        with pytest.raises(ValueError, match="reset"):
            seed.seed_demo_data()

    def test_clear_all(self, demo_data):
        """清空全部台账后 is_empty 为真（日志保留）。"""
        seed.clear_all()
        assert seed.is_empty() is True
        assert table("filings").count() == 0
        assert table("income").count() == 0

    def test_seed_empty_storage(self):
        """空台账时 is_empty 为真，可直接生成示例数据。"""
        assert seed.is_empty() is True
        counts = seed.seed_demo_data()
        assert counts["income"] == 21
        assert counts["journal_entries"] == 66
        assert counts["filings"] == 0


# =============================================================================
# CSV 导入导出与审计日志
# =============================================================================
class TestCsvAndAudit:
    def test_income_csv_roundtrip(self, demo_data):
        """导出收入 CSV（中文表头）后再导入，条数一致。"""
        exported = income_ledger.export_income_csv()
        assert isinstance(exported, Path) and exported.exists()
        content = exported.read_text(encoding="utf-8-sig")
        assert "含税金额" in content and "客户名称" in content

        table("income").replace_all([])
        assert income_ledger.summarize_income()["count"] == 0
        imported = income_ledger.import_income_csv(exported)
        assert imported == 21
        assert income_ledger.summarize_income()["total_inclusive"] == 850000.0

    def test_expense_and_rd_csv_export(self, demo_data):
        """费用 CSV 可导出可导入；研发辅助账与合同 CSV 可导出。"""
        expense_csv = expense_ledger.export_expense_csv()
        assert expense_csv.exists()
        table("expense").replace_all([])
        assert expense_ledger.import_expense_csv(expense_csv) == 40
        assert expense_ledger.summarize_expenses()["count"] == 40

        rd_csv = rd_auxiliary.export_rd_csv()
        assert rd_csv.exists() and "研发项目" in rd_csv.read_text(encoding="utf-8-sig")
        contract_csv = reports.export_contracts_csv()
        assert contract_csv.exists() and "合同类型" in contract_csv.read_text(encoding="utf-8-sig")

    def test_audit_logs_written(self):
        """新增收入/费用、登记凭证都会写对应审计日志。"""
        record = income_ledger.add_income("2026-01-05", "云图科技", 20600.0)
        expense = expense_ledger.add_expense("2026-01-06", "办公耗材", 500.0, category="办公")
        journal.post_entry(journal.build_income_entry(record))
        journal.post_entry(journal.build_expense_entry(expense))

        user_actions = read_log("user_action_log")
        assert any(row["action"] == "新增收入" for row in user_actions)
        assert any(row["action"] == "新增费用" for row in user_actions)
        journal_logs = read_log("journal_log")
        assert len(journal_logs) == 2
        assert journal_logs[0]["debit"] and journal_logs[0]["credit"]

        logs = audit.recent_logs(limit=10)
        assert set(logs) == {
            "calculation_log",
            "journal_log",
            "policy_check_log",
            "user_action_log",
            "filing_log",
        }
        assert len(logs["user_action_log"]) > 0

    def test_calculation_log_helper(self):
        """audit.log_calculation 写入算税日志。"""
        audit.log_calculation("增值税", "2026Q1", {"total": 103000}, {"vat_payable": 0})
        rows = read_log("calculation_log")
        assert len(rows) == 1
        assert rows[0]["calc_type"] == "增值税"
        assert "103000" in rows[0]["inputs"]
