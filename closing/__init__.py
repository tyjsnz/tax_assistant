"""月结/季结状态机与期间快照：可审计地关账。

模块职责
--------
* :mod:`closing.periods`  期间键、``periods`` 状态与派生数据（收入/费用/利润/笔数/锁定状态）；
* :mod:`closing.checks`   结账前 9 项固定检查（未来期间、重复结账、上期未结、凭证平衡、零活动、
  零元凭证、待匹配票据、重复发票、政策区间）；
* :mod:`closing.close`    结账/反结账、损益结转凭证、季度结账（写期间状态的唯一入口）；
* :mod:`closing.snapshot` 可复现 SHA-256 快照与篡改检测。

已结账期间由 :mod:`ledger.locks` 锁定，台账/凭证/研发/匹配入账等写路径会被拒绝，需先反结账。
"""

from __future__ import annotations

from closing import checks, close, periods, snapshot
from closing.checks import CHECK_KEYS, CHECK_NAMES, pre_close_checks
from closing.close import (
    close_period,
    close_quarter,
    closing_entries,
    is_closed,
    reopen_period,
    reopen_quarter,
)
from closing.periods import (
    CLOSED_STATUSES,
    current_period,
    ensure_period,
    find_period_row,
    get_period,
    list_periods,
    normalize_period,
    period_bounds,
    period_overview,
    period_type_of,
)
from closing.snapshot import canonical_json, period_snapshot, verify_snapshot

__all__ = [
    # 模块
    "periods",
    "checks",
    "close",
    "snapshot",
    # 期间
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
    # 检查
    "CHECK_KEYS",
    "CHECK_NAMES",
    "pre_close_checks",
    # 结账状态机
    "is_closed",
    "closing_entries",
    "close_period",
    "reopen_period",
    "close_quarter",
    "reopen_quarter",
    # 快照
    "canonical_json",
    "period_snapshot",
    "verify_snapshot",
]
