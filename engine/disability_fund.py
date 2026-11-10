"""残疾人就业保障金（残保金）测算（确定性代码）。

口径（现行有效，具体以当地残联/税务口径为准）
--------------------------------------------
1. 应缴额 =（上年在职职工人数 × 1.5% − 上年实际安排残疾人就业人数）× 上年在职职工年平均工资。
2. 分档减缴（财政部公告 2023 年第 8 号，执行至 2027-12-31）：
   - 安排残疾人就业比例 ≥1.5%：不缴纳；
   - 1%（含）~1.5%：按应缴额的 **50%** 缴纳；
   - 1% 以下：按应缴额的 **90%** 缴纳。
3. 在职职工人数 30 人（含）以下的企业，**继续免征**残保金（财政部公告 2023 年第 8 号）。
4. 年平均工资超过当地社会平均工资 2 倍的，按 2 倍计征（本模块给出提示，需传入当地社平工资）。

政策依据
--------
* 《残疾人就业保障金征收使用管理办法》（财税〔2015〕72号）
* 《关于延续实施残疾人就业保障金优惠政策的公告》（财政部公告2023年第8号）
"""

from __future__ import annotations

from typing import Any

import config
from decimal import Decimal

from engine.money import dec, pct, round2, yuan

DISCLAIMER = config.DISCLAIMER

#: 法定安排残疾人就业比例
REQUIRED_DISABILITY_RATIO = 0.015
#: 30 人（含）以下企业免征
EXEMPTION_EMPLOYEE_THRESHOLD = 30
#: 分档减缴比例
TIER_RATIO_THRESHOLD = 0.01
TIER_DISCOUNT_50 = 0.5
TIER_DISCOUNT_90 = 0.9
#: 年平均工资上限倍数（当地社平工资的 2 倍）
AVERAGE_WAGE_CAP_MULTIPLIER = 2.0

DISABILITY_SOURCES: list[dict[str, str]] = [
    {
        "title": "残疾人就业保障金征收使用管理办法",
        "source": "财税〔2015〕72号",
        "effective_date": "2015-10-01",
        "expiry_date": "长期",
    },
    {
        "title": "延续实施残疾人就业保障金优惠政策（分档减缴、30 人以下免征）",
        "source": "财政部公告2023年第8号",
        "effective_date": "2023-01-01",
        "expiry_date": "2027-12-31",
    },
]

LAST_VERIFIED = "2026-11-10"


def get_disability_sources() -> list[dict[str, str]]:
    return [dict(item) for item in DISABILITY_SOURCES]


def calc_disability_fund(
    *,
    employees: int | float,
    average_wage: float,
    disabled_employed: float = 0.0,
    required_ratio: float | None = None,
    exemption_threshold: int | None = None,
    local_average_wage: float | None = None,
) -> dict[str, Any]:
    """测算残疾人就业保障金应缴额（年度）。

    参数
    ----
    employees : 上年在职职工人数
    average_wage : 上年在职职工年平均工资（元）
    disabled_employed : 上年实际安排残疾人就业人数（可带小数）
    required_ratio : 覆盖法定比例（默认 1.5%）
    exemption_threshold : 覆盖免征人数门槛（默认 30 人）
    local_average_wage : 当地社会平均工资（用于 2 倍封顶提示/计算）

    返回
    ----
    dict：比例、差额人数、应缴基数、分档折扣、是否免征、最终应缴额、说明与政策依据。
    """
    count = int(employees or 0)
    wage = round2(average_wage)
    disabled = float(disabled_employed or 0.0)
    ratio = float(required_ratio if required_ratio is not None else REQUIRED_DISABILITY_RATIO)
    threshold = int(exemption_threshold if exemption_threshold is not None else EXEMPTION_EMPLOYEE_THRESHOLD)
    warnings: list[str] = []

    if count < 0 or wage < 0 or disabled < 0:
        raise ValueError("在职职工人数、年平均工资、安排残疾人人数均不能为负数")
    if ratio <= 0:
        raise ValueError("安排残疾人就业比例必须大于 0")

    wage_cap_note = ""
    if local_average_wage and float(local_average_wage) > 0:
        cap = round2(float(local_average_wage) * AVERAGE_WAGE_CAP_MULTIPLIER)
        if wage > cap:
            warnings.append(
                f"年平均工资 {yuan(wage)} 元超过当地社平工资 2 倍（{yuan(cap)} 元），"
                "按规定应按 2 倍封顶计算，请核对当地执行口径。"
            )
            wage_cap_note = f"已提示按当地社平 2 倍（{yuan(cap)} 元）封顶"

    actual_ratio = (disabled / count) if count else 0.0
    # 差额人数允许为小数，按精确比例计算后再对金额四舍五入（避免中间取整放大误差）
    required_people_d = dec(count) * dec(ratio)
    shortfall_d = max(required_people_d - dec(disabled), Decimal("0"))
    required_people = round2(required_people_d)
    shortfall = round2(shortfall_d)
    base_amount = round2(shortfall_d * dec(wage))

    exempt = count <= threshold
    if exempt:
        discount_rate = 0.0
        payable = 0.0
        note = f"在职职工人数 {count} 人 ≤ {threshold} 人，按现行政策免征残疾人就业保障金。"
    elif actual_ratio >= ratio:
        discount_rate = 0.0
        payable = 0.0
        note = (
            f"安排残疾人就业比例 {pct(actual_ratio, 2)} ≥ 法定比例 {pct(ratio, 2)}，"
            "达到要求，无需缴纳残保金。"
        )
    elif actual_ratio >= TIER_RATIO_THRESHOLD:
        discount_rate = TIER_DISCOUNT_50
        payable = round2(base_amount * TIER_DISCOUNT_50)
        note = (
            f"安排残疾人就业比例 {pct(actual_ratio, 2)}（1%（含）~1.5%），"
            f"按应缴额 50% 缴纳：应缴基数 {yuan(base_amount)} 元 × 50% = {yuan(payable)} 元。"
        )
    else:
        discount_rate = TIER_DISCOUNT_90
        payable = round2(base_amount * TIER_DISCOUNT_90)
        note = (
            f"安排残疾人就业比例 {pct(actual_ratio, 2)}（低于 1%），"
            f"按应缴额 90% 缴纳：应缴基数 {yuan(base_amount)} 元 × 90% = {yuan(payable)} 元。"
        )

    if count > 0 and disabled == 0 and not exempt:
        warnings.append("上年未安排残疾人就业：除缴纳残保金外，建议了解当地按比例安排就业的补贴/奖励政策。")

    return {
        "employees": count,
        "average_wage": wage,
        "disabled_employed": round2(disabled),
        "required_ratio": ratio,
        "actual_ratio": round2(actual_ratio * 10000) / 10000,
        "required_people": required_people,
        "shortfall": shortfall,
        "base_amount": base_amount,
        "exempt": exempt,
        "exemption_threshold": threshold,
        "discount_rate": discount_rate,
        "payable": payable,
        "wage_cap_note": wage_cap_note,
        "note": note,
        "warnings": warnings,
        "policy_sources": DISABILITY_SOURCES,
        "last_verified": LAST_VERIFIED,
        "disclaimer": DISCLAIMER,
    }
