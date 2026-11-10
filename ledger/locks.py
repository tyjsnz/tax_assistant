"""会计期间锁：已结账期间禁止写入台账与凭证。

设计目的
--------
月结/季结后，该期间的收入、费用、接收到的流水与凭证不应再被改动，否则已结账的报表、
申报表与归档包会与账面不一致。本模块提供**轻量、无循环依赖**的期间锁判断，供：

* ``ledger/income_ledger.py`` / ``ledger/expense_ledger.py`` 的新增/修改/删除/导入；
* ``ledger/journal.py`` 的凭证登记；
* ``ledger/rd_auxiliary.py`` 的研发辅助账写入；
* ``ingest/matcher.py`` 的匹配入账；

在写入前调用 :func:`ensure_open`，命中已结账期间时抛出 :class:`PeriodLockedError`。

锁规则
------
1. 目标**月份**自身状态不为「未结账」→ 锁定；
2. 目标月份所属**季度**状态不为「未结账」→ 锁定（季结会锁定季度内所有月份）；
3. 存在**晚于目标月份的已结账期间** → 锁定（禁止在已结账期间之前的期间倒记，保证账务连续性）。

状态读取 ``periods`` 表（由 ``closing`` 模块维护）；表不存在或期间未建记录时视为未锁定。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from engine.tax_calendar import period_range, quarter_of
from ledger import models
from ledger.store import table

PERIOD_TABLE = "periods"

#: 视为「锁定」的期间状态
LOCKED_STATUSES: tuple[str, ...] = tuple(status for status in models.PERIOD_STATUSES if status != "未结账")


class PeriodLockedError(ValueError):
    """期间已结账，禁止写入。"""

    def __init__(self, period: str, action: str = "记账", status: str = "已结账") -> None:
        self.period = period
        self.action = action
        self.status = status
        super().__init__(
            f"期间 {period} 当前状态为「{status}」，禁止{action}。如需修改，请先在「期间结账」中反结账，"
            f"或在未结账期间重新登记。"
        )


# =============================================================================
# 期间解析
# =============================================================================
def month_key(value: str | date | None) -> str:
    """把日期/期间规范为月份键 ``2026-01``。"""
    if isinstance(value, date):
        return f"{value.year}-{value.month:02d}"
    text = str(value or "").strip()
    if not text:
        return f"{date.today().year}-{date.today().month:02d}"
    if "Q" in text.upper() or "季" in text:
        start, _ = period_range(models.normalize_period(text))
        return f"{start.year}-{start.month:02d}"
    normalized = models.normalize_period(text, default="")
    if normalized.count("-") == 1:
        return normalized
    if normalized.isdigit() and len(normalized) == 4:
        return f"{normalized}-01"
    return models.normalize_date(text)[:7]


def quarter_key(value: str | date | None) -> str:
    """把日期/期间规范为季度键 ``2026Q1``。"""
    if isinstance(value, date):
        return quarter_of(value)
    text = str(value or "").strip()
    if "Q" in text.upper():
        return models.normalize_period(text)
    if text.count("-") == 1:
        return quarter_of(f"{text}-01")
    if text.isdigit() and len(text) == 4:
        return f"{text}Q1"
    return quarter_of(text or date.today())


def period_key(value: str | date | None) -> str:
    """通用期间键：季度输入返回 ``2026Q1``，日期/月份输入返回 ``2026-01``。"""
    text = str(value or "").strip()
    if isinstance(value, date) or (text and "Q" not in text.upper() and "季" not in text):
        return month_key(value)
    return quarter_key(value)


# =============================================================================
# 状态读取
# =============================================================================
def _period_rows() -> list[dict[str, Any]]:
    try:
        return table(PERIOD_TABLE).read()
    except Exception:  # noqa: BLE001 - 期间表未初始化时视为未锁定
        return []


def status_map() -> dict[str, str]:
    return {str(row.get("period") or ""): str(row.get("status") or "未结账") for row in _period_rows()}


def latest_closed_month() -> str | None:
    """返回最新的已结账月份（含季度结账覆盖的月份）。"""
    closed: list[str] = []
    for row in _period_rows():
        status = str(row.get("status") or "未结账")
        if status not in LOCKED_STATUSES:
            continue
        key = str(row.get("period") or "")
        if not key:
            continue
        if "Q" in key.upper():
            start, end = period_range(key)
            cursor = start
            while cursor <= end:
                closed.append(f"{cursor.year}-{cursor.month:02d}")
                cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        else:
            closed.append(month_key(key))
    return max(closed) if closed else None


def is_locked(period_or_date: str | date | None) -> bool:
    """判断某期间/日期是否已锁定。"""
    target_month = month_key(period_or_date)
    target_quarter = quarter_of(f"{target_month}-01")
    statuses = status_map()

    own_status = statuses.get(target_month, "未结账")
    if own_status in LOCKED_STATUSES:
        return True
    quarter_status = statuses.get(target_quarter, "未结账")
    if quarter_status in LOCKED_STATUSES:
        return True

    latest = latest_closed_month()
    if latest and target_month < latest:
        return True
    return False


def status_of(period_or_date: str | date | None) -> str:
    """返回期间状态：优先月份状态，其次季度状态，否则「未结账」。"""
    target_month = month_key(period_or_date)
    statuses = status_map()
    if target_month in statuses:
        return statuses[target_month]
    quarter = quarter_of(f"{target_month}-01")
    return statuses.get(quarter, "未结账")


def ensure_open(period_or_date: str | date | None, action: str = "记账") -> None:
    """写入前校验：期间已锁定则抛 :class:`PeriodLockedError`。"""
    target_month = month_key(period_or_date)
    if not is_locked(target_month):
        return
    raise PeriodLockedError(target_month, action=action, status=status_of(target_month))


# =============================================================================
# 汇总（UI / Agent 展示）
# =============================================================================
def locked_periods() -> list[dict[str, Any]]:
    rows = [row for row in _period_rows() if str(row.get("status") or "") in LOCKED_STATUSES]
    rows.sort(key=lambda row: str(row.get("period") or ""))
    return rows


def lock_summary() -> dict[str, Any]:
    """期间锁概览：最近已结账月份、当前是否可记账、锁定期间列表。"""
    latest = latest_closed_month()
    today = date.today()
    current = f"{today.year}-{today.month:02d}"
    return {
        "latest_closed_month": latest,
        "current_month": current,
        "current_locked": is_locked(current),
        "locked_periods": [
            {
                "period": str(row.get("period") or ""),
                "status": str(row.get("status") or ""),
                "closed_at": str(row.get("closed_at") or ""),
            }
            for row in locked_periods()
        ],
        "disclaimer": "已结账期间的数据已锁定，如需调整请先反结账。",
    }
