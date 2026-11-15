"""发票查重与入账核对（v1.39）。

回答两个问题：
1. 这张发票之前是不是已经做账了？
2. 补录/导入时是不是重复了？

判定依据：
* 发票池状态「已入账」；
* 收入台账按发票号码匹配；
* 费用台账按凭证号/备注匹配；
* 发票池内按「发票代码 + 发票号码」分组，识别重复与信息冲突。
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any

from ledger import store

INVOICE_TABLE = "invoices"


def _text(value: Any) -> str:
    return str(value or "").strip()


def invoice_identity(row: dict[str, Any]) -> str:
    """发票身份键：优先发票号码；有代码时带上代码。"""
    no = _text(row.get("invoice_no"))
    code = _text(row.get("invoice_code"))
    if no:
        return f"{code}|{no}" if code else no
    digest = hashlib.sha256(
        "|".join([_text(row.get("seller")), _text(row.get("buyer")), _text(row.get("invoice_date")), _text(row.get("total_inclusive"))]).encode()
    ).hexdigest()[:16]
    return f"HASH|{digest}"


def _ledger_matches(invoice: dict[str, Any]) -> list[dict[str, Any]]:
    no = _text(invoice.get("invoice_no"))
    if not no:
        return []
    direction = _text(invoice.get("direction"))
    matches: list[dict[str, Any]] = []
    if direction != "进项":
        for row in store.table("income").read():
            if _text(row.get("invoice_no")) == no:
                matches.append({"kind": "收入", "id": row.get("id"), "date": row.get("date"), "amount": row.get("inclusive_amount"), "client": row.get("client")})
    if direction != "销项":
        for row in store.table("expense").read():
            if _text(row.get("receipt_no")) == no or no in _text(row.get("note")):
                matches.append({"kind": "费用", "id": row.get("id"), "date": row.get("date"), "amount": row.get("amount"), "client": row.get("vendor")})
    return matches


def reconcile() -> dict[str, Any]:
    """全量核对发票池与台账，返回逐张结果与汇总。"""
    invoices = store.table(INVOICE_TABLE).read()
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in invoices:
        groups[invoice_identity(row)].append(row)

    results: list[dict[str, Any]] = []
    for key, rows in groups.items():
        conflict = len({
            (_text(r.get("invoice_date")), round(float(r.get("total_inclusive") or 0), 2), _text(r.get("direction")), _text(r.get("seller")))
            for r in rows
        }) > 1
        for index, row in enumerate(rows):
            ledger = _ledger_matches(row)
            posted = _text(row.get("status")) == "已入账" or bool(ledger)
            if posted:
                verdict = "已入账"
            elif len(rows) > 1 and conflict:
                verdict = "信息冲突"
            elif len(rows) > 1 and index > 0:
                verdict = "重复录入"
            elif len(rows) > 1:
                verdict = "重复录入"
            else:
                verdict = "未入账"
            results.append({
                **row,
                "identity": key,
                "verdict": verdict,
                "ledger_matches": ledger,
                "ledger_text": "、".join(f"{m['kind']}{m['id']}" for m in ledger) or "—",
                "duplicate_count": len(rows),
            })
    results.sort(key=lambda row: (_text(row.get("invoice_date")), _text(row.get("invoice_no"))), reverse=True)
    summary = {
        "total": len(results),
        "posted": sum(1 for row in results if row["verdict"] == "已入账"),
        "unposted": sum(1 for row in results if row["verdict"] == "未入账"),
        "duplicate": sum(1 for row in results if row["verdict"] in {"重复录入", "信息冲突"}),
        "conflict": sum(1 for row in results if row["verdict"] == "信息冲突"),
    }
    return {"rows": results, "summary": summary}


def check_one(keyword: str) -> dict[str, Any]:
    """按发票号码/代码/销售方查一张或多张发票，并核对台账。"""
    text = _text(keyword)
    if not text:
        raise ValueError("请输入发票号码或发票代码")
    rows = [
        row for row in store.table(INVOICE_TABLE).read()
        if text in _text(row.get("invoice_no")) or text in _text(row.get("invoice_code")) or text in _text(row.get("seller"))
    ]
    checked = []
    for row in rows:
        ledger = _ledger_matches(row)
        checked.append({**row, "ledger_matches": ledger, "posted": _text(row.get("status")) == "已入账" or bool(ledger)})
    return {"keyword": text, "rows": checked}


def explain_rules() -> list[str]:
    return [
        "发票号码是主键：同一号码重复出现，系统会标记重复；有发票代码时按「代码+号码」判断。",
        "没有号码的发票，按销售方+购买方+日期+金额生成内容指纹，指纹相同视为疑似重复。",
        "「已入账」= 发票池状态已入账，或收入台账的发票号码、费用台账的凭证号/备注能匹配到。",
        "号码相同但金额/日期/方向/销售方不一致，标记「信息冲突」，需要人工核对，不能直接入账。",
        "导入文件时系统本来就会跳过重复发票；补录前建议先在「查重核对」查一次。",
    ]


__all__ = ["invoice_identity", "reconcile", "check_one", "explain_rules"]
