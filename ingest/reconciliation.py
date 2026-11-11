"""发票对账看板（v1.26）：归集/待匹配/重复/疑似红冲作废/跨期。"""

from __future__ import annotations

from datetime import date
from typing import Any

from ledger import store

INVOICE_TABLE = "invoices"
MATCH_TABLE = "matches"
LOG_TABLE = "collection_log"


def _month(value: Any) -> str:
    return str(value or "")[:7]


def build_dashboard(today: date | None = None) -> dict[str, Any]:
    now = today or date.today()
    current_month = now.strftime("%Y-%m")
    current_quarter_start = ((now.month - 1) // 3) * 3 + 1
    quarter_start = date(now.year, current_quarter_start, 1)

    invoices = store.table(INVOICE_TABLE).read()
    matches = store.table(MATCH_TABLE).read()
    logs = store.table(LOG_TABLE).read()

    total = len(invoices)
    this_month = [row for row in invoices if _month(row.get("invoice_date")) == current_month]
    pending = [row for row in invoices if str(row.get("status") or "") == "待匹配"]
    matched = [row for row in invoices if str(row.get("status") or "") in ("已匹配", "已入账")]

    # 重复发票号（相同号码出现多次）
    seen: dict[str, int] = {}
    for row in invoices:
        number = str(row.get("invoice_no") or "").strip()
        if number:
            seen[number] = seen.get(number, 0) + 1
    duplicate_numbers = [{"invoice_no": number, "count": count} for number, count in seen.items() if count > 1]

    # 疑似红冲/作废：发票号码/备注/原始 JSON 中出现关键词，或金额为负
    suspected = []
    for row in invoices:
        text = " ".join(
            str(row.get(key) or "")
            for key in ("invoice_type", "note", "raw_json", "full_text")
        )
        amount = 0.0
        try:
            amount = float(row.get("total_inclusive") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        if any(word in text for word in ("红冲", "作废", "冲红", "负数发票")) or amount < 0:
            suspected.append(
                {"invoice_no": row.get("invoice_no"), "seller": row.get("seller"), "amount": amount, "note": str(row.get("note") or "")[:80]}
            )

    # 跨期发票：开票日期早于本季度开始（可按需调整）
    cross_period = []
    for row in invoices:
        try:
            invoice_date = date.fromisoformat(str(row.get("invoice_date") or "")[:10])
        except ValueError:
            continue
        if invoice_date < quarter_start:
            cross_period.append(
                {"invoice_no": row.get("invoice_no"), "invoice_date": row.get("invoice_date"), "seller": row.get("seller"), "amount": row.get("total_inclusive")}
            )

    failed_logs = [row for row in logs if str(row.get("status") or "") == "失败"]
    skipped_logs = [row for row in logs if str(row.get("status") or "") == "跳过"]

    return {
        "total": total,
        "this_month": len(this_month),
        "pending": len(pending),
        "matched": len(matched),
        "duplicate_numbers": duplicate_numbers[:20],
        "suspected_red": suspected[:20],
        "cross_period": cross_period[:20],
        "failed_logs": failed_logs[:20],
        "skipped_logs": len(skipped_logs),
        "collection_logs": len(logs),
        "pending_purchase": sum(1 for row in pending if str(row.get("direction") or "") == "进项"),
        "pending_sales": sum(1 for row in pending if str(row.get("direction") or "") == "销项"),
        "current_month": current_month,
        "quarter_start": quarter_start.isoformat(),
    }


__all__ = ["build_dashboard"]
