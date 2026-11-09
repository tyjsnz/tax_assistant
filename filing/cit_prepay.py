"""企业所得税月（季）度预缴纳税申报表（A 类）生成。

行次口径
--------
1 营业收入（本年累计，不含税）
2 营业成本（本年累计）
3 利润总额 = 1 - 2
4 加：特定业务应纳税所得额（本企业无，填 0）
5 减：不征税收入（0）
6 减：免税收入（0）
7 减：减计收入（0）
8 减：加计扣除（研发费用本年累计加计扣除额）
9 实际利润额 = 3 + 4 - 5 - 6 - 7 - 8
10 税率（25%）
11 应纳所得税额 = 9 × 25%
12 减免所得税额 = 11 - 13（小型微利企业优惠部分）
13 实际应纳所得税额 = 9 × 实际税负（小型微利企业 5%）
14 本年累计已预缴税额（按前几个季度预缴额累计）
15 本期应补（退）税额 = 13 - 14

计算全部调用 :func:`engine.calc_cit_prepay`（本年累计口径），研发费用取自研发辅助账。
"""

from __future__ import annotations

from typing import Any

import config
from engine import calc_cit_prepay, period_label, round2, yuan
from ledger.audit import log_calculation
from ledger.expense_ledger import period_totals as expense_period_totals
from ledger.income_ledger import period_totals as income_period_totals
from ledger.reports import normalize_quarter, quarterly_summary
from ledger.rd_auxiliary import ytd_summary as rd_ytd_summary
from filing.guide import build_guide
from filing.validator import validate_bundle

FORM_ID = "CIT_PREPAY"
FORM_NAME = "企业所得税月（季）度预缴纳税申报表（A类）"
CATEGORY = "企业所得税"
FREQUENCY = "quarterly"
PERIOD_KIND = "quarter"

_LINE_NAMES = {
    "1": "营业收入",
    "2": "营业成本",
    "3": "利润总额",
    "4": "加：特定业务应纳税所得额",
    "5": "减：不征税收入",
    "6": "减：免税收入",
    "7": "减：减计收入",
    "8": "减：加计扣除（研发费用）",
    "9": "实际利润额",
    "10": "税率（25%）",
    "11": "应纳所得税额",
    "12": "减免所得税额",
    "13": "实际应纳所得税额",
    "14": "本年累计已预缴税额",
    "15": "本期应补（退）税额",
}

_LINE_RULES = {
    "1": "收入台账本年累计不含税收入",
    "2": "费用台账本年累计成本费用",
    "3": "第 1 行 - 第 2 行",
    "4": "本企业无特定业务，填 0",
    "5": "本企业无不征税收入，填 0",
    "6": "本企业无免税收入，填 0",
    "7": "本企业无减计收入，填 0",
    "8": "研发辅助账本年累计加计扣除额（100%）",
    "9": "第 3 行 + 第 4 行 - 第 5 行 - 第 6 行 - 第 7 行 - 第 8 行",
    "10": "法定税率 25%；小型微利企业实际税负 5%",
    "11": "第 9 行 × 25%",
    "12": "第 11 行 - 第 13 行",
    "13": "第 9 行 × 实际税负（小型微利企业 5%）",
    "14": "本年前几个季度已预缴税额合计",
    "15": "第 13 行 - 第 14 行",
}

_LINE_SOURCES = {
    "1": "收入台账（本年累计）",
    "2": "费用台账（本年累计）",
    "8": "研发辅助账（本年累计）",
    "14": "前几个季度预缴结果累计",
}


# =============================================================================
# 内部工具
# =============================================================================
def _line(line_no: str, value: Any, source: str = "", rule: str = "", value_text: str = "") -> dict[str, Any]:
    amount = round2(value)
    return {
        "line_no": line_no,
        "name": _LINE_NAMES[line_no],
        "value": amount,
        "value_text": value_text or yuan(amount),
        "source": source or _LINE_SOURCES.get(line_no, "按台账口径估算"),
        "rule": rule or _LINE_RULES[line_no],
        "highlight": line_no in ("9", "13", "15"),
    }


def _quarter_stats(quarters: list[str]) -> list[dict[str, Any]]:
    """逐季度统计收入、费用、利润与研发费用（用于计算本年累计与已预缴税额）。"""
    stats: list[dict[str, Any]] = []
    for quarter in quarters:
        income = income_period_totals(quarter)
        expense = expense_period_totals(quarter)
        rd = quarterly_summary(quarter)
        stats.append(
            {
                "period": quarter,
                "income_exclusive": income["total_exclusive"],
                "income_inclusive": income["total_inclusive"],
                "expense": expense["total"],
                "profit": round2(income["total_exclusive"] - expense["total"]),
                "rd_cost": rd["rd_cost"],
                "rd_deduction": rd["rd_deduction"],
            }
        )
    return stats


