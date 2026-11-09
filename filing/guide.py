"""填报指引：把申报表 bundle 转成可操作的中文步骤清单与逐栏复制文本。

* :func:`build_guide` 生成「填报步骤」列表，写入 bundle 的 ``guide`` 字段并供导出 Excel 的
  「填报指引」sheet 使用。
* :func:`export_submission_text` 生成逐栏文本，便于对照电子税务局页面人工录入。
"""

from __future__ import annotations

import config
from engine.money import yuan

_FORM_STEPS: dict[str, list[str]] = {
    "VAT_QUARTERLY": [
        "登录云南省电子税务局（https://etax.yunnan.chinatax.gov.cn），进入【我要办税】→【税费申报及缴纳】→【增值税及附加税费申报（小规模纳税人适用）】。",
        "确认税款所属期与纳税人识别号无误后进入申报表；系统会自动带出开票数据，请与下方栏次核对。",
        "按主表栏次逐项录入：第 1 栏（应征增值税不含税销售额）、第 2 栏（税务机关代开的专用发票销售额）、第 10 栏（小微企业免税销售额）、第 11 栏（未达起征点销售额）。",
        "核对第 18 栏（应纳税额）、第 19 栏（本期免税额）、第 22 栏（应纳税额减征额），系统自动计算第 24 栏（本期应补（退）税额）。",
        "进入附加税费模块，核对城市维护建设税、教育费附加、地方教育附加，小规模纳税人享受「六税两费」减半。",
        "确认校验提示全部通过后提交申报；如产生税款，请在申报期内完成缴款。",
        "回到本系统点击「标记已申报」，并留存申报表 PDF 与完税凭证备查。",
    ],
    "CIT_PREPAY": [
        "登录云南省电子税务局，进入【我要办税】→【税费申报及缴纳】→【企业所得税月（季）度预缴纳税申报（A类）】。",
        "确认税款所属期，选择「按实际利润额预缴」方式，核对本年累计营业收入、营业成本与利润总额。",
        "按 A 类预缴表行次录入第 1—9 行，研发费用加计扣除填入第 8 行（按研发辅助账本年累计数）。",
        "核对第 10—13 行税率与减免税额，第 14 行填写本年累计已预缴税额，第 15 行为本期应补（退）税额。",
        "提交前确认符合小型微利企业条件（应纳税所得额≤300 万元、从业人数≤300 人、资产总额≤5000 万元）。",
        "提交成功后回到本系统点击「标记已申报」，并与财务报表数据交叉核对。",
    ],
    "STAMP_QUARTERLY": [
        "登录云南省电子税务局，进入【我要办税】→【税费申报及缴纳】→【财产和行为税纳税申报】→ 选择「印花税」。",
        "按合同台账逐份录入应税合同：合同类型、计税金额、适用税率；技术合同与买卖合同税率为 0.3‰。",
        "核对应纳税额与减免税额（小规模纳税人「六税两费」减半征收），确认本期应补（退）税额。",
        "提交申报并在申报期内完成缴款；合同原件与台账留存备查。",
        "回到本系统点击「标记已申报」。",
    ],
    "FIN_REPORT": [
        "登录云南省电子税务局，进入【我要办税】→【税费申报及缴纳】→【财务报表报送】。",
        "选择「小企业会计准则」报表类型，确认所属期。",
        "按本表「资产负债表」「利润表」数据录入，注意「年初数」「本年累计数」口径与账面一致。",
        "提交前核对：资产总计 = 负债和所有者权益总计；净利润 = 利润总额 - 所得税费用。",
        "提交成功后回到本系统点击「标记已申报」。",
    ],
}


def _lines(bundle: dict) -> dict[str, dict]:
    """把列次按 line_no 展平（先出现者为准，主表优先于附列资料）。"""
    mapping: dict[str, dict] = {}
    for section in bundle.get("sections") or []:
        for line in section.get("lines") or []:
            line_no = str(line.get("line_no") or "")
            if line_no and line_no not in mapping:
                mapping[line_no] = line
    return mapping


