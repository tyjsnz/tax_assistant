"""申报资料导出：单表 Excel、整包 Excel、JSON 与申报核对清单（Markdown）。

Excel 结构
----------
每个报表 sheet 包含：抬头（报表名称/所属期/状态）、栏次明细（栏次、项目、金额、来源、规则）、
合计数据、说明与免责声明；整包导出另有「申报清单」汇总 sheet，单表导出另有「校验结果」
与「填报指引」sheet。金额单元格统一使用 ``#,##0.00`` 格式，中文表头，可直接打印或核对。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

import config
from engine.money import yuan

EXPORT_DIR = Path(config.EXPORT_DIR)

_INVALID_SHEET_CHARS = set(r"[]:*?/\\")

_TITLE_FILL = PatternFill("solid", fgColor="1F4E79")
_HEADER_FILL = PatternFill("solid", fgColor="2E75B6")
_SECTION_FILL = PatternFill("solid", fgColor="DDEBF7")
_TOTAL_FILL = PatternFill("solid", fgColor="FFF2CC")
_STATUS_FILL = {
    "PASS": PatternFill("solid", fgColor="E2EFDA"),
    "WARNING": PatternFill("solid", fgColor="FFF2CC"),
    "ERROR": PatternFill("solid", fgColor="FCE4D6"),
}
_TITLE_FONT = Font(bold=True, color="FFFFFF", size=14)
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
_SECTION_FONT = Font(bold=True, color="1F4E79", size=11)
_THIN = Side(style="thin", color="BFBFBF")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_WRAP = Alignment(wrap_text=True, vertical="top")
_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
_RIGHT = Alignment(horizontal="right", vertical="center")

_TOTAL_LABELS: dict[str, str] = {
    "vat_payable": "本期应纳增值税",
    "vat_exempt": "本期免税额",
    "vat_at_standard": "按 3% 征收率计算的应纳税额",
    "vat_reduction": "应纳税额减征额",
    "surtax": "附加税费合计",
    "surtax_city": "城市维护建设税",
    "surtax_education": "教育费附加",
    "surtax_local_education": "地方教育附加",
    "total_tax": "税费合计",
    "total_inclusive": "含税销售额合计",
    "total_exclusive": "不含税销售额合计",
    "special_inclusive": "专用发票含税金额",
    "special_exclusive": "专用发票不含税金额",
    "ordinary_inclusive": "普票+未开票含税金额",
    "ordinary_exclusive": "普票+未开票不含税金额",
    "taxable_sales": "应征增值税销售额",
    "exempt_sales": "免税销售额",
    "within_exemption_limit": "是否在免税额度内",
    "revenue": "营业收入（本年累计）",
    "cost": "营业成本（本年累计）",
    "profit": "利润总额（本年累计）",
    "rd_deduction": "研发费用加计扣除额",
    "taxable_income": "实际利润额",
    "standard_tax": "应纳所得税额（25%）",
    "tax_reduction": "减免所得税额",
    "cit_accrued": "实际应纳所得税额",
    "cit_paid": "本年累计已预缴税额",
    "prepay_payable": "本期应补（退）税额",
    "quarter_profit": "本季度利润",
    "ytd_profit": "本年累计利润",
    "quarter_income_exclusive": "本季度不含税收入",
    "quarter_expense": "本季度费用",
    "total_amount": "计税金额合计",
    "total_before_discount": "减免前应纳税额",
    "discount_amount": "减免税额",
    "total_payable": "本期应缴税额",
    "contract_count": "合同份数",
    "total_assets": "资产总计",
    "total_liabilities": "负债合计",
    "total_equity": "所有者权益合计",
    "net_profit": "本期净利润",
    "revenue_ytd": "本年累计营业收入",
    "profit_ytd": "本年累计利润总额",
    "net_profit_ytd": "本年累计净利润",
    "income_tax_ytd": "本年累计所得税费用",
    "cash": "货币资金（期末）",
    "effective_rate": "实际税负",
    "standard_rate": "法定税率",
    "qualifies_small_low_profit": "是否符合小型微利企业",
    "estimate_note": "估算说明",
}


# =============================================================================
# 内部工具
# =============================================================================
def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _default_path(name: str, extension: str) -> Path:
    return Path(config.EXPORT_DIR) / f"{name}_{_timestamp()}.{extension}"


def _sheet_title(bundle: dict[str, Any]) -> str:
    """生成合法且唯一的 sheet 名称（≤31 字符，去掉非法字符）。"""
    raw = str(bundle.get("form_name") or bundle.get("form_id") or "申报表")
    cleaned = "".join(char for char in raw if char not in _INVALID_SHEET_CHARS).strip()
    return cleaned[:31] or "申报表"


def _format_total(key: str, value: Any) -> Any:
    """把合计值格式化为便于阅读的单元格值。"""
    if isinstance(value, bool):
        return "是" if value else "否"
    if key in ("effective_rate", "standard_rate") and isinstance(value, (int, float)):
        return f"{float(value) * 100:.2f}%"
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (dict, list)):
        return ""
    return value if value is not None else ""


def _write_bundle_sheet(ws, bundle: dict[str, Any]) -> None:
    """把单个申报表 bundle 写入一个 worksheet。"""
    widths = {"A": 9, "B": 40, "C": 18, "D": 36, "E": 46}
    for column, width in widths.items():
        ws.column_dimensions[column].width = width

    ws.merge_cells("A1:E1")
    title_cell = ws["A1"]
    title_cell.value = str(bundle.get("form_name") or bundle.get("form_id") or "申报表")
    title_cell.font = _TITLE_FONT
    title_cell.fill = _TITLE_FILL
    title_cell.alignment = _CENTER
    ws.row_dimensions[1].height = 28

    ws["A2"] = "税款所属期"
    ws["B2"] = f"{bundle.get('period_label') or ''}（{bundle.get('period') or ''}）"
    ws["C2"] = "报表状态"
    ws["D2"] = str(bundle.get("status") or "草稿")
    ws["E2"] = str(bundle.get("generated_at") or "")
    ws["A3"] = "报表编码"
    ws["B3"] = str(bundle.get("form_id") or "")
    ws["C3"] = "申报频次"
    ws["D3"] = str(bundle.get("frequency") or "quarterly")
    ws["E3"] = str(bundle.get("disclaimer") or config.DISCLAIMER)
    for row in ws.iter_rows(min_row=2, max_row=3, min_col=1, max_col=5):
        for cell in row:
            cell.alignment = _WRAP
            cell.border = _BORDER
    for cell in (ws["A2"], ws["C2"], ws["A3"], ws["C3"]):
        cell.font = Font(bold=True)

    header_row = 5
    headers = ("栏次", "项目", "金额（元）", "数据来源", "填报规则")
    for index, header in enumerate(headers, start=1):
        cell = ws.cell(row=header_row, column=index, value=header)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = _CENTER
        cell.border = _BORDER

    row_index = header_row + 1
    for section in bundle.get("sections") or []:
        ws.merge_cells(start_row=row_index, start_column=1, end_row=row_index, end_column=5)
        section_cell = ws.cell(row=row_index, column=1, value=str(section.get("title") or "表格"))
        section_cell.font = _SECTION_FONT
        section_cell.fill = _SECTION_FILL
        section_cell.alignment = Alignment(vertical="center")
        row_index += 1
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

    ws.merge_cells(start_row=row_index, start_column=1, end_row=row_index, end_column=5)
    total_title = ws.cell(row=row_index, column=1, value="合计数据")
    total_title.font = _SECTION_FONT
    total_title.fill = _TOTAL_FILL
    row_index += 1
    totals = bundle.get("totals") or {}
    for key, value in totals.items():
        if key not in _TOTAL_LABELS:
            continue
        formatted = _format_total(key, value)
        ws.cell(row=row_index, column=1, value="合计")
        ws.cell(row=row_index, column=2, value=_TOTAL_LABELS.get(key, key))
        value_cell = ws.cell(row=row_index, column=3, value=formatted)
        if isinstance(formatted, float):
            value_cell.number_format = "#,##0.00"
        ws.cell(row=row_index, column=4, value="")
        ws.cell(row=row_index, column=5, value="")
        for column in range(1, 6):
            cell = ws.cell(row=row_index, column=column)
            cell.border = _BORDER
            cell.fill = _TOTAL_FILL
            cell.alignment = _CENTER if column == 1 else (_RIGHT if column == 3 else _WRAP)
        row_index += 1

    row_index += 1
    notes = [
        "说明：本表由「税小智」依据台账与确定性算税引擎自动生成，金额单位：元。",
        "填报前请与发票、银行流水及账面记录核对；正式申报以主管税务机关核定为准。",
        str(bundle.get("disclaimer") or config.DISCLAIMER),
    ]
    for note in notes:
        ws.merge_cells(start_row=row_index, start_column=1, end_row=row_index, end_column=5)
        note_cell = ws.cell(row=row_index, column=1, value=note)
        note_cell.alignment = _WRAP
        note_cell.font = Font(italic=note.startswith("填报前"), color="7F7F7F")
        row_index += 1

    ws.freeze_panes = f"A{header_row + 1}"


def _write_checks_sheet(ws, bundle: dict[str, Any]) -> None:
    """写入「校验结果」sheet。"""
    widths = {"A": 36, "B": 12, "C": 18, "D": 18, "E": 52}
    for column, width in widths.items():
        ws.column_dimensions[column].width = width
    summary = bundle.get("summary") or {}
    ws.merge_cells("A1:E1")
    title = ws["A1"]
    title.value = f"校验结果：{summary.get('level') or 'PASS'} — {summary.get('text') or ''}"
    title.font = _TITLE_FONT
    title.fill = _TITLE_FILL
    title.alignment = _CENTER
    ws.row_dimensions[1].height = 28
    headers = ("校验项目", "状态", "标准值", "实际值", "说明")
    for index, header in enumerate(headers, start=1):
        cell = ws.cell(row=3, column=index, value=header)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = _CENTER
        cell.border = _BORDER
    row_index = 4
    for check in bundle.get("checks") or []:
        status = str(check.get("status") or "PASS")
        ws.cell(row=row_index, column=1, value=str(check.get("field") or ""))
        status_cell = ws.cell(row=row_index, column=2, value=status)
        status_cell.fill = _STATUS_FILL.get(status, _STATUS_FILL["PASS"])
        status_cell.alignment = _CENTER
        expected = check.get("expected")
        actual = check.get("actual")
        ws.cell(row=row_index, column=3, value=expected if isinstance(expected, (int, float)) else "")
        ws.cell(row=row_index, column=4, value=actual if isinstance(actual, (int, float)) else "")
        for column in (3, 4):
            cell = ws.cell(row=row_index, column=column)
            cell.number_format = "#,##0.00"
            cell.alignment = _RIGHT
        ws.cell(row=row_index, column=5, value=str(check.get("message") or ""))
        for column in range(1, 6):
            cell = ws.cell(row=row_index, column=column)
            cell.border = _BORDER
            if column in (1, 5):
                cell.alignment = _WRAP
        row_index += 1
    if row_index == 4:
        ws.cell(row=4, column=1, value="暂无校验明细")
    ws.cell(row=row_index + 1, column=1, value=str(bundle.get("disclaimer") or config.DISCLAIMER))
    ws.freeze_panes = "A4"


def _write_guide_sheet(ws, bundle: dict[str, Any]) -> None:
    """写入「填报指引」sheet。"""
    ws.column_dimensions["A"].width = 8
    ws.column_dimensions["B"].width = 110
    ws.merge_cells("A1:B1")
    title = ws["A1"]
    title.value = f"填报指引 — {bundle.get('form_name') or ''}"
    title.font = _TITLE_FONT
    title.fill = _TITLE_FILL
    title.alignment = _CENTER
    ws.row_dimensions[1].height = 28
    ws["A3"] = "序号"
    ws["B3"] = "填报步骤"
    for cell in (ws["A3"], ws["B3"]):
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = _CENTER
    row_index = 4
    for index, step in enumerate(bundle.get("guide") or [], start=1):
        ws.cell(row=row_index, column=1, value=index).alignment = _CENTER
        step_cell = ws.cell(row=row_index, column=2, value=str(step))
        step_cell.alignment = _WRAP
        for column in (1, 2):
            ws.cell(row=row_index, column=column).border = _BORDER
        ws.row_dimensions[row_index].height = max(20, 15 * (len(str(step)) // 55 + 1))
        row_index += 1


# =============================================================================
# 导出接口
# =============================================================================
def export_bundle_excel(bundle: dict[str, Any], path: Any = None) -> Path:
    """导出单张申报表为 Excel（含「申报表」「校验结果」「填报指引」三个 sheet），返回文件路径。"""
    if not isinstance(bundle, dict):
        raise ValueError("申报表数据必须是 dict")
    target = Path(path) if path else _default_path(f"{bundle.get('period') or '期间'}_{bundle.get('form_id') or '申报表'}", "xlsx")
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "申报表"
    _write_bundle_sheet(sheet, bundle)
    _write_checks_sheet(workbook.create_sheet("校验结果"), bundle)
    _write_guide_sheet(workbook.create_sheet("填报指引"), bundle)
    workbook.save(target)
    return target


def export_package_excel(period: Any, bundles: list[dict[str, Any]] | None = None, path: Any = None) -> Path:
    """导出某期间全部申报表为一个 Excel 工作簿（含「申报清单」汇总 sheet），返回文件路径。"""
    from ledger.reports import period_bounds

    text, _, _ = period_bounds(period)
    if bundles is None:
        from filing.registry import build_all

        bundles = build_all(text)
    target = Path(path) if path else _default_path(f"申报包_{text}", "xlsx")
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    summary_sheet = workbook.active
    summary_sheet.title = "申报清单"
    _write_package_summary(summary_sheet, text, bundles)

    used_titles = {"申报清单"}
    for index, bundle in enumerate(bundles, start=1):
        base_title = _sheet_title(bundle)
        title = base_title
        suffix = 1
        while title in used_titles:
            suffix += 1
            title = f"{base_title[:27]}_{suffix}"
        used_titles.add(title)
        sheet = workbook.create_sheet(title)
        _write_bundle_sheet(sheet, bundle)
    workbook.save(target)
    return target


def _write_package_summary(ws, period: str, bundles: list[dict[str, Any]]) -> None:
    """写入整包导出的「申报清单」sheet。"""
    widths = {"A": 7, "B": 18, "C": 42, "D": 10, "E": 14, "F": 20}
    for column, width in widths.items():
        ws.column_dimensions[column].width = width
    ws.merge_cells("A1:F1")
    title = ws["A1"]
    title.value = f"申报资料包 — {period}"
    title.font = _TITLE_FONT
    title.fill = _TITLE_FILL
    title.alignment = _CENTER
    ws.row_dimensions[1].height = 28
    ws["A2"] = "企业"
    ws["B2"] = config.get_company().get("name") or ""
    ws["C2"] = f"生成时间：{config.now_text()}"
    ws.merge_cells("C2:F2")
    headers = ("序号", "报表编码", "报表名称", "状态", "校验结果", "关键税额")
    for index, header in enumerate(headers, start=1):
        cell = ws.cell(row=4, column=index, value=header)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = _CENTER
        cell.border = _BORDER
    row_index = 5
    for index, bundle in enumerate(bundles, start=1):
        summary = bundle.get("summary") or {}
        totals = bundle.get("totals") or {}
        key_amount = (
            totals.get("total_tax")
            or totals.get("prepay_payable")
            or totals.get("total_payable")
            or 0.0
        )
        ws.cell(row=row_index, column=1, value=index).alignment = _CENTER
        ws.cell(row=row_index, column=2, value=str(bundle.get("form_id") or ""))
        ws.cell(row=row_index, column=3, value=str(bundle.get("form_name") or ""))
        ws.cell(row=row_index, column=4, value=str(bundle.get("status") or "草稿")).alignment = _CENTER
        status = str(summary.get("level") or "PASS")
        status_cell = ws.cell(row=row_index, column=5, value=status)
        status_cell.fill = _STATUS_FILL.get(status, _STATUS_FILL["PASS"])
        status_cell.alignment = _CENTER
        amount_cell = ws.cell(row=row_index, column=6, value=float(key_amount or 0.0))
        amount_cell.number_format = "#,##0.00"
        amount_cell.alignment = _RIGHT
        for column in range(1, 7):
            cell = ws.cell(row=row_index, column=column)
            cell.border = _BORDER
            if column == 3:
                cell.alignment = _WRAP
        row_index += 1
    row_index += 1
    notes = [
        f"说明：本工作簿共 {len(bundles)} 张申报表，由「税小智」依据台账与算税引擎自动生成。",
        "请逐表核对栏次与校验结果，在电子税务局完成申报后回到系统标记「已申报」。",
        config.DISCLAIMER,
    ]
    for note in notes:
        ws.merge_cells(start_row=row_index, start_column=1, end_row=row_index, end_column=6)
        cell = ws.cell(row=row_index, column=1, value=note)
        cell.alignment = _WRAP
        row_index += 1
    ws.freeze_panes = "A5"


def export_bundle_json(bundle: dict[str, Any], path: Any = None) -> Path:
    """导出单张申报表为 JSON（UTF-8，便于程序读取或归档），返回文件路径。"""
    if not isinstance(bundle, dict):
        raise ValueError("申报表数据必须是 dict")
    target = Path(path) if path else _default_path(f"{bundle.get('period') or '期间'}_{bundle.get('form_id') or '申报表'}", "json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return target


def export_checklist_markdown(period: Any, bundles: list[dict[str, Any]] | None = None, path: Any = None) -> Path:
    """导出申报核对清单（Markdown），包含报表概览、逐表关键栏次、校验结论与填报步骤。"""
    from ledger.reports import period_bounds

    text, start, end = period_bounds(period)
    if bundles is None:
        from filing.registry import build_all

        bundles = build_all(text)
    target = Path(path) if path else _default_path(f"申报核对清单_{text}", "md")
    target.parent.mkdir(parents=True, exist_ok=True)

    company = config.get_company()
    lines = [
        f"# 申报核对清单（{text}）",
        "",
        f"- 企业名称：{company.get('name') or ''}",
        f"- 纳税人类型：{company.get('taxpayer_type') or '小规模纳税人'}",
        f"- 税款所属期：{start} 至 {end}（{text}）",
        f"- 生成时间：{config.now_text()}",
        "- 说明：本清单由台账与确定性算税引擎自动生成，申报前请与发票、银行流水核对。",
        "",
        "## 一、申报表概览",
        "",
        "| 序号 | 报表编码 | 报表名称 | 状态 | 校验结论 | 关键税额（元） |",
        "|---|---|---|---|---|---|",
    ]
    total_key_amount = 0.0
    for index, bundle in enumerate(bundles, start=1):
        summary = bundle.get("summary") or {}
        totals = bundle.get("totals") or {}
        key_amount = (
            totals.get("total_tax")
            or totals.get("prepay_payable")
            or totals.get("total_payable")
            or 0.0
        )
        total_key_amount = round(total_key_amount + float(key_amount or 0.0), 2) if bundle.get("form_id") == "VAT_QUARTERLY" else total_key_amount
        lines.append(
            f"| {index} | {bundle.get('form_id')} | {bundle.get('form_name')} | "
            f"{bundle.get('status')} | {summary.get('level') or 'PASS'} | {yuan(key_amount)} |"
        )
    lines.extend(["", "## 二、逐表核对", ""])

    for index, bundle in enumerate(bundles, start=1):
        summary = bundle.get("summary") or {}
        totals = bundle.get("totals") or {}
        lines.append(f"### {index}. {bundle.get('form_name')}（{bundle.get('form_id')}）")
        lines.append("")
        lines.append(f"- 所属期间：{bundle.get('period_label')}（{bundle.get('period')}）")
        lines.append(f"- 当前状态：{bundle.get('status') or '草稿'}")
        lines.append(f"- 校验结论：{summary.get('level') or 'PASS'} — {summary.get('text') or ''}")
        if bundle.get("form_id") == "VAT_QUARTERLY":
            lines.append(
                f"- 关键栏次：第1栏 {yuan(totals.get('taxable_sales'))} 元；"
                f"第10栏 {yuan(totals.get('exempt_sales'))} 元；"
                f"第18栏 {yuan(totals.get('line_18'))} 元；"
                f"第22栏 {yuan(totals.get('line_22'))} 元；"
                f"第24栏 {yuan(totals.get('line_24'))} 元；"
                f"附加税费 {yuan(totals.get('surtax'))} 元。"
            )
        elif bundle.get("form_id") == "CIT_PREPAY":
            lines.append(
                f"- 关键行次：第1行 {yuan(totals.get('revenue'))} 元；"
                f"第8行 {yuan(totals.get('rd_deduction'))} 元；"
                f"第13行 {yuan(totals.get('cit_accrued'))} 元；"
                f"第15行 {yuan(totals.get('prepay_payable'))} 元。"
            )
        elif bundle.get("form_id") == "STAMP_QUARTERLY":
            lines.append(
                f"- 印花税：合同 {int(totals.get('contract_count') or 0)} 份，"
                f"计税金额 {yuan(totals.get('total_amount'))} 元，"
                f"减免 {yuan(totals.get('discount_amount'))} 元，"
                f"应缴 {yuan(totals.get('total_payable'))} 元。"
            )
        elif bundle.get("form_id") == "FIN_REPORT":
            lines.append(
                f"- 报表估算：资产总计 {yuan(totals.get('total_assets'))} 元；"
                f"本期净利润 {yuan(totals.get('net_profit'))} 元（按台账估算，需与账面核对）。"
            )
        warnings = [check for check in bundle.get("checks") or [] if check.get("status") != "PASS"]
        if warnings:
            lines.append("- 需关注的校验提示：")
            for check in warnings:
                lines.append(f"  - [{check.get('status')}] {check.get('field')}：{check.get('message')}")
        lines.extend(["- 填报步骤：", ""])
        for step_index, step in enumerate(bundle.get("guide") or [], start=1):
            lines.append(f"  {step_index}. {step}")
        lines.append("")

    lines.extend(
        [
            "## 三、提交前检查",
            "",
            "- [ ] 申报表栏次与台账、发票数据一致",
            "- [ ] 校验结果无 ERROR 项，WARNING 项已逐条确认",
            "- [ ] 本期增值税、附加税费、企业所得税、印花税金额已核对",
            "- [ ] 电子税务局提交成功后，回到本系统标记「已申报」并留存申报表 PDF",
            "",
            config.DISCLAIMER,
            "",
        ]
    )
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


__all__ = [
    "EXPORT_DIR",
    "export_bundle_excel",
    "export_package_excel",
    "export_bundle_json",
    "export_checklist_markdown",
]
