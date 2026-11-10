"""残疾人就业保障金测算/申报辅助表（P1-D，辅助工具）。

残保金由用人单位按年向税务/残联申报缴纳；具体申报期限、审核流程以当地残联/税务公告为准。
本模块只做**测算与数据准备**，不代替申报。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import config
from engine.disability_fund import calc_disability_fund
from engine.money import pct, round2, yuan

DISCLAIMER = config.DISCLAIMER
FORM_ID = "DISABILITY_FUND"
FORM_NAME = "残疾人就业保障金测算/申报辅助表"


def _line(line_no: str, name: str, value: float | str, source: str = "", rule: str = "") -> dict[str, Any]:
    if isinstance(value, (int, float)):
        return {
            "line_no": line_no,
            "name": name,
            "value": round2(value),
            "value_text": f"{round2(value):,.2f}",
            "source": source,
            "rule": rule,
        }
    return {"line_no": line_no, "name": name, "value": 0.0, "value_text": str(value), "source": source, "rule": rule}


def _defaults(year: str) -> dict[str, Any]:
    """从台账/员工档案推断默认人数与年平均工资。"""
    from ledger import employees, settlement

    annual = settlement.annual_ledger_data(year)
    summary = employees.summarize_employees()
    headcount = int(summary.get("total") or 0)
    salary_total = round2(annual.get("salary_total"))
    average_wage = round2(salary_total / headcount) if headcount else 0.0
    return {
        "employees": headcount,
        "average_wage": average_wage,
        "salary_total": salary_total,
        "warnings": [] if headcount else ["员工档案为空：请手工填写上年在职职工人数与年平均工资。"],
    }


def build_disability_fund(
    year: str | int,
    *,
    employees: int | None = None,
    average_wage: float | None = None,
    disabled_employed: float = 0.0,
    local_average_wage: float | None = None,
) -> dict[str, Any]:
    """生成残保金测算/申报辅助 bundle（主表 + 政策依据 + 校验/操作步骤）。"""
    year_text = str(year)[:4]
    defaults = _defaults(year_text)
    employee_count = int(employees if employees is not None else defaults["employees"])
    wage = round2(average_wage if average_wage is not None else defaults["average_wage"])

    result = calc_disability_fund(
        employees=employee_count,
        average_wage=wage,
        disabled_employed=disabled_employed,
        local_average_wage=local_average_wage,
    )
    reduction = round2(result["base_amount"] - result["payable"])

    sections = [
        {
            "title": "主表（按年）",
            "lines": [
                _line("1", "上年在职职工人数", result["employees"], "员工档案/工资表", "以全年平均人数或年末人数按当地口径"),
                _line("2", "上年实际安排残疾人就业人数", result["disabled_employed"], "残联审核认定", "需经残联按比例安排就业审核"),
                _line("3", "安排残疾人就业比例", result["actual_ratio"] * 100, "第 2 行 ÷ 第 1 行", "百分比"),
                _line("4", "法定安排比例", result["required_ratio"] * 100, "政策规定", "1.5%"),
                _line("5", "差额人数", result["shortfall"], f"第 1 行×{pct(result['required_ratio'])}−第 2 行", "可为小数"),
                _line("6", "上年在职职工年平均工资", result["average_wage"], "工资费用/人数", "超过当地社平 2 倍按 2 倍计征"),
                _line("7", "应缴基数", result["base_amount"], "第 5 行 × 第 6 行", ""),
                _line("8", "分档减缴比例", result["discount_rate"] * 100, "政策分档", "1%（含）~1.5% 按 50%；低于 1% 按 90%"),
                _line("9", "减免金额", reduction, "第 7 行 − 第 10 行", ""),
                _line("10", "本期应缴残保金", result["payable"], "第 7 行 × 减缴比例", result["note"]),
            ],
        },
        {
            "title": "政策依据与说明",
            "lines": [
                _line("1", "安排残疾人就业比例", "1.5%", "财税〔2015〕72号"),
                _line("2", "分档减缴", "1%（含）~1.5% 按 50%；低于 1% 按 90%", "财政部公告2023年第8号（至2027-12-31）"),
                _line("3", "30 人以下免征", "在职职工人数 30 人（含）以下免征", "财政部公告2023年第8号（至2027-12-31）"),
                _line("4", "工资封顶", "年平均工资超过当地社平 2 倍的按 2 倍计征", "以当地口径为准"),
                _line("5", "申报期限", "按年申报，以当地残联/税务公告为准（多数地区 6–9 月）", result["note"]),
            ],
        },
    ]

    checks = [
        {
            "field": "免征判定",
            "status": "PASS" if result["exempt"] else "WARNING",
            "message": f"在职职工 {result['employees']} 人：{'符合 30 人以下免征' if result['exempt'] else '需按比例计算缴纳'}。",
        },
        {
            "field": "安排比例",
            "status": "PASS" if result["actual_ratio"] >= result["required_ratio"] else "WARNING",
            "message": f"实际安排比例 {pct(result['actual_ratio'], 2)}，法定 {pct(result['required_ratio'], 2)}。",
        },
        {
            "field": "工资封顶",
            "status": "WARNING" if result["wage_cap_note"] else "PASS",
            "message": result["wage_cap_note"] or "年平均工资未触发 2 倍封顶提示（如有当地社平工资可输入核对）。",
        },
        {
            "field": "人数一致性",
            "status": "PASS" if result["employees"] == defaults["employees"] else "WARNING",
            "message": f"使用人数 {result['employees']}，员工档案人数 {defaults['employees']}。",
        },
        {
            "field": "申报期限",
            "status": "WARNING",
            "message": "残保金申报期限与残联审核要求各地不同（多数地区 6–9 月），请以当地残联/税务公告为准。",
        },
        {
            "field": "残疾人就业审核",
            "status": "WARNING",
            "message": "安排残疾人就业需先经残联审核认定，审核通过后才能抵扣；未审核的不计入第 2 行。",
        },
    ]
    summary = {
        "level": "WARNING" if any(item["status"] != "PASS" for item in checks) else "PASS",
        "text": (
            f"残保金测算：应缴 {yuan(result['payable'])} 元"
            + ("（30 人以下免征）" if result["exempt"] else f"（分档减缴 {pct(result['discount_rate'], 0)}）")
            + "；请核对人数/工资口径后到当地残联/税务申报。"
        ),
    }
    guide = [
        "确认上年在职职工人数与年平均工资（与工资表、个税申报、社保缴费记录核对）。",
        "如安排残疾人就业，先向当地残联申报按比例安排残疾人就业审核，取得审核确认。",
        "登录当地电子税务局/政务服务网，进入「残疾人就业保障金申报」，按本表主表数据填写。",
        "核对 30 人以下免征、分档减缴（50%/90%）、年平均工资 2 倍封顶等优惠是否适用。",
        "申报后在申报期内完成缴款，留存残联审核材料、工资表、社保记录备查。",
        "具体申报期限与流程以当地残联/税务公告为准。",
        config.DISCLAIMER,
    ]

    return {
        "form_id": FORM_ID,
        "form_name": FORM_NAME,
        "category": "残保金",
        "period": year_text,
        "period_label": f"{year_text} 年度",
        "frequency": "yearly",
        "generated_at": config.now_text(),
        "status": "草稿",
        "sections": sections,
        "totals": {
            "employees": result["employees"],
            "disabled_employed": result["disabled_employed"],
            "actual_ratio": result["actual_ratio"],
            "required_ratio": result["required_ratio"],
            "base_amount": result["base_amount"],
            "discount_rate": result["discount_rate"],
            "reduction": reduction,
            "payable": result["payable"],
            "exempt": result["exempt"],
        },
        "calc": result,
        "checks": checks,
        "summary": summary,
        "guide": guide,
        "data": {"defaults": defaults},
        "disclaimer": DISCLAIMER,
    }


def export_disability_fund_excel(bundle: dict[str, Any], path: str | Path | None = None) -> Path:
    """导出残保金测算/申报辅助表（主表 + 校验/操作步骤）。"""
    from openpyxl import Workbook

    target = Path(path) if path else Path(config.EXPORT_DIR) / f"残保金测算_{bundle.get('period')}.xlsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "残保金测算"
    ws.append([f"{bundle.get('form_name')}（{bundle.get('period_label')}）"])
    ws.append(["系统只做测算与数据准备，实际申报以当地残联/税务口径为准"])
    ws.append([config.DISCLAIMER])
    ws.append([])
    for section in bundle.get("sections") or []:
        ws.append([section.get("title")])
        ws.append(["序号", "项目", "金额/内容", "口径/依据"])
        for line in section.get("lines") or []:
            ws.append([line.get("line_no"), line.get("name"), line.get("value_text"), line.get("rule") or line.get("source")])
        ws.append([])

    ws2 = wb.create_sheet("校验与操作步骤")
    ws2.append(["校验项", "状态", "说明"])
    for item in bundle.get("checks") or []:
        ws2.append([item.get("field"), item.get("status"), item.get("message")])
    ws2.append([])
    ws2.append(["操作步骤"])
    for step in bundle.get("guide") or []:
        ws2.append([step])
    wb.save(target)
    return target
