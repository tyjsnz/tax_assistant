"""台账报表：月度/季度汇总、收支报表、利润表、驾驶舱数据，以及合同台账（印花税）辅助。

口径说明
--------
* 收入按**不含税**口径计算利润；增值税、附加税费单独列示。
* 增值税按季度判定免税额度，因此月度报表中的 ``tax`` 仅在季末月份体现该季度应纳税费。
* 所有报表均为「台账估算」，需与账面对账后使用。
"""

from __future__ import annotations

import csv
import re
from calendar import monthrange
from datetime import datetime
from pathlib import Path
from typing import Any

import config
from engine import (
    calc_stamp_tax,
    calc_vat,
    current_quarter,
    get_params,
    period_label,
    period_range,
    round2,
)
from engine.tax_calendar import (
    next_deadline as engine_next_deadline,
    reminders as engine_reminders,
    upcoming_deadlines,
)
from ledger import models
from ledger.audit import log_user_action
from ledger.expense_ledger import list_expenses, period_totals as expense_period_totals, summarize_expenses
from ledger.income_ledger import list_income, period_totals as income_period_totals, summarize_income
from ledger.rd_auxiliary import summarize_rd
from ledger.store import table

CONTRACT_LABEL = "合同台账"

_CONTRACT_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "日期", "签订日期", "时间"),
    "contract_no": ("contract_no", "合同编号", "编号", "合同号"),
    "counterparty": ("counterparty", "对方单位", "客户", "对方", "供应商"),
    "contract_type": ("contract_type", "合同类型", "类型"),
    "amount": ("amount", "计税金额", "金额", "合同金额"),
    "is_rd_related": ("is_rd_related", "研发相关", "是否研发"),
    "note": ("note", "备注"),
}


# =============================================================================
# 期间工具（供 filing 复用）
# =============================================================================
def normalize_quarter(period: Any) -> str:
    """把季度/月份/年度期间规范为季度字符串 ``2026Q1``；非季度口径抛 ``ValueError``。"""
    text = models.normalize_period(period, default="")
    if not text:
        raise ValueError("请提供税款所属期间，如 2026Q1")
    if re.match(r"^\d{4}Q[1-4]$", text):
        return text
    match = re.match(r"^(\d{4})-(\d{1,2})$", text)
    if match:
        month = int(match.group(2))
        return f"{match.group(1)}Q{(month - 1) // 3 + 1}"
    raise ValueError(f"期间格式不正确：{period}，请使用 2026Q1 形式（按季申报）")


def period_bounds(period: Any) -> tuple[str, str, str]:
    """返回 ``(规范化期间, 起始日, 结束日)``，支持季度/月份/年度。"""
    text = models.normalize_period(period, default="")
    if not text:
        raise ValueError("请提供所属期间，如 2026Q1")
    try:
        start, end = period_range(text)
    except ValueError as exc:
        raise ValueError(f"期间格式不正确：{period}，请使用 2026Q1 / 2026-03 / 2026 形式") from exc
    return text, start.isoformat(), end.isoformat()


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    last_day = monthrange(year, month)[1]
    return f"{year}-{month:02d}-01", f"{year}-{month:02d}-{last_day:02d}"


# =============================================================================
# 月度 / 季度汇总
# =============================================================================
def monthly_summary(year: int, months: list[int] | None = None) -> list[dict[str, Any]]:
    """按月度汇总收入、费用、利润与税费（``tax`` 仅季末月份体现季度应纳税费）。"""
    year = int(year)
    if months is None:
        month_list = list(range(1, 13))
    else:
        month_list = []
        for item in months:
            month = int(item)
            if 1 <= month <= 12 and month not in month_list:
                month_list.append(month)
        month_list.sort()
    rows: list[dict[str, Any]] = []
    quarter_tax_cache: dict[str, float] = {}
    for month in month_list:
        month_text = f"{year}-{month:02d}"
        start, end = _month_bounds(year, month)
        income = summarize_income(start=start, end=end)
        expense = summarize_expenses(start=start, end=end)
        income_exclusive = round2(income["total_exclusive"])
        expense_total = round2(expense["total"])
        tax = 0.0
        quarter = f"{year}Q{(month - 1) // 3 + 1}"
        if month % 3 == 0:
            if quarter not in quarter_tax_cache:
                totals = income_period_totals(quarter)
                calc = calc_vat(totals["total_inclusive"], totals["special_inclusive"], period=quarter)
                quarter_tax_cache[quarter] = round2(calc["total_tax"])
            tax = quarter_tax_cache[quarter]
        rows.append(
            {
                "month": month_text,
                "month_label": f"{month}月",
                "income_inclusive": round2(income["total_inclusive"]),
                "income_exclusive": income_exclusive,
                "expense": expense_total,
                "profit": round2(income_exclusive - expense_total),
                "tax": tax,
                "quarter": quarter,
            }
        )
    return rows