def _compute(period: Any) -> dict[str, Any]:
    """计算企业所得税预缴所需的全部数据（供构建与校验复用）。"""
    quarter = normalize_quarter(period)
    year = int(quarter[:4])
    current_index = int(quarter[-1])
    quarters = [f"{year}Q{index}" for index in range(1, current_index + 1)]
    stats = _quarter_stats(quarters)
    current = stats[-1]
    ytd_profit = round2(sum(item["profit"] for item in stats))
    rd_ytd = rd_ytd_summary(quarter)

    # 本年累计已预缴税额：按前几个季度的预缴结果逐季累计
    paid = 0.0
    for index in range(1, current_index):
        prior_stats = stats[:index]
        prior_quarter_profit = prior_stats[-1]["profit"]
        prior_ytd_profit = round2(sum(item["profit"] for item in prior_stats))
        prior_rd_cost = round2(sum(item["rd_cost"] for item in prior_stats))
        prior = calc_cit_prepay(
            prior_quarter_profit,
            prior_ytd_profit,
            paid,
            period=quarters[index - 1],
            rd_cost_ytd=prior_rd_cost,
        )
        paid = round2(paid + prior["prepay_payable"])

    calc = calc_cit_prepay(
        current["profit"],
        ytd_profit,
        paid,
        period=quarter,
        rd_cost_ytd=rd_ytd["rd_cost"],
    )
    taxable = round2(calc["taxable_income_ytd"])
    standard_tax = round2(taxable * 0.25)
    actual_tax = round2(calc["cit_accrued"])
    reduction = round2(standard_tax - actual_tax)
    line_values = {
        "1": round2(sum(item["income_exclusive"] for item in stats)),
        "2": round2(sum(item["expense"] for item in stats)),
        "3": ytd_profit,
        "4": 0.0,
        "5": 0.0,
        "6": 0.0,
        "7": 0.0,
        "8": round2(calc["rd_deduction"]),
        "9": taxable,
        "10": 0.25,
        "11": standard_tax,
        "12": reduction,
        "13": actual_tax,
        "14": round2(calc["cit_paid"]),
        "15": round2(calc["prepay_payable"]),
    }
    return {
        "period": quarter,
        "period_label": period_label(quarter),
        "year": year,
        "start": f"{year}-01-01",
        "end": f"{year}-{current_index * 3:02d}-{_quarter_end_day(year, current_index)}",
        "quarters": quarters,
        "quarter_stats": stats,
        "quarter_profit": current["profit"],
        "ytd_profit": ytd_profit,
        "rd_ytd": {
            "rd_cost": round2(rd_ytd["rd_cost"]),
            "rd_deduction": round2(rd_ytd["rd_deduction"]),
            "quarters": rd_ytd["quarters"],
        },
        "calc": calc,
        "line_values": line_values,
    }


def _quarter_end_day(year: int, quarter_index: int) -> str:
    from calendar import monthrange

    month = quarter_index * 3
    return f"{monthrange(year, month)[1]:02d}"


# =============================================================================
# 构建
# =============================================================================
def build_cit_prepay(period: Any) -> dict[str, Any]:
    """生成企业所得税季度预缴申报表 bundle（A 类，本年累计口径）。"""
    computed = _compute(period)
    line_values = computed["line_values"]
    lines = [_line(str(index), line_values[str(index)]) for index in range(1, 16)]
    # 第 10 行税率用百分比展示
    lines[9]["value_text"] = "25%"
    calc = computed["calc"]
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
            {
                "title": "A类预缴表（本年累计）",
                "lines": lines,
            }
        ],
        "totals": {
            "revenue": line_values["1"],
            "cost": line_values["2"],
            "profit": line_values["3"],
            "rd_deduction": line_values["8"],
            "taxable_income": line_values["9"],
            "standard_tax": line_values["11"],
            "tax_reduction": line_values["12"],
            "cit_accrued": line_values["13"],
            "cit_paid": line_values["14"],
            "prepay_payable": line_values["15"],
            "quarter_profit": computed["quarter_profit"],
            "ytd_profit": computed["ytd_profit"],
            "quarter_income_exclusive": computed["quarter_stats"][-1]["income_exclusive"],
            "quarter_expense": computed["quarter_stats"][-1]["expense"],
            "effective_rate": calc["effective_rate"],
            "standard_rate": 0.25,
            "qualifies_small_low_profit": calc["qualifies_small_low_profit"],
        },
        "calc": calc,
        "checks": [],
        "summary": {"level": "PASS", "text": ""},
        "guide": [],
        "data": {
            "quarter_profit": computed["quarter_profit"],
            "ytd_profit": computed["ytd_profit"],
            "rd_ytd": computed["rd_ytd"],
            "quarters": computed["quarters"],
            "quarter_stats": computed["quarter_stats"],
            "start": computed["start"],
            "end": computed["end"],
            "source": "ledger",
        },
        "disclaimer": config.DISCLAIMER,
    }
    log_calculation(
        "企业所得税预缴",
        computed["period"],
        {
            "quarter_profit": computed["quarter_profit"],
            "ytd_profit": computed["ytd_profit"],
            "rd_cost_ytd": computed["rd_ytd"]["rd_cost"],
            "ytd_cit_paid": line_values["14"],
        },
        {
            "taxable_income_ytd": line_values["9"],
            "cit_accrued": line_values["13"],
            "prepay_payable": line_values["15"],
            "effective_rate": calc["effective_rate"],
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


__all__ = ["FORM_ID", "FORM_NAME", "CATEGORY", "FREQUENCY", "build_cit_prepay"]
