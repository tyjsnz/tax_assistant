"""离职员工多申报工资的更正测算（确定性代码，P1-E）。

场景：员工已离职，但公司忘记停发/停报，离职后月份仍做了工资表并申报个税；
员工在个人所得税 APP 发起异议申诉后又撤销。撤销只代表撤回异议，**申报数据仍需更正**。

本模块只做**核对与测算**：
* 以离职月份为界，离职后月份的工资/专项扣除/个税全部改为 0；
* 按累计预扣法逐月重算更正后的应预扣税额，得出多缴/少缴个税；
* 估算企业所得税工资薪金调增影响、社保公积金个人部分退费参考；
* 输出逐月更正清单与操作步骤（扣缴端/电子税务局路径）。

实际更正申报仍需在自然人电子税务局（扣缴端）完成，本模块不代替申报。
"""

from __future__ import annotations

from typing import Any

import config
from engine.iit import BASIC_DEDUCTION_PER_MONTH, calc_iit_withholding
from engine.money import round2, yuan
from engine.policy_params import get_params

DISCLAIMER = config.DISCLAIMER

#: 滞纳金按日加收比例（万分之五）
LATE_FEE_DAILY_RATE = 0.0005
#: 小型微利企业实际税负
SMALL_CIT_RATE = 0.05
#: 一般企业所得税税率
STANDARD_CIT_RATE = 0.25


def _income_of(row: dict[str, Any]) -> float:
    return round2(
        round2(row.get("gross_salary")) + round2(row.get("bonus")) + round2(row.get("allowance"))
    )


def _deductions_of(row: dict[str, Any]) -> dict[str, float]:
    return {
        "social": round2(row.get("social_insurance")),
        "housing": round2(row.get("housing_fund")),
        "special": round2(row.get("special_deduction")),
        "other": round2(row.get("other_deduction")),
    }