def quarterly_summary(period: Any) -> dict[str, Any]:
    """季度经营与税费汇总：收入、费用、利润、增值税、附加税、研发费用与加计扣除。"""
    text, start, end = period_bounds(period)
    income = income_period_totals(text)
    expense = expense_period_totals(text)
    calc = calc_vat(income["total_inclusive"], income["special_inclusive"], period=text)
    rd = summarize_rd(text)
    return {
        "period": text,
        "label": period_label(text),
        "start": start,
        "end": end,
        "income_inclusive": income["total_inclusive"],
        "income_exclusive": income["total_exclusive"],
        "special_inclusive": income["special_inclusive"],
        "ordinary_inclusive": income["ordinary_inclusive"],
        "expense": expense["total"],
        "profit": round2(income["total_exclusive"] - expense["total"]),
        "vat": calc["vat_payable"],
        "vat_exempt": calc["vat_exempt"],
        "surtax": calc["surtax"],
        "total_tax": calc["total_tax"],
        "rd_cost": rd["total_collectible"],
        "rd_deduction": rd["deduction_amount"],
        "within_exemption_limit": calc["within_exemption_limit"],
    }


def income_expense_report(period: Any) -> dict[str, Any]:
    """季度收支报表：在季度汇总基础上补充收入/费用类别拆分与笔数。"""
    text, start, end = period_bounds(period)
    summary = quarterly_summary(text)
    income = summarize_income(start=start, end=end)
    expense = summarize_expenses(start=start, end=end)
    summary.update(
        {
            "start": start,
            "end": end,
            "by_category_income": income["by_category"],
            "by_category_expense": {category: bucket["amount"] for category, bucket in expense["by_category"].items()},
            "count_income": income["count"],
            "count_expense": expense["count"],
        }
    )
    return summary


def profit_statement(period: Any) -> list[dict[str, Any]]:
    """按台账口径估算利润表（小企业会计准则简化版），返回行次列表。

    营业成本取非研发的工资/社保/房租/研发材料/折旧；管理费用取非研发的办公/差旅/
    软件订阅/其他；研发相关费用单列「研发费用」；所得税费用按小型微利实际税负 5% 估算。
    """
    text, start, end = period_bounds(period)
    income = income_period_totals(text)
    revenue = round2(income["total_exclusive"])
    calc = calc_vat(income["total_inclusive"], income["special_inclusive"], period=text)
    surtax = round2(calc["surtax"])

    rows = list_expenses(start=start, end=end)
    rd_expense = 0.0
    cost = 0.0
    admin = 0.0
    cost_categories = {"工资", "社保", "房租", "研发材料", "折旧"}
    admin_categories = {"办公", "差旅", "软件订阅", "其他"}
    for row in rows:
        amount = round2(row.get("amount"))
        category = str(row.get("category") or "其他")
        if models.parse_bool(row.get("is_rd_related")):
            rd_expense = round2(rd_expense + amount)
        elif category in cost_categories:
            cost = round2(cost + amount)
        elif category in admin_categories:
            admin = round2(admin + amount)
        else:
            admin = round2(admin + amount)

    stamp_tax = _period_stamp_tax(text, start, end)
    taxes_and_surcharges = round2(surtax + stamp_tax)
    operating_profit = round2(revenue - cost - taxes_and_surcharges - admin - rd_expense)
    total_profit = operating_profit
    params = get_params(text)
    income_tax = round2(max(total_profit, 0) * float(params["small_low_profit_rate"]))
    net_profit = round2(total_profit - income_tax)
    return [
        {"row": "一、营业收入", "amount": revenue},
        {"row": "减：营业成本", "amount": cost},
        {"row": "税金及附加", "amount": taxes_and_surcharges},
        {"row": "销售费用", "amount": 0.0},
        {"row": "管理费用", "amount": admin},
        {"row": "研发费用", "amount": rd_expense},
        {"row": "财务费用", "amount": 0.0},
        {"row": "二、营业利润", "amount": operating_profit},
        {"row": "三、利润总额", "amount": total_profit},
        {"row": "减：所得税费用", "amount": income_tax},
        {"row": "四、净利润", "amount": net_profit},
    ]


