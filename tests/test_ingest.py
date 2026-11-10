"""ingest 票据/流水导入与自动匹配测试（P0-2）。

覆盖契约第 7 节：发票 XML/OFD/PDF 容错解析、去重入库、银行流水 CSV/XLSX/编码/方向判定、
确定性匹配评分（金额/日期/名称/方向/窗口/贪心一对一）、确认入账与凭证、忽略、汇总、
演示数据与多文件编排。

测试数据使用 ``tests/fixtures/ingest/`` 下现成 fixture 与 ``examples/`` 示例文件，不访问网络。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import config
import ledger
from ledger import expense_ledger, income_ledger, journal
from ledger.store import read_log, table

import ingest
import ingest.bank_parser as bank_parser
import ingest.demo as demo
import ingest.importer as importer
import ingest.invoice_parser as invoice_parser
import ingest.matcher as matcher

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ingest"


def fixture_path(name: str) -> Path:
    return FIXTURES / name


def fixture_bytes(name: str) -> bytes:
    return fixture_path(name).read_bytes()


class FakeUpload:
    """模拟 Streamlit ``UploadedFile``（.name + .getvalue()）。"""

    def __init__(self, name: str, data: bytes) -> None:
        self.name = name
        self._data = data

    def getvalue(self) -> bytes:
        return self._data


def _invoice(**overrides) -> dict:
    base = {
        "id": "INV000001",
        "invoice_no": "26530100000012345678",
        "invoice_date": "2026-01-15",
        "direction": "销项",
        "invoice_type": "普票",
        "seller": "禄劝云睿网络科技开发有限公司",
        "buyer": "云图科技有限公司",
        "total_inclusive": 20600.0,
        "status": "待匹配",
    }
    base.update(overrides)
    return base


def _transaction(**overrides) -> dict:
    base = {
        "id": "BTX000001",
        "transaction_date": "2026-01-15",
        "direction": "收入",
        "amount": 20600.0,
        "counterparty": "云图科技有限公司",
        "summary": "技术服务费",
        "status": "待匹配",
    }
    base.update(overrides)
    return base


def _save_invoice_pair() -> dict:
    """入库一张销项发票 + 一笔等额收入流水，返回匹配建议。"""
    invoice = invoice_parser.parse_invoice_file(fixture_path("invoice_sales.xml"))
    transaction = bank_parser.parse_bank_file(fixture_path("bank_cmb.csv"))
    invoice_parser.save_invoices(invoice["invoices"])
    bank_parser.save_transactions(transaction["transactions"])
    result = matcher.suggest_matches()
    assert result["count"] >= 1
    return result


# =============================================================================
# 1. 发票解析
# =============================================================================
class TestInvoiceParsing:
    def test_parse_sales_xml(self):
        """销项 XML：方向销项、普票、金额与日期正确，来源文件回填。"""
        result = invoice_parser.parse_invoice_file(fixture_path("invoice_sales.xml"))
        assert result["ok"] is True
        assert result["format"] == "XML"
        assert len(result["invoices"]) == 1
        invoice = result["invoices"][0]
        assert invoice["invoice_no"] == "26530100000012345678"
        assert invoice["invoice_date"] == "2026-01-15"
        assert invoice["invoice_type"] == "普票"
        assert invoice["direction"] == "销项"
        assert invoice["seller"] == "禄劝云睿网络科技开发有限公司"
        assert invoice["buyer"] == "云图科技有限公司"
        assert invoice["amount_exclusive"] == 20396.04
        assert invoice["tax_amount"] == 203.96
        assert invoice["total_inclusive"] == 20600.0
        assert invoice["status"] == "待匹配"
        assert invoice["source_format"] == "XML"
        assert invoice["source_file"] == "invoice_sales.xml"
        assert isinstance(invoice["raw_json"], dict) and invoice["raw_json"]

    def test_parse_purchase_xml_direction_and_type(self):
        """进项 XML：购买方为本企业 → 方向进项，含「专用发票」→ 专票。"""
        result = invoice_parser.parse_invoice_file(fixture_path("invoice_purchase.xml"))
        assert result["ok"] is True
        invoice = result["invoices"][0]
        assert invoice["direction"] == "进项"
        assert invoice["invoice_type"] == "专票"
        assert invoice["total_inclusive"] == 11300.0
        assert invoice["seller"] == "昆明锐捷办公设备有限公司"

    def test_parse_namespaced_chinese_xml(self):
        """带命名空间 + 中文标签的 XML 可正确解析。"""
        result = invoice_parser.parse_invoice_file(fixture_path("invoice_namespaced.xml"))
        assert result["ok"] is True
        invoice = result["invoices"][0]
        assert invoice["invoice_no"] == "26530100000011223344"
        assert invoice["invoice_date"] == "2026-03-01"
        assert invoice["direction"] == "销项"
        assert invoice["invoice_type"] == "普票"
        assert invoice["buyer"] == "昆明智联信息技术有限公司"
        assert invoice["amount_exclusive"] == 4950.50
        assert invoice["tax_amount"] == 49.51
        assert invoice["total_inclusive"] == 5000.01

    def test_parse_ofd(self):
        """OFD（zip 内 OriginalInvoice.xml）解析出同一张发票。"""
        result = invoice_parser.parse_invoice_file(fixture_path("invoice_sales.ofd"))
        assert result["ok"] is True
        assert result["format"] == "OFD"
        assert result["text"]
        invoice = result["invoices"][0]
        assert invoice["invoice_no"] == "26530100000012345678"
        assert invoice["source_format"] == "OFD"
        assert invoice["total_inclusive"] == 20600.0

    def test_parse_pdf_invalid_degrades(self):
        """损坏/纯图片 PDF 解析失败但不崩溃，提示改用 XML/OFD 或手动录入。"""
        result = invoice_parser.parse_invoice_file(b"%PDF-1.4 broken", "x.pdf")
        assert result["ok"] is False
        assert result["format"] == "PDF"
        assert result["invoices"] == []
        assert any("手动录入" in message for message in result["errors"])

    def test_parse_pdf_text_with_mock_reader(self, monkeypatch):
        """pypdf 可用时，PDF 文本经正则提取为发票记录。"""
        import pypdf

        sample_text = (
            "电子发票（普通发票）\n"
            "发票号码：26530100000099998888\n"
            "开票日期：2026-01-15\n"
            "销售方名称：禄劝云睿网络科技开发有限公司\n"
            "购买方名称：云图科技有限公司\n"
            "合计金额：20,396.04\n"
            "合计税额：203.96\n"
            "价税合计（小写）¥20,600.00\n"
        )

        class FakePage:
            def extract_text(self) -> str:
                return sample_text

        class FakeReader:
            def __init__(self, *args, **kwargs) -> None:
                self.pages = [FakePage()]

        monkeypatch.setattr(pypdf, "PdfReader", FakeReader)
        result = invoice_parser.parse_pdf_invoice(b"%PDF-1.4 fake")
        assert result["ok"] is True
        invoice = result["invoice"]
        assert invoice["invoice_no"] == "26530100000099998888"
        assert invoice["direction"] == "销项"
        assert invoice["total_inclusive"] == 20600.0
        assert invoice["source_format"] == "PDF"

    def test_unknown_extension(self):
        """未知扩展名返回 ok=False 与 UNKNOWN 格式，不抛异常。"""
        result = invoice_parser.parse_invoice_file(b"hello", "说明.docx")
        assert result["ok"] is False
        assert result["format"] == "UNKNOWN"
        assert any("不支持" in message for message in result["errors"])

    def test_missing_fields_no_crash(self):
        """字段缺失的 XML：ok=True 但 errors 提示缺失项，字段留空/0。"""
        xml = "<Invoice><发票号码>12345678</发票号码></Invoice>".encode("utf-8")
        result = invoice_parser.parse_xml_invoice(xml)
        assert result["ok"] is True
        invoice = result["invoice"]
        assert invoice["invoice_no"] == "12345678"
        assert invoice["total_inclusive"] == 0.0
        assert invoice["invoice_date"] == ""
        joined = "；".join(result["errors"])
        assert "开票日期" in joined and "价税合计" in joined

    def test_normalize_money_and_date(self):
        """金额去千分位/货币符号，日期支持中文写法，缺失价税合计自动补算。"""
        invoice = invoice_parser.normalize_invoice(
            {
                "invoice_no": "001",
                "invoice_date": "2026年1月15日",
                "amount_exclusive": "¥20,396.04",
                "tax_amount": "203.96",
                "seller": "禄劝云睿网络科技开发有限公司",
                "buyer": "云图科技有限公司",
            }
        )
        assert invoice["invoice_date"] == "2026-01-15"
        assert invoice["amount_exclusive"] == 20396.04
        assert invoice["total_inclusive"] == 20600.00
        assert invoice["direction"] == "销项"

    def test_normalize_amount_mismatch_note(self):
        """三项金额差额 >0.01 时保留原值并在 note 提示勾稽不一致。"""
        invoice = invoice_parser.normalize_invoice(
            {
                "invoice_no": "002",
                "invoice_date": "2026-01-15",
                "amount_exclusive": "100.00",
                "tax_amount": "13.00",
                "total_inclusive": "120.00",
            }
        )
        assert invoice["total_inclusive"] == 120.0
        assert "金额勾稽不一致" in invoice["note"]

    def test_dedupe_save_invoices(self):
        """同一发票保存两次只入库一次。"""
        invoice = invoice_parser.parse_invoice_file(fixture_path("invoice_sales.xml"))["invoices"]
        first = invoice_parser.save_invoices(invoice)
        second = invoice_parser.save_invoices(invoice)
        assert len(first["saved"]) == 1 and first["skipped"] == []
        assert second["saved"] == []
        assert len(second["skipped"]) == 1 and second["skipped"][0]["reason"] == "重复"
        assert table("invoices").count() == 1

    def test_dedupe_without_invoice_no_uses_hash(self):
        """无发票号码时按销售方+金额+日期哈希去重。"""
        record = {
            "invoice_no": "",
            "invoice_date": "2026-01-15",
            "seller": "某某供应商",
            "buyer": "禄劝云睿网络科技开发有限公司",
            "amount_exclusive": 100.0,
            "tax_amount": 1.0,
            "total_inclusive": 101.0,
            "direction": "进项",
            "invoice_type": "普票",
            "status": "待匹配",
            "source_format": "XML",
            "raw_json": {},
        }
        first = invoice_parser.save_invoices([record])
        second = invoice_parser.save_invoices([dict(record)])
        assert len(first["saved"]) == 1
        assert second["saved"] == [] and second["skipped"]

    def test_parse_invoice_bytes_and_file_object(self):
        """bytes 与文件对象两种来源均可解析。"""
        data = fixture_bytes("invoice_sales.xml")
        from_bytes = invoice_parser.parse_invoice_bytes(data, "invoice_sales.xml")
        assert from_bytes["ok"] is True and from_bytes["invoices"]
        from_file = invoice_parser.parse_invoice_file(FakeUpload("invoice_sales.xml", data))
        assert from_file["ok"] is True and from_file["invoices"]


# =============================================================================
# 2. 银行流水解析
# =============================================================================
class TestBankParsing:
    def test_detect_columns(self):
        """列别名识别：招行风格表头映射到标准列。"""
        headers = ["交易日期", "交易时间", "收入", "支出", "余额", "摘要", "对方户名", "交易流水号"]
        columns = bank_parser.detect_columns(headers)
        assert columns["date"] == "交易日期"
        assert columns["income"] == "收入"
        assert columns["expense"] == "支出"
        assert columns["balance"] == "余额"
        assert columns["summary"] == "摘要"
        assert columns["counterparty"] == "对方户名"
        assert columns["transaction_no"] == "交易流水号"
        assert columns["amount"] == ""

    def test_parse_split_columns_csv(self):
        """收入/支出分列 CSV：方向与金额正确。"""
        result = bank_parser.parse_bank_file(fixture_path("bank_cmb.csv"))
        assert result["ok"] is True and result["format"] == "CSV"
        assert len(result["transactions"]) == 3
        first = result["transactions"][0]
        assert first["transaction_date"] == "2026-01-15"
        assert first["direction"] == "收入"
        assert first["amount"] == 20600.0
        assert first["counterparty"] == "云图科技有限公司"
        assert first["transaction_no"] == "CMB20260115001"
        assert first["balance"] == 120600.0
        assert first["status"] == "待匹配"
        assert result["transactions"][1]["direction"] == "支出"

    def test_parse_gbk_csv(self):
        """GBK 编码 CSV 自动识别，编码提示正确。"""
        result = bank_parser.parse_bank_file(fixture_path("bank_gbk.csv"))
        assert result["ok"] is True
        assert len(result["transactions"]) == 2
        assert result["transactions"][0]["amount"] == 20600.0
        assert result["encoding"] in ("gbk", "gb18030")

    def test_parse_single_amount_with_direction(self):
        """单金额列 + 收支方向列：按方向文本判定。"""
        result = bank_parser.parse_bank_file(fixture_path("bank_generic.csv"))
        assert result["ok"] is True
        directions = [row["direction"] for row in result["transactions"]]
        assert directions == ["收入", "支出", "收入"]
        assert result["transactions"][1]["amount"] == 11300.0

    def test_parse_signed_amount(self):
        """单金额列无方向列：按正负判断（负 → 支出），金额取绝对值，日期多格式归一。"""
        result = bank_parser.parse_bank_file(fixture_path("bank_signed.csv"))
        assert result["ok"] is True
        assert len(result["transactions"]) == 3
        first, second = result["transactions"][0], result["transactions"][1]
        assert first["direction"] == "收入" and first["amount"] == 20600.0
        assert second["direction"] == "支出" and second["amount"] == 11300.0
        assert first["transaction_date"] == "2026-01-15"

    def test_parse_xlsx(self):
        """XLSX 流水解析（openpyxl），金额与流水号正确。"""
        result = bank_parser.parse_bank_file(fixture_path("bank_cmb.xlsx"))
        assert result["ok"] is True and result["format"] == "XLSX"
        assert len(result["transactions"]) == 2
        assert result["transactions"][0]["transaction_no"] == "XLSX001"
        assert result["transactions"][0]["amount"] == 20600.0
        assert result["transactions"][1]["direction"] == "支出"

    def test_header_after_title_rows(self):
        """表头前有 1—3 行说明文字时仍能找到表头。"""
        text = (
            "某某银行账户流水明细\n"
            "导出时间：2026-01-15\n"
            "交易日期,收入,支出,摘要,对方户名,交易流水号\n"
            "2026-01-15,20600.00,,技术服务费,云图科技有限公司,T002\n"
        )
        result = bank_parser.parse_bank_csv(text.encode("utf-8"))
        assert result["ok"] is True
        assert len(result["transactions"]) == 1
        assert result["transactions"][0]["transaction_no"] == "T002"

    def test_custom_mapping(self):
        """自动识别失败时允许调用方传入自定义列映射。"""
        text = (
            "日期X,金额X,方向X,对方X,摘要X,流水号X\n"
            "2026-01-15,+20600.00,收入,云图科技有限公司,技术服务费,T100\n"
        )
        mapping = {
            "date": "日期X",
            "amount": "金额X",
            "direction": "方向X",
            "counterparty": "对方X",
            "summary": "摘要X",
            "transaction_no": "流水号X",
        }
        result = bank_parser.parse_bank_csv(text.encode("utf-8"), mapping)
        assert result["ok"] is True
        transaction = result["transactions"][0]
        assert transaction["direction"] == "收入"
        assert transaction["amount"] == 20600.0
        assert transaction["transaction_no"] == "T100"

    def test_skip_empty_direction_row(self):
        """收入与支出列均为空的行跳过并记录错误；其余行正常解析。"""
        text = (
            "交易日期,收入,支出,摘要,对方户名,交易流水号\n"
            "2026-01-15,,,无法判断,某公司,T003\n"
            "2026-01-16,20600.00,,正常,云图科技有限公司,T004\n"
        )
        result = bank_parser.parse_bank_csv(text.encode("utf-8"))
        assert result["ok"] is True
        assert len(result["transactions"]) == 1
        assert any("第 2 行" in message and "均为空" in message for message in result["errors"])

    def test_zero_amount_and_missing_date_skipped(self):
        """金额为 0、缺日期的行跳过并记入 errors。"""
        text = (
            "交易日期,交易金额,收支方向,对方户名,交易流水号\n"
            ",100.00,收入,某公司,T005\n"
            "2026-01-16,0.00,收入,某公司,T006\n"
            "2026-01-17,500.00,收入,云图科技有限公司,T007\n"
        )
        result = bank_parser.parse_bank_csv(text.encode("utf-8"))
        assert result["ok"] is True
        assert len(result["transactions"]) == 1
        joined = "；".join(result["errors"])
        assert "交易日期" in joined and "为 0" in joined

    def test_dedupe_save_transactions(self):
        """同一批流水保存两次，第二次全部按内容/流水号去重。"""
        parsed = bank_parser.parse_bank_file(fixture_path("bank_cmb.csv"))["transactions"]
        first = bank_parser.save_transactions(parsed)
        second = bank_parser.save_transactions(parsed)
        assert len(first["saved"]) == 3
        assert second["saved"] == [] and len(second["skipped"]) == 3
        assert table("bank_transactions").count() == 3

    def test_save_without_transaction_no_hash(self):
        """无流水号时按日期+方向+金额+对方+摘要哈希去重。"""
        record = {
            "transaction_date": "2026-01-15",
            "direction": "收入",
            "amount": 100.0,
            "counterparty": "某客户",
            "summary": "服务费",
            "transaction_no": "",
            "status": "待匹配",
            "source_format": "CSV",
        }
        first = bank_parser.save_transactions([record])
        second = bank_parser.save_transactions([dict(record)])
        assert len(first["saved"]) == 1
        assert second["saved"] == [] and second["skipped"]


# =============================================================================
# 3. 匹配评分
# =============================================================================
class TestMatching:
    def test_score_perfect_pair(self):
        """金额一致 + 同日 + 名称一致 → 100 分且可匹配。"""
        result = matcher.score_pair(_invoice(), _transaction())
        assert result["eligible"] is True
        assert result["score"] == 100.0
        assert result["amount_diff"] == 0.0
        assert result["date_diff"] == 0
        assert result["name_similarity"] == 1.0
        assert any("方向一致" in reason for reason in result["reasons"])

    def test_direction_mismatch_not_eligible(self):
        """方向不符（销项 ↔ 支出）直接不可匹配。"""
        result = matcher.score_pair(_invoice(), _transaction(direction="支出"))
        assert result["eligible"] is False
        assert result["score"] == 0.0
        assert any("方向不符" in reason for reason in result["reasons"])

    def test_amount_within_5_percent_lowers_score(self):
        """金额差 5% 以内可匹配但金额分下降（8+30+25=63）。"""
        invoice = _invoice(total_inclusive=2000.0)
        transaction = _transaction(amount=1900.0)
        result = matcher.score_pair(invoice, transaction)
        assert result["eligible"] is True
        assert result["score"] == 63.0
        assert result["amount_diff"] == 100.0

    def test_amount_over_5_percent_not_eligible(self):
        """金额差超过 5% 不可匹配。"""
        invoice = _invoice(total_inclusive=2000.0)
        transaction = _transaction(amount=1800.0)
        result = matcher.score_pair(invoice, transaction)
        assert result["eligible"] is False
        assert any("超过 5%" in reason for reason in result["reasons"])

    def test_date_window(self):
        """日期相差 5 天 → 日期分 18；超过窗口 → 不可匹配。"""
        matched = matcher.score_pair(_invoice(), _transaction(transaction_date="2026-01-20"))
        assert matched["eligible"] is True
        assert matched["date_diff"] == 5
        assert matched["score"] == 88.0

        outside = matcher.score_pair(_invoice(), _transaction(transaction_date="2026-02-20"))
        assert outside["eligible"] is False
        assert any("超过匹配窗口" in reason for reason in outside["reasons"])

    def test_name_similarity_affects_score(self):
        """名称完全不相似时仍可凭金额+日期匹配，但得分中不含名称分。"""
        result = matcher.score_pair(_invoice(), _transaction(counterparty="某某贸易有限公司"))
        assert result["eligible"] is True
        assert result["score"] == 75.0
        assert result["name_similarity"] < 0.5

    def test_suggest_greedy_one_to_one(self):
        """贪心一对一：两张发票不会匹配到同一笔流水。"""
        invoice_template = invoice_parser.parse_invoice_file(fixture_path("invoice_sales.xml"))["invoices"][0]
        first = dict(invoice_template, invoice_no="NO-A")
        second = dict(invoice_template, invoice_no="NO-B")
        invoice_parser.save_invoices([first, second])
        bank_parser.save_transactions(bank_parser.parse_bank_file(fixture_path("bank_cmb.csv"))["transactions"])
        result = matcher.suggest_matches()
        assert result["count"] == 1
        assert result["candidates"] == 2
        matches = matcher.list_matches("建议")
        assert len(matches) == 1
        matched_invoice_ids = {match["invoice_id"] for match in matches}
        assert len(matched_invoice_ids) == 1
        # 生成建议不改变票据状态；两张发票中只有一张被建议匹配
        pending = [row for row in table("invoices").read() if str(row.get("status")) == "待匹配"]
        assert len(pending) == 2

    def test_suggest_is_idempotent(self):
        """重复生成建议不会产生重复记录。"""
        _save_invoice_pair()
        before = len(matcher.list_matches())
        again = matcher.suggest_matches()
        assert again["count"] == 0
        assert again["candidates"] == 0
        assert len(matcher.list_matches()) == before

    def test_list_matches_contains_summaries(self):
        """建议列表包含发票与流水摘要字段。"""
        _save_invoice_pair()
        matches = matcher.list_matches()
        assert matches
        match = matches[0]
        assert match["invoice_buyer"] == "云图科技有限公司"
        assert match["invoice_total"] == 20600.0
        assert match["transaction_amount"] == 20600.0
        assert match["transaction_counterparty"] == "云图科技有限公司"
        assert match["match_type"] == "发票-流水"
        assert match["status"] == "建议"
        assert 0 <= int(match["score"]) <= 100


# =============================================================================
# 4. 确认入账 / 忽略
# =============================================================================
class TestConfirmAndJournal:
    def test_confirm_income_creates_ledger_and_journal(self):
        """确认销项匹配：生成收入台账 + 借贷平衡凭证，状态变已入账，写审计日志。"""
        result = _save_invoice_pair()
        match = result["created"][0]
        confirmed = matcher.confirm_match(match["id"])
        assert confirmed["ok"] is True
        assert confirmed["ledger"]["kind"] == "income"
        record = confirmed["ledger"]["record"]
        assert record["client"] == "云图科技有限公司"
        assert record["inclusive_amount"] == 20600.0
        assert record["exclusive_amount"] == 20396.04
        assert record["invoice_type"] == "普票"
        assert record["invoice_no"] == "26530100000012345678"
        assert record["note"] == "票据流水匹配入账"

        posted = confirmed["journal"]
        assert posted is not None and posted["entry_no"].startswith("记-202601-")
        debit = sum(line["amount"] for line in posted["lines"] if line["direction"] == "借")
        credit = sum(line["amount"] for line in posted["lines"] if line["direction"] == "贷")
        assert round(debit, 2) == round(credit, 2) == 20600.0
        balance = journal.trial_balance()
        assert balance["balanced"] is True

        assert table("invoices").get(match["invoice_id"])["status"] == "已入账"
        assert table("bank_transactions").get(match["transaction_id"])["status"] == "已入账"
        assert confirmed["match"]["status"] == "已确认"
        assert confirmed["match"]["confirmed_at"]
        actions = [row["action"] for row in read_log("user_action_log")]
        assert "票据匹配入账" in actions
        assert income_ledger.summarize_income()["count"] == 1

    def test_confirm_expense_creates_expense_ledger(self):
        """确认进项匹配：生成费用台账（vendor/seller、receipt_no、办公类别）与凭证。"""
        invoices = invoice_parser.parse_invoice_file(fixture_path("invoice_purchase.xml"))["invoices"]
        invoice_parser.save_invoices(invoices)
        bank_parser.save_transactions(bank_parser.parse_bank_file(fixture_path("bank_cmb.csv"))["transactions"])
        result = matcher.suggest_matches()
        assert result["count"] == 1
        confirmed = matcher.confirm_match(result["created"][0]["id"])
        assert confirmed["ledger"]["kind"] == "expense"
        record = confirmed["ledger"]["record"]
        assert record["vendor"] == "昆明锐捷办公设备有限公司"
        assert record["receipt_no"] == "26530100000087654321"
        assert record["category"] == "办公"
        assert record["amount"] == 11300.0
        assert record["note"] == "进项发票入账（小规模不抵扣）"
        assert confirmed["journal"] is not None
        assert expense_ledger.summarize_expenses()["count"] == 1

    def test_confirm_rejects_duplicate(self):
        """重复确认同一匹配抛 ValueError（幂等保护）。"""
        result = _save_invoice_pair()
        match_id = result["created"][0]["id"]
        matcher.confirm_match(match_id)
        with pytest.raises(ValueError, match="仅「建议」状态"):
            matcher.confirm_match(match_id)

    def test_confirm_amount_diff_warning(self):
        """发票与流水金额不一致时给出 warnings，仍按发票金额入账。"""
        invoice = invoice_parser.parse_invoice_file(fixture_path("invoice_sales.xml"))
        invoice_parser.save_invoices(invoice["invoices"])
        wrong = bank_parser.parse_bank_file(fixture_path("bank_wrong_amount.csv"))
        bank_parser.save_transactions(wrong["transactions"])
        result = matcher.suggest_matches()
        assert result["count"] == 1
        confirmed = matcher.confirm_match(result["created"][0]["id"])
        assert confirmed["ledger"]["record"]["inclusive_amount"] == 20600.0
        assert confirmed["warnings"]
        assert "相差" in confirmed["warnings"][0]

    def test_confirm_without_journal(self):
        """post_journal=False 时只生成台账，不登记凭证。"""
        result = _save_invoice_pair()
        confirmed = matcher.confirm_match(result["created"][0]["id"], post_journal=False)
        assert confirmed["journal"] is None
        assert table("journal_entries").count() == 0
        assert income_ledger.summarize_income()["count"] == 1

    def test_ignore_match_keeps_status(self):
        """忽略匹配：只改匹配状态，不动发票/流水状态与台账。"""
        result = _save_invoice_pair()
        match = result["created"][0]
        ignored = matcher.ignore_match(match["id"])
        assert ignored["ok"] is True
        assert ignored["match"]["status"] == "已忽略"
        assert table("invoices").get(match["invoice_id"])["status"] == "待匹配"
        assert table("bank_transactions").get(match["transaction_id"])["status"] == "待匹配"
        assert income_ledger.summarize_income()["count"] == 0
        assert "忽略票据匹配" in [row["action"] for row in read_log("user_action_log")]
        with pytest.raises(ValueError):
            matcher.ignore_match(match["id"])

    def test_confirm_missing_match(self):
        """匹配编号不存在时抛 KeyError。"""
        with pytest.raises(KeyError):
            matcher.confirm_match("MAT999999")

    def test_match_summary_counts(self):
        """确认后 match_summary 数量与金额正确。"""
        result = _save_invoice_pair()
        matcher.confirm_match(result["created"][0]["id"])
        summary = matcher.match_summary()
        assert summary["confirmed"] == 1
        assert summary["invoices_matched"] == 1
        assert summary["transactions_matched"] == 1
        assert summary["matched_income"] == 1
        assert summary["matched_income_amount"] == 20600.0
        assert summary["suggestions"] == 0
        assert summary["pool_total"] == {"invoices": 1, "transactions": 3, "matches": 1}


# =============================================================================
# 5. 编排 / 手动录入 / 池管理 / 演示数据
# =============================================================================
class TestImporter:
    def test_import_files_dispatch(self):
        """按扩展名分发导入：1 张发票 + 3 笔流水，自动匹配 1 条建议。"""
        files = [
            ("invoice_sales.xml", fixture_bytes("invoice_sales.xml")),
            ("bank_cmb.csv", fixture_bytes("bank_cmb.csv")),
        ]
        result = importer.import_files(files)
        assert result["parsed"] == 4
        assert result["saved"] == 4
        assert result["skipped"] == []
        assert result["skipped_count"] == 0
        assert result["invoices"] == 1
        assert result["transactions"] == 3
        assert result["errors"] == []
        assert result["by_format"] == {"XML": 1, "CSV": 1}
        assert result["match"]["count"] == 1
        assert table("invoices").count() == 1
        assert table("bank_transactions").count() == 3

    def test_import_files_dedupe_second_time(self):
        """重复导入同一批文件不产生重复记录。"""
        files = [
            ("invoice_sales.xml", fixture_bytes("invoice_sales.xml")),
            ("bank_cmb.csv", fixture_bytes("bank_cmb.csv")),
        ]
        importer.import_files(files)
        second = importer.import_files(files)
        assert second["saved"] == 0
        assert len(second["skipped"]) == 4
        assert second["skipped_count"] == 4
        assert second["invoices"] == 0
        assert second["transactions"] == 0
        assert second["match"]["count"] == 0
        assert table("invoices").count() == 1
        assert table("bank_transactions").count() == 3

    def test_import_with_upload_objects(self):
        """支持 Streamlit 上传对象（.name + .getvalue()）。"""
        result = importer.import_invoice_files([FakeUpload("invoice_sales.xml", fixture_bytes("invoice_sales.xml"))])
        assert result["saved"] == 1
        assert result["saved_records"][0]["invoice_no"] == "26530100000012345678"

    def test_import_bank_files_mapping_and_account(self):
        """导入流水时支持自定义 mapping 与账号回填。"""
        text = (
            "日期X,金额X,方向X,对方X,摘要X,流水号X\n"
            "2026-01-15,+20600.00,收入,云图科技有限公司,技术服务费,T100\n"
        ).encode("utf-8")
        mapping = {
            "date": "日期X",
            "amount": "金额X",
            "direction": "方向X",
            "counterparty": "对方X",
            "summary": "摘要X",
            "transaction_no": "流水号X",
        }
        result = importer.import_bank_files(
            [("custom.csv", text)], mapping=mapping, account_no="6222000011112222"
        )
        assert result["saved"] == 1
        assert result["saved_records"][0]["account_no"] == "6222000011112222"
        assert result["saved_records"][0]["direction"] == "收入"

    def test_import_parse_error_recorded(self):
        """解析失败的文件进入 errors，不中断其他文件。"""
        result = importer.import_files(
            [
                ("broken.xml", b"<Invoice><bad>"),
                ("invoice_sales.xml", fixture_bytes("invoice_sales.xml")),
                ("说明.docx", b"unknown"),
            ],
            auto_match=False,
        )
        assert result["saved"] == 1
        assert len(result["errors"]) == 2
        assert any(error["file"] == "说明.docx" for error in result["errors"])

    def test_add_manual_invoice(self):
        """手动录入发票入库并可自动匹配；重复录入抛 ValueError。"""
        record = importer.add_manual_invoice(
            {
                "invoice_no": "MANUAL001",
                "invoice_date": "2026-01-15",
                "total_inclusive": 20600.0,
                "invoice_type": "普通发票",
                "direction": "销项",
                "seller": "禄劝云睿网络科技开发有限公司",
                "buyer": "云图科技有限公司",
            }
        )
        assert record["invoice_no"] == "MANUAL001"
        assert record["source_format"] == "手动"
        assert record["status"] == "待匹配"
        assert record["total_inclusive"] == 20600.0
        with pytest.raises(ValueError, match="已存在"):
            importer.add_manual_invoice(
                {
                    "invoice_no": "MANUAL001",
                    "invoice_date": "2026-01-15",
                    "total_inclusive": 20600.0,
                }
            )
        with pytest.raises(ValueError):
            importer.add_manual_invoice({"note": "缺少号码和金额"})

    def test_pool_summary(self):
        """池汇总：数量、状态、方向、金额与最近导入时间。"""
        demo.seed_demo_pool(reset=True)
        summary = importer.pool_summary()
        assert summary["invoices"]["count"] == 2
        assert summary["invoices"]["by_direction"] == {"销项": 1, "进项": 1}
        assert summary["invoices"]["by_type"]["专票"] == 1
        assert summary["invoices"]["by_type"]["普票"] == 1
        assert summary["invoices"]["amount"] == 31900.0
        assert summary["transactions"]["count"] == 3
        assert summary["transactions"]["by_direction"] == {"收入": 2, "支出": 1}
        assert summary["transactions"]["amount"] == 61900.0
        assert summary["matches"]["count"] == 2
        assert summary["matches"]["by_status"]["建议"] == 2
        assert summary["last_import_at"]

    def test_reset_pool(self):
        """reset_pool 清空三张池表并写审计日志。"""
        demo.seed_demo_pool(reset=True)
        cleared = importer.reset_pool()
        assert cleared["ok"] is True
        assert cleared["cleared"] == {"invoices": 2, "transactions": 3, "matches": 2}
        assert table("invoices").count() == 0
        assert table("bank_transactions").count() == 0
        assert table("matches").count() == 0
        assert "清空票据流水池" in [row["action"] for row in read_log("user_action_log")]

    def test_list_update_delete_pool_records(self):
        """池记录查询、状态修改与删除（删除发票同时清理关联建议）。"""
        result = _save_invoice_pair()
        invoices = importer.list_pool_records("invoices")
        assert len(invoices) == 1
        updated = importer.update_pool_status("invoices", invoices[0]["id"], "已忽略")
        assert updated["status"] == "已忽略"
        with pytest.raises(ValueError):
            importer.update_pool_status("invoices", invoices[0]["id"], "非法状态")
        match_id = result["created"][0]["id"]
        assert table("matches").get(match_id) is not None
        assert importer.delete_pool_record("invoices", invoices[0]["id"]) is True
        assert table("matches").get(match_id) is None
        with pytest.raises(ValueError):
            importer.list_pool_records("unknown")


class TestDemo:
    def test_seed_demo_pool(self):
        """加载 examples/ 下 2 张发票 + 1 个流水，自动生成 2 条建议。"""
        result = demo.seed_demo_pool()
        assert result["invoices"] == 2
        assert result["transactions"] == 3
        assert result["suggestions"] == 2
        assert len(result["files"]) == 3
        assert result["pool"]["invoices"]["count"] == 2
        assert result["pool"]["transactions"]["count"] == 3
        assert result["pool"]["matches"]["count"] == 2
        actions = [row["action"] for row in read_log("user_action_log")]
        assert "加载示例票据流水" in actions
        matches = matcher.list_matches("建议")
        assert {match["invoice_no"] for match in matches} == {
            "26530100000012345678",
            "26530100000087654321",
        }

    def test_seed_demo_pool_is_idempotent(self):
        """重复加载示例不产生重复数据与重复建议。"""
        demo.seed_demo_pool(reset=True)
        second = demo.seed_demo_pool()
        assert second["invoices"] == 0
        assert second["transactions"] == 0
        assert second["suggestions"] == 0
        assert second["pool"]["invoices"]["count"] == 2
        assert second["pool"]["matches"]["count"] == 2

    def test_seed_demo_pool_reset(self):
        """reset=True 先清空池再导入。"""
        demo.seed_demo_pool(reset=True)
        matcher.confirm_match(matcher.list_matches()[0]["id"])
        after_reset = demo.seed_demo_pool(reset=True)
        assert after_reset["invoices"] == 2
        assert after_reset["suggestions"] == 2
        assert income_ledger.summarize_income()["count"] == 1  # 台账不受池清空影响
