"""印花税计算（确定性代码）。

口径：印花税按合同所列金额（不含增值税）乘以适用税率；小规模纳税人享受「六税两费」
减半征收（财政部 税务总局公告2023年第12号），不含证券交易印花税。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

import config
from engine.money import pct, round2, yuan
from engine.policy_params import get_params

DISCLAIMER = config.DISCLAIMER


def calc_stamp_tax(
    contracts: Iterable[dict[str, Any]] | None = None,
    *,
    small_scale: bool = True,
    liutax_discount: float | None = None,
    period: str = "",
) -> dict[str, Any]:
    """计算应缴印花税。

    参数
    ----
    contracts : 合同列表，每项至少包含 ``contract_type``（合同类型）与 ``amount``（计税金额，元）；
                可选 ``counterparty``、``contract_no``、``date``、``description``
    small_scale : 是否小规模纳税人（享受六税两费减半）
    liutax_discount : 覆盖减半比例（默认 0.5）

    返回
    ----
    dict：分合同的税额明细、减免前税额、减免额、应缴合计、解释文本。
    """
    params = get_params(period)
    rates: dict[str, float] = dict(params["stamp_tax_rates"])
    discount = Decimal(str(liutax_discount if liutax_discount is not None else params["liutax_liangfei_discount"]))
    if not small_scale:
        discount = Decimal("1")

    warnings: list[str] = list(params.get("warnings") or [])
    items: list[dict[str, Any]] = []
    total_before = Decimal("0.00")
    total_after = Decimal("0.00")

    for index, contract in enumerate(contracts or [], start=1):
        contract_type = str(contract.get("contract_type") or "").strip()
        amount = Decimal(str(round2(contract.get("amount"))))
        if amount < 0:
            warnings.append(f"第 {index} 份合同金额为负数，已跳过。")
            continue
        rate = rates.get(contract_type)
        if rate is None:
            for known, known_rate in rates.items():
                if known and (known in contract_type or contract_type in known):
                    contract_type, rate = known, known_rate
                    break
        if rate is None:
            warnings.append(f"未知合同类型「{contract_type}」，已跳过第 {index} 份合同，请核对。")
            continue
        tax_before = (amount * Decimal(str(rate))).quantize(Decimal("0.01"))
        tax_after = (tax_before * discount).quantize(Decimal("0.01"))
        total_before += tax_before
        total_after += tax_after
        items.append(
            {
                "contract_type": contract_type,
                "contract_no": str(contract.get("contract_no") or ""),
                "counterparty": str(contract.get("counterparty") or ""),
                "date": str(contract.get("date") or ""),
                "amount": round2(amount),
                "rate": rate,
                "rate_text": f"{rate * 1000:.2f}‰",
                "tax_before_discount": round2(tax_before),
                "discount_amount": round2(tax_before - tax_after),
                "tax_payable": round2(tax_after),
            }
        )

    total_before_f = round2(total_before)
    total_after_f = round2(total_after)
    discount_f = round2(total_before - total_after)
    note = (
        f"共 {len(items)} 份合同，计税金额合计 {yuan(sum(item['amount'] for item in items))} 元，"
        f"适用税率税额 {yuan(total_before_f)} 元；"
        + (
            f"小规模纳税人「六税两费」减半征收，减免 {yuan(discount_f)} 元，应缴印花税 {yuan(total_after_f)} 元。"
            if discount_f > 0
            else f"应缴印花税 {yuan(total_after_f)} 元。"
        )
    )
    if not items:
        note = "本期无合同台账记录，无需缴纳印花税（若已签订应税合同，请先登记合同台账）。"

    return {
        "period": period,
        "small_scale": bool(small_scale),
        "items": items,
        "total_amount": round2(sum(item["amount"] for item in items)),
        "total_before_discount": total_before_f,
        "discount_amount": discount_f,
        "total_payable": total_after_f,
        "discount_ratio": float(discount),
        "rates": {key: f"{value * 1000:.2f}‰" for key, value in rates.items()},
        "note": note,
        "warnings": warnings,
        "policy_version": params["policy_version"],
        "last_verified": params["last_verified"],
        "disclaimer": DISCLAIMER,
    }
