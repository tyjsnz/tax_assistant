"""企业所得税年度纳税申报辅助表（A 类）：主表关键行次 + 纳税调整明细 + 亏损弥补明细 + 政策依据。

口径
----
数据来自 :func:`ledger.settlement.compute_settlement`（确定性引擎 ``engine.calc_settlement``），
本表仅作为电子税务局 A 类年度申报的**辅助测算与录入对照**，不代替实际申报；
最终以 A 类年度申报表与主管税务机关口径为准。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import config
from engine import get_settlement_sources, pct, round2, yuan
from ledger.audit import log_calculation

from ledger import settlement as settlement_module

FORM_ID = "CIT_ANNUAL"
FORM_NAME = "企业所得税年度纳税申报辅助表（A 类）"
CATEGORY = "企业所得税"
FREQUENCY = "annual"
PERIOD_KIND = "year"

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

#: 汇算留存备查资料清单
MATERIALS: tuple[str, ...] = (
    "年度财务报表（资产负债表、利润表、现金流量表）",
    "纳税调整事项说明及对应合同、发票、凭证等证据资料",
    "研发项目立项决议、研发辅助账、研发费用分摊表及加计扣除明细",
    "以前年度亏损弥补台账及年度申报表",
    "季度企业所得税预缴申报表与完税凭证",
    "税收优惠（小型微利企业）相关资料与从业人数、资产总额计算表",
    "关联交易同期资料（如有）",
    "更正申报记录与税务机关要求的其他资料",
)


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


def _check(field: str, status: str, message: str) -> dict[str, Any]:
    return {"field": field, "status": status, "message": message}


def _summary(checks: list[dict[str, Any]]) -> dict[str, str]:
    errors = [item for item in checks if item["status"] == "ERROR"]
    warnings = [item for item in checks if item["status"] == "WARNING"]
    if errors:
        return {"level": "ERROR", "text": f"校验发现 {len(errors)} 项错误、{len(warnings)} 项提示，请修正后再申报。"}
    if warnings:
        return {"level": "WARNING", "text": f"校验发现 {len(warnings)} 项提示，请核对后到电子税务局申报。"}
    return {"level": "PASS", "text": "校验通过：主表与明细勾稽正确，可对照电子税务局 A 类年度申报表录入。"}


def _main_lines(result: dict[str, Any]) -> list[dict[str, Any]]:
    rate = float(result.get("applicable_rate") or 0.0)
    payable = round2(result.get("payable"))
    refund = round2(result.get("refund"))
    net_amount = round2(payable if refund == 0 else -refund)
    lines = [
        _line("1", "利润总额", result.get("accounting_profit"), "年度台账（收入－费用）或手工覆盖", "会计利润总额", highlight=True),
        _line(
            "2",
            "纳税调整增加额",
            round2(round2(result.get("limit_adjustment_add")) + round2(result.get("manual_adjustment_add"))),
            "限额类调整（业务招待费/广宣费/三项经费）+ 手工调增",
            "按税法规定调增应纳税所得额",
        ),
        _line("3", "纳税调整减少额", result.get("manual_adjustment_reduce"), "手工调减清单", "按税法规定调减应纳税所得额"),
        _line("4", "研发费用加计扣除", result.get("research_deduction"), "研发辅助账（100% 加计）", "按 engine 归集结果填列"),
        _line("5", "弥补以前年度亏损", result.get("loss_used"), "亏损弥补台账（5 年内）", "先到期先弥补"),
        _line("6", "应纳税所得额", result.get("taxable_income"), "第 1+2-3-4-5 行", "调整后应纳税所得额", highlight=True),
        _line("7", "适用税率", rate, "小型微利企业实际税负 5%，否则 25%", "按企业是否符合小型微利企业条件确定"),
        _line("8", "应纳所得税额", result.get("corporate_tax"), "应纳税所得额 × 适用税率", "减免后实际应纳所得税额"),
        _line("9", "减免所得税额", result.get("tax_reduction"), "小型微利企业优惠减免额", "法定税率 25% 与实际税负差额"),
        _line("10", "实际应纳所得税额", result.get("corporate_tax"), "第 8 行（已含减免）", "本年度实际应缴纳企业所得税"),
        _line("11", "本年累计已预缴税额", result.get("prepaid_tax"), "季度预缴申报记录/年度输入", "核对电子税务局已缴税额"),
        _line(
            "12",
            "应补（退）税额",
            net_amount,
            "第 10 行 - 第 11 行",
            "正数为应补税额，负数为应退税额",
            highlight=True,
        ),
    ]
    lines[6]["value_text"] = pct(rate)
    lines[6]["value"] = round2(rate)
    lines[11]["payable_label"] = str(result.get("payable_label") or ("应退税额" if refund else "应补税额"))
    return lines


def _adjustment_lines(result: dict[str, Any]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    index = 0
    for item in result.get("limit_adjustment_items") or []:
        index += 1
        lines.append(
            _line(
                str(index),
                f"{item.get('label')}（调增）",
                item.get("adjustment_add"),
                f"实际发生额 {yuan(item.get('actual'))} 元，税前可扣除 {yuan(item.get('deductible'))} 元",
                f"{item.get('rule')}；依据：{item.get('basis')}",
                accounting_amount=round2(item.get("actual")),
                tax_amount=round2(item.get("deductible")),
                adjustment_amount=round2(item.get("adjustment_add")),
                direction="调增",
                basis=str(item.get("basis") or ""),
            )
        )
    for item in result.get("manual_adjustment_items") or []:
        index += 1
        lines.append(
            _line(
                str(index),
                f"{item.get('item_type')}（{item.get('direction')}）",
                item.get("adjustment_amount"),
                f"会计金额 {yuan(item.get('accounting_amount'))} 元，税法金额 {yuan(item.get('tax_amount'))} 元",
                f"{item.get('basis') or '手工纳税调整'}"
                + (f"；依据：{item.get('source')}" if item.get("source") else ""),
                accounting_amount=round2(item.get("accounting_amount")),
                tax_amount=round2(item.get("tax_amount")),
                adjustment_amount=round2(item.get("adjustment_amount")),
                direction=str(item.get("direction") or "调增"),
                basis=str(item.get("basis") or ""),
            )
        )
    if not lines:
        lines.append(_line("1", "本年无纳税调整事项", 0.0, "—", "未录入纳税调整"))
    total_add = round2(round2(result.get("limit_adjustment_add")) + round2(result.get("manual_adjustment_add")))
    lines.append(
        _line(
            "合计",
            "纳税调整合计（调增）",
            total_add,
            f"限额类 {yuan(result.get('limit_adjustment_add'))} + 手工 {yuan(result.get('manual_adjustment_add'))}",
            f"手工调减合计 {yuan(result.get('manual_adjustment_reduce'))} 元",
            highlight=True,
        )
    )
    return lines


def _loss_lines(result: dict[str, Any]) -> list[dict[str, Any]]:
    loss_result = result.get("loss_carryforward") or {}
    lines: list[dict[str, Any]] = []
    for index, item in enumerate(loss_result.get("lines") or [], start=1):
        lines.append(
            _line(
                str(index),
                f"{item.get('year')} 年度亏损",
                item.get("used_this_year"),
                f"可弥补 {yuan(item.get('loss_amount'))} 元，已弥补 {yuan(item.get('used_amount'))} 元，"
                f"剩余 {yuan(item.get('remaining_after'))} 元",
                f"到期年度 {item.get('expire_year')} 年；状态 {item.get('status')}",
                loss_year=str(item.get("year") or ""),
                loss_amount=round2(item.get("loss_amount")),
                used_amount=round2(item.get("used_amount")),
                used_this_year=round2(item.get("used_this_year")),
                remaining=round2(item.get("remaining_after")),
                expire_year=str(item.get("expire_year") or ""),
                status=str(item.get("status") or ""),
            )
        )
    if not lines:
        lines.append(_line("1", "无以前年度可弥补亏损", 0.0, "—", "亏损弥补台账为空"))
    lines.append(
        _line(
            "合计",
            "本年弥补亏损合计",
            result.get("loss_used"),
            "亏损弥补台账（先进先出、5 年期限）",
            f"弥补后剩余亏损 {yuan(loss_result.get('remaining_loss'))} 元",
            highlight=True,
        )
    )
    return lines


def _policy_lines() -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for index, source in enumerate(get_settlement_sources(), start=1):
        lines.append(
            _line(
                str(index),
                str(source.get("title") or "政策依据"),
                round2(0.0),
                f"{source.get('source') or ''}（生效日期 {source.get('effective_date') or '—'}，有效期至 {source.get('expiry_date') or '长期'}）",
                "政策依据以主管税务机关与电子税务局现行口径为准",
            )
        )
    for index, material in enumerate(MATERIALS, start=1):
        lines.append(
            _line(f"资料{index}", f"留存备查：{material}", round2(0.0), "汇算清缴留存备查资料", "保存期限一般不少于 10 年")
        )
    return lines


def _build_checks(result: dict[str, Any], annual: dict[str, Any], inputs: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    corporate_tax = round2(result.get("corporate_tax"))
    prepaid = round2(result.get("prepaid_tax"))
    payable = round2(result.get("payable"))
    refund = round2(result.get("refund"))
    if corporate_tax <= 0:
        checks.append(_check("预缴与应纳所得税额", "PASS", "本年应纳所得税额为 0，无需补缴（如有预缴可申请退税/抵缴）。"))
    elif prepaid <= 0:
        checks.append(
            _check(
                "预缴与应纳所得税额",
                "WARNING",
                f"应纳所得税额 {corporate_tax:.2f} 元但未读取到季度预缴税额，请核对是否漏缴。",
            )
        )
    elif refund > 0:
        checks.append(
            _check(
                "预缴与应纳所得税额",
                "WARNING",
                f"已预缴 {prepaid:.2f} 元大于应纳所得税 {corporate_tax:.2f} 元，可申请退税/抵缴 {refund:.2f} 元。",
            )
        )
    else:
        checks.append(
            _check(
                "预缴与应纳所得税额",
                "PASS",
                f"已预缴 {prepaid:.2f} 元，应补税额 {payable:.2f} 元，请于次年 5 月 31 日前缴清。",
            )
        )

    loss_result = result.get("loss_carryforward") or {}
    expired = [line for line in loss_result.get("lines") or [] if str(line.get("status")) == "已过期"]
    if expired:
        checks.append(_check("亏损弥补期限", "WARNING", f"存在 {len(expired)} 条超过 5 年弥补期限的亏损，不得再弥补。"))
    else:
        checks.append(_check("亏损弥补期限", "PASS", "不存在超过弥补期限的亏损。"))

    manual_add_sum = round2(
        sum(round2(item.get("adjustment_amount")) for item in result.get("manual_adjustment_items") or [] if item.get("direction") == "调增")
    )
    manual_reduce_sum = round2(
        sum(round2(item.get("adjustment_amount")) for item in result.get("manual_adjustment_items") or [] if item.get("direction") == "调减")
    )
    limit_add_sum = round2(sum(round2(item.get("adjustment_add")) for item in result.get("limit_adjustment_items") or []))
    balanced = (
        abs(manual_add_sum - round2(result.get("manual_adjustment_add"))) <= 0.01
        and abs(manual_reduce_sum - round2(result.get("manual_adjustment_reduce"))) <= 0.01
        and abs(limit_add_sum - round2(result.get("limit_adjustment_add"))) <= 0.01
    )
    if balanced:
        checks.append(_check("纳税调整项平衡", "PASS", "纳税调整清单合计与主表调整额勾稽一致。"))
    else:
        checks.append(_check("纳税调整项平衡", "ERROR", "纳税调整明细合计与主表调整额不一致，请检查清单。"))

    expected_profit = round2(annual.get("profit"))
    reported_profit = round2(result.get("accounting_profit"))
    if inputs.get("accounting_profit") is None:
        if abs(reported_profit - expected_profit) <= 0.01:
            checks.append(_check("利润与台账一致性", "PASS", f"会计利润 {reported_profit:.2f} 元与台账推算一致。"))
        else:
            checks.append(
                _check(
                    "利润与台账一致性",
                    "ERROR",
                    f"会计利润 {reported_profit:.2f} 元与台账推算 {expected_profit:.2f} 元不一致，请核对。",
                )
            )
    elif abs(reported_profit - expected_profit) > 0.01:
        checks.append(
            _check(
                "利润与台账一致性",
                "WARNING",
                f"会计利润已手工覆盖为 {reported_profit:.2f} 元（台账推算 {expected_profit:.2f} 元），请确认调整原因。",
            )
        )
    else:
        checks.append(_check("利润与台账一致性", "PASS", "会计利润与台账推算一致。"))

    if result.get("qualifies_small_low_profit"):
        checks.append(
            _check("小型微利企业条件", "PASS", "符合小型微利企业条件，实际税负 5%（减免所得税额已计入）。")
        )
    else:
        reasons = "；".join((result.get("small_low_profit_check") or {}).get("reasons") or [])
        checks.append(
            _check(
                "小型微利企业条件",
                "WARNING",
                "不符合小型微利企业条件，按 25% 法定税率计算。" + (f"原因：{reasons}。" if reasons else ""),
            )
        )

    if round2(result.get("research_deduction")) > 0:
        checks.append(
            _check("研发加计资料", "PASS", f"研发费用加计扣除 {round2(result.get('research_deduction')):.2f} 元，请留存研发辅助账与立项资料。")
        )
    else:
        checks.append(_check("研发加计资料", "PASS", "本年无研发费用加计扣除。"))

    if round2(annual.get("revenue_exclusive")) == 0 and round2(annual.get("expense_total")) == 0:
        checks.append(_check("零申报提示", "WARNING", "全年无收入与费用记录，属零申报年度，仍需按期完成年度申报。"))
    else:
        checks.append(_check("零申报提示", "PASS", "本年有经营数据，正常申报。"))
    return checks


def _guide(year: str) -> list[str]:
    return [
        f"登录电子税务局，进入【我要办税】→【税费申报及缴纳】→【企业所得税年度申报（A类）】，选择 {year} 年度。",
        "依次填写/核对 A100000 主表、A105000 纳税调整项目明细表、A106000 企业所得税弥补亏损明细表、"
        "A107010 免税、减计收入及加计扣除优惠明细表、A107040 减免所得税优惠明细表。",
        "对照本辅助表主表行次录入：利润总额、纳税调整增加/减少额、研发加计扣除、弥补以前年度亏损、"
        "应纳税所得额、适用税率、应纳所得税额、已预缴与应补（退）税额。",
        f"在次年 5 月 31 日前提交申报并完成税款缴纳（多缴可申请退税/抵缴）；逾期请及时更正申报。",
        "留存备查资料：财务报表、纳税调整依据与合同发票、研发辅助账、亏损弥补资料、预缴申报表与完税凭证等。",
        "本表由台账与确定性引擎生成，仅作辅助测算与录入对照，不代替电子税务局年度申报；以主管税务机关口径为准。",
        config.DISCLAIMER,
    ]


# =============================================================================
# 构建 bundle
# =============================================================================
def build_cit_annual_return(year: Any, *, settlement: dict[str, Any] | None = None) -> dict[str, Any]:
    """生成企业所得税年度申报辅助表 bundle（主表 + 纳税调整 + 亏损弥补 + 政策资料）。"""
    text = settlement_module.normalize_year(year)
    result = dict(settlement) if isinstance(settlement, dict) else settlement_module.compute_settlement(text)
    annual = result.get("annual") if isinstance(result.get("annual"), dict) else settlement_module.annual_ledger_data(text)
    inputs = result.get("inputs") if isinstance(result.get("inputs"), dict) else settlement_module.get_annual_inputs(text)
    adjustments = result.get("adjustments") if isinstance(result.get("adjustments"), list) else settlement_module.list_adjustments(text)
    losses = result.get("losses") if isinstance(result.get("losses"), list) else [
        row for row in settlement_module.list_losses() if int(str(row.get("year") or "0")) < int(text)
    ]
    checks = _build_checks(result, annual, inputs)
    summary = _summary(checks)
    checklist = settlement_module.settlement_checklist(text, result)
    status = "草稿"
    record = settlement_module.get_settlement_record(text)
    if record is not None:
        status = str(record.get("status") or "草稿")

    start, end = f"{text}-01-01", f"{text}-12-31"
    bundle: dict[str, Any] = {
        "form_id": FORM_ID,
        "form_name": FORM_NAME,
        "category": CATEGORY,
        "period": text,
        "period_label": f"{text}年度",
        "frequency": FREQUENCY,
        "generated_at": config.now_text(),
        "status": status,
        "start": start,
        "end": end,
        "sections": [
            {"title": "主表（A 类关键行次）", "lines": _main_lines(result)},
            {"title": "纳税调整明细", "lines": _adjustment_lines(result)},
            {"title": "亏损弥补明细", "lines": _loss_lines(result)},
            {"title": "政策依据与资料留存", "lines": _policy_lines()},
        ],
        "totals": {
            "accounting_profit": round2(result.get("accounting_profit")),
            "adjustment_add": round2(
                round2(result.get("limit_adjustment_add")) + round2(result.get("manual_adjustment_add"))
            ),
            "adjustment_reduce": round2(result.get("manual_adjustment_reduce")),
            "rd_deduction": round2(result.get("research_deduction")),
            "loss_used": round2(result.get("loss_used")),
            "taxable_income": round2(result.get("taxable_income")),
            "corporate_tax": round2(result.get("corporate_tax")),
            "prepaid_tax": round2(result.get("prepaid_tax")),
            "payable": round2(result.get("payable")),
            "refund": round2(result.get("refund")),
            "payable_label": str(result.get("payable_label") or ""),
            "applicable_rate": float(result.get("applicable_rate") or 0.0),
        },
        "calc": result,
        "checks": checks,
        "summary": summary,
        "guide": _guide(text),
        "checklist": checklist,
        "data": {
            "year": text,
            "annual": annual,
            "inputs": inputs,
            "adjustments": adjustments,
            "losses": losses,
            "checklist": checklist,
            "prepaid_source": str(result.get("prepaid_source") or ""),
        },
        "policy_sources": get_settlement_sources(),
        "disclaimer": config.DISCLAIMER,
    }
    log_calculation(
        "企业所得税汇算辅助表",
        text,
        {"revenue_exclusive": annual.get("revenue_exclusive"), "prepaid_tax": result.get("prepaid_tax")},
        {
            "accounting_profit": result.get("accounting_profit"),
            "taxable_income": result.get("taxable_income"),
            "corporate_tax": result.get("corporate_tax"),
            "payable": result.get("payable"),
            "refund": result.get("refund"),
            "summary": summary["level"],
        },
    )
    return bundle


# =============================================================================
# Excel 导出（4 个 sheet）
# =============================================================================
def _default_path(year: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(config.EXPORT_DIR) / f"企业所得税年度申报辅助表_{year}_{stamp}.xlsx"


def _style_header(ws, row: int, headers: tuple[str, ...]) -> None:
    for index, header in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=index, value=header)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = _CENTER
        cell.border = _BORDER


def _write_title(ws, title: str, columns: int, subtitle: str = "") -> None:
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=columns)
    cell = ws.cell(row=1, column=1, value=title)
    cell.font = _TITLE_FONT
    cell.fill = _TITLE_FILL
    cell.alignment = _CENTER
    ws.row_dimensions[1].height = 28
    if subtitle:
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=columns)
        sub = ws.cell(row=2, column=1, value=subtitle)
        sub.alignment = _WRAP


def _write_main_sheet(ws, bundle: dict[str, Any]) -> None:
    widths = {"A": 10, "B": 40, "C": 18, "D": 46, "E": 46}
    for column, width in widths.items():
        ws.column_dimensions[column].width = width
    _write_title(
        ws,
        FORM_NAME,
        5,
        f"年度：{bundle.get('period')}　状态：{bundle.get('status')}　"
        "本表为辅助测算，实际申报在电子税务局完成",
    )
    _style_header(ws, 4, ("行次", "项目", "金额（元）", "数据来源", "口径说明"))
    row_index = 5
    for section in bundle.get("sections") or []:
        if section.get("title") != "主表（A 类关键行次）":
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
        "说明：本表由台账与确定性算税引擎生成，金额单位：元，保留 2 位小数。",
        "不代替电子税务局企业所得税年度申报（A 类）；最终以主管税务机关口径为准。",
        str(bundle.get("disclaimer") or config.DISCLAIMER),
    ]
    for note in notes:
        ws.merge_cells(start_row=row_index, start_column=1, end_row=row_index, end_column=5)
        cell = ws.cell(row=row_index, column=1, value=note)
        cell.alignment = _WRAP
        row_index += 1
    ws.freeze_panes = "A5"


def _write_adjustment_sheet(ws, bundle: dict[str, Any]) -> None:
    headers = ("序号", "调整项目", "方向", "会计金额", "税法金额", "调整金额", "计算依据", "政策依据")
    widths = (7, 24, 8, 14, 14, 14, 46, 36)
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    _write_title(ws, f"纳税调整明细 — {bundle.get('period')}年度", 8)
    _style_header(ws, 3, headers)
    row_index = 4
    for section in bundle.get("sections") or []:
        if section.get("title") != "纳税调整明细":
            continue
        for line in section.get("lines") or []:
            values = [
                line.get("line_no"),
                line.get("name"),
                line.get("direction") or "—",
                line.get("accounting_amount"),
                line.get("tax_amount"),
                line.get("adjustment_amount", line.get("value")),
                line.get("basis") or line.get("source"),
                line.get("rule"),
            ]
            for column, value in enumerate(values, start=1):
                cell = ws.cell(row=row_index, column=column, value=value)
                cell.border = _BORDER
                if column in (4, 5, 6):
                    cell.number_format = "#,##0.00"
                    cell.alignment = _RIGHT
                elif column in (1, 3):
                    cell.alignment = _CENTER
                else:
                    cell.alignment = _WRAP
            row_index += 1
    ws.freeze_panes = "A4"


def _write_loss_sheet(ws, bundle: dict[str, Any]) -> None:
    headers = ("序号", "亏损年度", "可弥补亏损额", "已弥补金额", "本年弥补", "剩余可弥补", "到期年度", "状态")
    widths = (7, 12, 16, 14, 14, 16, 12, 12)
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    _write_title(ws, f"亏损弥补明细 — {bundle.get('period')}年度", 8)
    _style_header(ws, 3, headers)
    row_index = 4
    for section in bundle.get("sections") or []:
        if section.get("title") != "亏损弥补明细":
            continue
        for line in section.get("lines") or []:
            if line.get("line_no") == "合计":
                ws.cell(row=row_index, column=1, value="合计")
                ws.cell(row=row_index, column=2, value=line.get("name"))
                ws.cell(row=row_index, column=5, value=float(line.get("value") or 0.0)).number_format = "#,##0.00"
                for column in range(1, 9):
                    ws.cell(row=row_index, column=column).border = _BORDER
                    ws.cell(row=row_index, column=column).fill = _TOTAL_FILL
                row_index += 1
                continue
            values = [
                line.get("line_no"),
                line.get("loss_year") or line.get("name"),
                line.get("loss_amount"),
                line.get("used_amount"),
                line.get("used_this_year", line.get("value")),
                line.get("remaining"),
                line.get("expire_year"),
                line.get("status"),
            ]
            for column, value in enumerate(values, start=1):
                cell = ws.cell(row=row_index, column=column, value=value)
                cell.border = _BORDER
                if column in (3, 4, 5, 6):
                    cell.number_format = "#,##0.00"
                    cell.alignment = _RIGHT
                elif column in (1, 2, 7, 8):
                    cell.alignment = _CENTER
            row_index += 1
    ws.freeze_panes = "A4"


def _write_policy_sheet(ws, bundle: dict[str, Any]) -> None:
    ws.column_dimensions["A"].width = 8
    ws.column_dimensions["B"].width = 120
    _write_title(ws, "政策依据、资料留存与操作步骤", 2)
    row_index = 3
    ws.cell(row=row_index, column=1, value="一、政策依据与留存资料")
    ws.cell(row=row_index, column=1).font = _SECTION_FONT
    ws.cell(row=row_index, column=1).fill = _SECTION_FILL
    ws.merge_cells(start_row=row_index, start_column=1, end_row=row_index, end_column=2)
    row_index += 1
    for section in bundle.get("sections") or []:
        if section.get("title") != "政策依据与资料留存":
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
    ws.cell(row=row_index, column=1, value="二、电子税务局操作步骤")
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
    ws.cell(row=row_index, column=1, value="三、自动检查清单")
    ws.cell(row=row_index, column=1).font = _SECTION_FONT
    ws.cell(row=row_index, column=1).fill = _SECTION_FILL
    ws.merge_cells(start_row=row_index, start_column=1, end_row=row_index, end_column=2)
    row_index += 1
    for item in bundle.get("checklist") or []:
        ws.cell(row=row_index, column=1, value=str(item.get("status") or ""))
        cell = ws.cell(row=row_index, column=2, value=f"{item.get('item')}：{item.get('message')}")
        cell.alignment = _WRAP
        status_cell = ws.cell(row=row_index, column=1)
        status_cell.fill = _STATUS_FILL.get(str(item.get("status")), _STATUS_FILL["PASS"])
        status_cell.alignment = _CENTER
        for column in (1, 2):
            ws.cell(row=row_index, column=column).border = _BORDER
        row_index += 1
    row_index += 1
    ws.cell(row=row_index, column=1, value=str(bundle.get("disclaimer") or config.DISCLAIMER))
    ws.merge_cells(start_row=row_index, start_column=1, end_row=row_index, end_column=2)


def export_cit_annual_excel(bundle: dict[str, Any], path: Any = None) -> Path:
    """导出年度申报辅助表 Excel（主表 / 纳税调整明细 / 亏损弥补明细 / 政策依据与操作步骤）。"""
    if not isinstance(bundle, dict):
        raise ValueError("申报表数据必须是 dict")
    year = str(bundle.get("period") or "年度")
    target = Path(path) if path else _default_path(year)
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    main_sheet = workbook.active
    main_sheet.title = "主表"
    _write_main_sheet(main_sheet, bundle)
    _write_adjustment_sheet(workbook.create_sheet("纳税调整明细"), bundle)
    _write_loss_sheet(workbook.create_sheet("亏损弥补明细"), bundle)
    _write_policy_sheet(workbook.create_sheet("政策依据与操作步骤"), bundle)
    workbook.save(target)
    return target


__all__ = [
    "FORM_ID",
    "FORM_NAME",
    "CATEGORY",
    "FREQUENCY",
    "PERIOD_KIND",
    "MATERIALS",
    "build_cit_annual_return",
    "export_cit_annual_excel",
]
