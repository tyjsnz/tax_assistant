"""会计期间（月/季）状态与派生数据：结账状态机的读入口。

职责
----
* 期间键规范化与类型判断（月 ``2026-01`` / 季 ``2026Q1``）；
* ``periods`` 表的创建（``ensure_period``）与查询（``get_period`` / ``list_periods``）；
* 为 UI / CLI 提供派生数据：收入、费用、税额、利润、凭证数与锁定状态。

结账状态的**唯一写入口**在 :mod:`closing.close`；本模块只负责读与期间初始化。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

import config
from engine import calc_vat, period_label, period_range
from engine.money import round2
from ledger import locks, models
from ledger.expense_ledger import period_totals as expense_period_totals
from ledger.income_ledger import period_totals as income_period_totals
from ledger.store import table

TABLE_NAME = "periods"
QUARTER_RE = re.compile(r"^\d{4}Q[1-4]$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

#: 视为「已关闭」的期间状态
CLOSED_STATUSES: tuple[str, ...] = tuple(
    status for status in models.PERIOD_STATUSES if status != "未结账"
)


# =============================================================================
# 期间键
# =============================================================================
def normalize_period(period: Any) -> str:
    """把期间/日期规范为 ``2026-01`` 或 ``2026Q1``；无法识别抛 ``ValueError``。"""
    if isinstance(period, (date, datetime)):
        text = locks.month_key(period)
        return text
    raw = str(period or "").strip()
    if not raw:
        raise ValueError("请提供会计期间，如 2026-01（月）或 2026Q1（季）")
    text = models.normalize_period(raw, default="")
    if QUARTER_RE.match(text):
        return text
    if MONTH_RE.match(text):
        return text
    if text.isdigit() and len(text) == 4:
        return f"{text}-01"
    raise ValueError(f"期间格式不正确：{period}，请使用 2026-01（月）或 2026Q1（季）")


def period_type_of(period: Any) -> str:
    """判断期间类型：``季`` 或 ``月``。"""
    if isinstance(period, (date, datetime)):
        return "月"
    text = str(period or "").strip().upper()
    if "Q" in text:
        return "季"
    return "月"


def current_period(today: Any = None) -> str:
    """当前月份键，如 ``2026-11``。"""
    if today is None:
        current = config.today()
    elif isinstance(today, datetime):
        current = today.date()
    elif isinstance(today, date):
        current = today
    else:
        current = date.fromisoformat(str(today)[:10])
    return f"{current.year}-{current.month:02d}"


def period_bounds(period: Any) -> tuple[str, str, str]:
    """返回 ``(规范化期间, 起始日, 结束日)``。"""
    text = normalize_period(period)
    try:
        start, end = period_range(text)
    except ValueError as exc:
        raise ValueError(f"期间格式不正确：{period}") from exc
    return text, start.isoformat(), end.isoformat()


# =============================================================================
# 期间行读写
# =============================================================================
def _period_rows() -> list[dict[str, Any]]:
    return table(TABLE_NAME).read()


def find_period_row(period: Any) -> dict[str, Any] | None:
    """按期间键读取 ``periods`` 原始行（供本包与其他读接口使用）。"""
    text = normalize_period(period)
    for row in _period_rows():
        if str(row.get("period") or "") == text:
            return row
    return None


def ensure_period(period: Any) -> dict[str, Any]:
    """确保期间在 ``periods`` 表存在（不存在则以「未结账」状态创建），返回原始行。"""
    text = normalize_period(period)
    row = find_period_row(text)
    if row is not None:
        return row
    created = table(TABLE_NAME).append(
        {
            "period": text,
            "period_type": period_type_of(text),
            "status": "未结账",
            "snapshot_hash": "",
            "snapshot_json": {},
            "note": "",
        }
    )
    return created


# =============================================================================
# 派生数据
# =============================================================================
def _period_metrics(period: str) -> dict[str, Any]:
    """计算期间的收入/费用/税额/利润/笔数等派生数据。"""
    text, start, end = period_bounds(period)
    income = income_period_totals(text)
    expense = expense_period_totals(text)
    calc = calc_vat(income["total_inclusive"], income["special_inclusive"], period=text)
    counts = {
        "income": int(income.get("count") or 0),
        "expense": int(expense.get("count") or 0),
        "journal_entries": len(table("journal_entries").filter(start=start, end=end)),
        "invoices": len(table("invoices").filter(start=start, end=end)),
        "transactions": len(table("bank_transactions").filter(start=start, end=end)),
    }
    return {
        "start": start,
        "end": end,
        "income_inclusive": round2(income["total_inclusive"]),
        "income_exclusive": round2(income["total_exclusive"]),
        "vat": round2(calc["vat_payable"]),
        "surtax": round2(calc["surtax"]),
        "expense": round2(expense["total"]),
        "profit": round2(income["total_exclusive"] - expense["total"]),
        "counts": counts,
    }


def _derive_period(period: str, row: dict[str, Any] | None = None) -> dict[str, Any]:
    """把期间行 + 派生数据组装为对外结构。"""
    text = normalize_period(period)
    metrics = _period_metrics(text)
    row = row or {}
    status = str(row.get("status") or "未结账")
    return {
        "id": str(row.get("id") or ""),
        "period": text,
        "period_label": period_label(text),
        "period_type": str(row.get("period_type") or period_type_of(text)),
        "status": status,
        "closed_at": str(row.get("closed_at") or ""),
        "closed_by": str(row.get("closed_by") or ""),
        "reopened_at": str(row.get("reopened_at") or ""),
        "snapshot_hash": str(row.get("snapshot_hash") or ""),
        "note": str(row.get("note") or ""),
        "updated_at": str(row.get("updated_at") or ""),
        "locked": locks.is_locked(text),
        "start": metrics["start"],
        "end": metrics["end"],
        "income_inclusive": metrics["income_inclusive"],
        "income_exclusive": metrics["income_exclusive"],
        "vat": metrics["vat"],
        "surtax": metrics["surtax"],
        "expense": metrics["expense"],
        "profit": metrics["profit"],
        "counts": metrics["counts"],
    }


def _candidate_periods() -> list[str]:
    """收集需要展示的期间：已登记期间 + 台账数据出现的月份 + 当前月份。"""
    candidates: set[str] = set()
    for row in _period_rows():
        try:
            candidates.add(normalize_period(row.get("period")))
        except ValueError:
            continue
    start_months: set[str] = set()
    for table_name, date_column in (
        ("income", "date"),
        ("expense", "date"),
        ("journal_entries", "date"),
    ):
        for row in table(table_name).read():
            value = str(row.get(date_column) or "")[:7]
            if MONTH_RE.match(value):
                start_months.add(value)
    candidates.update(start_months)
    try:
        candidates.add(current_period())
    except (ValueError, OSError):
        pass
    return sorted(candidates)


def list_periods(kind: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    """列出具期间状态与派生数据（新的在前），可按 ``月`` / ``季`` 过滤并限制条数。"""
    type_filter = str(kind or "").strip()
    if type_filter and type_filter not in models.PERIOD_TYPES:
        raise ValueError(f"期间类型「{type_filter}」不合法，可选：{'、'.join(models.PERIOD_TYPES)}")
    rows = {str(row.get("period") or ""): row for row in _period_rows()}
    results: list[dict[str, Any]] = []
    for period in _candidate_periods():
        row = rows.get(period) or {}
        if type_filter and str(row.get("period_type") or period_type_of(period)) != type_filter:
            continue
        results.append(_derive_period(period, row))

    def _sort_key(item: dict[str, Any]) -> tuple[int, int]:
        start, _ = period_range(item["period"])
        return (start.toordinal(), 0 if item["period_type"] == "月" else 1)

    results.sort(key=_sort_key, reverse=True)
    if limit is not None and int(limit) >= 0:
        results = results[: int(limit)]
    return results


def get_period(period: Any) -> dict[str, Any] | None:
    """读取单个期间的派生数据；期间未登记时返回 ``None``。"""
    row = find_period_row(period)
    if row is None:
        return None
    return _derive_period(normalize_period(period), row)


def period_overview(period: Any) -> dict[str, Any]:
    """期间总览：派生数据 + 结账前检查摘要（未登记期间返回虚拟「未结账」期间）。"""
    text = normalize_period(period)
    row = find_period_row(text)
    overview = _derive_period(text, row or {})
    try:
        from closing.checks import pre_close_checks

        report = pre_close_checks(text)
        checklist = {
            "ok": bool(report["ok"]),
            "level": report["level"],
            "total": len(report["checks"]),
            "pass": sum(1 for check in report["checks"] if check["status"] == "PASS"),
            "warning": len(report["warnings"]),
            "error": len(report["blocking"]),
            "generated_at": report["generated_at"],
        }
    except (ValueError, KeyError) as exc:  # 检查失败不阻塞总览展示
        checklist = {
            "ok": False,
            "level": "WARNING",
            "total": 0,
            "pass": 0,
            "warning": 1,
            "error": 0,
            "generated_at": config.now_text(),
            "message": f"结账前检查执行失败：{exc}",
        }
    overview["checklist"] = checklist
    return overview


__all__ = [
    "TABLE_NAME",
    "CLOSED_STATUSES",
    "normalize_period",
    "period_type_of",
    "current_period",
    "period_bounds",
    "find_period_row",
    "ensure_period",
    "list_periods",
    "get_period",
    "period_overview",
]
