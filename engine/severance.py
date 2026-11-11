"""离职经济补偿金与一次性补偿个税测算（确定性代码，P2）。

适用场景：员工离职时，公司支付经济补偿金 / 一次性补偿收入，需要：

1. 判断**经济补偿金**的法定参考标准（《劳动合同法》第四十七条）；
2. 计算**一次性补偿收入**的个人所得税：当地上年职工平均工资 3 倍以内免征，
   超过部分**不并入当年综合所得**，单独适用综合所得税率表计税（财税〔2018〕164号第五条）。

本模块只做测算，实际支付、申报与税前扣除请以当地口径和主管税务机关要求为准。
"""

from __future__ import annotations

from typing import Any

import config
from engine.iit import IIT_RATE_TABLE
from engine.money import pct, round2, yuan

DISCLAIMER = config.DISCLAIMER

#: 免税倍数（当地上年职工平均工资的 3 倍以内免征）
SEVERANCE_TAX_EXEMPTION_MULTIPLIER = 3.0
#: 高收入者经济补偿金封顶：月工资高于当地社平 3 倍的，按 3 倍计，年限最高 12 年
SEVERANCE_PAY_CAP_MULTIPLIER = 3.0
SEVERANCE_PAY_MAX_YEARS = 12

SEVERANCE_SOURCES: list[dict[str, str]] = [
    {
        "title": "解除劳动关系一次性补偿收入个税（3 倍社平以内免征、超出部分单独计税）",
        "source": "财税〔2018〕164号第五条",
        "effective_date": "2019-01-01",
        "expiry_date": "长期",
    },
    {
        "title": "经济补偿金计算标准（每满一年一个月工资，六个月以上不满一年按一年，不满六个月半个月）",
        "source": "《中华人民共和国劳动合同法》第四十七条",
        "effective_date": "2008-01-01",
        "expiry_date": "长期",
    },
]

LAST_VERIFIED = "2026-11-10"


def get_severance_sources() -> list[dict[str, str]]:
    return [dict(item) for item in SEVERANCE_SOURCES]


def severance_service_months(years: int | float, months: int | float = 0) -> int:
    """工作年限折算为月数（用于判断“满 6 个月/不满 6 个月”）。"""
    return max(int(years or 0) * 12 + int(months or 0), 0)


def severance_years_factor(years: int | float, months: int | float = 0) -> float:
    """按劳动合同法折算补偿月数：满 1 年 1 个月；6 个月以上不满 1 年按 1 年；不满 6 个月半个月。

    例：3 年 7 个月 → 4 个月；2 年 3 个月 → 2.5 个月；0 年 3 个月 → 0.5 个月。
    """
    total_months = severance_service_months(years, months)
    full_years = total_months // 12
    remainder = total_months % 12
    factor = float(full_years)
    if remainder >= 6:
        factor += 1.0
    elif remainder > 0:
        factor += 0.5
    return factor


def calc_severance_pay(
    monthly_wage: float,
    years: int | float,
    *,
    months: int | float = 0,
    local_average_wage: float | None = None,
    cap_high_earner: bool = True,
) -> dict[str, Any]:
    """按《劳动合同法》第四十七条计算经济补偿金参考金额。

    * 每满 1 年支付 1 个月工资；6 个月以上不满 1 年按 1 年；不满 6 个月支付半个月；
    * 月工资高于当地社平工资 3 倍的，按 3 倍计算，且补偿年限最高不超过 12 年；
    * ``local_average_wage`` 未提供时不做 3 倍封顶判断（并给出提示）。
    """
    wage = round2(monthly_wage)
    if wage < 0:
        raise ValueError("月工资不能为负数")
    if years < 0 or months < 0:
        raise ValueError("工作年限不能为负数")
    factor = severance_years_factor(years, months)
    warnings: list[str] = []
    base_wage = wage
    years_capped = False

    if local_average_wage and float(local_average_wage) > 0 and cap_high_earner:
        cap = round2(float(local_average_wage) * SEVERANCE_PAY_CAP_MULTIPLIER)
        if wage > cap:
            base_wage = cap
            warnings.append(
                f"月工资 {yuan(wage)} 元高于当地社平工资 3 倍（{yuan(cap)} 元），按 3 倍封顶计算。"
            )
        if factor > SEVERANCE_PAY_MAX_YEARS:
            factor = float(SEVERANCE_PAY_MAX_YEARS)
            years_capped = True
            warnings.append("高收入者经济补偿年限最高按 12 年计算。")
    elif not local_average_wage:
        warnings.append("未提供当地上年职工月平均工资，无法判断 3 倍封顶；如有需要请补充。")

    amount = round2(base_wage * factor)
    return {
        "monthly_wage": wage,
        "base_wage": base_wage,
        "years": int(years or 0),
        "months": int(months or 0),
        "service_months": severance_service_months(years, months),
        "years_factor": factor,
        "amount": amount,
        "local_average_wage": round2(local_average_wage or 0.0),
        "cap_high_earner": bool(cap_high_earner),
        "years_capped": years_capped,
        "warnings": warnings,
        "policy_sources": SEVERANCE_SOURCES,
        "last_verified": LAST_VERIFIED,
        "disclaimer": DISCLAIMER,
    }