def _period_stamp_tax(period: str, start: str, end: str) -> float:
    """按期间合同台账估算印花税（小规模六税两费减半）。"""
    contracts = list_contracts(start=start, end=end)
    if not contracts:
        return 0.0
    return round2(calc_stamp_tax(contracts, period=period)["total_payable"])


def dashboard() -> dict[str, Any]:
    """首页驾驶舱数据：本季度收入/费用/利润/税费、本年累计、台账笔数、申报提醒与最近记录。"""
    period = current_quarter()
    quarter = quarterly_summary(period)
    today = config.today()
    start_of_year = f"{today.year}-01-01"
    ytd_income = summarize_income(start=start_of_year, end=today.isoformat())
    ytd_expense = summarize_expenses(start=start_of_year, end=today.isoformat())
    upcoming = upcoming_deadlines(today, lookahead_days=120)
    deadlines = engine_reminders(today, lookahead_days=45)
    recent_income = list_income()[-5:][::-1]
    recent_expense = list_expenses()[-5:][::-1]
    return {
        "period": period,
        "period_label": period_label(period),
        "quarter_income_inclusive": quarter["income_inclusive"],
        "quarter_income_exclusive": quarter["income_exclusive"],
        "quarter_expense": quarter["expense"],
        "quarter_profit": quarter["profit"],
        "vat_payable": quarter["vat"],
        "vat_exempt": quarter["vat_exempt"],
        "surtax": quarter["surtax"],
        "total_tax": quarter["total_tax"],
        "ytd_income": ytd_income["total_exclusive"],
        "ytd_income_inclusive": ytd_income["total_inclusive"],
        "ytd_expense": ytd_expense["total"],
        "ytd_profit": round2(ytd_income["total_exclusive"] - ytd_expense["total"]),
        "counts": {
            "income": table("income").count(),
            "expense": table("expense").count(),
            "rd": table("rd").count(),
            "contracts": table("contracts").count(),
        },
        "next_deadline": upcoming[0] if upcoming else engine_next_deadline(today),
        "reminders": deadlines,
        "recent_income": recent_income,
        "recent_expense": recent_expense,
        "updated_at": config.now_text(),
        "disclaimer": config.DISCLAIMER,
    }


# =============================================================================
# 合同台账（印花税）辅助 CRUD
# =============================================================================
def add_contract(
    date: Any = None,
    counterparty: str = "",
    amount: float = 0.0,
    *,
    contract_type: str = "技术合同",
    contract_no: str = "",
    is_rd_related: bool = False,
    note: str = "",
) -> dict[str, Any]:
    """新增一份合同台账记录（用于印花税申报），返回写库后的记录。"""
    record = models.normalize_contract(
        {
            "date": date,
            "contract_no": contract_no,
            "counterparty": counterparty,
            "contract_type": contract_type,
            "amount": amount,
            "is_rd_related": is_rd_related,
            "note": note,
        }
    )
    added = table("contracts").append(record)
    log_user_action(
        "新增合同",
        f"{added['date']} {added['contract_type']} {added['counterparty'] or '（未填对方）'} "
        f"计税金额 {added['amount']:.2f} 元，编号 {added['id']}",
    )
    return added


