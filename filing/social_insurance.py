"""社会保险费申报辅助（v1.34）。

说明：本模块只汇总**个人代扣社保/公积金、缴费基数、人数**，用于和社保/税务申报页面核对；
单位部分费率各地不同，系统不硬编码，需按当地社保/税务口径填写。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import config
from ledger import employees, payroll
from storage.export_paths import export_file


def _month(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) == 7 and text[4] == "-":
        return text
    if len(text) == 10:
        return text[:7]
    return config.today().strftime("%Y-%m")


def build_social_insurance_worksheet(period: Any = None) -> dict[str, Any]:
    """汇总某月社保申报辅助数据。"""
    month = _month(period)
    rows = payroll.list_payroll(month)
    people = {str(row.get("id")): row for row in employees.list_employees(status=None, include_left=True)}
    items = []
    for row in rows:
        employee = people.get(str(row.get("employee_id"))) or {}
        items.append(
            {
                "employee_id": row.get("employee_id"),
                "employee_name": row.get("employee_name") or employee.get("name"),
                "social_base": float(employee.get("social_insurance_base") or 0),
                "housing_base": float(employee.get("housing_fund_base") or 0),
                "personal_social": float(row.get("social_insurance") or 0),
                "personal_housing": float(row.get("housing_fund") or 0),
                "gross_salary": float(row.get("gross_salary") or 0),
            }
        )
    totals = {
        "count": len(items),
        "social_base": round(sum(item["social_base"] for item in items), 2),
        "housing_base": round(sum(item["housing_base"] for item in items), 2),
        "personal_social": round(sum(item["personal_social"] for item in items), 2),
        "personal_housing": round(sum(item["personal_housing"] for item in items), 2),
        "gross_salary": round(sum(item["gross_salary"] for item in items), 2),
    }
    warnings = []
    if not items:
        warnings.append(f"{month} 还没有工资表；请先到「👥 员工与工资」生成工资表，再核对社保。")
    return {
        "period": month,
        "rows": items,
        "totals": totals,
        "warnings": warnings,
        "note": "单位部分费率各地不同，系统不硬编码；请按当地社保/税务口径在电子税务局或社保费客户端申报。",
        "steps": [
            "核对本月参保人数、缴费基数、人员增减（入职/离职/停保）。",
            "到当地电子税务局「社保费申报」或社保费客户端，核对单位部分与个人部分。",
            "申报后完成缴费，保存缴费凭证；本表可与工资表、银行扣款记录核对。",
        ],
    }


def export_social_insurance_excel(bundle: dict[str, Any], path: str | Path | None = None) -> Path:
    """导出社保申报辅助表 Excel。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    period = str(bundle.get("period") or "")
    target = Path(path) if path else export_file(f"社保费申报辅助_{period}", "xlsx", period=period, category="社保")
    target.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "社保申报辅助"
    ws.append([f"{period} 社会保险费申报辅助表"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append(["说明：单位部分费率请按当地社保/税务口径填写；本表仅汇总个人代扣与基数"])
    ws.append([])
    ws.append(["员工编号", "姓名", "社保基数", "公积金基数", "个人社保", "个人公积金", "应发工资"])
    for row in bundle.get("rows") or []:
        ws.append([
            row.get("employee_id"), row.get("employee_name"), row.get("social_base"),
            row.get("housing_base"), row.get("personal_social"), row.get("personal_housing"),
            row.get("gross_salary"),
        ])
    totals = bundle.get("totals") or {}
    ws.append(["合计", f"{totals.get('count', 0)} 人", totals.get("social_base"), totals.get("housing_base"),
               totals.get("personal_social"), totals.get("personal_housing"), totals.get("gross_salary")])
    for cell in ws[4]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="E6FFFB")
    wb.save(target)
    return target


__all__ = ["build_social_insurance_worksheet", "export_social_insurance_excel"]