def correction_periods(leave_date: str, periods: list[str]) -> tuple[list[str], list[str]]:
    """按离职月份拆分：离职当月及之前为保留月份，之后为需删除/改 0 的月份。"""
    import re

    text = str(leave_date or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", text):
        raise ValueError("离职日期格式应为 YYYY-MM-DD（用于确定离职月份）")
    leave_month = text[:7]
    kept: list[str] = []
    removed: list[str] = []
    for period in sorted({str(item) for item in periods}):
        (kept if period <= leave_month else removed).append(period)
    return kept, removed


def estimate_cit_impact(removed_wage_total: float, *, year: str | int | None = None) -> dict[str, Any]:
    """估算多列工资对企业所得税的影响（工资已税前扣除时需纳税调增）。"""
    adjustment = round2(max(removed_wage_total, 0.0))
    params = get_params(str(year)[:4]) if year else None
    standard_rate = float(params["cit_standard_rate"]) if params else STANDARD_CIT_RATE
    small_rate = float(params["small_low_profit_rate"]) if params else SMALL_CIT_RATE
    return {
        "removed_wage_total": adjustment,
        "small_low_profit_rate": small_rate,
        "standard_rate": standard_rate,
        "small_low_profit_tax": round2(adjustment * small_rate),
        "standard_tax": round2(adjustment * standard_rate),
        "note": (
            f"若多列工资 {yuan(adjustment)} 元已在企业所得税税前扣除，需做纳税调增："
            f"小型微利企业约补税 {yuan(adjustment * small_rate)} 元，一般企业约补税 "
            f"{yuan(adjustment * standard_rate)} 元；以前年度还需加收滞纳金（每日万分之五）。"
        ),
    }


def estimate_late_fee(tax: float, days: int, *, rate: float = LATE_FEE_DAILY_RATE) -> float:
    """滞纳金估算：应纳税额 × 日万分之五 × 天数。"""
    return round2(max(float(tax), 0.0) * float(rate) * max(int(days), 0))


def calc_correction_plan(
    employee: dict[str, Any],
    payroll_rows: list[dict[str, Any]],
    *,
    leave_month: str | None = None,
    removed_periods: list[str] | None = None,
    year: str | int | None = None,
    late_fee_days: int = 0,
) -> dict[str, Any]:
    """生成单个员工的离职多申报更正测算。

    参数
    ----
    employee : 员工档案（name；leave_date 可选）
    payroll_rows : 该员工各月工资行（period、应发/奖金/补贴、社保、公积金、专项附加、其他、
                   tax_withheld 原已预扣税额、status）
    leave_month : 手动指定离职月份（YYYY-MM）。员工忘记登记离职日期/从未标记离职时使用，优先级高于 leave_date。
    removed_periods : 手动指定需更正的月份列表；适用于离职月份不确定、但已知申诉/误申报月份的场景，优先级最高。
    year : 用于企业所得税税率的年度（缺省取最后一行所属年度）
    late_fee_days : 若更正后需补税，估算滞纳金天数（0 表示不估算）

    返回
    ----
    dict：离职月份、逐月更正明细、原/更正后收入与税额、多缴/少缴个税、
          企业所得税影响、社保退费参考、操作步骤与风险提示。
    """
    import re as _re

    leave_date = str(employee.get("leave_date") or "")
    rows = [dict(item) for item in (payroll_rows or []) if str(item.get("period") or "")]
    if not rows:
        raise ValueError("该员工没有工资记录，无需更正。")
    rows.sort(key=lambda item: str(item.get("period")))
    periods = [str(item.get("period")) for item in rows]

    manual_removed = [str(item) for item in (removed_periods or []) if str(item).strip()]
    if manual_removed:
        unknown = [item for item in manual_removed if item not in periods]
        if unknown:
            raise ValueError(f"需更正月份不在该员工工资记录中：{'、'.join(unknown)}")
        kept_periods = [item for item in periods if item not in manual_removed]
        removed_periods = [item for item in periods if item in manual_removed]
        leave_month = leave_month or (max(kept_periods) if kept_periods else removed_periods[0])
        cutoff_source = "manual_periods"
    elif leave_month:
        if not _re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", str(leave_month).strip()):
            raise ValueError("手动离职月份/日期格式应为 YYYY-MM 或 YYYY-MM-DD")
        leave_month = str(leave_month).strip()[:7]
        kept_periods, removed_periods = correction_periods(f"{leave_month}-01", periods)
        cutoff_source = "manual_leave_month"
    elif leave_date:
        leave_month = leave_date[:7]
        kept_periods, removed_periods = correction_periods(leave_date, periods)
        cutoff_source = "leave_date"
    else:
        raise ValueError(
            "该员工未登记离职日期。请补充离职日期，或使用手动模式指定离职月份/需更正月份"
            "（适用于员工忘记登记离职、未在系统标记离职的情况）。"
        )

    row_map = {str(item.get("period")): item for item in rows}
    one_time = str(employee.get("deduction_choice") or "") == "一次性60000"
    qualified = employee.get("is_deduction_qualified")
    if qualified is None:
        qualified = True

    detail: list[dict[str, Any]] = []
    cumulative_income = 0.0
    cumulative_social = 0.0
    cumulative_housing = 0.0
    cumulative_special = 0.0
    cumulative_other = 0.0
    cumulative_tax = 0.0
    months_declared = 0
    original_income_total = 0.0
    original_tax_total = 0.0
    corrected_income_total = 0.0
    corrected_tax_total = 0.0
    removed_wage_total = 0.0
    removed_social = 0.0
    removed_housing = 0.0

    for period in sorted(periods):
        row = row_map[period]
        income = _income_of(row)
        deductions = _deductions_of(row)
        original_tax = round2(row.get("tax_withheld"))
        original_income_total = round2(original_income_total + income)
        original_tax_total = round2(original_tax_total + original_tax)

        if period in removed_periods:
            removed_wage_total = round2(removed_wage_total + income)
            removed_social = round2(removed_social + deductions["social"])
            removed_housing = round2(removed_housing + deductions["housing"])
            detail.append(
                {
                    "period": period,
                    "action": "删除申报/收入改 0",
                    "original_income": income,
                    "corrected_income": 0.0,
                    "original_tax": original_tax,
                    "corrected_tax": 0.0,
                    "tax_diff": round2(0.0 - original_tax),
                    "cumulative_income": cumulative_income,
                    "months_declared": months_declared,
                    "note": "离职后月份，需在扣缴端更正申报中删除该员工记录或收入改为 0",
                }
            )
            continue

        # 保留月份：按累计预扣法逐月重算
        months_declared += 1
        cumulative_income = round2(cumulative_income + income)
        cumulative_social = round2(cumulative_social + deductions["social"])
        cumulative_housing = round2(cumulative_housing + deductions["housing"])
        cumulative_special = round2(cumulative_special + deductions["special"])
        cumulative_other = round2(cumulative_other + deductions["other"])
        result = calc_iit_withholding(
            cumulative_income,
            months_declared=months_declared,
            cumulative_social_insurance=cumulative_social,
            cumulative_housing_fund=cumulative_housing,
            cumulative_special_additional=cumulative_special,
            cumulative_other_deduction=cumulative_other,
            cumulative_tax_withheld=cumulative_tax,
            one_time_basic_deduction=one_time,
            period=period,
        )
        current_tax = round2(result["tax_payable"])
        cumulative_tax = round2(result["cumulative_tax"])
        corrected_income_total = round2(corrected_income_total + income)
        corrected_tax_total = round2(corrected_tax_total + current_tax)
        detail.append(
            {
                "period": period,
                "action": "保留申报（核对离职当月工资）",
                "original_income": income,
                "corrected_income": income,
                "original_tax": original_tax,
                "corrected_tax": current_tax,
                "tax_diff": round2(current_tax - original_tax),
                "cumulative_income": cumulative_income,
                "months_declared": months_declared,
                "taxable_income": result.get("cumulative_taxable_income"),
                "note": "离职当月正常工资（按实际工作天数结算），可保留",
            }
        )

    tax_refund = round2(max(original_tax_total - corrected_tax_total, 0.0))
    tax_payable = round2(max(corrected_tax_total - original_tax_total, 0.0))
    cit = estimate_cit_impact(removed_wage_total, year=year or (removed_periods[-1][:4] if removed_periods else leave_month[:4]))
    late_fee = estimate_late_fee(tax_payable, late_fee_days)

    warnings: list[str] = []
    if removed_periods:
        warnings.append(
            f"发现 {len(removed_periods)} 个离职后月份仍在申报（{', '.join(removed_periods)}），"
            f"涉及工资 {yuan(removed_wage_total)} 元，需逐月更正申报。"
        )
    if tax_refund > 0:
        warnings.append(f"更正后预计多缴个税 {yuan(tax_refund)} 元，可申请退（抵）税。")
    if tax_payable > 0:
        warnings.append(f"更正后预计少缴个税 {yuan(tax_payable)} 元，需补税并可能产生滞纳金。")
    if not removed_periods:
        warnings.append("未发现离职后仍申报的工资记录；如员工申诉，请核对申诉类型后再处理。")
    warnings.append(
        "员工在个税 APP 撤销申诉不等于更正申报：撤销仅撤回异议，申报数据错误仍需按本方案更正。"
    )

    steps = [
        "第一步：核实离职日期、最后工作日、离职当月工资结算与银行流水，确认哪些月份属于误申报。",
        "第二步：自然人电子税务局（扣缴端）→ 人员信息采集 → 找到该员工 → 人员状态改为「离职」并填写离职日期 → 报送。",
        "第三步：综合所得申报 → 正常工资薪金所得 → 选择需更正的所属月份 →「更正申报」→ 删除该员工记录或收入改为 0 → 重新计算 → 发送申报。",
        *(
            [
                f"第四步：按月份顺序逐月更正后续月份（{', '.join(removed_periods[1:])}），"
                "因累计预扣法累计数会随之变化，需逐月重算并发送。"
                if len(removed_periods) > 1
                else "第四步：核对更正后的累计收入、累计减除费用与已预扣税额，确保后续月份数据一致。"
            ]
        ),
        "第五步：更正后形成多缴税款的，在电子税务局「我要办税 → 一般退（抵）税管理 → 误收多缴退抵税」申请退税或抵缴；少缴的补税并按日加收万分之五滞纳金。",
        "第六步：企业所得税联动——多列工资已税前扣除的，年度汇算做纳税调增；涉及以前年度的更正年度申报并补税、滞纳金。",
        "第七步：社保/公积金办理减员，多缴月份申请退费；残保金人数口径同步核对。",
        "第八步：账务红冲多计提工资及应付职工薪酬；跨年度通过「以前年度损益调整」处理；核对银行是否实际发放。",
        "第九步：留存离职证明、工资结算单、银行流水、更正申报截图、退税/补税凭证、员工申诉及撤销截图、情况说明，闭环归档。",
    ]

    return {
        "employee_id": str(employee.get("id") or ""),
        "employee_name": str(employee.get("name") or ""),
        "leave_date": leave_date,
        "leave_month": leave_month,
        "cutoff_source": cutoff_source,
        "kept_periods": kept_periods,
        "removed_periods": removed_periods,
        "rows": detail,
        "totals": {
            "original_income": original_income_total,
            "corrected_income": corrected_income_total,
            "original_tax": original_tax_total,
            "corrected_tax": corrected_tax_total,
            "tax_refund": tax_refund,
            "tax_payable": tax_payable,
            "late_fee_estimate": late_fee,
            "removed_wage_total": removed_wage_total,
            "removed_social_personal": removed_social,
            "removed_housing_personal": removed_housing,
            "months_to_correct": len(removed_periods),
        },
        "cit_impact": cit,
        "social_refund_reference": round2(removed_social + removed_housing),
        "steps": steps,
        "warnings": warnings,
        "dispute_note": (
            "员工撤销申诉路径：个人所得税 APP → 服务 → 收入纳税明细查询 → 异议申诉记录 → 撤销申诉。"
            "撤销后请保存截图；若税务核查，提供情况说明与更正记录。"
        ),
        "disclaimer": DISCLAIMER,
        "calculated_at": config.now_text(),
    }
