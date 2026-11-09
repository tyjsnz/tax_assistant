"""审计日志：算税、分录、申报、政策检查与关键用户操作统一写入 ``logs/*.csv``。

设计要点
--------
* 所有写日志动作最终都调用 :func:`ledger.store.append_log`，日志列定义见 ``store.LOG_DEFS``。
* 日志写入失败不影响主流程（``append_log`` 内部已捕获 ``OSError``）。
* 复杂结构（输入参数、计算结果）以 JSON 字符串落盘，便于事后追溯；金额统一 2 位小数。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ledger.store import LOG_DEFS, append_log, read_log


def _jsonable(value: Any, depth: int = 0) -> Any:
    """把任意值递归转换为可 JSON 序列化的普通结构（限制深度，避免日志爆炸）。"""
    if depth > 6:
        return str(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item, depth + 1) for item in value]
    return str(value)


def log_calculation(
    calc_type: str,
    period: str,
    inputs: dict[str, Any] | None,
    result: dict[str, Any] | None,
    operator: str = "system",
) -> None:
    """记录一次算税计算（算税类型、所属期间、输入参数与结果快照）。"""
    append_log(
        "calculation_log",
        {
            "calc_type": str(calc_type or ""),
            "period": str(period or ""),
            "inputs": _jsonable(inputs or {}),
            "result": _jsonable(result or {}),
            "operator": str(operator or "system"),
        },
    )


def log_journal(entries: list[dict[str, Any]] | None, source: str = "") -> None:
    """记录生成的记账凭证：每张凭证写一行，含借贷科目摘要与金额。"""
    for entry in entries or []:
        lines = entry.get("lines") or []
        debit = "、".join(
            str(line.get("account") or "") for line in lines if str(line.get("direction")) == "借"
        )
        credit = "、".join(
            str(line.get("account") or "") for line in lines if str(line.get("direction")) == "贷"
        )
        append_log(
            "journal_log",
            {
                "entry_no": str(entry.get("entry_no") or ""),
                "description": str(entry.get("description") or ""),
                "debit": debit,
                "credit": credit,
                "amount": round(float(entry.get("total_amount") or 0), 2),
                "source": str(entry.get("source_ref") or source or ""),
            },
        )


def log_policy_check(
    checked_total: int,
    stale_count: int,
    latest_verified: str,
    detail: str = "",
) -> None:
    """记录一次政策知识库更新检查（检查总数、过期数量、最近核验日期）。"""
    append_log(
        "policy_check_log",
        {
            "checked_total": int(checked_total or 0),
            "stale_count": int(stale_count or 0),
            "latest_verified": str(latest_verified or ""),
            "detail": str(detail or ""),
        },
    )


def log_user_action(action: str, detail: str = "") -> None:
    """记录一次关键用户操作（新增台账、清空数据、加载示例数据、导出等）。"""
    append_log(
        "user_action_log",
        {"action": str(action or ""), "detail": str(detail or "")},
    )


def log_filing(
    period: str,
    form_id: str,
    form_name: str,
    action: str,
    status: str,
) -> None:
    """记录一次申报表操作（保存草稿、状态变更、标记已申报等）。"""
    append_log(
        "filing_log",
        {
            "period": str(period or ""),
            "form_id": str(form_id or ""),
            "form_name": str(form_name or ""),
            "action": str(action or ""),
            "status": str(status or ""),
        },
    )


def recent_logs(limit: int = 20) -> dict[str, list[dict[str, Any]]]:
    """读取各类审计日志最近若干条（新的在前），供界面展示与追溯。"""
    return {name: read_log(name, limit=limit) for name in LOG_DEFS}


__all__ = [
    "log_calculation",
    "log_journal",
    "log_policy_check",
    "log_user_action",
    "log_filing",
    "recent_logs",
]
