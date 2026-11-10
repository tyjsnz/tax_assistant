"""工商年报数据准备表（P1-D，辅助工具，不代替国家企业信用信息公示系统申报）。

工商年报由市场监管部门（国家企业信用信息公示系统 gsxt.gov.cn）办理，每年 1 月 1 日—6 月 30 日
报送上一年度报告；逾期未报会被列入经营异常名录。本模块只做**数据准备与核对**。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import config
from engine.money import round2, yuan

DISCLAIMER = config.DISCLAIMER
FORM_ID = "ANNUAL_REPORT"
FORM_NAME = "工商年报数据准备表"


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


def build_annual_report(year: str | int) -> dict[str, Any]:
    """生成工商年报数据准备 bundle（企业基本信息 + 资产状况 + 人数 + 校验/操作步骤）。"""
    from ledger import employees, settlement

    year_text = str(year)[:4]
    company = config.get_company()
    annual = settlement.annual_ledger_data(year_text)
    employee_summary = employees.summarize_employees()
    headcount = int(employee_summary.get("total") or 0)
    revenue = round2(annual.get("revenue_exclusive"))
    expense = round2(annual.get("expense_total"))
    profit = round2(annual.get("profit"))
    assets = round2(max(revenue - expense, 0.0))  # 货币资金简化估算
    liabilities = 0.0
    equity = round2(assets - liabilities)

    tax_total = 0.0
    try:
        from engine import calc_vat
        from ledger.settlement import compute_settlement

        vat = calc_vat(annual.get("revenue_inclusive", 0.0), 0.0, year_text)
        tax_total = round2(vat.get("total_tax", 0.0))
        cit = compute_settlement(year_text).get("corporate_tax", 0.0)
        tax_total = round2(tax_total + float(cit or 0))
    except Exception:  # noqa: BLE001 - 税费汇总失败不阻断年报数据准备
        pass

    sections = [
        {
            "title": "企业基本信息",
            "lines": [
                _line("1", "企业名称", company.get("name") or "", "设置 → 企业信息"),
                _line("2", "统一社会信用代码", company.get("unified_social_credit_code") or "", "设置 → 企业信息"),
                _line("3", "纳税人类型", company.get("taxpayer_type") or "", "设置 → 企业信息"),
                _line("4", "主管税务机关", company.get("tax_authority") or "", "设置 → 企业信息"),
                _line("5", "经营状态", "存续（在营、开业、在册）", "按工商登记", "如有变更请以登记为准"),
                _line("6", "从业人数", headcount, "员工档案 / 工资表", "与社保参保人数、年报口径核对"),
                _line("7", "社保参保人数", headcount, "按实际参保人数填写", "需与社保缴费记录一致"),
                _line("8", "联系电话", company.get("phone") or "（请填写）", "设置 → 企业信息"),
            ],
        },
        {
            "title": "资产状况（按台账估算，需与财务报表核对）",
            "lines": [
                _line("1", "资产总额", assets, "台账估算", "货币资金≈全年收入−全年费用，需与资产负债表核对"),
                _line("2", "负债总额", liabilities, "台账估算", "如有借款/应付款需补录"),
                _line("3", "所有者权益合计", equity, "台账估算", "资产−负债"),
                _line("4", "营业总收入", revenue, "收入台账（不含税）"),
                _line("5", "利润总额", profit, "台账估算", "收入−费用"),
                _line("6", "纳税总额", tax_total, "增值税及附加 + 企业所得税（估算）", "以完税凭证/申报记录为准"),
                _line("7", "研发费用", round2(annual.get("rd_cost")), "研发辅助账"),
            ],
        },
        {
            "title": "股东及出资信息（需人工补充）",
            "lines": [
                _line("1", "股东名称/姓名", "请从公司章程、工商登记信息填写", "", "年报必填项"),
                _line("2", "认缴出资额 / 实缴出资额", "请按股东出资情况填写", "", "单位：万元"),
                _line("3", "出资方式 / 出资时间", "请按章程填写", "", "货币/实物/知识产权等"),
            ],
        },
        {
            "title": "网站 / 网店、对外投资（需人工补充）",
            "lines": [
                _line("1", "网站或网店信息", "如有请填写网址/网店名称", "", "无则填“无”"),
                _line("2", "对外投资信息", "如有请填写被投资企业名称/统一代码", "", "无则填“无”"),
            ],
        },
    ]

    checks = [
        {
            "field": "从业人数",
            "status": "WARNING" if headcount == 0 else "PASS",
            "message": "员工档案为空：请先维护员工或按实际人数手工填写年报人数。"
            if headcount == 0
            else f"从业人数 {headcount} 人，与员工档案一致。",
        },
        {
            "field": "收入与台账一致",
            "status": "PASS",
            "message": f"营业总收入 {yuan(revenue)} 元来自台账，与财务报表核对后填写。",
        },
        {
            "field": "资产状况",
            "status": "WARNING",
            "message": "资产/负债为台账估算值，请以资产负债表为准（尤其存在借款、应收应付、固定资产时）。",
        },
        {
            "field": "股东及出资",
            "status": "WARNING",
            "message": "股东及出资信息需从公司章程/工商登记获取，系统无法自动生成。",
        },
        {
            "field": "申报期限",
            "status": "PASS",
            "message": "工商年报每年 1 月 1 日—6 月 30 日通过国家企业信用信息公示系统报送，逾期会被列入经营异常名录。",
        },
        {
            "field": "公示后修改",
            "status": "WARNING",
            "message": "年报公示后如需更正，应在 6 月 30 日前修改；6 月 30 日后更正需向登记机关申请。",
        },
    ]
    summary = {
        "level": "WARNING" if any(item["status"] != "PASS" for item in checks) else "PASS",
        "text": "工商年报数据准备完成，资产/股东信息需人工核对后到公示系统填报。",
    }
    guide = [
        "登录国家企业信用信息公示系统（https://www.gsxt.gov.cn），选择企业所在地区。",
        "使用电子营业执照或企业联络员信息登录，进入「企业信息填报」→「年度报告填写」。",
        "按本表「企业基本信息」「资产状况」填写；股东出资、网站网店、对外投资按公司章程/实际情况填写。",
        "从业人数、社保人数与工资表/社保缴费记录核对；资产状况与年度资产负债表、利润表核对。",
        "提交并公示（每年 1 月 1 日—6 月 30 日）；逾期未报将被列入经营异常名录。",
        "公示后如需更正，6 月 30 日前可自行修改；之后需向登记机关申请。",
        config.DISCLAIMER,
    ]

    return {
        "form_id": FORM_ID,
        "form_name": FORM_NAME,
        "category": "工商年报",
        "period": year_text,
        "period_label": f"{year_text} 年度",
        "frequency": "yearly",
        "generated_at": config.now_text(),
        "status": "草稿",
        "sections": sections,
        "totals": {
            "assets": assets,
            "liabilities": liabilities,
            "equity": equity,
            "revenue": revenue,
            "profit": profit,
            "tax_total": tax_total,
            "rd_cost": round2(annual.get("rd_cost")),
            "employees": headcount,
        },
        "calc": annual,
        "checks": checks,
        "summary": summary,
        "guide": guide,
        "data": {"annual": annual, "employees": employee_summary},
        "disclaimer": DISCLAIMER,
    }


def export_annual_report_excel(bundle: dict[str, Any], path: str | Path | None = None) -> Path:
    """导出工商年报数据准备表（主表 + 校验/操作步骤）。"""
    from openpyxl import Workbook

    target = Path(path) if path else Path(config.EXPORT_DIR) / f"工商年报数据准备_{bundle.get('period')}.xlsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "工商年报数据准备"
    ws.append([f"{bundle.get('form_name')}（{bundle.get('period_label')}）"])
    ws.append(["系统只做数据准备，实际填报请在国家企业信用信息公示系统完成"])
    ws.append([config.DISCLAIMER])
    ws.append([])
    for section in bundle.get("sections") or []:
        ws.append([section.get("title")])
        ws.append(["序号", "项目", "金额/内容", "来源/口径"])
        for line in section.get("lines") or []:
            ws.append([line.get("line_no"), line.get("name"), line.get("value_text"), line.get("source") or line.get("rule")])
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
