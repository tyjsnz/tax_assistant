"""月结/季结状态机：结账、反结账、损益结转凭证与季度结账。

结账规则
--------
1. 结账前执行 :func:`closing.checks.pre_close_checks`，存在 **ERROR** 一律拒绝（``force`` 不放行）；
2. 按期间内 ``journal_lines`` 的会计科目净额生成「损益结转」凭证（``source_ref=CLOSE-{period}``，
   ``voucher_type=结转``）：

   * 收入类科目（主营业务收入、其他业务收入、其他收益）贷方净额 → 借各收入科目 / 贷本年利润；
   * 费用类科目（主营业务成本、税金及附加、销售费用、财务费用、管理费用*、研发支出*）
     借方净额 → 借本年利润 / 贷各费用科目；

   金额为 0 的科目不生成分录；收入与费用均为 0 时不生成凭证，但仍写期间状态；
3. 结账时生成期间快照（:mod:`closing.snapshot`）并写入 ``periods.snapshot_hash/snapshot_json``；
4. 反结账只允许**最近一个已结账期间**，删除该期间结转凭证（凭证头 + 分录行），状态回「未结账」；
5. 季度结账要求季度内 3 个月均已结账，只写季度期间状态与快照，不重复生成月度结转凭证。

结账内部生成/删除凭证使用 ``journal.post_entry(..., check_lock=False)`` 或直接读写 ``store.table``，
以绕开期间锁（否则会把自己锁在门外）。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import config
from engine import period_label, period_range
from engine.money import round2
from ledger import journal, locks
from ledger.audit import log_user_action
from ledger.store import table

from closing import checks as checks_module
from closing import periods
from closing import snapshot as snapshot_module

PERIOD_TABLE = "periods"
ENTRY_TABLE = "journal_entries"
LINE_TABLE = "journal_lines"
PROFIT_ACCOUNT = "本年利润"
INCOME_ACCOUNTS: tuple[str, ...] = ("主营业务收入", "其他业务收入", "其他收益")
EXPENSE_ACCOUNTS: tuple[str, ...] = ("主营业务成本", "税金及附加", "销售费用", "财务费用")
EXPENSE_PREFIXES: tuple[str, ...] = ("管理费用", "研发支出")

CLOSED_STATUSES = locks.LOCKED_STATUSES


# =============================================================================
# 内部工具
# =============================================================================
def _is_income_account(account: str) -> bool:
    return account in INCOME_ACCOUNTS


def _is_expense_account(account: str) -> bool:
    if account in EXPENSE_ACCOUNTS:
        return True
    return any(account.startswith(prefix) for prefix in EXPENSE_PREFIXES)


def _net_balances(period: str) -> tuple[dict[str, float], dict[str, float]]:
    """计算期间内收入类（贷-借）与费用类（借-贷）科目净额，剔除结转凭证自身分录。"""
    text = periods.normalize_period(period)
    _, start, end = periods.period_bounds(text)
    closing_ref = f"CLOSE-{text}"
    headers = table(ENTRY_TABLE).filter(start=start, end=end)
    closing_entry_nos = {
        str(row.get("entry_no") or "")
        for row in headers
        if str(row.get("source_ref") or "") == closing_ref
    }
    income: dict[str, float] = {}
    expense: dict[str, float] = {}
    for line in table(LINE_TABLE).filter(start=start, end=end):
        if str(line.get("entry_no") or "") in closing_entry_nos:
            continue
        account = str(line.get("account") or "")
        amount = round2(line.get("amount"))
        direction = str(line.get("direction") or "借")
        if _is_income_account(account):
            delta = amount if direction == "贷" else -amount
            income[account] = round2(income.get(account, 0.0) + delta)
        elif _is_expense_account(account):
            delta = amount if direction == "借" else -amount
            expense[account] = round2(expense.get(account, 0.0) + delta)
    return income, expense


def _quarter_months(quarter: str) -> list[str]:
    start, end = period_range(quarter)
    months: list[str] = []
    cursor = start
    while cursor <= end:
        months.append(f"{cursor.year}-{cursor.month:02d}")
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    return months


def _quarter_last_month(quarter: str) -> str:
    months = _quarter_months(quarter)
    return months[-1] if months else ""


def _row_status(period: str) -> str:
    row = periods.find_period_row(period)
    return str((row or {}).get("status") or "未结账")


def _update_period(period: str, patch: dict[str, Any]) -> dict[str, Any]:
    """更新期间行（不存在时先创建），返回更新后的原始行。"""
    row = periods.ensure_period(period)
    payload = dict(patch)
    payload.setdefault("period_type", periods.period_type_of(period))
    return table(PERIOD_TABLE).update(row["id"], payload)


def _delete_closing_entries(period: str) -> list[str]:
    """删除期间结转凭证（凭证头 + 分录行），返回被删除的凭证号列表。"""
    ref = f"CLOSE-{period}"
    entry_table = table(ENTRY_TABLE)
    line_table = table(LINE_TABLE)
    headers = [row for row in entry_table.read() if str(row.get("source_ref") or "") == ref]
    entry_nos = [str(row.get("entry_no") or "") for row in headers]
    for row in headers:
        entry_table.delete(row["id"])
    if entry_nos:
        entry_no_set = set(entry_nos)
        for line in line_table.read():
            if str(line.get("entry_no") or "") in entry_no_set:
                line_table.delete(line["id"])
    return entry_nos


# =============================================================================
# 查询 / 预览
# =============================================================================
def is_closed(period: Any) -> bool:
    """判断期间是否已结账（季度已结账时其所属月份也视为已关闭）。"""
    text = periods.normalize_period(period)
    if _row_status(text) in CLOSED_STATUSES:
        return True
    if periods.period_type_of(text) == "月":
        quarter = locks.quarter_key(text)
        return _row_status(quarter) in CLOSED_STATUSES
    return False


def closing_entries(period: Any) -> list[dict[str, Any]]:
    """预览损益结转凭证（不写库）；无需结转时返回空列表。

    返回元素结构与 :func:`ledger.journal.post_entry` 接受的凭证一致：
    ``{"date","description","voucher_type":"结转","source_ref":"CLOSE-{period}","lines":[...]}``。
    """
    text = periods.normalize_period(period)
    if is_closed(text):
        # 已结账期间无需再生成结转凭证（避免重复结转）
        return []
    income, expense = _net_balances(text)
    _, _, end = periods.period_bounds(text)

    debit_lines: list[dict[str, Any]] = []
    credit_lines: list[dict[str, Any]] = []
    income_total = 0.0
    expense_total = 0.0
    for account in sorted(income):
        net = round2(income[account])
        if net <= 0.005:
            continue
        debit_lines.append({"account": account, "direction": "借", "amount": net})
        income_total = round2(income_total + net)
    for account in sorted(expense):
        net = round2(expense[account])
        if net <= 0.005:
            continue
        credit_lines.append({"account": account, "direction": "贷", "amount": net})
        expense_total = round2(expense_total + net)
    if income_total <= 0 and expense_total <= 0:
        return []
    if income_total > 0:
        credit_lines.insert(0, {"account": PROFIT_ACCOUNT, "direction": "贷", "amount": income_total})
    if expense_total > 0:
        debit_lines.append({"account": PROFIT_ACCOUNT, "direction": "借", "amount": expense_total})
    return [
        {
            "date": end,
            "description": f"损益结转（{period_label(text)}）",
            "voucher_type": "结转",
            "source_ref": f"CLOSE-{text}",
            "lines": debit_lines + credit_lines,
        }
    ]


# =============================================================================
# 月度结账 / 反结账
# =============================================================================
def close_period(
    period: Any,
    *,
    operator: str = "system",
    force: bool = False,
    note: str = "",
) -> dict[str, Any]:
    """结账一个月份：执行检查 → 生成损益结转凭证 → 生成快照 → 更新期间状态。

    存在 ERROR 检查项时抛 ``ValueError``（``force=True`` 也不放行）；重复结账同样抛错。
    """
    text = periods.normalize_period(period)
    if periods.period_type_of(text) == "季":
        raise ValueError(f"{text} 是季度期间，请使用 close_quarter 进行季度结账。")
    quarter_status = _row_status(locks.quarter_key(text))
    if quarter_status in CLOSED_STATUSES and _row_status(text) == "未结账":
        raise ValueError(f"期间 {text} 所属季度 {locks.quarter_key(text)} 已结账，无需重复结账。")

    report = checks_module.pre_close_checks(text)
    if report["blocking"]:
        messages = "；".join(check["message"] for check in report["blocking"])
        raise ValueError(f"无法结账：{messages}")

    entries = closing_entries(text)
    posted: list[dict[str, Any]] = []
    for entry in entries:
        posted.append(journal.post_entry(entry, check_lock=False))

    snapshot = snapshot_module.period_snapshot(text)
    now = config.now_text()
    previous_note = str((periods.find_period_row(text) or {}).get("note") or "")
    merged_note = "；".join(part for part in (previous_note, note) if part)[:500]
    updated = _update_period(
        text,
        {
            "status": "已结账",
            "closed_at": now,
            "closed_by": operator,
            "reopened_at": "",
            "snapshot_hash": snapshot["hash"],
            "snapshot_json": snapshot,
            "note": merged_note,
        },
    )
    log_user_action(
        "期间结账",
        f"{text}（{periods.period_type_of(text)}）：结转凭证 {len(posted)} 张，"
        f"快照 {snapshot['hash'][:16]}…，操作人 {operator}",
    )
    return {
        "ok": True,
        "period": text,
        "period_type": periods.period_type_of(text),
        "status": str(updated.get("status") or "已结账"),
        "entries": [
            {
                "entry_no": item.get("entry_no", ""),
                "lines": list(item.get("lines") or []),
                "total": round2(item.get("total_amount")),
            }
            for item in posted
        ],
        "snapshot_hash": snapshot["hash"],
        "snapshot": snapshot,
        "checks": report,
    }


def reopen_period(
    period: Any,
    *,
    operator: str = "system",
    reason: str = "",
) -> dict[str, Any]:
    """反结账一个月份：仅最近一期可反结，删除该期间损益结转凭证并回到「未结账」。"""
    text = periods.normalize_period(period)
    if periods.period_type_of(text) == "季":
        raise ValueError(f"{text} 是季度期间，请使用 reopen_quarter 进行季度反结账。")
    row = periods.find_period_row(text)
    status = str((row or {}).get("status") or "未结账")
    if row is None or status == "未结账":
        raise ValueError(f"期间 {text} 未结账，无需反结账。")

    quarter = locks.quarter_key(text)
    quarter_status = _row_status(quarter)
    if quarter_status in CLOSED_STATUSES:
        raise ValueError(f"期间 {text} 所属季度 {quarter} 已结账，请先反结账季度 {quarter}。")

    latest = locks.latest_closed_month()
    if latest and latest != text:
        raise ValueError(f"只能反结账最近一个已结账期间：当前最近已结账月份为 {latest}，不能反结 {text}。")

    removed = _delete_closing_entries(text)
    now = config.now_text()
    previous_note = str(row.get("note") or "")
    merged_note = "；".join(
        part for part in (previous_note, f"反结账：{reason}" if reason else "") if part
    )[:500]
    updated = _update_period(
        text,
        {
            "status": "未结账",
            "reopened_at": now,
            "snapshot_hash": "",
            "snapshot_json": {},
            "note": merged_note,
        },
    )
    log_user_action(
        "期间反结账",
        f"{text}：删除结转凭证 {len(removed)} 张，操作人 {operator}"
        + (f"，原因：{reason}" if reason else ""),
    )
    return {
        "ok": True,
        "period": text,
        "status": str(updated.get("status") or "未结账"),
        "removed_entries": removed,
        "snapshot_hash": "",
        "warnings": [] if reason else ["未填写反结账原因，建议在备注中说明调整事项。"],
    }


# =============================================================================
# 季度结账 / 反结账
# =============================================================================
def close_quarter(
    period: Any,
    *,
    operator: str = "system",
    force: bool = False,
    note: str = "",
) -> dict[str, Any]:
    """结账一个季度：要求季度内 3 个月均已结账，写季度状态与快照（不重复生成结转凭证）。"""
    quarter = periods.normalize_period(period)
    if periods.period_type_of(quarter) != "季":
        raise ValueError(f"{quarter} 不是季度期间，请使用 close_period 进行月度结账。")

    months = _quarter_months(quarter)
    statuses = {month: _row_status(month) for month in months}
    missing = [month for month, status in statuses.items() if status not in CLOSED_STATUSES]
    if missing:
        raise ValueError(
            f"季度结账要求季度内 3 个月均已结账，以下月份未结账：{'、'.join(missing)}。"
        )
    current_status = _row_status(quarter)
    if current_status in CLOSED_STATUSES:
        raise ValueError(f"季度 {quarter} 当前状态为「{current_status}」，请勿重复结账。")

    report = checks_module.pre_close_checks(quarter)
    if report["blocking"]:
        messages = "；".join(check["message"] for check in report["blocking"])
        raise ValueError(f"无法季度结账：{messages}")

    snapshot = snapshot_module.period_snapshot(quarter)
    now = config.now_text()
    previous_note = str((periods.find_period_row(quarter) or {}).get("note") or "")
    merged_note = "；".join(part for part in (previous_note, note) if part)[:500]
    updated = _update_period(
        quarter,
        {
            "status": "已结账",
            "closed_at": now,
            "closed_by": operator,
            "reopened_at": "",
            "snapshot_hash": snapshot["hash"],
            "snapshot_json": snapshot,
            "note": merged_note,
        },
    )
    log_user_action(
        "季度结账",
        f"{quarter}：3 个月均已结账（{'、'.join(months)}），快照 {snapshot['hash'][:16]}…，操作人 {operator}",
    )
    return {
        "ok": True,
        "period": quarter,
        "period_type": "季",
        "status": str(updated.get("status") or "已结账"),
        "months": statuses,
        "snapshot_hash": snapshot["hash"],
        "snapshot": snapshot,
        "checks": report,
    }


def reopen_quarter(
    period: Any,
    *,
    operator: str = "system",
    reason: str = "",
) -> dict[str, Any]:
    """反结账一个季度：仅最近已结账季度可反结，月度状态保持不变。"""
    quarter = periods.normalize_period(period)
    if periods.period_type_of(quarter) != "季":
        raise ValueError(f"{quarter} 不是季度期间，请使用 reopen_period 进行月度反结账。")
    row = periods.find_period_row(quarter)
    status = str((row or {}).get("status") or "未结账")
    if row is None or status == "未结账":
        raise ValueError(f"季度 {quarter} 未结账，无需反结账。")

    closed_quarters = [
        item
        for item in periods.list_periods(kind="季")
        if item["status"] in CLOSED_STATUSES
    ]
    closed_quarters.sort(key=lambda item: item["start"])
    latest_quarter = closed_quarters[-1]["period"] if closed_quarters else ""
    if latest_quarter and latest_quarter != quarter:
        raise ValueError(
            f"只能反结账最近一个已结账季度：当前最近已结账季度为 {latest_quarter}，不能反结 {quarter}。"
        )
    latest_month = locks.latest_closed_month()
    last_month = _quarter_last_month(quarter)
    if latest_month and last_month and latest_month > last_month:
        raise ValueError(f"存在更晚的已结账期间 {latest_month}，只能反结最近一期。")

    now = config.now_text()
    previous_note = str(row.get("note") or "")
    merged_note = "；".join(
        part for part in (previous_note, f"季度反结账：{reason}" if reason else "") if part
    )[:500]
    updated = _update_period(
        quarter,
        {
            "status": "未结账",
            "reopened_at": now,
            "snapshot_hash": "",
            "snapshot_json": {},
            "note": merged_note,
        },
    )
    log_user_action(
        "季度反结账",
        f"{quarter}：季度状态回「未结账」（月度状态不变），操作人 {operator}"
        + (f"，原因：{reason}" if reason else ""),
    )
    return {
        "ok": True,
        "period": quarter,
        "status": str(updated.get("status") or "未结账"),
        "snapshot_hash": "",
        "months_unchanged": True,
        "warnings": [] if reason else ["未填写反结账原因，建议在备注中说明调整事项。"],
    }


__all__ = [
    "INCOME_ACCOUNTS",
    "EXPENSE_ACCOUNTS",
    "EXPENSE_PREFIXES",
    "PROFIT_ACCOUNT",
    "is_closed",
    "closing_entries",
    "close_period",
    "reopen_period",
    "close_quarter",
    "reopen_quarter",
]
