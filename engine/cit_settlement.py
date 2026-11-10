"""企业所得税汇算清缴辅助（确定性代码，LLM 不参与计算）。

定位：**数据准备 + 纳税调整测算 + 亏损弥补 + 年度申报表辅助 + 检查清单**，
不做"全自动汇算申报"，最终以电子税务局年度申报表与主管税务机关口径为准。

覆盖的纳税调整规则（限额类自动计算，其他由用户录入）
------------------------------------------------------
| 项目 | 规则 | 依据 |
|---|---|---|
| 业务招待费 | 发生额 × 60% 与 营业收入 × 5‰ 孰低 | 企业所得税法实施条例第 43 条 |
| 广告费和业务宣传费 | 营业收入 × 15% | 实施条例第 44 条 |
| 职工福利费 | 工资薪金总额 × 14% | 实施条例第 40 条 |
| 职工教育经费 | 工资薪金总额 × 8% | 实施条例第 42 条 |
| 工会经费 | 工资薪金总额 × 2% | 实施条例第 41 条 |
| 研发费用加计扣除 | 未形成无形资产 100% 加计 | 财政部 税务总局公告 2023 年第 7 号 |
| 亏损弥补 | 一般企业 5 年；高新技术/科技型中小企业 10 年 | 企业所得税法第 18 条、财税〔2018〕76 号 |
| 小型微利企业 | 应纳税所得额 ≤300 万等条件，实际税负 5% | 财政部 税务总局公告 2023 年第 12 号 |
"""

from __future__ import annotations

from typing import Any

import config
from engine.money import pct, round2, yuan
from engine.policy_params import get_params

DISCLAIMER = config.DISCLAIMER

#: 业务招待费：发生额扣除比例与营业收入上限比例
ENTERTAINMENT_ACTUAL_RATE = 0.60
ENTERTAINMENT_REVENUE_RATE = 0.005
#: 广告费和业务宣传费：营业收入比例
ADVERTISING_RATE = 0.15
#: 职工薪酬相关比例
WELFARE_RATE = 0.14
EDUCATION_RATE = 0.08
UNION_RATE = 0.02
#: 亏损弥补年限（一般企业）
LOSS_CARRYFORWARD_YEARS = 5
#: 高新技术企业 / 科技型中小企业亏损弥补年限
LOSS_CARRYFORWARD_YEARS_HITECH = 10

#: 政策依据
SETTLEMENT_SOURCES: list[dict[str, str]] = [
    {
        "title": "业务招待费、广告费、职工福利费/教育经费/工会经费扣除限额",
        "source": "《中华人民共和国企业所得税法实施条例》第四十条至第四十四条",
        "effective_date": "2008-01-01",
        "expiry_date": "长期",
    },
    {
        "title": "研发费用加计扣除 100%（未形成无形资产）",
        "source": "财政部 税务总局公告2023年第7号",
        "effective_date": "2023-01-01",
        "expiry_date": "长期",
    },
    {
        "title": "小型微利企业减按 25% 计入应纳税所得额、20% 税率（实际 5%）",
        "source": "财政部 税务总局公告2023年第12号",
        "effective_date": "2023-01-01",
        "expiry_date": "2027-12-31",
    },
    {
        "title": "亏损结转弥补年限 5 年（高新技术/科技型中小企业 10 年）",
        "source": "《企业所得税法》第十八条；财税〔2018〕76号",
        "effective_date": "2008-01-01",
        "expiry_date": "长期",
    },
]

LAST_VERIFIED = "2026-11-10"


def get_settlement_sources() -> list[dict[str, str]]:
    return [dict(item) for item in SETTLEMENT_SOURCES]


def _limit_item(
    key: str,
    label: str,
    *,
    actual: float,
    limit: float,
    rule: str,
    basis: str,
    source: str,
) -> dict[str, Any]:
    actual_value = round2(actual)
    limit_value = round2(limit)
    deductible = round2(min(actual_value, limit_value))
    adjustment = round2(max(actual_value - limit_value, 0.0))
    return {
        "key": key,
        "label": label,
        "actual": actual_value,
        "limit": limit_value,
        "deductible": deductible,
        "adjustment_add": adjustment,
        "rule": rule,
        "basis": basis,
        "source": source,
    }


