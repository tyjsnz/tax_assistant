"""金额、日期、百分比与状态的展示格式化。

界面统一口径：金额 ``¥1,234.56``，百分比 ``1.00%``，日期 ``2026-01-05``。
所有函数对 ``None`` / 空字符串 / 非法值都安全返回 ``—`` 或 0，绝不抛出异常。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

PLACEHOLDER = "—"

_STATUS_ICONS = {
    "PASS": "✅",
    "WARNING": "⚠️",
    "ERROR": "❌",
    "草稿": "📝",
    "已确认": "✅",
    "已申报": "📤",
    "urgent": "🔴",
    "soon": "🟠",
    "normal": "🟢",
    "overdue": "⛔",
}


def to_float(value: Any, default: float = 0.0) -> float:
    """安全转换为 float。"""
    if value is None or value == "":
        return default
    try:
        return float(str(value).replace(",", "").replace("¥", "").strip())
    except (TypeError, ValueError):
        return default


def fmt_amount(value: Any) -> str:
    """金额千分位（不带币种），如 ``1,234.56``；空值返回 ``—``。"""
    if value is None or value == "":
        return PLACEHOLDER
    return f"{to_float(value):,.2f}"


def fmt_money(value: Any) -> str:
    """金额千分位带币种，如 ``¥1,234.56``。"""
    if value is None or value == "":
        return PLACEHOLDER
    return f"¥{to_float(value):,.2f}"


def fmt_money_short(value: Any) -> str:
    """大额金额简写（元/万元），用于指标卡。"""
    amount = to_float(value)
    if abs(amount) >= 100_000_000:
        return f"¥{amount / 100_000_000:,.2f} 亿"
    if abs(amount) >= 100_000:
        return f"¥{amount / 10_000:,.2f} 万"
    return fmt_money(amount)


def fmt_pct(value: Any, digits: int = 2) -> str:
    """小数转百分比，如 0.01 → ``1.00%``。"""
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return PLACEHOLDER


def fmt_date(value: Any) -> str:
    """规范为 ``YYYY-MM-DD``；非法返回 ``—``。"""
    if value is None or value == "":
        return PLACEHOLDER
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def fmt_date_cn(value: Any) -> str:
    """``2026-04-15`` → ``2026年4月15日``。"""
    text = fmt_date(value)
    if text == PLACEHOLDER:
        return PLACEHOLDER
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return text
    return f"{day.year}年{day.month}月{day.day}日"


def fmt_datetime(value: Any) -> str:
    """时间文本安全展示。"""
    if value is None or value == "":
        return PLACEHOLDER
    return str(value)


def status_icon(status: Any) -> str:
    """状态图标（PASS/WARNING/ERROR、申报状态、紧急程度）。"""
    return _STATUS_ICONS.get(str(status or "").strip(), "ℹ️")


def status_alert_level(status: Any) -> str:
    """状态 → Streamlit 提示级别（success / warning / error / info）。"""
    text = str(status or "").strip().upper()
    if text == "PASS":
        return "success"
    if text == "WARNING":
        return "warning"
    if text == "ERROR":
        return "error"
    return "info"


def bool_text(value: Any) -> str:
    """bool → 是/否。"""
    if isinstance(value, str):
        return "是" if value.strip() in {"是", "true", "True", "1"} else "否"
    return "是" if value else "否"


def mask_text(value: Any, keep: int = 1) -> str:
    """文本脱敏（客户名等），如 ``云***``。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= keep:
        return "*" * len(text)
    return text[:keep] + "*" * (len(text) - keep)


def safe_text(value: Any, default: str = PLACEHOLDER) -> str:
    """任意值转展示文本。"""
    if value is None or value == "":
        return default
    return str(value)


def truncate(text: Any, limit: int = 60) -> str:
    """截断过长文本（用于表格摘要）。"""
    value = safe_text(text, "")
    return value if len(value) <= limit else value[: limit - 1] + "…"
