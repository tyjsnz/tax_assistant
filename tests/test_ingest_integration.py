"""P0-2 端到端集成测试：示例票据/流水 → 自动匹配 → 确认入账 → 台账/凭证/池状态。

后端单元测试见 ``tests/test_ingest.py``；本文件只验证跨模块闭环（Lead 维护）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import config
from ledger import income_ledger, journal, store
from ledger import expense_ledger

FIXTURES = Path(__file__).parent / "fixtures" / "ingest"


def _fixture_files() -> list[tuple[str, bytes]]:
    return [
        ("invoice_sales.xml", (FIXTURES / "invoice_sales.xml").read_bytes()),
        ("invoice_purchase.xml", (FIXTURES / "invoice_purchase.xml").read_bytes()),
        ("bank_cmb.csv", (FIXTURES / "bank_cmb.csv").read_bytes()),
    ]


class TestIngestPipeline:
    def test_import_and_match(self):
        from ingest import importer, matcher

        importer.reset_pool()
        result = importer.import_files(_fixture_files(), auto_match=True)
        assert result["invoices"] == 2, result
        assert result["transactions"] == 3, result

        matches = matcher.list_matches()
        suggestions = [match for match in matches if match["status"] == "建议"]
        assert suggestions, "应生成发票-流水匹配建议"
        # 销项 20,600 与收入流水金额一致，应得高分
        top = max(suggestions, key=lambda match: float(match["score"]))
        assert float(top["score"]) >= 60
        assert top["invoice_no"] and top["transaction_no"]

        summary = matcher.match_summary()
        assert summary["invoices_pending"] >= 0
        assert summary["suggestions"] >= 1

    def test_confirm_match_writes_ledger_and_journal(self):
        from ingest import importer, matcher

        importer.reset_pool()
        importer.import_files(_fixture_files(), auto_match=True)
        suggestions = [match for match in matcher.list_matches() if match["status"] == "建议"]
        target = next(
            (match for match in suggestions if match.get("invoice_no") == "26530100000012345678"),
            suggestions[0],
        )
        outcome = matcher.confirm_match(target["id"])
        assert outcome["ok"] is True
        assert outcome["ledger"]["kind"] in {"income", "expense"}
        record = outcome["ledger"]["record"]
        assert record["id"]

        # 台账确实写入
        assert income_ledger.list_income() or expense_ledger.list_expenses()
        # 凭证借贷平衡
        balance = journal.trial_balance()
        assert balance["balanced"] is True
        # 状态流转
        assert outcome["invoice_status"] == "已入账"
        assert outcome["transaction_status"] == "已入账"
        invoice = store.table("invoices").get(target["invoice_id"]) or {}
        assert invoice.get("status") == "已入账"
        # 幂等：重复确认报错
        with pytest.raises(ValueError):
            matcher.confirm_match(target["id"])

    def test_ignore_does_not_touch_ledger(self):
        from ingest import importer, matcher

        importer.reset_pool()
        importer.import_files(_fixture_files(), auto_match=True)
        suggestions = [match for match in matcher.list_matches() if match["status"] == "建议"]
        assert suggestions
        before = store.table("income").count() + store.table("expense").count()
        matcher.ignore_match(suggestions[0]["id"])
        after = store.table("income").count() + store.table("expense").count()
        assert after == before
        assert any(match["status"] == "已忽略" for match in matcher.list_matches())


class TestIngestCommandLine:
    def test_cli_ingest_and_pool(self, capsys):
        import main

        assert (
            main.main(
                [
                    "ingest",
                    "--invoices",
                    str(FIXTURES / "invoice_sales.xml"),
                    str(FIXTURES / "invoice_purchase.xml"),
                    "--bank",
                    str(FIXTURES / "bank_cmb.csv"),
                ]
            )
            == 0
        )
        output = capsys.readouterr().out
        assert "导入完成" in output
        assert "匹配建议" in output

        assert main.main(["pool"]) == 0
        pool_output = capsys.readouterr().out
        assert "票据与流水池" in pool_output


class TestDemoPool:
    def test_seed_demo_pool(self):
        from ingest import demo, importer, matcher

        result = demo.seed_demo_pool(reset=True)
        assert result["invoices"] == 2
        assert result["transactions"] == 3
        assert result["suggestions"] >= 1
        pool = importer.pool_summary()
        assert pool["invoices"]["count"] == 2
        assert matcher.match_summary()["suggestions"] >= 1
        # 示例文件来自项目 examples/ 目录，路径必须可移植
        assert (config.BASE_DIR / "examples" / "invoices").exists()
