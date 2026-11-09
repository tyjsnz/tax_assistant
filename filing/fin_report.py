"""财务报表（小企业会计准则简化版）生成：资产负债表 + 利润表。

口径说明（**按台账估算，需与账面核对**）
--------------------------------------
* 货币资金 ≈ 本年累计不含税收入 - 本年累计费用（应收、应付均按 0 估算）。
* 税金及附加 = 各季度附加税费 + 印花税。
* 研发相关费用单列「研发费用」；所得税费用按小型微利企业实际税负 5% 估算。
* 资产负债表按「资产总计 = 负债和所有者权益总计」勾稽；年初数在无历史数据时按 0 估算。
"""

from __future__ import annotations

import config
from engine import calc_stamp_tax, calc_vat, period_label, round2, yuan
from ledger.audit import log_calculation
from ledger.expense_ledger import list_expenses
from ledger.income_ledger import period_totals as income_period_totals, summarize_income
from ledger.reports import list_contracts, normalize_quarter, period_bounds
from filing.guide import build_guide
from filing.validator import validate_bundle

FORM_ID = "FIN_REPORT"
FORM_NAME = "财务报表（资产负债表、利润表）"
CATEGORY = "财务报表"
FREQUENCY = "quarterly"
PERIOD_KIND = "quarter"

_ESTIMATE_NOTE = "按台账估算，需与账面核对"

_COST_CATEGORIES = {"工资", "社保", "房租", "研发材料", "折旧"}
_ADMIN_CATEGORIES = {"办公", "差旅", "软件订阅", "其他"}


# =============================================================================
# 内部工具
# =============================================================================
def _line(line_no: str, name: str, value: float, *, highlight: bool = False) -> dict[str, object]:
    amount = round2(value)
    return {
        "line_no": str(line_no),
        "name": name,
        "value": amount,
        "value_text": yuan(amount),
        "source": "台账估算",
        "rule": _ESTIMATE_NOTE,
        "highlight": highlight,
    }


def _quarters_in(period: str) -> list[str]:
    year = int(period[:4])
    current = int(period[-1])
    return [f"{year}Q{index}" for index in range(1, current + 1)]


def _statement_amounts(start: str, end: str, quarters: list[str]) -> dict[str, float]:
    """按日期区间汇总利润表项目（附加税费按区间内各季度分别计算后合计）。"""
    income = summarize_income(start=start, end=end)
    revenue = round2(income["total_exclusive"])
    rows = list_expenses(start=start, end=end)
    rd_expense = 0.0
    cost = 0.0
    admin = 0.0
    for row in rows:
        amount = round2(row.get("amount"))
        category = str(row.get("category") or "其他")
        if bool(row.get("is_rd_related")):
            rd_expense = round2(rd_expense + amount)
        elif category in _COST_CATEGORIES:
            cost = round2(cost + amount)
        else:
            admin = round2(admin + amount)
    surtax = 0.0
    for quarter in quarters:
        totals = income_period_totals(quarter)
        surtax = round2(surtax + calc_vat(totals["total_inclusive"], totals["special_inclusive"], period=quarter)["surtax"])
    contracts = list_contracts(start=start, end=end)
    stamp_tax = round2(calc_stamp_tax(contracts, period=quarters[-1] if quarters else "")["total_payable"])
    surtax = round2(surtax + stamp_tax)
    operating_profit = round2(revenue - cost - surtax - admin - rd_expense)
    income_tax = round2(max(operating_profit, 0) * 0.05)
    net_profit = round2(operating_profit - income_tax)
    return {
        "revenue": revenue,
        "cost": cost,
        "taxes_and_surcharges": surtax,
        "admin": admin,
        "rd_expense": rd_expense,
        "operating_profit": operating_profit,
        "total_profit": operating_profit,
        "income_tax": income_tax,
        "net_profit": net_profit,
        "expense_total": round2(cost + surtax + admin + rd_expense),
        "income_inclusive": round2(income["total_inclusive"]),
    }


def _balance_sheet(cash: float) -> list[dict[str, object]]:
    """按「货币资金≈累计收入-累计费用」估算资产负债表（应收应付为 0）。"""
    return [
        _line("1", "货币资金", cash),
        _line("2", "应收账款", 0.0),
        _line("3", "流动资产合计", cash),
        _line("4", "非流动资产合计", 0.0),
        _line("5", "资产总计", cash, highlight=True),
        _line("6", "应付账款", 0.0),
        _line("7", "应交税费", 0.0),
        _line("8", "负债合计", 0.0),
        _line("9", "实收资本", 0.0),
        _line("10", "未分配利润", cash),
        _line("11", "所有者权益合计", cash),
        _line("12", "负债和所有者权益总计", cash, highlight=True),
    ]


