"""发票合规判断（v1.19，规则式，不依赖 AI）。

面向小规模纳税人，回答三个问题：
1. 这张票/凭证能不能入账？
2. 能不能抵扣增值税？（小规模纳税人通常不能抵扣）
3. 能不能在企业所得税前扣除？

规则仅作入门判断，复杂业务（视同销售、混合销售、跨境等）请咨询专业人士。
"""

from __future__ import annotations

from typing import Any

DISCLAIMER = "⚠️ 仅为入门规则判断，不构成税务意见；具体以主管税务机关口径为准。"

INVOICE_TYPES = [
    "增值税专用发票",
    "增值税普通发票",
    "数电票（增值税专用发票）",
    "数电票（普通发票）",
    "收据/财政票据",
    "白条/无票支出",
]

PURPOSES = [
    "与经营相关，用于应税项目",
    "用于免税项目",
    "用于集体福利/个人消费",
    "与公司经营无关",
]

RISK_LABELS = {"low": "低风险", "medium": "需注意", "high": "高风险"}


def assess_invoice(
    invoice_type: str,
    *,
    buyer_is_company: bool = True,
    compliant: bool = True,
    purpose: str = "与经营相关，用于应税项目",
    cross_year: bool = False,
    small_scale: bool = True,
    amount: float | None = None,
) -> dict[str, Any]:
    """返回入账/抵扣/税前扣除判断与风险提示。"""
    invoice_type = str(invoice_type or "").strip()
    purpose = str(purpose or "")
    notes: list[str] = []
    risk = "low"
    bookable = True
    vat_deductible = False
    cit_deductible = True

    if not buyer_is_company:
        bookable = False
        cit_deductible = False
        risk = "high"
        notes.append("发票抬头不是本公司：不能作为公司成本费用入账/税前扣除。")
    if not compliant:
        bookable = False
        cit_deductible = False
        risk = "high"
        notes.append("发票信息不真实/不合规（虚开、项目不符、假票）：不得入账，且存在税务风险。")
    if purpose == "与公司经营无关":
        bookable = False
        cit_deductible = False
        risk = "high"
        notes.append("与公司经营无关的支出：不能作为公司成本费用。")
    elif purpose == "用于集体福利/个人消费":
        bookable = True
        cit_deductible = False
        vat_deductible = False
        risk = "medium"
        notes.append("集体福利/个人消费：会计可入账，但企业所得税前不得扣除（另有规定的除外），进项税不得抵扣。")
    elif purpose == "用于免税项目":
        vat_deductible = False
        risk = "medium"
        notes.append("用于免税项目的进项税不得抵扣；企业所得税扣除需结合业务判断。")

    if invoice_type in ("收据/财政票据",):
        cit_deductible = False
        risk = "high" if amount is None or float(amount or 0) > 500 else "medium"
        notes.append("收据/财政票据一般不能作为企业所得税税前扣除凭证；小额零星经营业务可按现行规定判断。")
    elif invoice_type in ("白条/无票支出",):
        cit_deductible = False
        risk = "high" if amount is None or float(amount or 0) > 500 else "medium"
        notes.append("白条/无票支出：会计上可据实记录，但企业所得税前通常不得扣除；小额零星支出需符合条件并留存收款凭证。")
    elif invoice_type in ("增值税专用发票", "数电票（增值税专用发票）"):
        if small_scale:
            vat_deductible = False
            notes.append("小规模纳税人不能抵扣增值税进项税；专票可作为合规成本费用凭证。")
        elif purpose == "与经营相关，用于应税项目" and buyer_is_company and compliant:
            vat_deductible = True
            notes.append("一般纳税人取得合规专票且用于应税项目，可按规定抵扣进项税。")
        else:
            vat_deductible = False
            notes.append("专票用于免税项目/集体福利/个人消费等，进项税不得抵扣。")
    elif invoice_type in ("增值税普通发票", "数电票（普通发票）"):
        vat_deductible = False
        notes.append("普通发票不能抵扣增值税进项税；合规且与经营相关的，可作为企业所得税税前扣除凭证。")

    if cross_year:
        risk = "high" if risk == "low" else risk
        notes.append("跨年度发票：会计可入账，但企业所得税按权责发生制归属年度，可能需纳税调整，建议先确认归属期。")
    if invoice_type in ("收据/财政票据", "白条/无票支出") and cit_deductible:
        cit_deductible = False

    if not notes:
        notes.append("基础信息未发现明显问题；仍需核对业务真实性、发票内容与合同/付款是否一致。")
    notes.append("无论能否税前扣除，业务真实性、合同、付款凭证、发票四流一致都很重要。")

    return {
        "invoice_type": invoice_type,
        "purpose": purpose,
        "bookable": bool(bookable),
        "vat_deductible": bool(vat_deductible),
        "cit_deductible": bool(cit_deductible),
        "risk": risk,
        "risk_label": RISK_LABELS.get(risk, risk),
        "notes": notes,
        "disclaimer": DISCLAIMER,
    }


__all__ = ["assess_invoice", "INVOICE_TYPES", "PURPOSES", "RISK_LABELS", "DISCLAIMER"]
