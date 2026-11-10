"""个人所得税工资薪金累计预扣法（确定性代码，LLM 不参与计算）。

适用：居民个人工资薪金所得，由扣缴义务人按月预扣预缴。

核心口径
--------
1. 累计预扣法：本期应预扣预缴税额 = 累计应纳税所得额 × 预扣率 − 速算扣除数 − 累计已预扣预缴税额。
2. 累计应纳税所得额 = 累计收入 − 累计减除费用 − 累计专项扣除（三险一金）− 累计专项附加扣除 − 累计其他扣除。
3. 减除费用：5,000 元/月。**2026 年扣缴端升级后，减除费用按"实际申报月份"累计**——
   未申报的月份额度作废、不再补发；因此零工资月份也应做零申报（本模块把"已申报月份数"作为入参）。
4. 预扣率表（累计预扣法，居民个人工资薪金）：

   | 累计应纳税所得额 | 预扣率 | 速算扣除数 |
   |---|---|---|
   | ≤ 36,000 | 3% | 0 |
   | 36,000 ~ 144,000 | 10% | 2,520 |
   | 144,000 ~ 300,000 | 20% | 16,920 |
   | 300,000 ~ 420,000 | 25% | 31,920 |
   | 420,000 ~ 660,000 | 30% | 52,920 |
   | 660,000 ~ 960,000 | 35% | 85,920 |
   | > 960,000 | 45% | 181,920 |

5. 专项附加扣除标准（国发〔2023〕13 号提高后）：
   子女教育 2,000 元/月/孩、3 岁以下婴幼儿照护 2,000 元/月/孩、继续教育 400 元/月（学历）
   或 3,600 元/年（职业资格）、住房贷款利息 1,000 元/月、住房租金 1,500/1,100/800 元/月（城市档）、
   赡养老人 3,000 元/月（独生）或分摊不超过 1,500 元/月、大病医疗 80,000 元/年（**仅年度汇算扣除**）。

政策依据见 :func:`get_iit_sources`；本模块所有金额四舍五入 2 位小数。
"""

from __future__ import annotations

from typing import Any

import config
from engine.money import pct, round2, yuan

DISCLAIMER = config.DISCLAIMER

#: 基本减除费用（元/月）
BASIC_DEDUCTION_PER_MONTH = 5_000.0
#: 一次性扣除 60,000 元（符合条件员工可选择）
ONE_TIME_BASIC_DEDUCTION = 60_000.0

#: 累计预扣率表：(累计应纳税所得额上限, 预扣率, 速算扣除数)
IIT_RATE_TABLE: list[tuple[float, float, float]] = [
    (36_000.0, 0.03, 0.0),
    (144_000.0, 0.10, 2_520.0),
    (300_000.0, 0.20, 16_920.0),
    (420_000.0, 0.25, 31_920.0),
    (660_000.0, 0.30, 52_920.0),
    (960_000.0, 0.35, 85_920.0),
    (float("inf"), 0.45, 181_920.0),
]

#: 专项附加扣除月度标准
SPECIAL_DEDUCTION_STANDARDS: dict[str, dict[str, Any]] = {
    "children_education": {"label": "子女教育", "monthly_per_child": 2_000.0, "note": "每个子女 2,000 元/月，可 100% 或 50% 分摊"},
    "infant_care": {"label": "3 岁以下婴幼儿照护", "monthly_per_child": 2_000.0, "note": "每个婴幼儿 2,000 元/月"},
    "continuing_education": {"label": "继续教育", "monthly": 400.0, "annual": 3_600.0, "note": "学历 400 元/月；职业资格 3,600 元/年（当年）"},
    "housing_loan_interest": {"label": "住房贷款利息", "monthly": 1_000.0, "note": "首套住房贷款利息 1,000 元/月"},
    "housing_rent": {"label": "住房租金", "monthly_tiers": {"直辖市/省会/计划单列市": 1_500.0, "市辖区户籍人口>100万": 1_100.0, "其他": 800.0}, "note": "与住房贷款利息不可同时享受"},
    "elderly_care": {"label": "赡养老人", "monthly_only_child": 3_000.0, "monthly_shared_max": 1_500.0, "note": "独生子女 3,000 元/月；非独生分摊不超过 1,500 元/月"},
    "serious_illness": {"label": "大病医疗", "annual_limit": 80_000.0, "note": "仅年度汇算清缴扣除，预扣预缴阶段不扣"},
}