def calc_adjustment_limits(
    *,
    revenue_exclusive: float = 0.0,
    salary_total: float = 0.0,
    entertainment_actual: float = 0.0,
    advertising_actual: float = 0.0,
    welfare_actual: float = 0.0,
    education_actual: float = 0.0,
    union_actual: float = 0.0,
) -> dict[str, Any]:
    """按税法限额计算业务招待费/广宣费/三项经费的纳税调增额。

    返回
    ----
    dict：``items``（逐项：发生额/限额/可扣除/调增/依据）、``total_adjustment_add``、
    ``warnings``（超限提示）、``policy_sources``。
    """
    revenue = round2(revenue_exclusive)
    salary = round2(salary_total)
    items: list[dict[str, Any]] = []
    warnings: list[str] = []

    if entertainment_actual:
        limit = min(round2(entertainment_actual * ENTERTAINMENT_ACTUAL_RATE), round2(revenue * ENTERTAINMENT_REVENUE_RATE))
        item = _limit_item(
            "business_entertainment",
            "业务招待费",
            actual=entertainment_actual,
            limit=limit,
            rule=f"发生额 × 60% 与营业收入 × 5‰ 孰低（营业收入 {yuan(revenue)} 元）",
            basis="实际发生额的 60% 与当年销售（营业）收入 5‰ 的孰低值",
            source="企业所得税法实施条例第四十三条",
        )
        items.append(item)
        warnings.append(
            f"业务招待费发生 {yuan(item['actual'])} 元，税前可扣除 {yuan(item['deductible'])} 元，"
            f"纳税调增 {yuan(item['adjustment_add'])} 元。"
        )

    if advertising_actual:
        limit = round2(revenue * ADVERTISING_RATE)
        item = _limit_item(
            "advertising",
            "广告费和业务宣传费",
            actual=advertising_actual,
            limit=limit,
            rule=f"营业收入 × 15%（营业收入 {yuan(revenue)} 元）",
            basis="不超过当年销售（营业）收入 15% 的部分准予扣除，超过部分准予结转以后年度扣除",
            source="企业所得税法实施条例第四十四条",
        )
        items.append(item)
        if item["adjustment_add"] > 0:
            warnings.append(
                f"广告费和业务宣传费超限额 {yuan(item['adjustment_add'])} 元，可结转以后年度扣除（本年度调增）。"
            )

    if salary_total or welfare_actual:
        limit = round2(salary * WELFARE_RATE)
        item = _limit_item(
            "welfare",
            "职工福利费",
            actual=welfare_actual,
            limit=limit,
            rule=f"工资薪金总额 × 14%（工资薪金总额 {yuan(salary)} 元）",
            basis="不超过工资薪金总额 14% 的部分准予扣除",
            source="企业所得税法实施条例第四十条",
        )
        items.append(item)
    if salary_total or education_actual:
        limit = round2(salary * EDUCATION_RATE)
        item = _limit_item(
            "education",
            "职工教育经费",
            actual=education_actual,
            limit=limit,
            rule=f"工资薪金总额 × 8%（工资薪金总额 {yuan(salary)} 元）",
            basis="不超过工资薪金总额 8% 的部分准予扣除，超过部分准予结转以后年度扣除",
            source="企业所得税法实施条例第四十二条",
        )
        items.append(item)
    if salary_total or union_actual:
        limit = round2(salary * UNION_RATE)
        item = _limit_item(
            "union",
            "工会经费",
            actual=union_actual,
            limit=limit,
            rule=f"工资薪金总额 × 2%（工资薪金总额 {yuan(salary)} 元）",
            basis="不超过工资薪金总额 2% 的部分准予扣除",
            source="企业所得税法实施条例第四十一条",
        )
        items.append(item)

    for item in items:
        if item["adjustment_add"] > 0 and item["key"] in {"welfare", "education", "union"}:
            warnings.append(
                f"{item['label']}超限额 {yuan(item['adjustment_add'])} 元，纳税调增"
                + ("，可结转以后年度扣除。" if item["key"] == "education" else "。")
            )

    return {
        "items": items,
        "total_adjustment_add": round2(sum(item["adjustment_add"] for item in items)),
        "warnings": warnings,
        "policy_sources": SETTLEMENT_SOURCES,
        "last_verified": LAST_VERIFIED,
        "disclaimer": DISCLAIMER,
    }


