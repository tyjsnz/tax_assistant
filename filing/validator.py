"""申报表规则校验：栏次勾稽、与台账数据比对、与 OCR/手工栏次值比对。

校验级别
--------
* ``PASS``    ：栏次勾稽正确、与台账数据一致；
* ``WARNING`` ：存在提示项（如手工指定数据、单笔金额异常、口径说明），需人工复核；
* ``ERROR``   ：存在错误（与台账或标准答案不一致、勾稽关系不成立），必须修正后再提交。

对外函数
--------
* :func:`validate_bundle`            ：校验任意申报表 bundle（按 form_id 分派规则）；
* :func:`validate_vat_return`        ：重算增值税申报表并与 bundle 比对；
* :func:`check_ledger_consistency`   ：台账自检（发票拆分、金额勾稽、研发归集、合同等）；
* :func:`validate_against_fields`    ：与 OCR/手工填报的栏次值比对（标准答案来自台账重算）。
"""

from __future__ import annotations

import re
from typing import Any

import config
from engine import round2, yuan
from ledger import models
from ledger.expense_ledger import period_totals as expense_period_totals
from ledger.income_ledger import summarize_income
from ledger.reports import normalize_quarter, period_bounds
from ledger.rd_auxiliary import summarize_rd

STATUS_LEVELS = ("PASS", "WARNING", "ERROR")

#: 与 OCR / 手工填报对应的栏次标准答案键
_FIELD_KEYS = (
    "line_1",
    "line_2",
    "line_10",
    "line_11",
    "line_18",
    "line_19",
    "line_22",
    "line_24",
    "surtax_city",
    "surtax_education",
    "surtax_local_education",
    "surtax_total",
    "vat_payable",
    "total_payable",
)