def list_contracts(start: Any = None, end: Any = None, **filters: Any) -> list[dict[str, Any]]:
    """按签订日期区间查询合同台账（按日期升序），可按合同类型等字段过滤。"""
    rows = table("contracts").filter(start=start, end=end)
    if filters:
        filtered = []
        for row in rows:
            matched = True
            for key, value in filters.items():
                if value is None or value == "":
                    continue
                if key == "is_rd_related":
                    if models.parse_bool(row.get(key)) != models.parse_bool(value):
                        matched = False
                        break
                    continue
                if str(row.get(key)) != str(value).strip():
                    matched = False
                    break
            if matched:
                filtered.append(row)
        rows = filtered
    return sorted(rows, key=lambda row: str(row.get("date") or ""))


def get_contract(contract_id: str) -> dict[str, Any] | None:
    """按编号读取一份合同记录。"""
    return table("contracts").get(contract_id)


def update_contract(contract_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """按编号更新合同记录，返回更新后的记录。"""
    if not isinstance(patch, dict):
        raise ValueError("更新内容必须是 dict")
    current = get_contract(contract_id)
    if current is None:
        raise KeyError(f"{CONTRACT_LABEL}中不存在编号 {contract_id}")
    merged = dict(current)
    for key, value in patch.items():
        if key in ("id", "created_at"):
            continue
        merged[key] = value
    record = models.normalize_contract(merged)
    updated = table("contracts").update(contract_id, record)
    log_user_action("修改合同", f"编号 {contract_id}：{updated['counterparty']} {updated['amount']:.2f} 元")
    return updated


def delete_contract(contract_id: str) -> bool:
    """按编号删除合同记录，返回是否删除成功。"""
    deleted = table("contracts").delete(contract_id)
    if deleted:
        log_user_action("删除合同", f"删除编号 {contract_id}")
    return deleted


def import_contracts_csv(path: Any) -> int:
    """从 CSV 导入合同台账，返回成功导入条数（兼容中英文表头）。"""
    csv_path = Path(path)
    if not csv_path.exists():
        raise ValueError(f"文件不存在：{csv_path}")
    records = []
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                payload: dict[str, Any] = {}
                for field_name, aliases in _CONTRACT_ALIASES.items():
                    for alias in aliases:
                        if alias in raw and str(raw[alias]).strip() != "":
                            payload[field_name] = raw[alias]
                            break
                try:
                    records.append(models.normalize_contract(payload))
                except ValueError:
                    continue
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        raise ValueError(f"读取 CSV 失败：{exc}") from exc
    added = table("contracts").append_many(records)
    log_user_action("导入合同CSV", f"文件 {csv_path.name}，成功导入 {len(added)} 条")
    return len(added)


def export_contracts_csv(path: Any = None) -> Path:
    """导出合同台账为 CSV（中文表头），返回文件路径。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = Path(path) if path else Path(config.EXPORT_DIR) / f"{CONTRACT_LABEL}_{stamp}.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    columns = models.TABLE_DEFS["contracts"]["columns"]
    rows = list_contracts()
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([column["label"] for column in columns])
        for row in rows:
            writer.writerow([row.get(column["name"]) for column in columns])
    log_user_action("导出合同CSV", f"文件 {target.name}，共 {len(rows)} 条")
    return target


def summarize_contracts(start: Any = None, end: Any = None) -> dict[str, Any]:
    """汇总合同台账：份数、计税金额合计、按合同类型拆分。"""
    rows = list_contracts(start=start, end=end)
    by_type: dict[str, dict[str, Any]] = {}
    total_amount = 0.0
    for row in rows:
        contract_type = str(row.get("contract_type") or "其他")
        amount = round2(row.get("amount"))
        total_amount = round2(total_amount + amount)
        bucket = by_type.setdefault(contract_type, {"count": 0, "amount": 0.0})
        bucket["count"] = int(bucket["count"]) + 1
        bucket["amount"] = round2(bucket["amount"] + amount)
    return {"count": len(rows), "total_amount": total_amount, "by_type": by_type}


__all__ = [
    "normalize_quarter",
    "period_bounds",
    "monthly_summary",
    "quarterly_summary",
    "income_expense_report",
    "profit_statement",
    "dashboard",
    "add_contract",
    "list_contracts",
    "get_contract",
    "update_contract",
    "delete_contract",
    "import_contracts_csv",
    "export_contracts_csv",
    "summarize_contracts",
]
