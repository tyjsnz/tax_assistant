"""金额与数值工具：统一使用 Decimal + 四舍五入（ROUND_HALF_UP）保留 2 位小数。

税务金额计算必须可复现、可审计，禁止使用浮点噪声累积。所有引擎内部计算使用
``Decimal``，仅在对外输出字典时转换为 ``float``。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

TWO_PLACES = Decimal("0.01")


def dec(value: Any) -> Decimal:
    """安全转换为 Decimal（None / 空字符串 → 0）。"""
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, AttributeError):
        return Decimal("0")


def round2(value: Any) -> float:
    """四舍五入到 2 位小数，返回 float（用于输出与存储）。"""
    try:
        return float(dec(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return 0.0


def round2d(value: Any) -> Decimal:
    """四舍五入到 2 位小数，返回 Decimal（用于引擎内部继续计算）。"""
    return dec(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def money(value: Any) -> float:
    """金额规范化别名（四舍五入到 2 位小数）。"""
    return round2(value)


def yuan(value: Any) -> str:
    """格式化为带千分位的金额字符串，如 ``1,019.80``。"""
    amount = round2(value)
    return f"{amount:,.2f}"


def yuan_cn(value: Any) -> str:
    """格式化为 ``¥1,019.80``。"""
    return f"¥{yuan(value)}"


def pct(value: Any, digits: int = 2) -> str:
    """把小数格式化为百分比，如 0.01 → ``1.00%``。"""
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "0.00%"


def safe_div(numerator: Any, denominator: Any, default: float = 0.0) -> float:
    """安全除法，分母为 0 时返回 default。"""
    try:
        d = dec(denominator)
        if d == 0:
            return default
        return float(dec(numerator) / d)
    except (InvalidOperation, ZeroDivisionError):
        return default


def clamp(value: Any, low: float | None = None, high: float | None = None) -> float:
    result = round2(value)
    if low is not None and result < low:
        result = low
    if high is not None and result > high:
        result = high
    return result


def sum_money(values: Any) -> float:
    """对可迭代金额求和并保留 2 位小数。"""
    total = Decimal("0")
    for value in values:
        total += dec(value)
    return round2(total)