#: 政策依据（按现行有效文件整理，具体执行以扣缴端/主管税务机关为准）
IIT_SOURCES: list[dict[str, str]] = [
    {
        "title": "居民个人工资薪金累计预扣法、预扣率表",
        "source": "国家税务总局公告2018年第61号《个人所得税扣缴申报管理办法（试行）》",
        "effective_date": "2019-01-01",
        "expiry_date": "长期",
    },
    {
        "title": "综合所得基本减除费用 5,000 元/月、专项附加扣除制度",
        "source": "《中华人民共和国个人所得税法》（2018 年修正）及国发〔2018〕41号",
        "effective_date": "2019-01-01",
        "expiry_date": "长期",
    },
    {
        "title": "提高 3 岁以下婴幼儿照护、子女教育、赡养老人专项附加扣除标准",
        "source": "国发〔2023〕13号《国务院关于提高个人所得税有关专项附加扣除标准的通知》",
        "effective_date": "2023-01-01",
        "expiry_date": "长期",
    },
    {
        "title": "2026 年扣缴端升级：减除费用按实际申报月份累计、未申报月份额度作废",
        "source": "自然人电子税务局扣缴端升级说明（操作口径，具体以扣缴端实际规则为准）",
        "effective_date": "2026-01-01",
        "expiry_date": "长期",
    },
]

LAST_VERIFIED = "2026-11-10"

#: 2026 年扣缴端升级要点（展示给用户，避免漏做零申报）
ZERO_DECLARATION_NOTE = (
    "2026 年扣缴端升级后，5,000 元/月减除费用按**实际申报月份**累计：未申报的月份额度作废、不再补发。"
    "即使当月未发工资，也建议做零申报（收入填 0），以保留该月减除额度。"
)


# =============================================================================
# 预扣率与政策信息
# =============================================================================
def get_rate_table() -> list[dict[str, Any]]:
    """返回累计预扣率表（供界面展示）。"""
    rows: list[dict[str, Any]] = []
    lower = 0.0
    for upper, rate, quick in IIT_RATE_TABLE:
        upper_text = "超过 960,000" if upper == float("inf") else f"{lower:,.0f} ~ {upper:,.0f}"
        rows.append(
            {
                "级数": len(rows) + 1,
                "累计应纳税所得额": upper_text,
                "预扣率": pct(rate),
                "速算扣除数": quick,
            }
        )
        lower = upper if upper != float("inf") else lower
    return rows


def get_iit_sources() -> list[dict[str, str]]:
    return [dict(item) for item in IIT_SOURCES]


def get_special_deduction_standards() -> dict[str, Any]:
    return {key: dict(value) for key, value in SPECIAL_DEDUCTION_STANDARDS.items()}


# =============================================================================
# 税率与计算
# =============================================================================
def _rate_for(taxable: float) -> tuple[float, float]:
    """按累计应纳税所得额查预扣率与速算扣除数。"""
    for upper, rate, quick in IIT_RATE_TABLE:
        if taxable <= upper:
            return rate, quick
    upper, rate, quick = IIT_RATE_TABLE[-1]
    return rate, quick


