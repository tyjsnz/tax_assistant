"""发票待办：直接入账与忽略状态测试（v1.41）。"""

from __future__ import annotations

from ingest import invoice_tasks, matcher
from ledger import store


def test_post_invoice_direct_income():
    invoice = store.table("invoices").append({
        "invoice_no": "TASKPOST001", "invoice_date": "2026-05-01", "direction": "销项",
        "seller": "本公司", "buyer": "客户A", "total_inclusive": 500, "invoice_type": "普票", "status": "待匹配",
    })
    result = matcher.post_invoice_direct(str(invoice["id"]))
    assert result["ok"] is True
    assert result["ledger"]["kind"] == "收入"
    assert store.table("invoices").get(str(invoice["id"]))["status"] == "已入账"
    assert any(row.get("invoice_no") == "TASKPOST001" for row in store.table("income").read())


def test_ignore_and_restore():
    invoice_tasks.ignore("INV|TEST", note="冲突")
    assert "INV|TEST" in invoice_tasks.ignored_keys()
    invoice_tasks.restore("INV|TEST")
    assert "INV|TEST" not in invoice_tasks.ignored_keys()
