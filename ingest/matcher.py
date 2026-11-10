"""发票 ↔ 银行流水自动匹配与确认入账（确定性评分，便于测试与复核）。

评分规则（总分 100）
-------------------
* **金额 45 分**：差异 ≤0.01 元 →45；≤0.5% →35；≤1% →30；≤3% →18；≤5% →8；>5% 不可匹配；
* **日期 30 分**：同日 →30；≤3 天 →25；≤7 天 →18；≤15 天（窗口）→10；超窗口不可匹配；
* **名称 25 分**：发票对方（销项取购买方、进项取销售方）与流水对方户名归一化后
  ``difflib.SequenceMatcher`` 相似度 × 25。

只匹配同方向（销项 ↔ 收入、进项 ↔ 支出），按分数降序贪心分配，一张发票/一笔流水只匹配一次；
已有「建议 / 已确认 / 已忽略」记录的组合跳过，避免重复建议。

确认入账
--------
``confirm_match`` 调用台账模块写入收入/费用台账，并用 ``ledger.journal`` 生成凭证（可选是否记账），
随后更新匹配与票据/流水状态并写审计日志；仅「建议」状态可确认，重复确认抛 ``ValueError``。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from engine.money import round2
from ledger import expense_ledger, income_ledger, journal
from ledger.audit import log_user_action
from ledger.locks import ensure_open
from ledger.store import table

from ingest._common import name_similarity, now_text, parse_date

INVOICE_TABLE = "invoices"
TRANSACTION_TABLE = "bank_transactions"
MATCH_TABLE = "matches"
MATCH_TYPE = "发票-流水"

#: 金额评分档位：(差异比例上限, 分值)
_AMOUNT_TIERS: tuple[tuple[float, int], ...] = (
    (0.005, 35),
    (0.01, 30),
    (0.03, 18),
    (0.05, 8),
)


# =============================================================================
# 评分
# =============================================================================
def score_pair(
    invoice: dict[str, Any],
    transaction: dict[str, Any],
    *,
    date_window_days: int = 15,
) -> dict[str, Any]:
    """给一对「发票-流水」打分，返回 ``eligible/score/amount_diff/date_diff/name_similarity/reasons``。"""
    result: dict[str, Any] = {
        "eligible": False,
        "score": 0.0,
        "amount_diff": 0.0,
        "date_diff": 0,
        "name_similarity": 0.0,
        "reasons": [],
    }
    reasons: list[str] = result["reasons"]
    invoice_direction = str(invoice.get("direction") or "")
    transaction_direction = str(transaction.get("direction") or "")
    expected_transaction_direction = {"销项": "收入", "进项": "支出"}.get(invoice_direction)
    if expected_transaction_direction is None:
        reasons.append(f"发票方向「{invoice_direction or '未知'}」无法识别，不可匹配")
        return result
    if transaction_direction != expected_transaction_direction:
        reasons.append(
            f"方向不符：发票为{invoice_direction}，流水为{transaction_direction or '未知'}"
            f"（应匹配{expected_transaction_direction}）"
        )
        return result
    reasons.append(f"方向一致：{invoice_direction} ↔ {expected_transaction_direction}")

    invoice_total = round2(invoice.get("total_inclusive"))
    transaction_amount = round2(transaction.get("amount"))
    amount_diff = round2(abs(invoice_total - transaction_amount))
    denominator = max(abs(invoice_total), abs(transaction_amount), 0.01)
    ratio = amount_diff / denominator
    result["amount_diff"] = amount_diff
    if amount_diff <= 0.01:
        amount_score = 45
        reasons.append("金额一致（差异 ≤0.01 元）")
    elif ratio <= 0.05:
        amount_score = next(score for limit, score in _AMOUNT_TIERS if ratio <= limit)
        reasons.append(f"金额差异 {ratio * 100:.2f}%（{amount_diff:.2f} 元），降低得分")
    else:
        reasons.append(f"金额差异 {ratio * 100:.2f}%（{amount_diff:.2f} 元）超过 5%，不可匹配")
        return result

    invoice_date = parse_date(invoice.get("invoice_date"))
    transaction_date = parse_date(transaction.get("transaction_date"))
    if not invoice_date or not transaction_date:
        reasons.append("开票日期或交易日期缺失，无法按日期窗口匹配")
        return result
    date_diff = abs((date.fromisoformat(invoice_date) - date.fromisoformat(transaction_date)).days)
    result["date_diff"] = date_diff
    if date_diff > int(date_window_days):
        reasons.append(f"日期相差 {date_diff} 天，超过匹配窗口 {date_window_days} 天，不可匹配")
        return result
    if date_diff == 0:
        date_score = 30
        reasons.append("交易日期与开票日期相同")
    elif date_diff <= 3:
        date_score = 25
        reasons.append(f"日期相差 {date_diff} 天")
    elif date_diff <= 7:
        date_score = 18
        reasons.append(f"日期相差 {date_diff} 天")
    else:
        date_score = 10
        reasons.append(f"日期相差 {date_diff} 天（接近窗口上限）")

    counterparty = str(invoice.get("buyer") or "") if invoice_direction == "销项" else str(invoice.get("seller") or "")
    similarity = name_similarity(counterparty, transaction.get("counterparty"))
    result["name_similarity"] = similarity
    name_score = round(similarity * 25, 1)
    reasons.append(f"对方名称相似度 {similarity * 100:.1f}%（{name_score:.1f} 分）")

    total_score = round(amount_score + date_score + name_score, 1)
    result.update({"eligible": True, "score": total_score})
    return result


# =============================================================================
# 生成 / 查询建议
# =============================================================================
def _match_note(score_result: dict[str, Any]) -> str:
    """把评分理由压缩成匹配记录备注。"""
    return "；".join(score_result.get("reasons") or [])[:180]


def suggest_matches(*, date_window_days: int = 15, min_score: float = 60.0) -> dict[str, Any]:
    """计算并保存确定性匹配建议（贪心一对一），返回 ``created/count/candidates/summary``。"""
    invoice_table = table(INVOICE_TABLE)
    transaction_table = table(TRANSACTION_TABLE)
    match_table = table(MATCH_TABLE)
    invoices = [row for row in invoice_table.read() if str(row.get("status")) == "待匹配"]
    transactions = [row for row in transaction_table.read() if str(row.get("status")) == "待匹配"]
    existing_pairs = {
        (str(row.get("invoice_id")), str(row.get("transaction_id")))
        for row in match_table.read()
    }

    candidates: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for invoice in invoices:
        for transaction in transactions:
            if (str(invoice.get("id")), str(transaction.get("id"))) in existing_pairs:
                continue
            score_result = score_pair(invoice, transaction, date_window_days=date_window_days)
            if score_result["eligible"]:
                candidates.append((invoice, transaction, score_result))
    candidate_count = len(candidates)
    candidates.sort(
        key=lambda item: (
            -item[2]["score"],
            item[2]["amount_diff"],
            item[2]["date_diff"],
            str(item[0].get("id")),
            str(item[1].get("id")),
        )
    )
    used_invoices: set[str] = set()
    used_transactions: set[str] = set()
    created: list[dict[str, Any]] = []
    for invoice, transaction, score_result in candidates:
        score = float(score_result["score"])
        if score < min_score:
            break
        invoice_id = str(invoice.get("id"))
        transaction_id = str(transaction.get("id"))
        if invoice_id in used_invoices or transaction_id in used_transactions:
            continue
        used_invoices.add(invoice_id)
        used_transactions.add(transaction_id)
        created.append(
            match_table.append(
                {
                    "invoice_id": invoice_id,
                    "invoice_no": str(invoice.get("invoice_no") or ""),
                    "transaction_id": transaction_id,
                    "transaction_no": str(transaction.get("transaction_no") or ""),
                    "score": int(round(score)),
                    "match_type": MATCH_TYPE,
                    "status": "建议",
                    "amount_diff": score_result["amount_diff"],
                    "date_diff": int(score_result["date_diff"]),
                    "note": _match_note(score_result),
                    "confirmed_at": "",
                }
            )
        )
    return {
        "created": created,
        "count": len(created),
        "candidates": candidate_count,
        "summary": match_summary(),
    }


def list_matches(status: str | None = None) -> list[dict[str, Any]]:
    """列出匹配建议（可按状态过滤），附发票与流水摘要，按分数从高到低排序。"""
    invoice_map = {str(row.get("id")): row for row in table(INVOICE_TABLE).read()}
    transaction_map = {str(row.get("id")): row for row in table(TRANSACTION_TABLE).read()}
    status_text = str(status or "").strip()
    results: list[dict[str, Any]] = []
    for row in table(MATCH_TABLE).read():
        if status_text and str(row.get("status")) != status_text:
            continue
        invoice = invoice_map.get(str(row.get("invoice_id"))) or {}
        transaction = transaction_map.get(str(row.get("transaction_id"))) or {}
        entry = dict(row)
        entry.update(
            {
                "invoice_seller": invoice.get("seller", ""),
                "invoice_buyer": invoice.get("buyer", ""),
                "invoice_date": invoice.get("invoice_date", ""),
                "invoice_type": invoice.get("invoice_type", ""),
                "invoice_direction": invoice.get("direction", ""),
                "invoice_total": invoice.get("total_inclusive", 0.0),
                "transaction_date": transaction.get("transaction_date", ""),
                "transaction_amount": transaction.get("amount", 0.0),
                "transaction_counterparty": transaction.get("counterparty", ""),
                "transaction_summary": transaction.get("summary", ""),
                "invoice": invoice,
                "transaction": transaction,
            }
        )
        results.append(entry)
    results.sort(key=lambda item: (-float(item.get("score") or 0), str(item.get("created_at") or "")))
    return results


# =============================================================================
# 确认入账 / 忽略
# =============================================================================
def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _guess_income_category(*texts: Any) -> str:
    """按摘要/项目名称猜测收入业务类别（软件开发/运维服务/技术服务）。"""
    text = " ".join(str(item or "") for item in texts)
    if any(keyword in text for keyword in ("软件", "开发", "系统", "平台", "定制")):
        return "软件开发"
    if any(keyword in text for keyword in ("运维", "维护", "巡检", "技术支持")):
        return "运维服务"
    return "技术服务"


def _guess_expense_category(*texts: Any) -> str:
    """按摘要/项目名称猜测费用类别（办公/差旅/软件订阅/研发材料/其他）。"""
    text = " ".join(str(item or "") for item in texts)
    if any(keyword in text for keyword in ("办公", "耗材", "用品", "设备", "家具", "打印")):
        return "办公"
    if any(keyword in text for keyword in ("差旅", "住宿", "交通", "机票", "酒店", "车费", "出差")):
        return "差旅"
    if any(keyword in text for keyword in ("软件", "云服务", "订阅", "平台使用", "会员")):
        return "软件订阅"
    if any(keyword in text for keyword in ("研发", "材料", "样机", "元件")):
        return "研发材料"
    return "其他"


def _invoice_description(invoice: dict[str, Any], transaction: dict[str, Any]) -> str:
    """生成台账摘要：优先流水摘要，其次发票项目名称，最后发票类型+号码。"""
    item_name = ""
    raw_json = invoice.get("raw_json")
    if isinstance(raw_json, dict):
        for key in ("ItemName", "项目名称", "货物或应税劳务名称"):
            if raw_json.get(key):
                item_name = str(raw_json[key]).strip()
                break
    return _first_text(
        transaction.get("summary"),
        item_name,
        f"{invoice.get('invoice_type') or '发票'} {invoice.get('invoice_no') or ''}".strip(),
        "票据流水匹配入账",
    )


def _posted_journal(entry: dict[str, Any]) -> dict[str, Any] | None:
    """登记凭证并返回登记结果；登记失败时返回 ``None``（台账已生成，不阻塞确认）。"""
    try:
        return journal.post_entry(entry)
    except (ValueError, KeyError):
        return None


def confirm_match(match_id: str, *, post_journal: bool = True) -> dict[str, Any]:
    """确认匹配并一键入账：生成台账记录、凭证，更新状态并写审计日志。

    仅「建议」状态可确认；重复确认抛 ``ValueError``。
    """
    match_table = table(MATCH_TABLE)
    match = match_table.get(match_id)
    if match is None:
        raise KeyError(f"匹配记录不存在：{match_id}")
    status = str(match.get("status") or "")
    if status != "建议":
        raise ValueError(f"匹配记录当前状态为「{status}」，仅「建议」状态可确认入账，请勿重复确认。")
    invoice = table(INVOICE_TABLE).get(str(match.get("invoice_id")))
    transaction = table(TRANSACTION_TABLE).get(str(match.get("transaction_id")))
    if invoice is None or transaction is None:
        raise ValueError("匹配记录关联的发票或流水已不存在，无法确认入账。")

    invoice_direction = str(invoice.get("direction") or "")
    invoice_total = round2(invoice.get("total_inclusive"))
    transaction_amount = round2(transaction.get("amount"))
    warnings: list[str] = []
    if abs(invoice_total - transaction_amount) > 0.01:
        warnings.append(
            f"发票价税合计 {invoice_total:.2f} 元与流水金额 {transaction_amount:.2f} 元相差 "
            f"{abs(invoice_total - transaction_amount):.2f} 元，已按发票金额入账，请核对。"
        )
    entry_date = _first_text(invoice.get("invoice_date"), transaction.get("transaction_date"))
    ensure_open(entry_date, action="票据匹配入账")
    description = _invoice_description(invoice, transaction)

    if invoice_direction == "销项":
        kind = "income"
        income_type = "专票" if str(invoice.get("invoice_type")) == "专票" else "普票"
        record = income_ledger.add_income(
            entry_date,
            _first_text(invoice.get("buyer")),
            invoice_total,
            invoice_type=income_type,
            category=_guess_income_category(description, transaction.get("summary")),
            description=description,
            invoice_no=_first_text(invoice.get("invoice_no")),
            note="票据流水匹配入账",
        )
        entry = journal.build_income_entry(record)
    elif invoice_direction == "进项":
        kind = "expense"
        record = expense_ledger.add_expense(
            entry_date,
            description,
            invoice_total,
            category=_guess_expense_category(description, transaction.get("summary")),
            is_rd_related=False,
            vendor=_first_text(invoice.get("seller")),
            receipt_no=_first_text(invoice.get("invoice_no")),
            note="进项发票入账（小规模不抵扣）",
        )
        entry = journal.build_expense_entry(record)
    else:
        raise ValueError(f"发票方向「{invoice_direction}」无法识别，无法确认入账。")

    posted = _posted_journal(entry) if post_journal else None
    if post_journal and posted is None:
        warnings.append("凭证登记失败（可能凭证号冲突），台账已入账，请在凭证管理中手工补录。")

    confirmed_at = now_text()
    previous_note = str(match.get("note") or "")
    note = f"{previous_note}；确认入账 {confirmed_at}".strip("；") if previous_note else f"确认入账 {confirmed_at}"
    updated_match = match_table.update(match_id, {"status": "已确认", "confirmed_at": confirmed_at, "note": note})
    table(INVOICE_TABLE).update(str(match.get("invoice_id")), {"status": "已入账"})
    table(TRANSACTION_TABLE).update(str(match.get("transaction_id")), {"status": "已入账"})

    entry_no = (posted or {}).get("entry_no", "")
    log_user_action(
        "票据匹配入账",
        f"匹配 {match_id}：发票 {invoice.get('invoice_no') or '—'} ↔ 流水 {transaction.get('transaction_no') or '—'}，"
        f"生成{'收入' if kind == 'income' else '费用'}台账 {record.get('id') or '—'}，"
        f"金额 {invoice_total:.2f} 元，凭证 {entry_no or '（未记账）'}",
    )
    return {
        "ok": True,
        "match": updated_match,
        "ledger": {"kind": kind, "record": record},
        "journal": posted,
        "invoice_status": "已入账",
        "transaction_status": "已入账",
        "warnings": warnings,
    }


def ignore_match(match_id: str) -> dict[str, Any]:
    """忽略一条匹配建议（不修改发票/流水状态），写审计日志。"""
    match_table = table(MATCH_TABLE)
    match = match_table.get(match_id)
    if match is None:
        raise KeyError(f"匹配记录不存在：{match_id}")
    status = str(match.get("status") or "")
    if status != "建议":
        raise ValueError(f"匹配记录当前状态为「{status}」，仅「建议」状态可忽略。")
    updated = match_table.update(match_id, {"status": "已忽略"})
    log_user_action(
        "忽略票据匹配",
        f"匹配 {match_id}：发票 {match.get('invoice_no') or '—'} ↔ 流水 {match.get('transaction_no') or '—'}",
    )
    return {"ok": True, "match": updated, "warnings": []}


# =============================================================================
# 汇总
# =============================================================================
def match_summary() -> dict[str, Any]:
    """票据/流水/匹配池数量汇总（供驾驶舱与「票据与流水」页面指标卡）。"""
    invoices = table(INVOICE_TABLE).read()
    transactions = table(TRANSACTION_TABLE).read()
    matches = table(MATCH_TABLE).read()
    invoice_map = {str(row.get("id")): row for row in invoices}

    def _count_status(rows: list[dict[str, Any]], status: str) -> int:
        return sum(1 for row in rows if str(row.get("status")) == status)

    confirmed = [row for row in matches if str(row.get("status")) == "已确认"]
    matched_income = 0
    matched_expense = 0
    matched_income_amount = 0.0
    matched_expense_amount = 0.0
    for match in confirmed:
        invoice = invoice_map.get(str(match.get("invoice_id"))) or {}
        amount = round2(invoice.get("total_inclusive"))
        if str(invoice.get("direction")) == "销项":
            matched_income += 1
            matched_income_amount = round2(matched_income_amount + amount)
        else:
            matched_expense += 1
            matched_expense_amount = round2(matched_expense_amount + amount)

    return {
        "invoices_pending": _count_status(invoices, "待匹配"),
        "invoices_matched": sum(
            1 for row in invoices if str(row.get("status")) in ("已匹配", "已入账")
        ),
        "transactions_pending": _count_status(transactions, "待匹配"),
        "transactions_matched": sum(
            1 for row in transactions if str(row.get("status")) in ("已匹配", "已入账")
        ),
        "suggestions": _count_status(matches, "建议"),
        "confirmed": len(confirmed),
        "ignored": _count_status(matches, "已忽略"),
        "matched_income": matched_income,
        "matched_expense": matched_expense,
        "matched_income_amount": matched_income_amount,
        "matched_expense_amount": matched_expense_amount,
        "pool_total": {
            "invoices": len(invoices),
            "transactions": len(transactions),
            "matches": len(matches),
        },
        "disclaimer": "匹配与入账结果仅供参考，请核对原始票据与银行流水后确认。",
    }


__all__ = [
    "score_pair",
    "suggest_matches",
    "list_matches",
    "confirm_match",
    "ignore_match",
    "match_summary",
]
