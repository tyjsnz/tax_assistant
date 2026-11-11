"""逾期申报滞纳金试算（v1.18，确定性代码）。

依据《税收征收管理法》及其实施细则：从滞纳税款之日起，按日加收滞纳税款
万分之五（0.05%）的滞纳金。公式：滞纳金 = 滞纳税款 × 0.05% × 滞纳天数。
仅用于风险提示，实际金额以主管税务机关核定为准。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

DAILY_RATE = Decimal("0.0005")
DISCLAIMER = "⚠️ 滞纳金试算仅供参考，实际以主管税务机关核定为准。"


def _parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def calc_late_fee(
    tax_amount: Any,
    due_date: Any,
    paid_date: Any | None = None,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """计算滞纳金；``paid_date`` 为空时按今天计算（用于预估）。"""
    due = _parse_date(due_date)
    if due is None:
        raise ValueError("请填写正确的税款缴纳期限")
    paid = _parse_date(paid_date) or today or date.today()
    days = max(0, (paid - due).days)
    try:
        amount = Decimal(str(tax_amount or 0))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("税款金额格式不正确") from exc
    if amount < 0:
        raise ValueError("税款金额不能为负数")
    fee = (amount * DAILY_RATE * Decimal(days)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "tax_amount": float(amount),
        "due_date": due.isoformat(),
        "paid_date": paid.isoformat(),
        "days": days,
        "daily_rate": float(DAILY_RATE),
        "late_fee": float(fee),
        "total": float((amount + fee).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "disclaimer": DISCLAIMER,
    }


__all__ = ["calc_late_fee", "DAILY_RATE", "DISCLAIMER"]
