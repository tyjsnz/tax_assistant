"""结账前检查：9 项固定检查（key 固定，UI 与测试直接依赖）。

检查口径
--------
=====================  ==========================================  ========
key                    检查内容                                    不通过
=====================  ==========================================  ========
period_in_future       不允许结未来期间                            ERROR
status_not_closed      目标期间未结账（防重复结账）                ERROR
previous_period_closed 上一期间已结账（首期/无业务可忽略）          WARNING
journal_balanced       期间内凭证借贷合计平衡（|差|≤0.01）         ERROR
has_activity           期间有收入/费用/凭证（否则为零申报）        WARNING
no_zero_vouchers       无 0 元凭证                                 WARNING
no_unmatched_ingest    无待匹配发票/流水/待确认匹配建议            WARNING
invoice_duplicates     期间内发票号码无重复                        ERROR
policy_period          期间在已验证政策区间                        WARNING
=====================  ==========================================  ========

返回结构见 :func:`pre_close_checks`；``blocking`` 为 ERROR 列表，``warnings`` 为 WARNING 列表。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import config
from engine import period_label, period_range
from engine.money import round2
from engine.policy_params import VERIFIED_FROM, VERIFIED_TO
from ledger import locks
from ledger.expense_ledger import period_totals as expense_period_totals
from ledger.income_ledger import period_totals as income_period_totals
from ledger.store import table

from closing import periods

CHECK_KEYS: tuple[str, ...] = (
    "period_in_future",
    "status_not_closed",
    "previous_period_closed",
    "journal_balanced",
    "has_activity",
    "no_zero_vouchers",
    "no_unmatched_ingest",
    "invoice_duplicates",
    "policy_period",
)

CHECK_NAMES: dict[str, str] = {
    "period_in_future": "期间不在未来",
    "status_not_closed": "目标期间未结账",
    "previous_period_closed": "上一期间已结账",
    "journal_balanced": "凭证借贷平衡",
    "has_activity": "期间有业务发生",
    "no_zero_vouchers": "无 0 元凭证",
    "no_unmatched_ingest": "无待匹配票据/流水",
    "invoice_duplicates": "期间内发票无重复",
    "policy_period": "期间在已验证政策区间",
}


# =============================================================================
# 工具
# =============================================================================
def _check(
    key: str,
    status: str,
    message: str,
    fix_hint: str = "",
) -> dict[str, Any]:
    """构造一项检查结果（key 固定，便于 UI 着色与测试断言）。"""
    if status not in ("PASS", "WARNING", "ERROR"):
        status = "WARNING"
    return {
        "key": key,
        "name": CHECK_NAMES.get(key, key),
        "status": status,
        "message": message,
        "fix_hint": fix_hint,
    }


def _previous_period(period: str) -> str:
    """返回上一期间键（月 → 上个月；季 → 上一季度）。"""
    start, _ = period_range(period)
    previous_day = start - timedelta(days=1)
    if periods.period_type_of(period) == "季":
        return f"{previous_day.year}Q{(previous_day.month - 1) // 3 + 1}"
    return f"{previous_day.year}-{previous_day.month:02d}"


def _period_activity(period: str) -> dict[str, int]:
    """期间内的收入/费用/凭证笔数。"""
    start, end = period_range(period)
    return {
        "income": int(income_period_totals(period).get("count") or 0),
        "expense": int(expense_period_totals(period).get("count") or 0),
        "journal_entries": len(table("journal_entries").filter(start=start.isoformat(), end=end.isoformat())),
    }


def _journal_totals(period: str) -> tuple[float, float]:
    """期间内凭证分录的借贷合计。"""
    start, end = period_range(period)
    debit = 0.0
    credit = 0.0
    for line in table("journal_lines").filter(start=start.isoformat(), end=end.isoformat()):
        amount = round2(line.get("amount"))
        if str(line.get("direction")) == "借":
            debit = round2(debit + amount)
        else:
            credit = round2(credit + amount)
    return debit, credit


# =============================================================================
# 检查实现
# =============================================================================
def _check_period_in_future(period: str) -> dict[str, Any]:
    start, _ = period_range(period)
    today = config.today()
    if start > today:
        return _check(
            "period_in_future",
            "ERROR",
            f"期间 {period} 尚未开始（起始日 {start.isoformat()}），不能结未来期间。",
            "请选择已经发生的月份/季度结账；未来期间无需结账。",
        )
    return _check("period_in_future", "PASS", f"期间 {period} 已开始，可正常结账。")


def _check_status_not_closed(period: str) -> dict[str, Any]:
    row = periods.find_period_row(period)
    status = str((row or {}).get("status") or "未结账")
    if status != "未结账":
        closed_at = str((row or {}).get("closed_at") or "")
        return _check(
            "status_not_closed",
            "ERROR",
            f"期间 {period} 当前状态为「{status}」"
            + (f"（结账时间 {closed_at}）" if closed_at else "")
            + "，请勿重复结账。",
            "如需调整账务，请先反结账后再重新结账。",
        )
    return _check("status_not_closed", "PASS", f"期间 {period} 尚未结账，可结账。")


def _check_previous_period_closed(period: str) -> dict[str, Any]:
    previous = _previous_period(period)
    if previous == period:
        return _check("previous_period_closed", "PASS", "首个会计期间，无上一期间。")
    previous_row = periods.find_period_row(previous)
    previous_status = str((previous_row or {}).get("status") or "未结账")
    if previous_status not in locks.LOCKED_STATUSES:
        activity = _period_activity(previous)
        if sum(activity.values()) == 0:
            return _check(
                "previous_period_closed",
                "PASS",
                f"上一期间 {previous} 无业务数据（首期或空账），可继续结账。",
            )
        return _check(
            "previous_period_closed",
            "WARNING",
            f"上一期间 {previous} 尚未结账（状态「{previous_status}」），账务连续性可能受影响。",
            f"建议先结账 {previous}，或确认无需结账后继续。",
        )
    return _check("previous_period_closed", "PASS", f"上一期间 {previous} 已结账。")


def _check_journal_balanced(period: str) -> dict[str, Any]:
    debit, credit = _journal_totals(period)
    difference = round2(debit - credit)
    if abs(difference) > 0.01:
        return _check(
            "journal_balanced",
            "ERROR",
            f"期间内凭证借贷不平衡：借方合计 {debit:.2f} 元、贷方合计 {credit:.2f} 元，差额 {difference:.2f} 元。",
            "请检查并修正不平衡凭证（可在「台账管理 → 凭证」中核对后重新登记）。",
        )
    return _check(
        "journal_balanced",
        "PASS",
        f"期间内凭证借贷平衡（借方 {debit:.2f} 元 = 贷方 {credit:.2f} 元）。",
    )


def _check_has_activity(period: str) -> dict[str, Any]:
    activity = _period_activity(period)
    total = sum(activity.values())
    if total == 0:
        return _check(
            "has_activity",
            "WARNING",
            f"期间 {period} 无收入、费用与凭证记录，属零申报/无业务期间。",
            "如确无业务可直接结账；否则请先补录台账与凭证。",
        )
    return _check(
        "has_activity",
        "PASS",
        f"期间有业务发生：收入 {activity['income']} 笔、费用 {activity['expense']} 笔、凭证 {activity['journal_entries']} 张。",
    )


def _check_no_zero_vouchers(period: str) -> dict[str, Any]:
    start, end = period_range(period)
    zero_entries = [
        row
        for row in table("journal_entries").filter(start=start.isoformat(), end=end.isoformat())
        if round2(row.get("total_amount")) == 0
    ]
    if zero_entries:
        numbers = "、".join(str(row.get("entry_no") or row.get("id")) for row in zero_entries[:5])
        return _check(
            "no_zero_vouchers",
            "WARNING",
            f"期间内存在 {len(zero_entries)} 张 0 元凭证（{numbers}）。",
            "请核对是否为空凭证；如无实际业务建议删除或补充金额。",
        )
    return _check("no_zero_vouchers", "PASS", "期间内无 0 元凭证。")


def _check_no_unmatched_ingest(period: str) -> dict[str, Any]:
    start, end = period_range(period)
    invoices = table("invoices").filter(start=start.isoformat(), end=end.isoformat())
    transactions = table("bank_transactions").filter(start=start.isoformat(), end=end.isoformat())
    pending_invoices = [row for row in invoices if str(row.get("status")) == "待匹配"]
    pending_transactions = [row for row in transactions if str(row.get("status")) == "待匹配"]
    invoice_ids = {str(row.get("id")) for row in invoices}
    transaction_ids = {str(row.get("id")) for row in transactions}
    pending_matches = [
        row
        for row in table("matches").read()
        if str(row.get("status")) == "建议"
        and (
            str(row.get("invoice_id")) in invoice_ids
            or str(row.get("transaction_id")) in transaction_ids
        )
    ]
    total = len(pending_invoices) + len(pending_transactions) + len(pending_matches)
    if total:
        return _check(
            "no_unmatched_ingest",
            "WARNING",
            f"期间内仍有待处理票据：待匹配发票 {len(pending_invoices)} 张、"
            f"待匹配流水 {len(pending_transactions)} 笔、待确认匹配建议 {len(pending_matches)} 条。",
            "建议先在「票据与流水」页面完成匹配确认或忽略，再结账。",
        )
    return _check("no_unmatched_ingest", "PASS", "期间内票据/流水均已处理。")


def _check_invoice_duplicates(period: str) -> dict[str, Any]:
    start, end = period_range(period)
    grouped: dict[str, int] = {}
    for row in table("invoices").filter(start=start.isoformat(), end=end.isoformat()):
        invoice_no = str(row.get("invoice_no") or "").strip()
        if not invoice_no:
            continue
        grouped[invoice_no] = grouped.get(invoice_no, 0) + 1
    duplicates = {number: count for number, count in grouped.items() if count > 1}
    if duplicates:
        detail = "、".join(f"{number}（{count} 张）" for number, count in list(duplicates.items())[:5])
        return _check(
            "invoice_duplicates",
            "ERROR",
            f"期间内存在重复发票号码：{detail}。",
            "请删除重复录入的发票，或核对是否为红冲/重开票据后处理。",
        )
    return _check("invoice_duplicates", "PASS", "期间内发票号码无重复。")


def _check_policy_period(period: str) -> dict[str, Any]:
    start, _ = period_range(period)
    if VERIFIED_FROM <= start <= VERIFIED_TO:
        return _check(
            "policy_period",
            "PASS",
            f"期间 {period} 在已验证政策区间（{VERIFIED_FROM.isoformat()} ~ {VERIFIED_TO.isoformat()}）内。",
        )
    return _check(
        "policy_period",
        "WARNING",
        f"期间 {period} 不在已验证政策区间（{VERIFIED_FROM.isoformat()} ~ {VERIFIED_TO.isoformat()}）内，"
        "税费计算可能需按最新政策复核。",
        "请核对最新税收政策或咨询主管税务机关后再结账。",
    )


# =============================================================================
# 对外接口
# =============================================================================
def pre_close_checks(period: Any) -> dict[str, Any]:
    """执行结账前 9 项检查，返回汇总报告（含 ``blocking`` / ``warnings``）。

    返回结构::

        {"period","period_label","ok","level","checks":[...],
         "blocking":[ERROR...],"warnings":[WARNING...],"generated_at","disclaimer"}
    """
    text = periods.normalize_period(period)
    checks = [
        _check_period_in_future(text),
        _check_status_not_closed(text),
        _check_previous_period_closed(text),
        _check_journal_balanced(text),
        _check_has_activity(text),
        _check_no_zero_vouchers(text),
        _check_no_unmatched_ingest(text),
        _check_invoice_duplicates(text),
        _check_policy_period(text),
    ]
    blocking = [check for check in checks if check["status"] == "ERROR"]
    warnings = [check for check in checks if check["status"] == "WARNING"]
    level = "ERROR" if blocking else ("WARNING" if warnings else "PASS")
    return {
        "period": text,
        "period_label": period_label(text),
        "ok": not blocking,
        "level": level,
        "checks": checks,
        "blocking": blocking,
        "warnings": warnings,
        "generated_at": config.now_text(),
        "disclaimer": config.DISCLAIMER,
    }


__all__ = [
    "CHECK_KEYS",
    "CHECK_NAMES",
    "pre_close_checks",
]