def calc_iit_withholding(
    cumulative_income: float,
    *,
    months_declared: int,
    cumulative_social_insurance: float = 0.0,
    cumulative_housing_fund: float = 0.0,
    cumulative_special_additional: float = 0.0,
    cumulative_other_deduction: float = 0.0,
    cumulative_tax_withheld: float = 0.0,
    one_time_basic_deduction: bool = False,
    period: str = "",
) -> dict[str, Any]:
    """计算本期工资薪金应预扣预缴税额（累计预扣法）。

    参数
    ----
    cumulative_income : 本年累计收入（含本期）
    months_declared : 本年已**实际申报**的月份数（2026 年起减除费用按已申报月份累计）
    cumulative_social_insurance / cumulative_housing_fund : 累计专项扣除（三险一金个人部分）
    cumulative_special_additional : 累计专项附加扣除
    cumulative_other_deduction : 累计其他扣除（年金、商业健康险、税延养老等）
    cumulative_tax_withheld : 本年累计已预扣预缴税额
    one_time_basic_deduction : 是否选择一次性扣除 60,000 元

    返回
    ----
    dict：累计各项、适用税率、累计应纳税额、本期应预扣税额、说明与政策依据。
    """
    warnings: list[str] = []
    income = round2(cumulative_income)
    social = round2(cumulative_social_insurance)
    fund = round2(cumulative_housing_fund)
    special = round2(cumulative_special_additional)
    other = round2(cumulative_other_deduction)
    withheld = round2(cumulative_tax_withheld)
    months = int(months_declared or 0)

    if income < 0:
        warnings.append("累计收入为负数，已按 0 处理，请核对工资表。")
        income = 0.0
    if social < 0 or fund < 0 or special < 0 or other < 0:
        warnings.append("扣除项出现负数，已按 0 处理，请核对数据。")
        social = max(social, 0.0)
        fund = max(fund, 0.0)
        special = max(special, 0.0)
        other = max(other, 0.0)
    if months < 0:
        months = 0
    if months > 12:
        warnings.append("已申报月份数超过 12，已按 12 计算。")
        months = 12

    if one_time_basic_deduction:
        basic = round2(ONE_TIME_BASIC_DEDUCTION)
        basic_note = "按 60,000 元一次性扣除"
    else:
        basic = round2(BASIC_DEDUCTION_PER_MONTH * months)
        basic_note = f"5,000 元/月 × 已申报 {months} 个月"
        if months == 0:
            warnings.append("本年尚未申报任何月份：减除费用为 0（2026 年起未申报月份额度作废），请确认是否漏做零申报。")

    total_deduction = round2(basic + social + fund + special + other)
    taxable = round2(income - total_deduction)
    if taxable < 0:
        taxable = 0.0

    rate, quick = _rate_for(taxable)
    cumulative_tax_raw = round2(taxable * rate - quick)
    cumulative_tax = max(cumulative_tax_raw, 0.0)
    if cumulative_tax_raw < 0:
        warnings.append("按税率表计算的累计应纳税额为负数，已按 0 处理。")

    tax_payable = round2(cumulative_tax - withheld)
    if tax_payable < 0:
        tax_payable = 0.0
        warnings.append(
            f"累计已预扣税额 {yuan(withheld)} 元大于累计应纳税额 {yuan(cumulative_tax)} 元，"
            "本期不再预扣，多缴部分可在年度汇算清缴时申请退税或抵缴。"
        )

    note = (
        f"累计收入 {yuan(income)} 元，累计减除费用 {yuan(basic)} 元（{basic_note}），"
        f"累计专项扣除 {yuan(social + fund)} 元，累计专项附加扣除 {yuan(special)} 元，"
        f"累计其他扣除 {yuan(other)} 元；累计应纳税所得额 {yuan(taxable)} 元，"
        f"适用预扣率 {pct(rate)}、速算扣除数 {yuan(quick)}，累计应纳税额 {yuan(cumulative_tax)} 元，"
        f"已预扣 {yuan(withheld)} 元，**本期应预扣 {yuan(tax_payable)} 元**。"
    )

    return {
        "period": period,
        "cumulative_income": income,
        "months_declared": months,
        "basic_deduction_per_month": BASIC_DEDUCTION_PER_MONTH,
        "one_time_basic_deduction": bool(one_time_basic_deduction),
        "cumulative_basic_deduction": basic,
        "cumulative_social_insurance": social,
        "cumulative_housing_fund": fund,
        "cumulative_special_additional": special,
        "cumulative_other_deduction": other,
        "cumulative_total_deduction": total_deduction,
        "cumulative_taxable_income": taxable,
        "applicable_rate": rate,
        "quick_deduction": quick,
        "cumulative_tax": cumulative_tax,
        "cumulative_tax_withheld": withheld,
        "tax_payable": tax_payable,
        "note": note,
        "warnings": warnings,
        "zero_declaration_note": ZERO_DECLARATION_NOTE,
        "policy_sources": IIT_SOURCES,
        "last_verified": LAST_VERIFIED,
        "disclaimer": DISCLAIMER,
    }


