"""个人所得税扣缴申报辅助表（工资薪金）：主表 + 员工明细 + 政策依据与操作步骤。

口径
----
* 数据来源：`ledger.payroll` 当月工资表（``零申报``行不计人数与收入）；
* 本期汇总按当月工资行统计：收入 = 应发+奖金+补贴；减除费用按「累计预扣法」本期新增额
  （2026 扣缴端：按实际申报月份累计，未申报月份额度作废）；
* 应预扣税额 = 累计应纳税额（engine 累计预扣法）− 已预扣税额；
* 本表仅为**辅助表**，用于核对与录入扣缴端，实际申报仍在自然人电子税务局扣缴端完成。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import config
from engine import (
    BASIC_DEDUCTION_PER_MONTH,
    ZERO_DECLARATION_NOTE,
    calc_iit_withholding,
    get_iit_rate_table,
    get_iit_sources,
    period_label,
    round2,
    yuan,
)
from ledger import models, payroll
from ledger.audit import log_calculation
from ledger.store import table

FORM_ID = "IIT_WITHHOLDING"
FORM_NAME = "个人所得税扣缴申报辅助表（工资薪金）"
CATEGORY = "个人所得税"
FREQUENCY = "monthly"
PERIOD_KIND = "month"

_TITLE_FILL = PatternFill("solid", fgColor="1F4E79")
_HEADER_FILL = PatternFill("solid", fgColor="2E75B6")
_SECTION_FILL = PatternFill("solid", fgColor="DDEBF7")
_TOTAL_FILL = PatternFill("solid", fgColor="FFF2CC")
_TITLE_FONT = Font(bold=True, color="FFFFFF", size=14)
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
_SECTION_FONT = Font(bold=True, color="1F4E79", size=11)
_THIN = Side(style="thin", color="BFBFBF")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_WRAP = Alignment(wrap_text=True, vertical="top")
_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
_RIGHT = Alignment(horizontal="right", vertical="center")


# =============================================================================
# 内部工具
# =============================================================================
def _line(line_no: str, name: str, value: Any, source: str, rule: str, **extra: Any) -> dict[str, Any]:
    amount = round2(value)
    line = {
        "line_no": str(line_no),
        "name": name,
        "value": amount,
        "value_text": yuan(amount),
        "source": source,
        "rule": rule,
        "highlight": False,
    }
    line.update(extra)
    return line


def _employee_of(employee_id: str) -> dict[str, Any]:
    from ledger import employees as employees_module

    return employees_module.get_employee(employee_id) or {}


def _row_income(row: dict[str, Any]) -> float:
    return round2(
        round2(row.get("gross_salary")) + round2(row.get("bonus")) + round2(row.get("allowance"))
    )


def _current_basic_deduction(employee: dict[str, Any], row: dict[str, Any]) -> float:
    """本期新增减除费用 = 当前行累计减除费用 − 上一有效工资行累计减除费用。

    累计预扣：5,000 元/月按实际申报月份累计；一次性扣除：60,000 元在首个有效月份一次性计入。
    未申报月份的额度作废，因此只与「已确认/已申报」的历史行比较。
    """
    if models.parse_bool(row.get("zero_declaration")):
        return 0.0
    if not models.parse_bool(employee.get("is_deduction_qualified"), default=True):
        return 0.0
    employee_id = str(row.get("employee_id") or "")
    period = str(row.get("period") or "")
    year = period[:4]
    # 2026 扣缴端规则：只有「已申报」的历史月份额度才有效；已确认未申报的月份额度作废，
    # 因此计算"本期新增减除费用"时只能与「已申报」历史行比较，不能用未申报行的累计快照。
    declared_rows = [
        item
        for item in table("payroll").read()
        if str(item.get("employee_id") or "") == employee_id
        and str(item.get("period") or "").startswith(f"{year}-")
        and str(item.get("period") or "") < period
        and str(item.get("status") or "") == "已申报"
    ]
    declared_rows.sort(key=lambda item: str(item.get("period") or ""))
    prior_basic = round2(declared_rows[-1].get("cumulative_basic_deduction")) if declared_rows else 0.0
    current_basic = round2(row.get("cumulative_basic_deduction"))
    if str(employee.get("deduction_choice") or "") == "一次性60000":
        # 一次性 60,000 元在首个有效申报月份一次性计入，后续月份不再新增
        return round2(max(current_basic, 0.0)) if not declared_rows else 0.0
    return round2(max(current_basic - prior_basic, 0.0))


def _detail_row(row: dict[str, Any]) -> dict[str, Any]:
    """把工资行整理为扣缴辅助表员工明细（含累计口径与本期税额）。"""
    employee = _employee_of(str(row.get("employee_id") or ""))
    period = str(row.get("period") or "")
    cumulative = payroll.cumulative_for_employee(str(row.get("employee_id") or ""), period)
    income = _row_income(row)
    social = round2(row.get("social_insurance"))
    housing = round2(row.get("housing_fund"))
    special = round2(row.get("special_deduction"))
    other = round2(row.get("other_deduction"))
    employee_name = str(row.get("employee_name") or employee.get("name") or "（未命名）")
    return {
        "employee_id": str(row.get("employee_id") or ""),
        "employee_name": employee_name,
        "department": str(employee.get("department") or ""),
        "income": income,
        "social_insurance": social,
        "housing_fund": housing,
        "special_deduction": special,
        "other_deduction": other,
        "basic_deduction": _current_basic_deduction(employee, row),
        "cumulative_income": round2(row.get("cumulative_income")),
        "cumulative_taxable": round2(row.get("cumulative_taxable")),
        "cumulative_tax": round2(row.get("cumulative_tax")),
        "cumulative_tax_withheld": round2(cumulative["cumulative_tax_withheld"]),
        "months_declared": int(cumulative["months_declared"]),
        "tax_withheld": round2(row.get("tax_withheld")),
        "net_pay": round2(row.get("net_pay")),
        "status": str(row.get("status") or "草稿"),
        "zero_declaration": models.parse_bool(row.get("zero_declaration")),
    }


def _expected_declared_months(employee: dict[str, Any], period: str) -> int:
    """按员工入职月份计算截至目标月应已申报的月份数（同年入职按实际月数，跨年按目标月份数）。"""
    target_year = int(period[:4])
    target_month = int(period[5:7])
    hire_date = str(employee.get("hire_date") or "")
    if hire_date[:4].isdigit() and int(hire_date[:4]) == target_year:
        hire_month = int(hire_date[5:7]) if hire_date[5:7].isdigit() else 1
        return max(target_month - max(hire_month, 1) + 1, 1)
    return max(target_month, 1)


def _check(field: str, status: str, message: str) -> dict[str, Any]:
    return {"field": field, "status": status, "message": message}


def _build_checks(
    detail_rows: list[dict[str, Any]], period: str, zero_declaration: bool = False
) -> list[dict[str, Any]]:
    """生成扣缴辅助表校验项（社保/专项附加不超收入、状态、零申报、无员工、2026 扣缴端提示）。"""
    checks: list[dict[str, Any]] = []
    over_social = [
        row
        for row in detail_rows
        if row["income"] > 0
        and round2(row["social_insurance"] + row["housing_fund"]) > round2(row["income"] + 0.01)
    ]
    if over_social:
        names = "、".join(row["employee_name"] for row in over_social[:5])
        checks.append(
            _check(
                "专项扣除（社保+公积金）≤ 本期收入",
                "WARNING",
                f"以下员工的社保+公积金超过本期收入：{names}，请核对缴费基数。",
            )
        )
    else:
        checks.append(_check("专项扣除（社保+公积金）≤ 本期收入", "PASS", "社保与公积金合计未超过本期收入。"))

    over_special = [
        row for row in detail_rows if row["income"] > 0 and row["special_deduction"] > row["income"] + 0.01
    ]
    if over_special:
        names = "、".join(row["employee_name"] for row in over_special[:5])
        checks.append(
            _check("专项附加扣除 ≤ 本期收入", "WARNING", f"以下员工专项附加扣除超过本期收入：{names}，请核对。")
        )
    else:
        checks.append(_check("专项附加扣除 ≤ 本期收入", "PASS", "专项附加扣除未超过本期收入。"))

    draft_rows = [row for row in detail_rows if row["status"] == "草稿"]
    if zero_declaration and not detail_rows:
        checks.append(_check("工资表状态", "PASS", "本期为零申报，无需确认工资表。"))
    elif draft_rows:
        checks.append(
            _check(
                "工资表状态",
                "WARNING",
                f"本期存在 {len(draft_rows)} 行草稿工资，请先确认工资表并生成凭证后再申报。",
            )
        )
    elif detail_rows:
        checks.append(_check("工资表状态", "PASS", "本期工资表已确认/已申报。"))

    if zero_declaration:
        checks.append(_check("零申报", "PASS", "本期已标记零申报（收入与扣除均为 0）。"))
    elif not detail_rows:
        checks.append(
            _check(
                "零申报",
                "WARNING",
                "本期无工资数据：如确无工资，请在自然人电子税务局扣缴端做零申报，保留本月减除费用额度。",
            )
        )
    else:
        checks.append(_check("零申报", "PASS", "本期有工资数据，无需零申报。"))

    if zero_declaration:
        checks.append(_check("员工与工资表", "PASS", "本期已零申报，无员工工资记录，无需生成工资表。"))
    elif not detail_rows:
        checks.append(
            _check(
                "员工与工资表",
                "WARNING",
                "本期没有员工工资记录：请先在「员工与工资」生成工资表（或确认公司无在职员工）。",
            )
        )
    else:
        checks.append(_check("员工与工资表", "PASS", f"本期申报人数 {len(detail_rows)} 人。"))

    deficits = []
    for row in detail_rows:
        employee = _employee_of(row["employee_id"])
        expected = _expected_declared_months(employee, period)
        if int(row["months_declared"]) < expected:
            deficits.append((row, expected))
    if deficits:
        names = "、".join(f"{row['employee_name']}（{row['months_declared']}/{expected}）" for row, expected in deficits[:5])
        checks.append(
            _check(
                "2026 扣缴端减除费用口径",
                "WARNING",
                f"以下员工本年已申报月份不足（{names}）：2026 年起减除费用按实际申报月份累计，"
                "未申报月份额度作废，请补做零申报。",
            )
        )
    else:
        checks.append(
            _check(
                "2026 扣缴端减除费用口径",
                "PASS",
                f"本期为 {period}：员工已申报月份覆盖至本月，减除费用按实际申报月份累计。",
            )
        )
    return checks


def _summary(checks: list[dict[str, Any]]) -> dict[str, str]:
    errors = [check for check in checks if check["status"] == "ERROR"]
    warnings = [check for check in checks if check["status"] == "WARNING"]
    if errors:
        return {"level": "ERROR", "text": f"校验发现 {len(errors)} 项错误、{len(warnings)} 项提示，请修正后再申报。"}
    if warnings:
        return {"level": "WARNING", "text": f"校验发现 {len(warnings)} 项提示，请核对后到扣缴端申报。"}
    return {"level": "PASS", "text": "校验通过：主表与员工明细勾稽正确，可对照扣缴端录入申报。"}


def _guide(detail_rows: list[dict[str, Any]]) -> list[str]:
    steps = [
        "登录自然人电子税务局（扣缴端），确认【人员信息采集】中员工状态、身份证号与专项附加扣除信息完整准确。",
        "进入【综合所得申报】→【正常工资薪金所得】，导入或录入本期工资数据（收入、社保、公积金、专项附加扣除）。",
        f"本期申报人数 {len(detail_rows)} 人；核对每人收入、累计减除费用（按实际申报月份累计）、"
        "累计应纳税所得额与本期应预扣税额。",
        "点击【税款计算】核对累计预扣结果，再提交【申报】并在申报期内完成缴款。",
        "申报完成后回到本系统「员工与工资」页面点击「标记已申报」，留存工资表与扣缴申报表备查。",
    ]
    if not detail_rows:
        steps.insert(1, "本期无工资数据：如确无工资，请在扣缴端做零申报（收入填 0），以保留本月减除费用额度。")
    steps.append(
        "提示：2026 年扣缴端减除费用按实际申报月份累计，未申报月份额度作废、不再补发；"
        "连续零工资月份也应逐月零申报。"
    )
    steps.append("本表为扣缴申报辅助表，不代替扣缴端申报；实际申报以自然人电子税务局扣缴端为准。")
    steps.append(config.DISCLAIMER)
    return steps


# =============================================================================
# 构建 bundle
# =============================================================================
def build_iit_return(period: str, *, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """生成个人所得税扣缴申报辅助表 bundle（主表 11 行 + 员工明细 + 政策依据）。

    参数
    ----
    period : 所属月份，如 ``2026-11``
    rows   : 可选，指定工资表行（默认读取 ``ledger.payroll.list_payroll(period)``）
    """
    text = payroll.normalize_month(period)
    all_rows = list(rows) if rows is not None else payroll.list_payroll(text)
    zero_declaration = any(models.parse_bool(row.get("zero_declaration")) for row in all_rows)
    effective_rows = [row for row in all_rows if not models.parse_bool(row.get("zero_declaration"))]
    detail_rows = [_detail_row(row) for row in effective_rows]

    total_income = round2(sum(item["income"] for item in detail_rows))
    total_basic = round2(sum(item["basic_deduction"] for item in detail_rows))
    total_social = round2(sum(item["social_insurance"] for item in detail_rows))
    total_housing = round2(sum(item["housing_fund"] for item in detail_rows))
    total_special = round2(sum(item["special_deduction"] for item in detail_rows))
    total_other = round2(sum(item["other_deduction"] for item in detail_rows))
    total_cumulative_tax = round2(sum(item["cumulative_tax"] for item in detail_rows))
    total_withheld_history = round2(sum(item["cumulative_tax_withheld"] for item in detail_rows))
    total_tax = round2(sum(item["tax_withheld"] for item in detail_rows))
    total_net = round2(sum(item["net_pay"] for item in detail_rows))
    total_taxable = round2(max(total_income - total_basic - total_social - total_housing - total_special - total_other, 0.0))

    main_lines = [
        _line("1", "本期收入合计", total_income, "当月工资表（应发+奖金+补贴）", "零申报行不计入"),
        _line("2", "本期免税收入", 0.0, "工资薪金无免税收入", "按现行政策填 0"),
        _line(
            "3",
            "减除费用合计",
            total_basic,
            "累计预扣法：5,000 元/月 × 实际申报月份（一次性扣除为 60,000 元）",
            "2026 扣缴端：减除费用按实际申报月份累计，未申报月份额度作废",
        ),
        _line("4", "专项扣除（社保+公积金）", round2(total_social + total_housing), "当月工资表个人社保+个人公积金", "三险一金个人缴纳部分"),
        _line("5", "专项附加扣除", total_special, "员工专项附加扣除明细（engine 月度标准）", "子女教育/婴幼儿照护/继续教育/住房/赡养老人等"),
        _line("6", "其他扣除", total_other, "当月工资表其他扣除", "年金、商业健康险、税延养老等"),
        _line("7", "应纳税所得额", total_taxable, "第 1 行 - 第 2 行 - 第 3 行 - 第 4 行 - 第 5 行 - 第 6 行", "本期口径，负数按 0 处理"),
        _line("8", "应预扣税额", total_cumulative_tax, "累计应纳税额（engine 累计预扣法）", "累计预扣率表计算，累计口径"),
        _line("9", "已预扣税额", total_withheld_history, "本年以前月份已预扣预缴税额合计", "仅历史已确认/已申报工资行"),
        _line("10", "本期应补（退）税额", total_tax, "第 8 行 - 第 9 行", "本期应预扣预缴税额", highlight=True),
        _line("11", "申报人数", float(len(detail_rows)), "当月工资表人数（零申报行不计）", "自然人电子税务局扣缴端申报人数"),
    ]
    main_lines[10]["value_text"] = f"{len(detail_rows)} 人"

    detail_lines = [
        _line(
            str(index),
            item["employee_name"],
            item["tax_withheld"],
            f"本期收入 {yuan(item['income'])} 元；社保 {yuan(item['social_insurance'])} 元；"
            f"公积金 {yuan(item['housing_fund'])} 元；专项附加 {yuan(item['special_deduction'])} 元",
            f"累计应纳税所得额 {yuan(item['cumulative_taxable'])} 元；累计已预扣 "
            f"{yuan(item['cumulative_tax_withheld'])} 元；实发 {yuan(item['net_pay'])} 元",
            employee_id=item["employee_id"],
            employee_name=item["employee_name"],
            income=item["income"],
            social_insurance=item["social_insurance"],
            housing_fund=item["housing_fund"],
            special_deduction=item["special_deduction"],
            other_deduction=item["other_deduction"],
            cumulative_taxable=item["cumulative_taxable"],
            months_declared=item["months_declared"],
            tax_withheld=item["tax_withheld"],
            net_pay=item["net_pay"],
        )
        for index, item in enumerate(detail_rows, start=1)
    ]

    policy_lines: list[dict[str, Any]] = []
    for index, source in enumerate(get_iit_sources(), start=1):
        policy_lines.append(
            _line(
                str(index),
                str(source.get("title") or "政策依据"),
                round2(0.0),
                f"{source.get('source') or ''}（生效日期 {source.get('effective_date') or '—'}，"
                f"有效期至 {source.get('expiry_date') or '长期'}）",
                "政策依据以主管税务机关与扣缴端现行口径为准",
            )
        )
    for index, rate_row in enumerate(get_iit_rate_table(), start=1):
        policy_lines.append(
            _line(
                f"级{index}",
                f"累计预扣率：{rate_row['累计应纳税所得额']} → {rate_row['预扣率']}",
                round2(rate_row["速算扣除数"]),
                "engine.IIT_RATE_TABLE 累计预扣率表",
                f"速算扣除数 {yuan(rate_row['速算扣除数'])} 元",
            )
        )

    checks = _build_checks(detail_rows, text, zero_declaration)
    summary = _summary(checks)
    statuses = {item["status"] for item in detail_rows}
    if zero_declaration and not detail_rows:
        status = "已申报"
    elif detail_rows and all(item["status"] == "已申报" for item in detail_rows):
        status = "已申报"
    elif detail_rows and statuses <= {"已确认", "已申报"}:
        status = "已确认"
    else:
        status = "草稿"

    calc_rows = []
    for row in effective_rows:
        cumulative = payroll.cumulative_for_employee(str(row.get("employee_id") or ""), text)
        employee = _employee_of(str(row.get("employee_id") or ""))
        qualified = models.parse_bool(employee.get("is_deduction_qualified"), default=True)
        result = calc_iit_withholding(
            cumulative["cumulative_income"],
            months_declared=cumulative["months_declared"] if qualified else 0,
            cumulative_social_insurance=cumulative["cumulative_social"],
            cumulative_housing_fund=cumulative["cumulative_housing_fund"],
            cumulative_special_additional=cumulative["cumulative_special_additional"],
            cumulative_other_deduction=cumulative["cumulative_other"],
            cumulative_tax_withheld=cumulative["cumulative_tax_withheld"],
            one_time_basic_deduction=qualified and str(employee.get("deduction_choice") or "") == "一次性60000",
            period=text,
        )
        calc_rows.append(
            {
                "employee_id": str(row.get("employee_id") or ""),
                "employee_name": str(row.get("employee_name") or ""),
                "months_declared": result["months_declared"],
                "cumulative_income": result["cumulative_income"],
                "cumulative_basic_deduction": result["cumulative_basic_deduction"],
                "cumulative_taxable_income": result["cumulative_taxable_income"],
                "applicable_rate": result["applicable_rate"],
                "quick_deduction": result["quick_deduction"],
                "cumulative_tax": result["cumulative_tax"],
                "cumulative_tax_withheld": result["cumulative_tax_withheld"],
                "tax_payable": result["tax_payable"],
            }
        )

    bundle: dict[str, Any] = {
        "form_id": FORM_ID,
        "form_name": FORM_NAME,
        "category": CATEGORY,
        "period": text,
        "period_label": period_label(text),
        "frequency": FREQUENCY,
        "generated_at": config.now_text(),
        "status": status,
        "sections": [
            {"title": "主表（本期汇总）", "lines": main_lines},
            {"title": "员工明细", "lines": detail_lines},
            {"title": "政策依据与预扣率表", "lines": policy_lines},
        ],
        "totals": {
            "headcount": len(detail_rows),
            "total_income": total_income,
            "total_basic_deduction": total_basic,
            "total_social": total_social,
            "total_housing_fund": total_housing,
            "total_special": total_special,
            "total_other": total_other,
            "total_taxable": total_taxable,
            "total_cumulative_tax": total_cumulative_tax,
            "total_tax_withheld": total_tax,
            "total_tax": total_tax,
            "total_net": total_net,
            "zero_declaration": zero_declaration,
            "declared": bool(detail_rows) and all(item["status"] == "已申报" for item in detail_rows),
        },
        "calc": {
            "period": text,
            "rows": calc_rows,
            "rate_table": get_iit_rate_table(),
            "basic_deduction_per_month": BASIC_DEDUCTION_PER_MONTH,
            "zero_declaration_note": ZERO_DECLARATION_NOTE,
            "policy_sources": get_iit_sources(),
            "note": "个税计算由 engine.calc_iit_withholding（累计预扣法）确定性完成，模型不参与计算。",
        },
        "checks": checks,
        "summary": summary,
        "guide": _guide(detail_rows),
        "data": {
            "period": text,
            "rows": detail_rows,
            "payroll_rows": all_rows,
            "zero_declaration": zero_declaration,
            "source": "payroll",
        },
        "policy_sources": get_iit_sources(),
        "disclaimer": config.DISCLAIMER,
    }
    log_calculation(
        "个人所得税扣缴",
        text,
        {
            "headcount": len(detail_rows),
            "total_income": total_income,
            "total_tax": total_tax,
            "zero_declaration": zero_declaration,
        },
        {
            "total_taxable": total_taxable,
            "total_cumulative_tax": total_cumulative_tax,
            "total_tax_withheld": total_tax,
            "summary": summary["level"],
        },
    )
    return bundle


# =============================================================================
# Excel 导出（3 个 sheet）
# =============================================================================
def _default_path(period: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(config.EXPORT_DIR) / f"个人所得税扣缴辅助表_{period}_{stamp}.xlsx"


def _style_header(ws, row: int, headers: tuple[str, ...]) -> None:
    for index, header in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=index, value=header)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = _CENTER
        cell.border = _BORDER


def _write_main_sheet(ws, bundle: dict[str, Any]) -> None:
    widths = {"A": 9, "B": 40, "C": 18, "D": 46, "E": 44}
    for column, width in widths.items():
        ws.column_dimensions[column].width = width
    ws.merge_cells("A1:E1")
    title = ws["A1"]
    title.value = FORM_NAME
    title.font = _TITLE_FONT
    title.fill = _TITLE_FILL
    title.alignment = _CENTER
    ws.row_dimensions[1].height = 28
    ws["A2"] = "税款所属期"
    ws["B2"] = f"{bundle.get('period_label')}（{bundle.get('period')}）"
    ws["C2"] = "报表状态"
    ws["D2"] = str(bundle.get("status") or "草稿")
    ws["E2"] = str(bundle.get("generated_at") or "")
    ws["A3"] = "报表编码"
    ws["B3"] = FORM_ID
    ws["C3"] = "说明"
    ws["D3"] = "本表为扣缴申报辅助表，实际申报在自然人电子税务局扣缴端完成。"
    ws.merge_cells("D3:E3")
    for row in ws.iter_rows(min_row=2, max_row=3, min_col=1, max_col=5):
        for cell in row:
            cell.alignment = _WRAP
            cell.border = _BORDER
    for cell in (ws["A2"], ws["C2"], ws["A3"], ws["C3"]):
        cell.font = Font(bold=True)

    header_row = 5
    _style_header(ws, header_row, ("行次", "项目", "金额（元）", "数据来源", "口径说明"))
    row_index = header_row + 1
    for section in bundle.get("sections") or []:
        if section.get("title") != "主表（本期汇总）":
            continue
        for line in section.get("lines") or []:
            ws.cell(row=row_index, column=1, value=str(line.get("line_no") or ""))
            ws.cell(row=row_index, column=2, value=str(line.get("name") or ""))
            value_cell = ws.cell(row=row_index, column=3, value=float(line.get("value") or 0.0))
            value_cell.number_format = "#,##0.00"
            ws.cell(row=row_index, column=4, value=str(line.get("source") or ""))
            ws.cell(row=row_index, column=5, value=str(line.get("rule") or ""))
            for column in range(1, 6):
                cell = ws.cell(row=row_index, column=column)
                cell.border = _BORDER
                cell.alignment = _CENTER if column == 1 else (_RIGHT if column == 3 else _WRAP)
                if line.get("highlight"):
                    cell.font = Font(bold=True)
            row_index += 1
    row_index += 1
    notes = [
        f"2026 扣缴端提示：{ZERO_DECLARATION_NOTE}",
        "本表金额单位：元，保留 2 位小数；数据来自当月工资表与 engine 累计预扣法计算。",
        str(bundle.get("disclaimer") or config.DISCLAIMER),
    ]
    for note in notes:
        ws.merge_cells(start_row=row_index, start_column=1, end_row=row_index, end_column=5)
        cell = ws.cell(row=row_index, column=1, value=note)
        cell.alignment = _WRAP
        row_index += 1
    ws.freeze_panes = "A6"


def _write_detail_sheet(ws, bundle: dict[str, Any]) -> None:
    headers = (
        "序号",
        "姓名",
        "部门",
        "本期收入",
        "个人社保",
        "个人公积金",
        "专项附加扣除",
        "其他扣除",
        "累计应纳税所得额",
        "已申报月份",
        "本期应预扣税额",
        "实发工资",
        "状态",
    )
    widths = (6, 14, 14, 14, 12, 12, 14, 12, 18, 12, 16, 14, 10)
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    ws.merge_cells("A1:M1")
    title = ws["A1"]
    title.value = f"员工明细 — {bundle.get('period_label')}（{bundle.get('period')}）"
    title.font = _TITLE_FONT
    title.fill = _TITLE_FILL
    title.alignment = _CENTER
    ws.row_dimensions[1].height = 26
    _style_header(ws, 3, headers)
    rows = (bundle.get("data") or {}).get("rows") or []
    row_index = 4
    for index, item in enumerate(rows, start=1):
        values = [
            index,
            item.get("employee_name"),
            item.get("department"),
            item.get("income"),
            item.get("social_insurance"),
            item.get("housing_fund"),
            item.get("special_deduction"),
            item.get("other_deduction"),
            item.get("cumulative_taxable"),
            item.get("months_declared"),
            item.get("tax_withheld"),
            item.get("net_pay"),
            item.get("status"),
        ]
        for column, value in enumerate(values, start=1):
            cell = ws.cell(row=row_index, column=column, value=value)
            cell.border = _BORDER
            if column in (4, 5, 6, 7, 8, 9, 11, 12):
                cell.number_format = "#,##0.00"
                cell.alignment = _RIGHT
            elif column in (1, 10, 13):
                cell.alignment = _CENTER
        row_index += 1
    if not rows:
        ws.cell(row=4, column=1, value="本期无员工工资记录（如确无工资请在扣缴端零申报）")
        ws.merge_cells("A4:M4")
    row_index += 1
    ws.cell(row=row_index, column=1, value=str(bundle.get("disclaimer") or config.DISCLAIMER))
    ws.merge_cells(start_row=row_index, start_column=1, end_row=row_index, end_column=13)
    ws.freeze_panes = "A4"


def _write_policy_sheet(ws, bundle: dict[str, Any]) -> None:
    ws.column_dimensions["A"].width = 8
    ws.column_dimensions["B"].width = 120
    ws.merge_cells("A1:B1")
    title = ws["A1"]
    title.value = "政策依据与操作步骤"
    title.font = _TITLE_FONT
    title.fill = _TITLE_FILL
    title.alignment = _CENTER
    ws.row_dimensions[1].height = 26
    row_index = 3
    ws.cell(row=row_index, column=1, value="一、政策依据")
    ws.cell(row=row_index, column=1).font = _SECTION_FONT
    ws.cell(row=row_index, column=1).fill = _SECTION_FILL
    ws.merge_cells(start_row=row_index, start_column=1, end_row=row_index, end_column=2)
    row_index += 1
    for section in bundle.get("sections") or []:
        if section.get("title") != "政策依据与预扣率表":
            continue
        for line in section.get("lines") or []:
            ws.cell(row=row_index, column=1, value=str(line.get("line_no") or ""))
            text = f"{line.get('name')}｜{line.get('source')}｜{line.get('rule')}"
            cell = ws.cell(row=row_index, column=2, value=text)
            cell.alignment = _WRAP
            for column in (1, 2):
                ws.cell(row=row_index, column=column).border = _BORDER
            row_index += 1
    row_index += 1
    ws.cell(row=row_index, column=1, value="二、扣缴端操作步骤")
    ws.cell(row=row_index, column=1).font = _SECTION_FONT
    ws.cell(row=row_index, column=1).fill = _SECTION_FILL
    ws.merge_cells(start_row=row_index, start_column=1, end_row=row_index, end_column=2)
    row_index += 1
    for index, step in enumerate(bundle.get("guide") or [], start=1):
        ws.cell(row=row_index, column=1, value=index).alignment = _CENTER
        cell = ws.cell(row=row_index, column=2, value=str(step))
        cell.alignment = _WRAP
        for column in (1, 2):
            ws.cell(row=row_index, column=column).border = _BORDER
        row_index += 1
    row_index += 1
    ws.cell(row=row_index, column=1, value=str(bundle.get("disclaimer") or config.DISCLAIMER))
    ws.merge_cells(start_row=row_index, start_column=1, end_row=row_index, end_column=2)


def export_iit_excel(bundle: dict[str, Any], path: Any = None) -> Path:
    """导出扣缴申报辅助表 Excel（主表 / 员工明细 / 政策依据与操作步骤 3 个 sheet）。"""
    if not isinstance(bundle, dict):
        raise ValueError("申报表数据必须是 dict")
    period = str(bundle.get("period") or "期间")
    target = Path(path) if path else _default_path(period)
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    main_sheet = workbook.active
    main_sheet.title = "主表"
    _write_main_sheet(main_sheet, bundle)
    _write_detail_sheet(workbook.create_sheet("员工明细"), bundle)
    _write_policy_sheet(workbook.create_sheet("政策依据与操作步骤"), bundle)
    workbook.save(target)
    return target


__all__ = [
    "FORM_ID",
    "FORM_NAME",
    "CATEGORY",
    "FREQUENCY",
    "PERIOD_KIND",
    "build_iit_return",
    "export_iit_excel",
]