def calc_loss_carryforward(
    losses: list[dict[str, Any]],
    current_taxable_income: float,
    *,
    year: str | int,
    years: int = LOSS_CARRYFORWARD_YEARS,
) -> dict[str, Any]:
    """亏损弥补：按到期年度先进先出，用本年度应纳税所得额弥补历史亏损。

    ``losses`` 每项：``{"year": 2022, "loss_amount": 100000, "used_amount": 0}``；
    到期年度 = 亏损年度 + 弥补年限（默认 5 年）；超过期限的亏损不可弥补并提示。
    """
    current_year = int(str(year)[:4])
    available = round2(max(current_taxable_income, 0.0))
    lines: list[dict[str, Any]] = []
    warnings: list[str] = []
    total_used = 0.0

    normalized: list[dict[str, Any]] = []
    for item in losses or []:
        loss_year = int(str(item.get("year") or current_year))
        amount = round2(item.get("loss_amount"))
        used = round2(item.get("used_amount"))
        expire = int(str(item.get("expire_year") or loss_year + years))
        remaining = round2(max(amount - used, 0.0))
        if remaining <= 0:
            continue
        expired = current_year > expire
        normalized.append(
            {
                "year": str(loss_year),
                "loss_amount": amount,
                "used_amount": used,
                "remaining": remaining,
                "expire_year": str(expire),
                "expired": expired,
            }
        )
    normalized.sort(key=lambda item: (item["expire_year"], item["year"]))

    for item in normalized:
        if item["expired"]:
            warnings.append(
                f"{item['year']} 年度亏损 {yuan(item['remaining'])} 元已超过 {years} 年弥补期限，不得再弥补。"
            )
            lines.append({**item, "used_this_year": 0.0, "remaining_after": item["remaining"], "status": "已过期"})
            continue
        used_this_year = round2(min(item["remaining"], available))
        available = round2(available - used_this_year)
        total_used = round2(total_used + used_this_year)
        remaining_after = round2(item["remaining"] - used_this_year)
        lines.append(
            {
                **item,
                "used_this_year": used_this_year,
                "remaining_after": remaining_after,
                "status": "已弥补" if remaining_after <= 0.01 else ("部分弥补" if used_this_year else "未弥补"),
            }
        )

    return {
        "year": str(current_year),
        "carryforward_years": years,
        "lines": lines,
        "total_used": total_used,
        "remaining_loss": round2(sum(item["remaining_after"] for item in lines)),
        "available_after": available,
        "warnings": warnings,
        "policy_sources": SETTLEMENT_SOURCES,
        "last_verified": LAST_VERIFIED,
        "disclaimer": DISCLAIMER,
    }


