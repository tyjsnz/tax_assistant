"""申报草稿中心：按税种判断“本月/本季能准备什么草稿、还缺什么数据”（v1.55）。

原则：
* 只生成草稿，不自动申报；
* 工资/个税可从上一期带出；增值税/印花税必须用本期数据；
* 缺数据时明确提示，不把“空数据”当成零申报。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from engine import current_quarter, period_label
from ledger import employees, payroll, reports


def _previous_month(month: str) -> str:
    year, mon = [int(part) for part in month.split("-")]
    if mon == 1:
        return f"{year - 1}-12"
    return f"{year}-{mon - 1:02d}"


def _active_employee_count() -> int:
    try:
        return len(employees.list_employees(status="在职", include_left=False))
    except Exception:  # noqa: BLE001
        return 0


def _item(
    key: str, label: str, status: str, source: str, detail: str,
    *, warnings: list[str] | None = None, page: str = "", action: str = "", scope: str = "monthly",
) -> dict[str, Any]:
    return {
        "key": key, "label": label, "status": status, "source": source,
        "detail": detail, "warnings": warnings or [], "page": page, "action": action, "scope": scope,
    }


def build_center(month: str | None = None, quarter: str | None = None) -> dict[str, Any]:
    today = date.today()
    month = month or today.strftime("%Y-%m")
    quarter = quarter or current_quarter(today)
    previous = _previous_month(month)
    active_count = _active_employee_count()
    items: list[dict[str, Any]] = []

    # 工资 / 个税
    current_rows = payroll.list_payroll(month)
    previous_rows = payroll.list_payroll(previous)
    if current_rows:
        statuses = {str(row.get("status") or "草稿") for row in current_rows}
        if statuses == {"已申报"}:
            status = "done"
        elif statuses <= {"已确认", "已申报"}:
            status = "confirmed"
        else:
            status = "draft"
        zero_rows = [row for row in current_rows if float(row.get("gross_salary") or 0) <= 0]
        warnings = []
        if zero_rows:
            warnings.append(f"{len(zero_rows)} 人应发工资为 0，请确认是否漏填/停薪")
        if active_count > len(current_rows):
            warnings.append(f"在职员工 {active_count} 人，工资表只有 {len(current_rows)} 人，请核对新增人员")
        detail = f"{len(current_rows)} 人 · 应发合计 ¥{sum(float(r.get('gross_salary') or 0) + float(r.get('bonus') or 0) + float(r.get('allowance') or 0) for r in current_rows):,.2f}"
        items.append(_item("payroll", "工资表 / 个税扣缴", status, "本月工资表", detail, warnings=warnings, page="payroll", scope="monthly"))
    elif previous_rows:
        items.append(_item(
            "payroll", "工资表 / 个税扣缴", "action", f"{previous} 工资表",
            f"本月工资表未生成，可基于 {previous} 的 {len(previous_rows)} 人准备草稿",
            warnings=["生成后请核对人员增减、工资变化和税额"], page="payroll", action="build_payroll", scope="monthly",
        ))
    else:
        items.append(_item(
            "payroll", "工资表 / 个税扣缴", "warning", "没有上期工资底稿",
            "本月工资表未生成，也没有上一期数据可带出", page="payroll", action="build_payroll", scope="monthly",
        ))

    # 社保
    social_total = sum(float(row.get("social_insurance") or 0) for row in current_rows)
    if active_count == 0:
        items.append(_item("social", "社会保险费", "info", "员工档案", "当前没有在职员工，按当地口径确认是否无需申报", page="payroll", scope="monthly"))
    else:
        items.append(_item(
            "social", "社会保险费", "todo", "员工档案 + 上月参保",
            f"在职 {active_count} 人；本月工资表个人社保合计 ¥{social_total:,.2f}",
            warnings=["单位部分费率各地不同，需按当地社保/税务口径核对", "请确认本月新增/停保人员"], page="filing", scope="monthly",
        ))

    # 季度数据
    try:
        overview = reports.income_expense_report(quarter)
        income_count = int(overview.get("count_income") or 0)
        expense_count = int(overview.get("count_expense") or 0)
        income_exclusive = float(overview.get("income_exclusive") or 0)
        expense = float(overview.get("expense") or 0)
        profit = float(overview.get("profit") or 0)
    except Exception as exc:  # noqa: BLE001
        overview = {}
        income_count = expense_count = 0
        income_exclusive = expense = profit = 0.0
        items.append(_item("quarter", "季度申报数据", "warning", "台账读取失败", f"{exc}", page="filing", scope="quarterly"))
    if not overview:
        pass
    elif income_count == 0 and expense_count == 0:
        items.append(_item("vat", "增值税及附加", "warning", "本季度台账", "本季度收入/费用台账为空，请先补录发票和收入，不能直接按零申报", page="filing", scope="quarterly"))
        items.append(_item("cit", "企业所得税预缴", "warning", "本季度台账", "本季度台账为空，无法生成预缴草稿", page="filing", scope="quarterly"))
        items.append(_item("financial", "财务报表", "warning", "本季度台账", "本季度台账为空，无法生成报表草稿", page="filing", scope="quarterly"))
    else:
        items.append(_item(
            "vat", "增值税及附加", "todo", f"{quarter} 收入/发票台账",
            f"不含税收入 ¥{income_exclusive:,.2f} · 预计增值税/附加参考季度汇总",
            warnings=["增值税必须用本期发票和收入，不能复制上期", "请先确认发票归集完整"], page="filing", scope="quarterly",
        ))
        items.append(_item(
            "cit", "企业所得税预缴", "todo", f"{quarter} 利润表",
            f"本季利润 ¥{profit:,.2f} · 费用 ¥{expense:,.2f}",
            warnings=["请核对以前年度亏损、已预缴税额和优惠资格"], page="settlement", scope="quarterly",
        ))
        items.append(_item("financial", "财务报表", "todo", "本期台账 + 上期期末数", f"收入 {income_count} 笔、费用 {expense_count} 笔", page="filing", scope="quarterly"))

    # 印花税
    try:
        contracts = reports.list_contracts()
        contract_count = len(contracts)
        contract_amount = sum(float(row.get("amount") or 0) for row in contracts)
    except Exception:  # noqa: BLE001
        contract_count = 0
        contract_amount = 0.0
    if contract_count == 0:
        items.append(_item("stamp", "印花税", "warning", "合同台账", "本季度没有合同记录，若有合同请先补录，不能直接按 0 申报", page="ledger", scope="quarterly"))
    else:
        items.append(_item("stamp", "印花税", "todo", "合同台账", f"合同 {contract_count} 份 · 计税金额合计 ¥{contract_amount:,.2f}", page="filing", scope="quarterly"))

    # 统计
    total = len(items)
    done = sum(1 for item in items if item["status"] in {"done", "confirmed"})
    action = sum(1 for item in items if item["status"] in {"action", "todo"})
    warning = sum(1 for item in items if item["status"] == "warning")
    return {
        "month": month,
        "quarter": quarter,
        "month_label": period_label(month),
        "quarter_label": period_label(quarter),
        "items": items,
        "summary": {"total": total, "done": done, "action": action, "warning": warning},
        "disclaimer": "系统只准备草稿，不自动申报；所有数据需用户核对后使用。",
    }


__all__ = ["build_center"]
