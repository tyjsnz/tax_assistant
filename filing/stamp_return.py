"""印花税申报表生成：合同台账 → :func:`engine.calc_stamp_tax` → 申报表栏次。

口径
----
* 计税依据为合同所列金额（不含增值税），按合同类型适用税率（技术合同/买卖合同 0.3‰ 等）。
* 小规模纳税人享受「六税两费」减半征收（财政部 税务总局公告2023年第12号）。
* 行次按合同类型列示计税金额、税率、应纳税额、减免额与应缴税额，并附逐份合同明细。
"""

from __future__ import annotations

from typing import Any

import config
from engine import calc_stamp_tax, period_label, round2, yuan
from ledger.audit import log_calculation
from ledger.reports import list_contracts, normalize_quarter, period_bounds
from filing.guide import build_guide
from filing.validator import validate_bundle

FORM_ID = "STAMP_QUARTERLY"
FORM_NAME = "印花税申报表（按季）"
CATEGORY = "印花税"
FREQUENCY = "quarterly"
PERIOD_KIND = "quarter"


# =============================================================================
# 内部工具
# =============================================================================
def _line(
    line_no: str,
    name: str,
    value: Any,
    source: str,
    rule: str,
    *,
    highlight: bool = False,
) -> dict[str, Any]:
    amount = round2(value)
    return {
        "line_no": str(line_no),
        "name": name,
        "value": amount,
        "value_text": yuan(amount),
        "source": source,
        "rule": rule,
        "highlight": highlight,
    }


def _group_by_type(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把逐份合同税额按合同类型汇总。"""
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        contract_type = str(item.get("contract_type") or "其他")
        bucket = grouped.setdefault(
            contract_type,
            {
                "contract_type": contract_type,
                "count": 0,
                "amount": 0.0,
                "rate_text": item.get("rate_text") or "",
                "tax_before": 0.0,
                "discount": 0.0,
                "tax_payable": 0.0,
            },
        )
        bucket["count"] = int(bucket["count"]) + 1
        bucket["amount"] = round2(bucket["amount"] + round2(item.get("amount")))
        bucket["tax_before"] = round2(bucket["tax_before"] + round2(item.get("tax_before_discount")))
        bucket["discount"] = round2(bucket["discount"] + round2(item.get("discount_amount")))
        bucket["tax_payable"] = round2(bucket["tax_payable"] + round2(item.get("tax_payable")))
    return sorted(grouped.values(), key=lambda bucket: bucket["tax_payable"], reverse=True)


def _compute(period: Any) -> dict[str, Any]:
    """计算印花税申报所需数据（供构建与校验复用）。"""
    quarter = normalize_quarter(period)
    text, start, end = period_bounds(quarter)
    contracts = list_contracts(start=start, end=end)
    calc = calc_stamp_tax(contracts, period=quarter)
    groups = _group_by_type(calc.get("items") or [])
    return {
        "period": quarter,
        "period_label": period_label(quarter),
        "start": start,
        "end": end,
        "contracts": contracts,
        "calc": calc,
        "by_type": groups,
        "expected_totals": {
            "total_amount": round2(calc["total_amount"]),
            "total_before_discount": round2(calc["total_before_discount"]),
            "discount_amount": round2(calc["discount_amount"]),
            "total_payable": round2(calc["total_payable"]),
            "contract_count": len(calc.get("items") or []),
        },
    }


def _summary_lines(computed: dict[str, Any]) -> list[dict[str, Any]]:
    calc = computed["calc"]
    return [
        _line("1", "计税金额合计", calc["total_amount"], f"合同台账 {len(calc.get('items') or [])} 份", "按合同类型汇总计税金额"),
        _line("2", "减免前应纳税额", calc["total_before_discount"], "各合同计税金额 × 适用税率", "技术合同/买卖合同 0.3‰，租赁合同 1‰ 等"),
        _line("3", "减免税额", calc["discount_amount"], "六税两费减半征收（小规模纳税人）", "减免比例 50%"),
        _line("4", "本期应补（退）税额", calc["total_payable"], "减免前应纳税额 - 减免税额", "本期应缴纳印花税", highlight=True),
    ]


# =============================================================================
# 构建
# =============================================================================
def build_stamp_return(period: Any) -> dict[str, Any]:
    """生成印花税申报表 bundle（按合同类型汇总 + 逐份合同明细）。"""
    computed = _compute(period)
    calc = computed["calc"]
    expected = computed["expected_totals"]
    type_lines = [
        _line(
            str(index),
            f"{group['contract_type']}（{group['count']} 份）",
            group["tax_payable"],
            f"计税金额 {yuan(group['amount'])} 元",
            f"税率 {group['rate_text']}；减免前税额 {yuan(group['tax_before'])} 元，"
            f"减免 {yuan(group['discount'])} 元",
        )
        for index, group in enumerate(computed["by_type"], start=1)
    ]
    detail_lines = [
        _line(
            str(index),
            f"{item.get('counterparty') or '（未填对方单位）'} · {item.get('contract_type')}",
            item.get("tax_payable"),
            f"合同编号 {item.get('contract_no') or '—'}；签订日期 {item.get('date') or '—'}；"
            f"计税金额 {yuan(item.get('amount'))} 元",
            f"税率 {item.get('rate_text') or '—'}；减免前 {yuan(item.get('tax_before_discount'))} 元，"
            f"减免 {yuan(item.get('discount_amount'))} 元",
        )
        for index, item in enumerate(calc.get("items") or [], start=1)
    ]
    if not type_lines:
        type_lines.append(
            _line("1", "本期无应税合同", 0.0, "合同台账为空", "如已签订应税合同，请先在合同台账登记")
        )
    bundle: dict[str, Any] = {
        "form_id": FORM_ID,
        "form_name": FORM_NAME,
        "category": CATEGORY,
        "period": computed["period"],
        "period_label": computed["period_label"],
        "frequency": FREQUENCY,
        "generated_at": config.now_text(),
        "status": "草稿",
        "sections": [
            {"title": "主表（按合同类型汇总）", "lines": type_lines},
            {"title": "合同明细", "lines": detail_lines},
            {"title": "汇总", "lines": _summary_lines(computed)},
        ],
        "totals": {
            **expected,
            "small_scale": calc["small_scale"],
            "discount_ratio": calc["discount_ratio"],
            "rates": calc["rates"],
        },
        "calc": calc,
        "checks": [],
        "summary": {"level": "PASS", "text": ""},
        "guide": [],
        "data": {
            "contracts": computed["contracts"],
            "by_type": computed["by_type"],
            "start": computed["start"],
            "end": computed["end"],
            "source": "ledger",
        },
        "disclaimer": config.DISCLAIMER,
    }
    log_calculation(
        "印花税",
        computed["period"],
        {
            "contract_count": expected["contract_count"],
            "total_amount": expected["total_amount"],
            "small_scale": calc["small_scale"],
        },
        {
            "total_before_discount": calc["total_before_discount"],
            "discount_amount": calc["discount_amount"],
            "total_payable": calc["total_payable"],
            "note": calc["note"],
        },
    )
    report = validate_bundle(bundle)
    bundle["checks"] = [
        {
            "field": detail.get("field"),
            "status": detail.get("status"),
            "message": detail.get("message"),
            "expected": detail.get("expected"),
            "actual": detail.get("actual"),
        }
        for detail in report.get("details") or []
    ]
    bundle["summary"] = {"level": report["status"], "text": report["summary"]}
    bundle["guide"] = build_guide(bundle)
    return bundle


__all__ = ["FORM_ID", "FORM_NAME", "CATEGORY", "FREQUENCY", "build_stamp_return"]