def calc_special_deduction(breakdown: dict[str, Any] | None = None) -> dict[str, Any]:
    """按专项附加扣除明细计算月度可扣除金额。

    breakdown 支持（均可选）：
    ``children_education_children``（子女数）、``infant_care_children``（婴幼儿数）、
    ``continuing_education``（"学历"/"职业资格"/True）、``housing_loan_interest``（bool）、
    ``housing_rent``（"直辖市/省会/计划单列市"|"市辖区户籍人口>100万"|"其他" 或金额）、
    ``elderly_care``（"独生"|金额）、``serious_illness_annual``（年度金额，仅汇算扣除）。
    """
    breakdown = dict(breakdown or {})
    items: list[dict[str, Any]] = []
    monthly_total = 0.0
    annual_only = 0.0
    warnings: list[str] = []

    def _money(value: Any, default: float = 0.0) -> float:
        try:
            return round2(float(value))
        except (TypeError, ValueError):
            return default

    children = int(breakdown.get("children_education_children") or 0)
    if children > 0:
        amount = _money(breakdown.get("children_education_amount"), 2_000.0 * children)
        amount = min(amount, 2_000.0 * children)
        monthly_total += amount
        items.append({"label": "子女教育", "monthly": amount, "detail": f"{children} 个子女，每个 2,000 元/月"})

    infants = int(breakdown.get("infant_care_children") or 0)
    if infants > 0:
        amount = min(_money(breakdown.get("infant_care_amount"), 2_000.0 * infants), 2_000.0 * infants)
        monthly_total += amount
        items.append({"label": "3 岁以下婴幼儿照护", "monthly": amount, "detail": f"{infants} 个婴幼儿"})

    education = breakdown.get("continuing_education")
    if education:
        if education is True or str(education) == "学历":
            amount = 400.0
            detail = "学历继续教育 400 元/月"
        else:
            amount = round2(3_600.0 / 12)
            detail = "职业资格继续教育 3,600 元/年（按月摊入 300 元）"
        monthly_total += amount
        items.append({"label": "继续教育", "monthly": amount, "detail": detail})

    # 住房贷款利息与住房租金不可同时享受：按较高一项计入并提示
    loan_amount = 1_000.0 if breakdown.get("housing_loan_interest") else 0.0
    rent = breakdown.get("housing_rent")
    rent_amount = 0.0
    rent_detail = ""
    if rent:
        tiers = {"直辖市/省会/计划单列市": 1_500.0, "市辖区户籍人口>100万": 1_100.0, "其他": 800.0}
        if isinstance(rent, (int, float)) and not isinstance(rent, bool):
            rent_amount = _money(rent)
        else:
            rent_amount = tiers.get(str(rent), 800.0)
        rent_detail = f"按「{rent}」档标准"
    if loan_amount and rent_amount:
        warnings.append("住房贷款利息与住房租金不可同时享受，已按金额较高的一项计入，请核对员工选择。")
        if rent_amount > loan_amount:
            items.append({"label": "住房租金", "monthly": rent_amount, "detail": rent_detail + "（与贷款利息择一）"})
            monthly_total += rent_amount
        else:
            items.append({"label": "住房贷款利息", "monthly": loan_amount, "detail": "首套住房贷款利息 1,000 元/月（与租金择一）"})
            monthly_total += loan_amount
    elif loan_amount:
        monthly_total += loan_amount
        items.append({"label": "住房贷款利息", "monthly": loan_amount, "detail": "首套住房贷款利息 1,000 元/月"})
    elif rent_amount:
        monthly_total += rent_amount
        items.append({"label": "住房租金", "monthly": rent_amount, "detail": rent_detail})

    elderly = breakdown.get("elderly_care")
    if elderly:
        if str(elderly) == "独生":
            amount = 3_000.0
            detail = "独生子女 3,000 元/月"
        else:
            amount = min(_money(elderly, 1_500.0), 1_500.0)
            detail = f"非独生子女分摊 {yuan(amount)} 元/月（上限 1,500）"
        monthly_total += amount
        items.append({"label": "赡养老人", "monthly": amount, "detail": detail})

    illness = _money(breakdown.get("serious_illness_annual"))
    if illness > 0:
        annual_only = min(illness, 80_000.0)
        items.append(
            {
                "label": "大病医疗",
                "monthly": 0.0,
                "annual_only": annual_only,
                "detail": "仅年度汇算清缴扣除（限额 80,000 元/年）",
            }
        )
        warnings.append("大病医疗只能在年度汇算清缴时扣除，预扣预缴阶段不计入月度扣除。")

    return {
        "items": items,
        "monthly_total": round2(monthly_total),
        "annual_only_total": round2(annual_only),
        "warnings": warnings,
        "policy_sources": IIT_SOURCES,
        "last_verified": LAST_VERIFIED,
        "disclaimer": DISCLAIMER,
    }