# =============================================================================
# 基础工具
# =============================================================================
def _num(value: Any) -> float | None:
    """安全转换为金额（无法解析返回 ``None``）。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round2(value)
    text = str(value).strip().replace(",", "").replace("¥", "").replace("￥", "").replace("元", "")
    try:
        return round2(float(text))
    except (TypeError, ValueError):
        return None


def _detail(
    field: str,
    expected: Any = None,
    actual: Any = None,
    status: str = "PASS",
    message: str = "",
) -> dict[str, Any]:
    """构造一条校验明细。"""
    if status not in STATUS_LEVELS:
        status = "PASS"
    return {
        "field": str(field),
        "expected": expected,
        "actual": actual,
        "status": status,
        "message": str(message),
    }


def _compare(
    field: str,
    expected: Any,
    actual: Any,
    *,
    tol: float = 0.01,
    warn_tol: float | None = None,
    message: str = "",
) -> dict[str, Any]:
    """比较标准值与实际值，按差额给出 PASS/WARNING/ERROR。"""
    expected_value = _num(expected)
    actual_value = _num(actual)
    if actual_value is None:
        return _detail(field, expected_value, actual, "ERROR", message or "未填写或未识别到该栏次数据")
    expected_value = expected_value if expected_value is not None else 0.0
    diff = abs(expected_value - actual_value)
    if diff <= tol + 1e-9:
        text = message or f"勾稽正确（{yuan(actual_value)} 元）"
        if expected_value == 0 and actual_value == 0:
            text = message or "本期无发生额，勾稽正确"
        return _detail(field, expected_value, actual_value, "PASS", text)
    warn_limit = warn_tol if warn_tol is not None else max(1.0, abs(expected_value) * 0.005)
    if diff <= warn_limit:
        return _detail(
            field,
            expected_value,
            actual_value,
            "WARNING",
            message or f"与标准值相差 {yuan(diff)} 元（标准 {yuan(expected_value)} 元），请核对",
        )
    return _detail(
        field,
        expected_value,
        actual_value,
        "ERROR",
        message or f"与标准值不符：标准 {yuan(expected_value)} 元，实际 {yuan(actual_value)} 元，差额 {yuan(diff)} 元",
    )


def _report(details: list[dict[str, Any]], period: str = "", form_id: str = "") -> dict[str, Any]:
    """把校验明细汇总为差异报告（含状态、摘要与免责声明）。"""
    errors = [item for item in details if item["status"] == "ERROR"]
    warnings = [item for item in details if item["status"] == "WARNING"]
    if errors:
        status = "ERROR"
        summary = f"校验发现 {len(errors)} 项错误、{len(warnings)} 项提示，请修正后再提交。"
    elif warnings:
        status = "WARNING"
        summary = f"校验发现 {len(warnings)} 项提示，栏次勾稽基本正确，请人工复盘后提交。"
    else:
        status = "PASS"
        summary = "校验通过：栏次勾稽关系正确，数据与台账一致，可在申报前做最后复核。"
    result = {
        "status": status,
        "details": details,
        "summary": summary,
        "disclaimer": config.DISCLAIMER,
    }
    if period:
        result["period"] = period
    if form_id:
        result["form_id"] = form_id
    return result


def _lines(bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """把 bundle 所有分区的栏次按 ``line_no`` 展平（同名时以先出现者为准）。"""
    mapping: dict[str, dict[str, Any]] = {}
    for section in bundle.get("sections") or []:
        for line in section.get("lines") or []:
            line_no = str(line.get("line_no") or "")
            if line_no and line_no not in mapping:
                mapping[line_no] = line
    return mapping


def _section_lines(bundle: dict[str, Any], keyword: str) -> dict[str, dict[str, Any]]:
    """按分区标题关键字取栏次映射。"""
    for section in bundle.get("sections") or []:
        if keyword in str(section.get("title") or ""):
            return {str(line.get("line_no") or ""): line for line in section.get("lines") or []}
    return {}


def _line_value(lines: dict[str, dict[str, Any]], line_no: str) -> float:
    return _num((lines.get(str(line_no)) or {}).get("value")) or 0.0


def _find_by_name(lines: dict[str, dict[str, Any]], keyword: str) -> dict[str, Any] | None:
    for line in lines.values():
        if keyword in str(line.get("name") or ""):
            return line
    return None


# =============================================================================
# 增值税校验
# =============================================================================
def _check_vat(bundle: dict[str, Any], period: str) -> list[dict[str, Any]]:
    """增值税申报表勾稽校验（第 1/2/10/11/18/19/22/24 栏、附加税费、销售额拆分）。"""
    from filing.vat_return import _compute

    bundle_data = bundle.get("data") or {}
    manual_source = str(bundle_data.get("source") or "") == "manual"
    # 手工指定销售额时按 bundle 自身数据重算标准值，避免用台账数据误判
    computed = _compute(period, income_totals=bundle_data.get("income_totals")) if manual_source else _compute(period)
    calc = computed["calc"]
    lines = _lines(bundle)
    totals = bundle.get("totals") or {}
    details: list[dict[str, Any]] = []

    if manual_source:
        details.append(
            _detail(
                "数据来源",
                "收入台账",
                "手工指定销售额",
                "WARNING",
                "本期销售额为手工指定，未与收入台账逐笔比对；勾稽关系仍按填写值校验。",
            )
        )
    else:
        for line_no, expected in computed["expected_lines"].items():
            line = lines.get(line_no) or {}
            details.append(
                _compare(
                    f"第{line_no}栏 {line.get('name') or ''}".strip(),
                    expected,
                    line.get("value"),
                    message="",
                )
            )

    # ---- 栏次内勾稽关系（基于申报表自身填写值）----
    v1 = _line_value(lines, "1")
    v2 = _line_value(lines, "2")
    v10 = _line_value(lines, "10")
    v11 = _line_value(lines, "11")
    v18 = _line_value(lines, "18")
    v19 = _line_value(lines, "19")
    v22 = _line_value(lines, "22")
    v24 = _line_value(lines, "24")
    details.append(_compare("勾稽：第18栏 = 第1栏×3%", round2(v1 * 0.03), v18, tol=0.02, warn_tol=0.5))
    details.append(_compare("勾稽：第22栏 = 第1栏×2%", round2(v1 * 0.02), v22, tol=0.02, warn_tol=0.5))
    details.append(_compare("勾稽：第24栏 = 第18栏 - 第22栏", round2(v18 - v22), v24, tol=0.01, warn_tol=0.5))
    details.append(_compare("勾稽：第19栏 = 第10栏×3%", round2(v10 * 0.03), v19, tol=0.02, warn_tol=0.5))
    if v2 > v1 + 0.01:
        details.append(
            _detail("勾稽：第2栏 ≤ 第1栏", v1, v2, "ERROR", "专用发票销售额不得大于应征增值税销售额")
        )
    else:
        details.append(_detail("勾稽：第2栏 ≤ 第1栏", v1, v2, "PASS", "专用发票销售额未超过应征销售额"))

    # ---- 免税额度校验 ----
    limit = float(calc.get("exemption_limit") or 300000.0)
    total_exclusive = float(calc.get("total_exclusive") or 0.0)
    if v10 > 0:
        if v10 + v1 > limit + 0.05:
            details.append(
                _detail(
                    "免税额度：第1栏+第10栏",
                    limit,
                    round2(v10 + v1),
                    "ERROR",
                    f"免税销售额合计超过季度免税额度 {yuan(limit)} 元，不得享受小微企业免税。",
                )
            )
        else:
            details.append(
                _detail(
                    "免税额度：第1栏+第10栏",
                    limit,
                    round2(v10 + v1),
                    "PASS",
                    f"免税销售额合计 {yuan(v10 + v1)} 元未超过季度免税额度 {yuan(limit)} 元。",
                )
            )
    if v10 > limit + 0.05:
        details.append(
            _detail(
                "免税额度：第10栏 ≤ 300,000",
                limit,
                v10,
                "ERROR",
                f"第10栏免税销售额 {yuan(v10)} 元超过季度免税额度 {yuan(limit)} 元，不符合小微企业免税条件。",
            )
        )
    else:
        details.append(
            _detail(
                "免税额度：第10栏 ≤ 300,000",
                limit,
                v10,
                "PASS",
                f"第10栏免税销售额 {yuan(v10)} 元未超过季度免税额度 {yuan(limit)} 元。",
            )
        )
    details.append(_compare("勾稽：第1栏+第10栏 = 不含税总销售额", total_exclusive, round2(v1 + v10), tol=0.05, warn_tol=1.0))
    if v11 > 0.01:
        details.append(
            _detail(
                "第11栏 未达起征点销售额",
                0.0,
                v11,
                "WARNING",
                "按季申报的小规模纳税人免税销售额应填入第10栏；第11栏适用于按月申报未达起征点情形，请核对填报口径。",
            )
        )

    # ---- 附加税费校验 ----
    expected_surtax = computed["expected_surtax"]
    surtax_lines = _section_lines(bundle, "附加税费")
    surtax_names = [("城市维护建设税", "city"), ("教育费附加", "education"), ("地方教育附加", "local_education")]
    for keyword, key in surtax_names:
        line = _find_by_name(surtax_lines, keyword)
        details.append(
            _compare(
                f"附加税费：{keyword}",
                expected_surtax[key],
                line.get("value") if line else None,
                message="",
            )
        )
    surtax_total_line = _find_by_name(surtax_lines, "合计")
    details.append(
        _compare(
            "附加税费：合计",
            expected_surtax["total"],
            surtax_total_line.get("value") if surtax_total_line else totals.get("surtax"),
            message="附加税费合计（六税两费减半后）",
        )
    )

    # ---- 合计与台账一致性 ----
    details.append(_compare("合计：本期应补（退）税额", v24, totals.get("vat_payable"), message="第24栏与合计一致"))
    details.append(_compare("合计：不含税销售额", calc.get("total_exclusive"), totals.get("total_exclusive"), tol=0.01, message="申报销售额与台账一致"))
    details.append(_compare("合计：含税销售额", calc.get("total_inclusive"), totals.get("total_inclusive"), tol=0.01, message="申报含税销售额与台账一致"))

    # ---- 销售额拆分：专票 + 普票 + 未开票 = 合计 ----
    income_totals = computed["income_totals"]
    by_type = income_totals.get("by_invoice_type") or {}
    split_inclusive = 0.0
    split_exclusive = 0.0
    for invoice_type in models.INVOICE_TYPES:
        bucket = by_type.get(invoice_type) or {}
        split_inclusive = round2(split_inclusive + (_num(bucket.get("inclusive")) or 0.0))
        split_exclusive = round2(split_exclusive + (_num(bucket.get("exclusive")) or 0.0))
    if not by_type:
        details.append(
            _detail(
                "勾稽：发票类型拆分",
                "专票+普票+未开票",
                "未提供",
                "WARNING",
                "未提供发票类型拆分数据（手工指定销售额场景），无法核对专票+普票+未开票合计，请自行核对。",
            )
        )
    else:
        details.append(
            _compare(
                "勾稽：专票+普票+未开票 = 含税销售额合计",
                income_totals.get("total_inclusive"),
                split_inclusive,
                tol=0.01,
                message="发票类型拆分与含税销售额合计一致",
            )
        )
        details.append(
            _compare(
                "勾稽：专票+普票+未开票 = 不含税销售额合计",
                income_totals.get("total_exclusive"),
                split_exclusive,
                tol=0.05,
                warn_tol=1.0,
                message="发票类型拆分与不含税销售额合计一致（逐笔四舍五入允许分位差异）",
            )
        )
    return details


# =============================================================================
# 企业所得税校验
# =============================================================================
def _check_cit(bundle: dict[str, Any], period: str) -> list[dict[str, Any]]:
    """企业所得税预缴表 A 类行次勾稽校验（第 1—15 行）。"""
    from filing.cit_prepay import _compute

    computed = _compute(period)
    expected = computed["line_values"]
    lines = _lines(bundle)
    totals = bundle.get("totals") or {}
    details: list[dict[str, Any]] = []
    for index in range(1, 16):
        line_no = str(index)
        line = lines.get(line_no) or {}
        tolerance = 0.001 if index == 10 else 0.02
        details.append(
            _compare(
                f"第{index}行 {line.get('name') or ''}".strip(),
                expected[line_no],
                line.get("value"),
                tol=tolerance,
            )
        )

    v = {str(index): _line_value(lines, str(index)) for index in range(1, 16)}
    details.append(
        _compare("勾稽：第3行 = 第1行 - 第2行", round2(v["1"] - v["2"]), v["3"], tol=0.02, warn_tol=1.0)
    )
    details.append(
        _compare(
            "勾稽：第9行 = 第3行 + 第4行 - 第5行 - 第6行 - 第7行 - 第8行",
            round2(v["3"] + v["4"] - v["5"] - v["6"] - v["7"] - v["8"]),
            v["9"],
            tol=0.02,
            warn_tol=1.0,
        )
    )
    details.append(_compare("勾稽：第11行 = 第9行×25%", round2(v["9"] * 0.25), v["11"], tol=0.02, warn_tol=1.0))
    details.append(_compare("勾稽：第13行 = 第11行 - 第12行", round2(v["11"] - v["12"]), v["13"], tol=0.02, warn_tol=1.0))
    details.append(_compare("勾稽：第15行 = 第13行 - 第14行", round2(v["13"] - v["14"]), v["15"], tol=0.02, warn_tol=1.0))
    details.append(_compare("合计：本期应补（退）税额", v["15"], totals.get("prepay_payable"), message="第15行与合计一致"))
    details.append(_compare("合计：研发加计扣除额", v["8"], totals.get("rd_deduction"), message="第8行与研发辅助账一致"))
    details.append(
        _compare(
            "合计：实际应纳所得税额",
            v["13"],
            totals.get("cit_accrued"),
            message="第13行与合计一致",
        )
    )
    return details


# =============================================================================
# 印花税校验
# =============================================================================
def _check_stamp(bundle: dict[str, Any], period: str) -> list[dict[str, Any]]:
    """印花税申报表校验：合同类型汇总与逐份明细合计、减免额与合计一致。"""
    from filing.stamp_return import _compute

    computed = _compute(period)
    expected = computed["expected_totals"]
    totals = bundle.get("totals") or {}
    details: list[dict[str, Any]] = []
    for key, label in (
        ("total_amount", "计税金额合计"),
        ("total_before_discount", "减免前应纳税额"),
        ("discount_amount", "减免税额"),
        ("total_payable", "本期应补（退）税额"),
    ):
        details.append(_compare(f"合计：{label}", expected[key], totals.get(key), tol=0.02))
    details.append(
        _compare("合计：合同份数", expected["contract_count"], totals.get("contract_count"), tol=0.0, message="合同台账份数一致")
    )

    summary_lines = _section_lines(bundle, "主表")
    detail_lines = _section_lines(bundle, "合同明细")
    type_total = round2(sum(_line_value(summary_lines, str(index)) for index in range(1, len(summary_lines) + 1)))
    detail_total = round2(sum(_line_value(detail_lines, str(index)) for index in range(1, len(detail_lines) + 1)))
    details.append(
        _compare("勾稽：合同类型汇总 = 应缴税额合计", expected["total_payable"], type_total, tol=0.02, message="按合同类型汇总与合计一致")
    )
    details.append(
        _compare("勾稽：合同明细合计 = 应缴税额合计", expected["total_payable"], detail_total, tol=0.02, message="逐份合同税额与合计一致")
    )
    details.append(
        _compare(
            "勾稽：减免税额 = 减免前税额 - 应缴税额",
            round2(expected["total_before_discount"] - expected["total_payable"]),
            expected["discount_amount"],
            tol=0.02,
            message="六税两费减半减免额勾稽正确",
        )
    )
    return details


# =============================================================================
# 财务报表校验
# =============================================================================
def _check_fin(bundle: dict[str, Any], period: str) -> list[dict[str, Any]]:
    """财务报表校验：资产负债表平衡、利润表勾稽、与台账估算一致。"""
    from filing.fin_report import _compute

    computed = _compute(period)
    expected = computed["expected_totals"]
    totals = bundle.get("totals") or {}
    details: list[dict[str, Any]] = []
    for key, label in (
        ("total_assets", "资产总计"),
        ("total_liabilities", "负债合计"),
        ("total_equity", "所有者权益合计"),
        ("revenue", "本期营业收入"),
        ("cost", "本期营业成本"),
        ("profit", "本期利润总额"),
        ("net_profit", "本期净利润"),
        ("revenue_ytd", "本年累计营业收入"),
        ("net_profit_ytd", "本年累计净利润"),
    ):
        details.append(_compare(f"合计：{label}", expected[key], totals.get(key), tol=0.05))

    balance_lines = _section_lines(bundle, "期末数")
    v = {str(index): _line_value(balance_lines, str(index)) for index in range(1, 13)}
    details.append(
        _compare("勾稽：资产总计 = 负债和所有者权益总计", v["5"], v["12"], tol=0.02, message="资产负债表平衡")
    )
    details.append(
        _compare("勾稽：负债合计 + 所有者权益合计 = 资产总计", round2(v["8"] + v["11"]), v["5"], tol=0.02, message="资产负债勾稽正确")
    )
    details.append(
        _compare("勾稽：货币资金 = 流动资产合计", v["1"], v["3"], tol=0.02, message="流动资产勾稽正确")
    )

    income_lines = _section_lines(bundle, "本期数")
    i = {str(index): _line_value(income_lines, str(index)) for index in range(1, 12)}
    details.append(
        _compare(
            "勾稽：营业利润 = 营业收入 - 营业成本 - 税金及附加 - 销售费用 - 管理费用 - 研发费用 - 财务费用",
            round2(i["1"] - i["2"] - i["3"] - i["4"] - i["5"] - i["6"] - i["7"]),
            i["8"],
            tol=0.02,
            warn_tol=1.0,
        )
    )
    details.append(
        _compare("勾稽：利润总额 = 营业利润", i["8"], i["9"], tol=0.02, message="利润总额勾稽正确")
    )
    details.append(
        _compare("勾稽：净利润 = 利润总额 - 所得税费用", round2(i["9"] - i["10"]), i["11"], tol=0.02, message="净利润勾稽正确")
    )
    details.append(
        _detail(
            "数据口径",
            "账面数据",
            "按台账估算",
            "WARNING",
            "财务报表由台账推算（货币资金≈累计收入-累计费用，应收应付为 0），需与账面核对后再报送。",
        )
    )
    return details


# =============================================================================
# 对外接口
# =============================================================================
def validate_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """校验任意申报表 bundle，返回差异报告（``status`` / ``details`` / ``summary`` / ``disclaimer``）。"""
    if not isinstance(bundle, dict):
        raise ValueError("申报表数据必须是 dict")
    form_id = str(bundle.get("form_id") or "")
    period = str(bundle.get("period") or "")
    if not form_id:
        return _report(
            [_detail("报表编码", None, None, "ERROR", "申报表缺少 form_id，无法校验")],
            period=period,
        )
    try:
        if form_id == "VAT_QUARTERLY":
            details = _check_vat(bundle, period)
        elif form_id == "CIT_PREPAY":
            details = _check_cit(bundle, period)
        elif form_id == "STAMP_QUARTERLY":
            details = _check_stamp(bundle, period)
        elif form_id == "FIN_REPORT":
            details = _check_fin(bundle, period)
        else:
            details = _check_generic(bundle)
    except (ValueError, KeyError) as exc:
        details = [_detail("校验执行", None, None, "ERROR", f"校验失败：{exc}")]
    return _report(details, period=period, form_id=form_id)


def _check_generic(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """未知报表的通用结构校验。"""
    details: list[dict[str, Any]] = []
    sections = bundle.get("sections") or []
    if not sections:
        details.append(_detail("表格分区", "至少 1 个分区", 0, "ERROR", "申报表缺少表格分区 sections"))
    else:
        details.append(_detail("表格分区", "至少 1 个分区", len(sections), "PASS", "表格分区完整"))
    totals = bundle.get("totals")
    details.append(
        _detail(
            "合计数据",
            "dict",
            type(totals).__name__,
            "PASS" if isinstance(totals, dict) else "ERROR",
            "合计数据完整" if isinstance(totals, dict) else "合计数据 totals 缺失",
        )
    )
    details.append(
        _detail(
            "免责声明",
            config.DISCLAIMER,
            str(bundle.get("disclaimer") or ""),
            "PASS" if bundle.get("disclaimer") else "WARNING",
            "已附免责声明" if bundle.get("disclaimer") else "缺少免责声明",
        )
    )
    return details


def validate_vat_return(period: Any, bundle: dict[str, Any] | None = None) -> dict[str, Any]:
    """重算增值税申报表并与 ``bundle`` 比对（不传 bundle 时按台账重新生成）。"""
    from filing.vat_return import build_vat_return

    quarter = normalize_quarter(period)
    target = bundle if isinstance(bundle, dict) else build_vat_return(quarter)
    return validate_bundle(target)


def check_ledger_consistency(period: Any) -> dict[str, Any]:
    """台账自检：发票拆分、金额勾稽、季度归属、研发归集与合同金额异常。"""
    text, start, end = period_bounds(period)
    income = summarize_income(start=start, end=end)
    expense = expense_period_totals(text)
    details: list[dict[str, Any]] = []

    total_inclusive = income["total_inclusive"]
    total_exclusive = income["total_exclusive"]
    by_type = income["by_invoice_type"] or {}

    # 1) 发票类型拆分 = 合计
    split_inclusive = 0.0
    split_exclusive = 0.0
    for invoice_type in models.INVOICE_TYPES:
        bucket = by_type.get(invoice_type) or {}
        split_inclusive = round2(split_inclusive + float(bucket.get("inclusive") or 0.0))
        split_exclusive = round2(split_exclusive + float(bucket.get("exclusive") or 0.0))
    details.append(
        _compare("发票类型拆分：含税合计", total_inclusive, split_inclusive, tol=0.01, message="专票+普票+未开票含税金额与合计一致")
    )
    details.append(
        _compare(
            "发票类型拆分：不含税合计",
            total_exclusive,
            split_exclusive,
            tol=0.05,
            warn_tol=1.0,
            message="专票+普票+未开票不含税金额与合计一致（逐笔四舍五入允许分位差异）",
        )
    )

    # 2) 逐笔勾稽：不含税 + 增值税 = 含税
    bad_rows = 0
    zero_rows = 0
    from ledger.income_ledger import list_income

    rows = list_income(start=start, end=end)
    for row in rows:
        inclusive = round2(row.get("inclusive_amount"))
        exclusive = round2(row.get("exclusive_amount"))
        vat = round2(row.get("vat_amount"))
        if abs(round2(exclusive + vat) - inclusive) > 0.01:
            bad_rows += 1
        if inclusive <= 0:
            zero_rows += 1
    if bad_rows:
        details.append(
            _detail("逐笔金额勾稽", 0, bad_rows, "ERROR", f"有 {bad_rows} 笔收入的不含税金额+增值税额≠含税金额，请修正台账")
        )
    else:
        details.append(_detail("逐笔金额勾稽", 0, 0, "PASS", "每笔收入的不含税金额+增值税额=含税金额"))
    if zero_rows:
        details.append(_detail("金额异常", ">0", zero_rows, "WARNING", f"有 {zero_rows} 笔收入金额为 0，请确认是否误录"))

    # 3) 专票金额不得超过含税总额
    special_inclusive = float((by_type.get("专票") or {}).get("inclusive") or 0.0)
    if special_inclusive > total_inclusive + 0.01:
        details.append(
            _detail("专票金额校验", total_inclusive, special_inclusive, "ERROR", "专用发票含税金额大于全部含税收入，请核对")
        )
    else:
        details.append(_detail("专票金额校验", total_inclusive, special_inclusive, "PASS", "专用发票金额未超过收入合计"))

    # 4) 单笔金额异常
    if income["max_single"] > 1_000_000:
        details.append(
            _detail("单笔金额异常", "≤1,000,000", income["max_single"], "WARNING", "存在单笔超过 100 万元的收入，请确认是否录入错误")
        )
    else:
        details.append(_detail("单笔金额异常", "≤1,000,000", income["max_single"], "PASS", "单笔收入金额在合理范围"))

    # 5) 零收入提示
    if income["count"] == 0:
        details.append(_detail("收入记录", ">0 笔", 0, "WARNING", "本期无收入记录，请确认是否为零申报；零申报也需按期提交"))
    else:
        details.append(_detail("收入记录", ">0 笔", income["count"], "PASS", f"本期共 {income['count']} 笔收入记录"))

    # 6) 研发归集勾稽
    try:
        rd = summarize_rd(text)
        manual_total = round2(rd.get("manual_total") or 0.0)
        manual_items_total = round2(sum(float(value) for value in (rd.get("from_manual") or {}).get("items", {}).values()))
        details.append(
            _compare("研发归集：手工记录合计 = 六项费用合计", manual_total, manual_items_total, tol=0.02, message="研发手工记录勾稽正确")
        )
        total_collectible = round2(rd.get("total_collectible") or 0.0)
        deduction = round2(rd.get("deduction_amount") or 0.0)
        details.append(
            _compare(
                "研发归集：加计扣除额 = 可归集费用×100%",
                round2(total_collectible * float(rd.get("deduction_ratio") or 1.0)),
                deduction,
                tol=0.02,
                message="研发加计扣除额勾稽正确",
            )
        )
        skipped = (rd.get("from_expenses") or {}).get("skipped") or []
        if skipped:
            details.append(
                _detail(
                    "研发归集：剔除记录",
                    "0 笔",
                    len(skipped),
                    "WARNING",
                    "存在标记为研发相关但不属于加计扣除归集范围的费用（如房租），已剔除，请核对研发辅助账。",
                )
            )
    except (ValueError, KeyError) as exc:
        details.append(_detail("研发归集", None, None, "ERROR", f"研发归集校验失败：{exc}"))

    # 7) 合同金额异常
    from ledger.reports import list_contracts

    contracts = list_contracts(start=start, end=end)
    bad_contracts = [row for row in contracts if round2(row.get("amount")) <= 0]
    if bad_contracts:
        details.append(
            _detail("合同金额异常", ">0", len(bad_contracts), "WARNING", f"有 {len(bad_contracts)} 份合同计税金额为 0，请核对合同台账")
        )
    else:
        details.append(_detail("合同金额异常", ">0", 0, "PASS", "合同台账计税金额均大于 0"))

    # 8) 费用归集提示
    if expense["count"] == 0:
        details.append(_detail("费用记录", ">0 笔", 0, "WARNING", "本期无费用记录，请确认是否漏记成本费用"))
    else:
        details.append(_detail("费用记录", ">0 笔", expense["count"], "PASS", f"本期共 {expense['count']} 笔费用记录"))

    return _report(details, period=text)


def validate_against_fields(period: Any, fields: dict[str, Any]) -> dict[str, Any]:
    """与 OCR/手工填报的栏次值比对（标准答案来自台账重算的增值税申报表）。

    ``fields`` 键：``line_1``、``line_2``、``line_10``、``line_11``、``line_18``、``line_19``、
    ``line_22``、``line_24``、``surtax_city``、``surtax_education``、``surtax_local_education``、
    ``surtax_total``、``vat_payable``、``total_payable``；值为金额（float 或数字字符串）。
    """
    if not isinstance(fields, dict) or not fields:
        raise ValueError("未提供需要比对的栏次数据（fields 不能为空）")
    from filing.vat_return import build_vat_return

    quarter = normalize_quarter(period)
    bundle = build_vat_return(quarter)
    lines = _lines(bundle)
    totals = bundle.get("totals") or {}
    calc = bundle.get("calc") or {}
    return_lines = calc.get("return_lines") or {}
    expected: dict[str, float] = {
        "line_1": _num(return_lines.get("line_1_taxable_sales")) or 0.0,
        "line_2": _num(totals.get("special_exclusive")) or 0.0,
        "line_10": _num(return_lines.get("line_10_exempt_sales")) or 0.0,
        "line_11": 0.0,
        "line_18": _num(return_lines.get("line_18_tax_amount")) or 0.0,
        "line_19": _num(return_lines.get("line_19_tax_exempt_amount")) or 0.0,
        "line_22": _num(return_lines.get("line_22_reduction")) or 0.0,
        "line_24": _num(return_lines.get("line_24_payable")) or 0.0,
        "surtax_city": _num(return_lines.get("surtax_city")) or 0.0,
        "surtax_education": _num(return_lines.get("surtax_education")) or 0.0,
        "surtax_local_education": _num(return_lines.get("surtax_local_education")) or 0.0,
        "surtax_total": _num(totals.get("surtax")) or 0.0,
        "vat_payable": _num(totals.get("vat_payable")) or 0.0,
        "total_payable": round2((_num(totals.get("vat_payable")) or 0.0) + (_num(totals.get("surtax")) or 0.0)),
    }
    field_labels = {
        "line_1": "第1栏 应征增值税不含税销售额",
        "line_2": "第2栏 税务机关代开的增值税专用发票销售额",
        "line_10": "第10栏 小微企业免税销售额",
        "line_11": "第11栏 未达起征点销售额",
        "line_18": "第18栏 应纳税额",
        "line_19": "第19栏 本期免税额",
        "line_22": "第22栏 应纳税额减征额",
        "line_24": "第24栏 本期应补（退）税额",
        "surtax_city": "附加税费：城市维护建设税",
        "surtax_education": "附加税费：教育费附加",
        "surtax_local_education": "附加税费：地方教育附加",
        "surtax_total": "附加税费合计",
        "vat_payable": "应纳增值税合计",
        "total_payable": "本期应缴税费合计",
    }
    details: list[dict[str, Any]] = []
    provided: dict[str, float] = {}
    for raw_key, raw_value in fields.items():
        key = str(raw_key).strip()
        if key in expected:
            actual = _num(raw_value)
            if actual is None:
                details.append(_detail(field_labels[key], expected[key], raw_value, "WARNING", "未能识别该栏次金额，请人工核对"))
                continue
            provided[key] = actual
            details.append(_compare(field_labels[key], expected[key], actual))
            continue
        match = re.match(r"^line[_-]?(\d+)$", key)
        if match:
            line_no = str(int(match.group(1)))
            line = lines.get(line_no)
            if line is not None:
                actual = _num(raw_value)
                expected_value = _num(line.get("value")) or 0.0
                provided[key] = actual if actual is not None else 0.0
                details.append(_compare(f"第{line_no}栏 {line.get('name') or ''}".strip(), expected_value, actual))
            else:
                details.append(_detail(f"第{line_no}栏", None, raw_value, "WARNING", "该栏次暂无标准答案（本表未设置该栏），请人工核对"))
            continue
        details.append(_detail(key, None, raw_value, "WARNING", "未配置该栏次的比对口径，请人工核对"))

    # ---- 基于填报值自身的勾稽校验（不依赖台账）----
    def _f(key: str) -> float | None:
        return provided.get(key)

    if _f("line_1") is not None and _f("line_18") is not None:
        details.append(
            _compare("勾稽：第18栏 = 第1栏×3%", round2(_f("line_1") * 0.03), _f("line_18"), tol=0.02, warn_tol=0.5)
        )
    if _f("line_1") is not None and _f("line_22") is not None:
        details.append(
            _compare("勾稽：第22栏 = 第1栏×2%", round2(_f("line_1") * 0.02), _f("line_22"), tol=0.02, warn_tol=0.5)
        )
    if _f("line_18") is not None and _f("line_22") is not None and _f("line_24") is not None:
        details.append(
            _compare(
                "勾稽：第24栏 = 第18栏 - 第22栏",
                round2(_f("line_18") - _f("line_22")),
                _f("line_24"),
                tol=0.02,
                warn_tol=0.5,
            )
        )
    if _f("line_10") is not None and _f("line_19") is not None:
        details.append(
            _compare("勾稽：第19栏 = 第10栏×3%", round2(_f("line_10") * 0.03), _f("line_19"), tol=0.02, warn_tol=0.5)
        )
    if _f("line_24") is not None and _f("surtax_total") is not None:
        details.append(
            _compare("勾稽：附加税费 = 第24栏×6%", round2(_f("line_24") * 0.06), _f("surtax_total"), tol=0.02, warn_tol=0.5)
        )
    if _f("line_24") is not None and _f("surtax_total") is not None and _f("total_payable") is not None:
        details.append(
            _compare(
                "勾稽：应缴税费合计 = 第24栏 + 附加税费",
                round2(_f("line_24") + _f("surtax_total")),
                _f("total_payable"),
                tol=0.02,
                warn_tol=0.5,
            )
        )
    return _report(details, period=quarter)


__all__ = [
    "STATUS_LEVELS",
    "validate_bundle",
    "validate_vat_return",
    "check_ledger_consistency",
    "validate_against_fields",
]