def _income_statement(amounts: dict[str, float]) -> list[dict[str, object]]:
    return [
        _line("1", "一、营业收入", amounts["revenue"]),
        _line("2", "减：营业成本", amounts["cost"]),
        _line("3", "税金及附加", amounts["taxes_and_surcharges"]),
        _line("4", "销售费用", 0.0),
        _line("5", "管理费用", amounts["admin"]),
        _line("6", "研发费用", amounts["rd_expense"]),
        _line("7", "财务费用", 0.0),
        _line("8", "二、营业利润", amounts["operating_profit"], highlight=True),
        _line("9", "三、利润总额", amounts["total_profit"]),
        _line("10", "减：所得税费用", amounts["income_tax"]),
        _line("11", "四、净利润", amounts["net_profit"], highlight=True),
    ]


def _compute(period: object) -> dict[str, object]:
    """计算财务报表所需数据（供构建与校验复用）。"""
    quarter = normalize_quarter(period)
    text, start, end = period_bounds(quarter)
    year = int(quarter[:4])
    ytd_start = f"{year}-01-01"
    quarters = _quarters_in(quarter)
    period_amounts = _statement_amounts(start, end, [quarter])
    ytd_amounts = _statement_amounts(ytd_start, end, quarters)
    cash = round2(ytd_amounts["revenue"] - ytd_amounts["expense_total"])
    return {
        "period": quarter,
        "period_label": period_label(quarter),
        "start": start,
        "end": end,
        "ytd_start": ytd_start,
        "quarters": quarters,
        "period_amounts": period_amounts,
        "ytd_amounts": ytd_amounts,
        "cash": cash,
        "balance_sheet": _balance_sheet(cash),
        "balance_sheet_begin": _balance_sheet(0.0),
        "income_statement_period": _income_statement(period_amounts),
        "income_statement_ytd": _income_statement(ytd_amounts),
        "expected_totals": {
            "total_assets": cash,
            "total_liabilities": 0.0,
            "total_equity": cash,
            "revenue": period_amounts["revenue"],
            "cost": period_amounts["cost"],
            "profit": period_amounts["total_profit"],
            "net_profit": period_amounts["net_profit"],
            "revenue_ytd": ytd_amounts["revenue"],
            "profit_ytd": ytd_amounts["total_profit"],
            "net_profit_ytd": ytd_amounts["net_profit"],
            "income_tax_ytd": ytd_amounts["income_tax"],
            "cash": cash,
        },
    }


# =============================================================================
# 构建
# =============================================================================
def build_fin_report(period: object) -> dict[str, object]:
    """生成财务报表 bundle（资产负债表期末/年初 + 利润表本期/本年累计）。"""
    computed = _compute(period)
    expected = computed["expected_totals"]
    bundle: dict[str, object] = {
        "form_id": FORM_ID,
        "form_name": FORM_NAME,
        "category": CATEGORY,
        "period": computed["period"],
        "period_label": computed["period_label"],
        "frequency": FREQUENCY,
        "generated_at": config.now_text(),
        "status": "草稿",
        "sections": [
            {"title": f"资产负债表（期末数，{_ESTIMATE_NOTE}）", "lines": computed["balance_sheet"]},
            {"title": f"资产负债表（年初数，{_ESTIMATE_NOTE}）", "lines": computed["balance_sheet_begin"]},
            {"title": f"利润表（本期数，{_ESTIMATE_NOTE}）", "lines": computed["income_statement_period"]},
            {"title": f"利润表（本年累计数，{_ESTIMATE_NOTE}）", "lines": computed["income_statement_ytd"]},
        ],
        "totals": {**expected, "estimate_note": _ESTIMATE_NOTE},
        "calc": {
            "period_amounts": computed["period_amounts"],
            "ytd_amounts": computed["ytd_amounts"],
            "note": f"报表数据由台账推算（{_ESTIMATE_NOTE}）：货币资金≈本年累计不含税收入-本年累计费用，"
            "应收、应付按 0 估算；实际报送前请与总账、银行对账单核对。",
        },
        "checks": [],
        "summary": {"level": "PASS", "text": ""},
        "guide": [],
        "data": {
            "period_amounts": computed["period_amounts"],
            "ytd_amounts": computed["ytd_amounts"],
            "quarters": computed["quarters"],
            "start": computed["start"],
            "end": computed["end"],
            "ytd_start": computed["ytd_start"],
            "estimate_note": _ESTIMATE_NOTE,
            "source": "ledger",
        },
        "disclaimer": config.DISCLAIMER,
    }
    log_calculation(
        "财务报表",
        computed["period"],
        {"start": computed["start"], "end": computed["end"], "quarters": computed["quarters"]},
        {
            "total_assets": expected["total_assets"],
            "revenue_period": expected["revenue"],
            "profit_period": expected["profit"],
            "net_profit_period": expected["net_profit"],
            "revenue_ytd": expected["revenue_ytd"],
            "net_profit_ytd": expected["net_profit_ytd"],
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


__all__ = ["FORM_ID", "FORM_NAME", "CATEGORY", "FREQUENCY", "build_fin_report"]