def calc_settlement(
    *,
    year: str | int,
    revenue_exclusive: float = 0.0,
    cost_total: float = 0.0,
    accounting_profit: float | None = None,
    salary_total: float = 0.0,
    entertainment_actual: float = 0.0,
    advertising_actual: float = 0.0,
    welfare_actual: float = 0.0,
    education_actual: float = 0.0,
    union_actual: float = 0.0,
    rd_deduction: float = 0.0,
    extra_adjustments: list[dict[str, Any]] | None = None,
    losses: list[dict[str, Any]] | None = None,
    prepaid_tax: float = 0.0,
    employees: int | None = None,
    total_assets: float | None = None,
    is_restricted_industry: bool = False,
) -> dict[str, Any]:
    """计算企业所得税年度汇算结果（辅助测算，不代替申报）。

    返回：会计利润、限额类调整、手工调整、研发加计、亏损弥补、应纳税所得额、
    小型微利判定、应纳所得税额、已预缴、应补（退）税额，以及说明与政策依据。
    """
    year_text = str(year)[:4]
    params = get_params(year_text)
    warnings: list[str] = []
    profit = (
        round2(accounting_profit)
        if accounting_profit is not None
        else round2(round2(revenue_exclusive) - round2(cost_total))
    )

    limits = calc_adjustment_limits(
        revenue_exclusive=revenue_exclusive,
        salary_total=salary_total,
        entertainment_actual=entertainment_actual,
        advertising_actual=advertising_actual,
        welfare_actual=welfare_actual,
        education_actual=education_actual,
        union_actual=union_actual,
    )
    warnings.extend(limits["warnings"])

    manual_items: list[dict[str, Any]] = []
    manual_add = 0.0
    manual_reduce = 0.0
    for item in extra_adjustments or []:
        direction = str(item.get("direction") or "调增")
        amount = round2(item.get("adjustment_amount"))
        if amount < 0:
            amount = abs(amount)
        if direction not in {"调增", "调减"}:
            raise ValueError(f"纳税调整方向「{direction}」不合法，应为 调增 或 调减")
        record = {
            "item_type": str(item.get("item_type") or "其他调整"),
            "direction": direction,
            "accounting_amount": round2(item.get("accounting_amount")),
            "tax_amount": round2(item.get("tax_amount")),
            "adjustment_amount": amount,
            "basis": str(item.get("basis") or ""),
            "source": str(item.get("source") or ""),
            "note": str(item.get("note") or ""),
        }
        manual_items.append(record)
        if direction == "调增":
            manual_add = round2(manual_add + amount)
        else:
            manual_reduce = round2(manual_reduce + amount)

    rd = round2(rd_deduction)
    if rd < 0:
        warnings.append("研发费用加计扣除为负数，已按 0 处理。")
        rd = 0.0

    total_add = round2(limits["total_adjustment_add"] + manual_add)
    taxable_before_loss = round2(profit + total_add - manual_reduce - rd)
    if taxable_before_loss < 0:
        warnings.append(
            f"调整后应纳税所得额为负数（{yuan(taxable_before_loss)} 元），本年度可结转以后年度弥补亏损。"
        )

    loss_result = calc_loss_carryforward(
        losses or [],
        max(taxable_before_loss, 0.0),
        year=year_text,
        years=LOSS_CARRYFORWARD_YEARS,
    )
    warnings.extend(loss_result["warnings"])

    taxable_income = round2(max(taxable_before_loss - loss_result["total_used"], 0.0))
    check = _small_low_profit_check(taxable_income, employees, total_assets, is_restricted_industry, params)
    qualifies = check["qualifies"]
    standard_rate = float(params["cit_standard_rate"])
    small_rate = float(params["small_low_profit_rate"])
    rate = small_rate if qualifies else standard_rate
    tax = round2(taxable_income * rate)
    gross_tax = round2(taxable_income * standard_rate)
    reduction = round2(gross_tax - tax) if qualifies else 0.0
    prepaid = round2(prepaid_tax)
    payable = round2(tax - prepaid)
    if payable < 0:
        warnings.append(
            f"已预缴税额 {yuan(prepaid)} 元大于应纳所得税额 {yuan(tax)} 元，"
            f"可申请退税 {yuan(abs(payable))} 元或抵缴以后年度税款。"
        )
        payable = abs(payable)
        refund = payable
        payable_sign_text = "应退税额"
    else:
        refund = 0.0
        payable_sign_text = "应补税额"

    if not qualifies and check["reasons"]:
        warnings.append("不符合小型微利企业条件：" + "；".join(check["reasons"]) + "。")

    note = (
        f"{year_text} 年度会计利润 {yuan(profit)} 元；纳税调整增加 {yuan(total_add)} 元、"
        f"纳税调整减少 {yuan(manual_reduce)} 元、研发费用加计扣除 {yuan(rd)} 元；"
        f"弥补以前年度亏损 {yuan(loss_result['total_used'])} 元后，应纳税所得额 {yuan(taxable_income)} 元；"
        f"适用税率 {pct(rate)}，应纳所得税额 {yuan(tax)} 元，已预缴 {yuan(prepaid)} 元，"
        f"**{payable_sign_text} {yuan(refund if refund else payable)} 元**。"
    )

    return {
        "year": year_text,
        "revenue_exclusive": round2(revenue_exclusive),
        "cost_total": round2(cost_total),
        "accounting_profit": profit,
        "salary_total": round2(salary_total),
        "limit_adjustment_items": limits["items"],
        "limit_adjustment_add": limits["total_adjustment_add"],
        "manual_adjustment_items": manual_items,
        "manual_adjustment_add": manual_add,
        "manual_adjustment_reduce": manual_reduce,
        "research_deduction": rd,
        "taxable_income_before_loss": taxable_before_loss,
        "loss_carryforward": loss_result,
        "loss_used": loss_result["total_used"],
        "taxable_income": taxable_income,
        "qualifies_small_low_profit": qualifies,
        "small_low_profit_check": check,
        "applicable_rate": rate,
        "corporate_tax": tax,
        "tax_reduction": reduction,
        "prepaid_tax": prepaid,
        "payable": payable,
        "refund": refund,
        "payable_label": payable_sign_text,
        "note": note,
        "warnings": warnings,
        "policy_sources": SETTLEMENT_SOURCES,
        "last_verified": LAST_VERIFIED,
        "disclaimer": DISCLAIMER,
    }


def _small_low_profit_check(
    taxable_income: float,
    employees: int | None,
    total_assets: float | None,
    is_restricted_industry: bool,
    params: dict[str, Any],
) -> dict[str, Any]:
    """小型微利企业条件判定（与 engine.corporate_tax 口径一致）。"""
    income_ok = taxable_income <= params["small_low_profit_threshold"]
    employees_ok = True if employees is None else int(employees) <= params["employee_threshold"]
    assets_ok = True if total_assets is None else float(total_assets) <= params["asset_threshold"]
    industry_ok = not is_restricted_industry
    reasons = []
    if not income_ok:
        reasons.append(f"年应纳税所得额超过 {params['small_low_profit_threshold']:,.0f} 元")
    if not employees_ok:
        reasons.append(f"从业人数超过 {params['employee_threshold']} 人")
    if not assets_ok:
        reasons.append(f"资产总额超过 {params['asset_threshold']:,.0f} 元")
    if not industry_ok:
        reasons.append("属于国家限制或禁止行业")
    return {
        "qualifies": income_ok and employees_ok and assets_ok and industry_ok,
        "income_ok": income_ok,
        "employees_ok": employees_ok,
        "assets_ok": assets_ok,
        "industry_ok": industry_ok,
        "reasons": reasons,
    }
