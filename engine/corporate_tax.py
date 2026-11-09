"""企业所得税计算：年度应纳税额、季度预缴、小型微利企业判定（确定性代码）。

政策口径（财政部 税务总局公告2023年第12号，执行至 2027-12-31）
--------------------------------------------------------------
小型微利企业：年应纳税所得额 ≤ 300 万元、从业人数 ≤ 300 人、资产总额 ≤ 5000 万元，
且从事国家非限制和禁止行业。符合条件时减按 25% 计入应纳税所得额、按 20% 税率缴纳，
即实际税负 5%。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import config
from engine.money import pct, round2, yuan
from engine.policy_params import get_params

DISCLAIMER = config.DISCLAIMER


def _small_low_profit_check(
    taxable_income: float,
    employees: int | None,
    total_assets: float | None,
    is_restricted_industry: bool,
    params: dict[str, Any],
) -> dict[str, Any]:
    """小型微利企业三项条件判定。"""
    income_ok = taxable_income <= params["small_low_profit_threshold"]
    employees_ok = True if employees is None else int(employees) <= params["employee_threshold"]
    assets_ok = True if total_assets is None else float(total_assets) <= params["asset_threshold"]
    industry_ok = not is_restricted_industry
    qualifies = income_ok and employees_ok and assets_ok and industry_ok
    reasons: list[str] = []
    if not income_ok:
        reasons.append(f"年应纳税所得额超过 {params['small_low_profit_threshold']:,.0f} 元")
    if not employees_ok:
        reasons.append(f"从业人数超过 {params['employee_threshold']} 人")
    if not assets_ok:
        reasons.append(f"资产总额超过 {params['asset_threshold']:,.0f} 元")
    if not industry_ok:
        reasons.append("属于国家限制或禁止行业")
    return {
        "qualifies": qualifies,
        "income_ok": income_ok,
        "employees_ok": employees_ok,
        "assets_ok": assets_ok,
        "industry_ok": industry_ok,
        "reasons": reasons,
        "thresholds": {
            "taxable_income": params["small_low_profit_threshold"],
            "employees": params["employee_threshold"],
            "total_assets": params["asset_threshold"],
        },
    }


def calc_corp_tax(
    annual_profit: float | None = None,
    rd_cost: float = 0.0,
    *,
    annual_revenue_exclusive: float | None = None,
    annual_cost: float | None = None,
    employees: int | None = None,
    total_assets: float | None = None,
    is_restricted_industry: bool = False,
    is_small_low_profit: bool | None = None,
    rd_formed_intangible: bool = False,
    period: str = "",
) -> dict[str, Any]:
    """计算年度企业所得税。

    参数
    ----
    annual_profit : 会计利润总额（优先使用）；未提供时用 ``annual_revenue_exclusive - annual_cost`` 计算
    rd_cost       : 研发费用归集额（用于加计扣除）
    employees     : 从业人数（用于小微判定，可选）
    total_assets  : 资产总额（用于小微判定，可选）
    is_small_low_profit : 显式指定是否符合小型微利条件（可选，优先于自动判定）
    rd_formed_intangible : 研发是否形成无形资产（形成时按 200% 摊销，按年加计摊销额）

    返回
    ----
    dict：``taxable_income``、``effective_rate``、``corp_tax`` 等，并附判定过程与免责声明。
    """
    params = get_params(period)
    warnings: list[str] = list(params.get("warnings") or [])

    if annual_profit is None:
        if annual_revenue_exclusive is None and annual_cost is None:
            raise ValueError("请提供 annual_profit，或同时提供 annual_revenue_exclusive 与 annual_cost")
        revenue = Decimal(str(round2(annual_revenue_exclusive or 0)))
        cost = Decimal(str(round2(annual_cost or 0)))
        profit = revenue - cost
    else:
        profit = Decimal(str(round2(annual_profit)))

    rd_amount = Decimal(str(round2(rd_cost)))
    years = Decimal(str(params["rd_intangible_amortization_years"]))
    if rd_amount < 0:
        warnings.append("研发费用为负数，已按 0 处理。")
        rd_amount = Decimal("0")

    # ---- 加计扣除 -----------------------------------------------------------
    if rd_formed_intangible and rd_amount > 0:
        annual_amortization = (rd_amount / years).quantize(Decimal("0.01"))
        rd_deduction = (annual_amortization * Decimal(str(params["rd_deduction_ratio"]))).quantize(Decimal("0.01"))
        rd_note = (
            f"研发形成无形资产，按 200% 摊销：无形资产成本 {yuan(rd_amount)} 元，"
            f"每年摊销 {yuan(annual_amortization)} 元，加计扣除 {yuan(rd_deduction)} 元/年。"
        )
    else:
        rd_deduction = (rd_amount * Decimal(str(params["rd_deduction_ratio"]))).quantize(Decimal("0.01"))
        rd_note = (
            f"研发费用未形成无形资产，按 100% 加计扣除 {yuan(rd_deduction)} 元。"
            if rd_deduction > 0
            else "本期无研发费用加计扣除。"
        )

    taxable = profit - rd_deduction
    if taxable < 0:
        warnings.append(f"加计扣除后应纳税所得额为负数（{yuan(taxable)} 元），本期不缴企业所得税，亏损可按规定结转以后年度弥补。")
        taxable = Decimal("0")
    taxable = taxable.quantize(Decimal("0.01"))

    check = _small_low_profit_check(
        round2(taxable),
        employees,
        total_assets,
        is_restricted_industry,
        params,
    )
    qualifies = check["qualifies"] if is_small_low_profit is None else bool(is_small_low_profit)
    standard_rate = Decimal(str(params["cit_standard_rate"]))
    small_rate = Decimal(str(params["small_low_profit_rate"]))
    effective_rate = small_rate if qualifies else standard_rate
    corp_tax = (taxable * effective_rate).quantize(Decimal("0.01"))
    gross_tax = (taxable * standard_rate).quantize(Decimal("0.01"))
    reduction = (gross_tax - corp_tax).quantize(Decimal("0.01")) if qualifies else Decimal("0.00")
    if reduction < 0:
        reduction = Decimal("0.00")

    if qualifies:
        note = (
            f"会计利润 {yuan(profit)} 元，研发加计扣除 {yuan(rd_deduction)} 元，"
            f"应纳税所得额 {yuan(taxable)} 元；符合小型微利企业条件，实际税负 {pct(effective_rate)}，"
            f"应缴企业所得税 {yuan(corp_tax)} 元（减免 {yuan(reduction)} 元）。"
        )
    else:
        note = (
            f"会计利润 {yuan(profit)} 元，研发加计扣除 {yuan(rd_deduction)} 元，"
            f"应纳税所得额 {yuan(taxable)} 元；不符合小型微利企业条件，按法定税率 {pct(effective_rate)}，"
            f"应缴企业所得税 {yuan(corp_tax)} 元。"
        )
        if check["reasons"]:
            note += "不符合原因：" + "；".join(check["reasons"]) + "。"

    return {
        "period": period,
        "annual_profit": round2(profit),
        "annual_revenue_exclusive": round2(annual_revenue_exclusive) if annual_revenue_exclusive is not None else None,
        "annual_cost": round2(annual_cost) if annual_cost is not None else None,
        "rd_cost": round2(rd_amount),
        "rd_deduction": round2(rd_deduction),
        "rd_formed_intangible": bool(rd_formed_intangible),
        "rd_note": rd_note,
        "taxable_income": round2(taxable),
        "qualifies_small_low_profit": bool(qualifies),
        "small_low_profit_check": check,
        "standard_rate": float(standard_rate),
        "effective_rate": float(effective_rate),
        "corp_tax": round2(corp_tax),
        "gross_tax": round2(gross_tax),
        "tax_reduction": round2(reduction),
        "note": note,
        "warnings": warnings,
        "policy_version": params["policy_version"],
        "last_verified": params["last_verified"],
        "disclaimer": DISCLAIMER,
    }


def calc_cit_prepay(
    quarter_profit: float,
    ytd_profit: float,
    ytd_cit_paid: float = 0.0,
    *,
    period: str = "",
    rd_cost_ytd: float = 0.0,
    employees: int | None = None,
    total_assets: float | None = None,
    is_restricted_industry: bool = False,
    is_small_low_profit: bool | None = None,
) -> dict[str, Any]:
    """计算季度企业所得税预缴额（按月/季实际利润额预缴，可享受研发加计扣除）。

    预缴口径：本年累计实际利润额 - 累计研发加计扣除 = 累计应纳税所得额；
    应补（退）预缴税额 = 累计应纳税所得额 × 适用税率 - 本年已预缴税额。
    """
    params = get_params(period)
    warnings: list[str] = list(params.get("warnings") or [])
    quarter_profit_d = Decimal(str(round2(quarter_profit)))
    ytd_profit_d = Decimal(str(round2(ytd_profit)))
    ytd_paid_d = Decimal(str(round2(ytd_cit_paid)))
    rd_deduction = (Decimal(str(round2(rd_cost_ytd))) * Decimal(str(params["rd_deduction_ratio"]))).quantize(Decimal("0.01"))

    taxable_ytd = ytd_profit_d - rd_deduction
    if taxable_ytd < 0:
        taxable_ytd = Decimal("0.00")
    check = _small_low_profit_check(round2(taxable_ytd), employees, total_assets, is_restricted_industry, params)
    qualifies = check["qualifies"] if is_small_low_profit is None else bool(is_small_low_profit)
    rate = Decimal(str(params["small_low_profit_rate"])) if qualifies else Decimal(str(params["cit_standard_rate"]))
    accrued = (taxable_ytd * rate).quantize(Decimal("0.01"))
    payable = accrued - ytd_paid_d
    if payable < 0:
        warnings.append(f"本年已预缴税额大于累计应纳所得税额，多缴 {yuan(-payable)} 元，可在以后季度抵缴或年度汇算时申请退税。")
        payable = Decimal("0.00")
    payable = payable.quantize(Decimal("0.01"))

    note = (
        f"本季度利润 {yuan(quarter_profit_d)} 元，本年累计利润 {yuan(ytd_profit_d)} 元，"
        f"累计研发加计扣除 {yuan(rd_deduction)} 元，累计应纳税所得额 {yuan(taxable_ytd)} 元，"
        f"适用税率 {pct(rate)}，累计应纳所得税 {yuan(accrued)} 元，已预缴 {yuan(ytd_paid_d)} 元，"
        f"本期应预缴 {yuan(payable)} 元。"
    )
    if ytd_paid_d == 0 and accrued > 0:
        note += "（首次预缴或尚未预缴）"

    return {
        "period": period,
        "quarter_profit": round2(quarter_profit_d),
        "ytd_profit": round2(ytd_profit_d),
        "rd_cost_ytd": round2(rd_cost_ytd),
        "rd_deduction": round2(rd_deduction),
        "taxable_income_ytd": round2(taxable_ytd),
        "qualifies_small_low_profit": bool(qualifies),
        "small_low_profit_check": check,
        "effective_rate": float(rate),
        "cit_accrued": round2(accrued),
        "cit_paid": round2(ytd_paid_d),
        "prepay_payable": round2(payable),
        "note": note,
        "warnings": warnings,
        "policy_version": params["policy_version"],
        "disclaimer": DISCLAIMER,
    }