def _value(bundle: dict, line_no: str) -> float:
    line = _lines(bundle).get(str(line_no)) or {}
    try:
        return float(line.get("value") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _amount_text(value: float) -> str:
    return f"{yuan(value)} 元"


def build_guide(bundle: dict) -> list[str]:
    """生成申报填报步骤清单（中文、可操作），并在末尾附免责声明。"""
    if not isinstance(bundle, dict):
        raise ValueError("申报表数据必须是 dict")
    form_id = str(bundle.get("form_id") or "")
    period = str(bundle.get("period") or "")
    period_label_text = str(bundle.get("period_label") or period)
    totals = bundle.get("totals") or {}
    steps: list[str] = [f"税款所属期：{period_label_text}（{period}），报表：{bundle.get('form_name') or form_id}。"]
    details: list[str] = []

    if form_id == "VAT_QUARTERLY":
        steps.extend(_FORM_STEPS["VAT_QUARTERLY"])
        details.append(
            "本表关键栏次："
            f"第 1 栏 {_amount_text(_value(bundle, '1'))}；"
            f"第 2 栏 {_amount_text(_value(bundle, '2'))}；"
            f"第 10 栏 {_amount_text(_value(bundle, '10'))}；"
            f"第 11 栏 {_amount_text(_value(bundle, '11'))}。"
        )
        details.append(
            "本期税额："
            f"第 18 栏 {_amount_text(_value(bundle, '18'))}，"
            f"第 19 栏 {_amount_text(_value(bundle, '19'))}，"
            f"第 22 栏 {_amount_text(_value(bundle, '22'))}，"
            f"第 24 栏 {_amount_text(_value(bundle, '24'))}；"
            f"附加税费合计 {_amount_text(float(totals.get('surtax') or 0))}。"
        )
        if float(totals.get("vat_payable") or 0) == 0:
            details.append("本期增值税免征，仍需按期完成免税申报或零申报，不可直接不报。")
    elif form_id == "CIT_PREPAY":
        steps.extend(_FORM_STEPS["CIT_PREPAY"])
        details.append(
            "本表关键行次："
            f"第 1 行营业收入 {_amount_text(float(totals.get('revenue') or 0))}；"
            f"第 2 行营业成本 {_amount_text(float(totals.get('cost') or 0))}；"
            f"第 8 行研发加计扣除 {_amount_text(float(totals.get('rd_deduction') or 0))}；"
            f"第 15 行本期应补（退）税额 {_amount_text(float(totals.get('prepay_payable') or 0))}。"
        )
    elif form_id == "STAMP_QUARTERLY":
        steps.extend(_FORM_STEPS["STAMP_QUARTERLY"])
        details.append(
            "本表合计："
            f"合同 {int(totals.get('contract_count') or 0)} 份，"
            f"计税金额 {_amount_text(float(totals.get('total_amount') or 0))}，"
            f"减免前税额 {_amount_text(float(totals.get('total_before_discount') or 0))}，"
            f"减免税额 {_amount_text(float(totals.get('discount_amount') or 0))}，"
            f"应缴税额 {_amount_text(float(totals.get('total_payable') or 0))}。"
        )
    elif form_id == "FIN_REPORT":
        steps.extend(_FORM_STEPS["FIN_REPORT"])
        details.append(
            "本表估算结果："
            f"期末资产总计 {_amount_text(float(totals.get('total_assets') or 0))}；"
            f"本期营业收入 {_amount_text(float(totals.get('revenue') or 0))}；"
            f"本期净利润 {_amount_text(float(totals.get('net_profit') or 0))}。"
        )
    else:
        steps.append("请按申报表栏次逐项录入，并与台账数据核对后再提交。")
        steps.append("提交成功后回到本系统标记已申报，并留存申报表备查。")

    steps.extend(details)
    summary = (bundle.get("summary") or {}).get("text")
    if summary:
        steps.append(f"校验结论：{summary}")
    policy_text = str(bundle.get("policy_basis_text") or "").strip()
    if policy_text:
        steps.append(f"政策依据：{policy_text}。")
    steps.append(config.DISCLAIMER)
    return steps


def export_submission_text(bundle: dict) -> str:
    """生成逐栏复制文本（便于对照电子税务局人工录入），返回多行字符串。"""
    if not isinstance(bundle, dict):
        raise ValueError("申报表数据必须是 dict")
    lines: list[str] = [
        f"【{bundle.get('form_name') or bundle.get('form_id')}】",
        f"税款所属期：{bundle.get('period_label') or ''}（{bundle.get('period') or ''}）",
        f"状态：{bundle.get('status') or '草稿'}    生成时间：{bundle.get('generated_at') or ''}",
        "",
    ]
    for section in bundle.get("sections") or []:
        lines.append(f"—— {section.get('title') or '表格'} ——")
        for line in section.get("lines") or []:
            lines.append(
                f"第{line.get('line_no')}栏 {line.get('name')}：{yuan(line.get('value'))} 元"
                f"（来源：{line.get('source') or '—'}；说明：{line.get('rule') or '—'}）"
            )
        lines.append("")
    lines.append("—— 合计 ——")
    total_labels = {
        "vat_payable": "本期应纳增值税",
        "vat_exempt": "本期免税额",
        "surtax": "附加税费合计",
        "total_tax": "税费合计",
        "total_inclusive": "含税销售额合计",
        "total_exclusive": "不含税销售额合计",
        "prepay_payable": "本期应补（退）企业所得税",
        "total_payable": "应缴印花税",
        "total_assets": "期末资产总计",
    }
    for key, label in total_labels.items():
        if key in (bundle.get("totals") or {}):
            lines.append(f"{label}：{yuan(bundle['totals'][key])} 元")
    checks = bundle.get("checks") or []
    if checks:
        lines.append("")
        lines.append("—— 校验结果 ——")
        for check in checks:
            lines.append(f"[{check.get('status')}] {check.get('field')}：{check.get('message')}")
    lines.append("")
    lines.append(bundle.get("disclaimer") or config.DISCLAIMER)
    return "\n".join(lines)


__all__ = ["build_guide", "export_submission_text"]