def calc_severance_tax(
    amount: float,
    local_average_wage: float,
    *,
    year: str | int | None = None,
) -> dict[str, Any]:
    """一次性补偿收入个税：3 倍当地社平以内免征，超过部分单独按综合所得税率表计税。

    注意：该笔收入**不并入当年综合所得**，不扣 5,000 元/月减除费用与专项附加扣除。
    """
    total = round2(amount)
    if total < 0:
        raise ValueError("补偿金金额不能为负数")
    avg = round2(local_average_wage)
    if avg < 0:
        raise ValueError("当地社平工资不能为负数")
    warnings: list[str] = []
    exemption = round2(avg * SEVERANCE_TAX_EXEMPTION_MULTIPLIER)
    if avg == 0:
        warnings.append("未提供当地上年职工月平均工资：无法计算 3 倍免税额度，暂按全额计税测算。")
    taxable = round2(max(total - exemption, 0.0))
    rate = 0.0
    quick = 0.0
    for upper, bracket_rate, bracket_quick in IIT_RATE_TABLE:
        if taxable <= upper:
            rate, quick = bracket_rate, bracket_quick
            break
    tax = round2(max(taxable * rate - quick, 0.0))
    return {
        "amount": total,
        "local_average_wage": avg,
        "exemption_limit": exemption,
        "taxable_amount": taxable,
        "rate": rate,
        "quick_deduction": quick,
        "tax": tax,
        "net_amount": round2(total - tax),
        "note": (
            f"一次性补偿收入 {yuan(total)} 元，当地社平 3 倍免税额度 {yuan(exemption)} 元，"
            f"应税部分 {yuan(taxable)} 元，按 {pct(rate)} 单独计税，应缴个税 {yuan(tax)} 元。"
        ),
        "warnings": warnings,
        "policy_sources": SEVERANCE_SOURCES,
        "last_verified": LAST_VERIFIED,
        "disclaimer": DISCLAIMER,
    }


def calc_severance(
    monthly_wage: float,
    years: int | float,
    *,
    months: int | float = 0,
    local_average_wage: float,
    amount: float | None = None,
    cap_high_earner: bool = True,
    year: str | int | None = None,
) -> dict[str, Any]:
    """组合测算：经济补偿金参考金额 + 一次性补偿收入个税 + 税后金额。

    ``amount`` 可覆盖实际补偿金额（例如公司另加 N+1、协商补偿）；缺省使用法定参考金额。
    """
    pay = calc_severance_pay(
        monthly_wage,
        years,
        months=months,
        local_average_wage=local_average_wage,
        cap_high_earner=cap_high_earner,
    )
    total = round2(amount if amount is not None else pay["amount"])
    tax = calc_severance_tax(total, local_average_wage, year=year)
    warnings = list(pay["warnings"]) + list(tax["warnings"])
    if amount is not None and abs(round2(amount) - pay["amount"]) > 0.01:
        warnings.append(
            f"实际补偿金额 {yuan(amount)} 元与法定参考 {yuan(pay['amount'])} 元不同，请确认是否符合当地规定或协商约定。"
        )
    return {
        "pay": pay,
        "tax": tax,
        "total_amount": total,
        "net_amount": tax["net_amount"],
        "warnings": warnings,
        "note": (
            f"经济补偿金参考 {yuan(pay['amount'])} 元（{pay['years_factor']:g} 个月 × {yuan(pay['base_wage'])} 元）；"
            f"一次性补偿收入个税 {yuan(tax['tax'])} 元，税后到手 {yuan(tax['net_amount'])} 元。"
        ),
        "policy_sources": SEVERANCE_SOURCES,
        "last_verified": LAST_VERIFIED,
        "disclaimer": DISCLAIMER,
        "calculated_at": config.now_text(),
    }
