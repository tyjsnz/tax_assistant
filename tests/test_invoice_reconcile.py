"""发票查重/入账核对测试（v1.39）。"""

from __future__ import annotations

from ingest import invoice_reconcile
from ledger import store


def test_reconcile_marks_duplicate_and_posted():
    inv = store.table("invoices")
    inv.append({"invoice_no": "RECON001", "invoice_code": "CODE1", "invoice_date": "2026-03-01", "direction": "销项", "seller": "测试公司", "buyer": "客户", "total_inclusive": 100, "status": "待匹配"})
    inv.append({"invoice_no": "RECON001", "invoice_code": "CODE1", "invoice_date": "2026-03-01", "direction": "销项", "seller": "测试公司", "buyer": "客户", "total_inclusive": 100, "status": "待匹配"})
    store.table("income").append({"date": "2026-03-01", "client": "客户", "invoice_no": "RECON002", "inclusive_amount": 50})
    inv.append({"invoice_no": "RECON002", "invoice_date": "2026-03-02", "direction": "销项", "total_inclusive": 50, "status": "待匹配"})
    result = invoice_reconcile.reconcile()
    rows = {row["invoice_no"]: row for row in result["rows"]}
    assert rows["RECON001"]["verdict"] in {"重复录入", "信息冲突"}
    assert rows["RECON002"]["verdict"] == "已入账"
    assert result["summary"]["duplicate"] >= 2


def test_check_one():
    store.table("invoices").append({"invoice_no": "CHECK001", "invoice_date": "2026-03-03", "direction": "销项", "total_inclusive": 10, "status": "待匹配"})
    result = invoice_reconcile.check_one("CHECK001")
    assert result["rows"] and result["rows"][0]["posted"] is False


def test_build_tasks_dedupes_one_per_invoice():
    inv = store.table("invoices")
    inv.append({"invoice_no": "TASK001", "invoice_date": "2026-04-01", "direction": "销项", "total_inclusive": 200, "status": "待匹配"})
    inv.append({"invoice_no": "TASK001", "invoice_date": "2026-04-01", "direction": "销项", "total_inclusive": 200, "status": "待匹配"})
    inv.append({"invoice_no": "TASK002", "invoice_date": "2026-04-02", "direction": "进项", "total_inclusive": 50, "status": "已入账"})
    tasks = invoice_reconcile.build_tasks()
    nos = [task["invoice_no"] for task in tasks]
    assert nos.count("TASK001") == 1
    assert "TASK002" not in nos
    data = invoice_reconcile.export_tasks_csv(tasks)
    assert "发票号码" in data.decode("utf-8-sig")
