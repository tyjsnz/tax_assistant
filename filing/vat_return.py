"""增值税及附加税费申报表（小规模纳税人适用）生成。

栏次口径（与 :func:`engine.calc_vat` 的 ``return_lines`` 严格对齐）
---------------------------------------------------------------
* 第 1 栏 应征增值税不含税销售额（3% 征收率）= 免税额度内专票销售额；超限时为全部销售额
* 第 2 栏 税务机关代开的增值税专用发票不含税销售额 = 专票不含税销售额
* 第 10 栏 小微企业免税销售额 = 普票 + 未开票的不含税销售额（免税时）
* 第 11 栏 未达起征点销售额：按季申报不填（适用于按月申报未达起征点情形）
* 第 18 栏 应纳税额 = 第 1 栏 × 3%
* 第 19 栏 本期免税额 = 第 10 栏 × 3%
* 第 22 栏 应纳税额减征额 = 第 1 栏 ×（3% - 1%）
* 第 24 栏 本期应补（退）税额 = 第 18 栏 - 第 22 栏

附加税费：城建税 7%、教育费附加 3%、地方教育附加 2%，均按六税两费减半征收。
"""

from __future__ import annotations

from typing import Any

import config
from engine import calc_vat, period_label, quarter_range, round2, yuan
from ledger import income_ledger
from ledger.audit import log_calculation
from ledger.reports import normalize_quarter
from filing.guide import build_guide
from filing.validator import validate_bundle

FORM_ID = "VAT_QUARTERLY"
FORM_NAME = "增值税及附加税费申报表（小规模纳税人适用）"
CATEGORY = "增值税"
FREQUENCY = "quarterly"
PERIOD_KIND = "quarter"


# =============================================================================
# 内部工具
# =============================================================================
def _line(
    line_no: str,
    name: str,
    value: Any,
    source: str,
    rule: str,
    *,
    highlight: bool = False,
) -> dict[str, Any]:
    amount = round2(value)
    return {
        "line_no": str(line_no),
        "name": name,
        "value": amount,
        "value_text": yuan(amount),
        "source": source,
        "rule": rule,
        "highlight": highlight,
    }


def _normalize_income_totals(period: str, income_totals: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
    """规范化收入汇总数据；返回 ``(数据, 是否为手工指定)``。"""
    if income_totals is None:
        data = dict(income_ledger.period_totals(period))
        data["source"] = "ledger"
        return data, False
    if not isinstance(income_totals, dict):
        raise ValueError("income_totals 必须是 dict")
    total_inclusive = round2(income_totals.get("total_inclusive"))
    special_inclusive = round2(income_totals.get("special_inclusive"))
    if total_inclusive < 0 or special_inclusive < 0:
        raise ValueError("销售额不能为负数")
    if special_inclusive > total_inclusive:
        special_inclusive = total_inclusive
    total_exclusive = round2(total_inclusive / 1.01)
    special_exclusive = round2(special_inclusive / 1.01)
    by_invoice_type = income_totals.get("by_invoice_type") or {}
    data = {
        "period": period,
        "start": income_totals.get("start") or quarter_range(period)[0].isoformat(),
        "end": income_totals.get("end") or quarter_range(period)[1].isoformat(),
        "total_inclusive": total_inclusive,
        "special_inclusive": special_inclusive,
        "ordinary_inclusive": round2(total_inclusive - special_inclusive),
        "total_exclusive": total_exclusive,
        "special_exclusive": special_exclusive,
        "ordinary_exclusive": round2(total_exclusive - special_exclusive),
        "count": int(income_totals.get("count") or 0),
        "by_invoice_type": by_invoice_type,
        "source": "manual",
    }
    return data, True


def _compute(period: Any, income_totals: dict[str, Any] | None = None) -> dict[str, Any]:
    """计算增值税申报所需的全部数据（供构建与校验复用）。"""
    quarter = normalize_quarter(period)
    data, manual = _normalize_income_totals(quarter, income_totals)
    calc = calc_vat(data["total_inclusive"], data["special_inclusive"], period=quarter)
    return {
        "period": quarter,
        "period_label": period_label(quarter),
        "start": data["start"],
        "end": data["end"],
        "income_totals": data,
        "manual_input": manual,
        "calc": calc,
        "expected_lines": {
            "1": round2(calc["return_lines"]["line_1_taxable_sales"]),
            "2": round2(calc["special_exclusive"]),
            "10": round2(calc["return_lines"]["line_10_exempt_sales"]),
            "11": 0.0,
            "18": round2(calc["return_lines"]["line_18_tax_amount"]),
            "19": round2(calc["return_lines"]["line_19_tax_exempt_amount"]),
            "22": round2(calc["return_lines"]["line_22_reduction"]),
            "24": round2(calc["return_lines"]["line_24_payable"]),
        },
        "expected_surtax": {
            "city": round2(calc["return_lines"]["surtax_city"]),
            "education": round2(calc["return_lines"]["surtax_education"]),
            "local_education": round2(calc["return_lines"]["surtax_local_education"]),
            "total": round2(calc["surtax"]),
        },
    }


def _finalize(bundle: dict[str, Any]) -> dict[str, Any]:
    """为 bundle 补充校验结果与填报指引。"""
    report = validate_bundle(bundle)
    bundle["checks"] = [
        {
            "field": detail.get("field"),
            "status": detail.get("status"),
            "message": detail.get("message"),
            "expected": detail.get("expected"),
            "actual": detail.get("actual"),
        }
        for detail in report.get("details") or []
    ]
    bundle["summary"] = {"level": report["status"], "text": report["summary"]}
    bundle["guide"] = build_guide(bundle)
    return bundle


def _main_lines(computed: dict[str, Any]) -> list[dict[str, Any]]:
    calc = computed["calc"]
    totals = computed["income_totals"]
    by_type = totals.get("by_invoice_type") or {}
    ordinary_bucket = by_type.get("普票") or {}
    untitled_bucket = by_type.get("未开票") or {}
    ordinary_count = int(ordinary_bucket.get("count") or 0) + int(untitled_bucket.get("count") or 0)
    special_bucket = by_type.get("专票") or {}
    return [
        _line(
            "1",
            "应征增值税不含税销售额（3%征收率）",
            calc["return_lines"]["line_1_taxable_sales"],
            f"收入台账（专票 {int(special_bucket.get('count') or 0)} 笔；超免税额度时为全部销售额）",
            "免税额度内仅专票部分应征；超过季度免税额度时全部销售额按 1% 征收",
        ),
        _line(
            "2",
            "税务机关代开的增值税专用发票不含税销售额",
            calc["special_exclusive"],
            f"收入台账（专票 {int(special_bucket.get('count') or 0)} 笔，含税 {yuan(calc['special_inclusive'])} 元）",
            "专票含税金额 ÷ (1 + 1%)",
        ),
        _line(
            "10",
            "小微企业免税销售额",
            calc["return_lines"]["line_10_exempt_sales"],
            f"收入台账（普票 {int(ordinary_bucket.get('count') or 0)} 笔、未开票 {int(untitled_bucket.get('count') or 0)} 笔）",
            "季度不含税销售额 ≤ 300,000 元时，普票+未开票部分免征增值税",
        ),
        _line(
            "11",
            "未达起征点销售额",
            0.0,
            "—",
            "按季申报且未超季度免税额度时免税销售额填入第 10 栏；本栏适用于按月申报未达起征点情形，故填 0",
        ),
        _line(
            "18",
            "应纳税额",
            calc["return_lines"]["line_18_tax_amount"],
            "第 1 栏 × 3% 征收率",
            "按法定征收率 3% 计算的本期应纳税额",
        ),
        _line(
            "19",
            "本期免税额",
            calc["return_lines"]["line_19_tax_exempt_amount"],
            "第 10 栏 × 3%",
            "免税销售额按 3% 征收率计算的免税额",
        ),
        _line(
            "22",
            "应纳税额减征额",
            calc["return_lines"]["line_22_reduction"],
            "第 1 栏 × 2%（3% 减按 1% 征收）",
            "小规模纳税人 3% 征收率减按 1% 形成的减征额",
        ),
        _line(
            "24",
            "本期应补（退）税额",
            calc["return_lines"]["line_24_payable"],
            "第 18 栏 - 第 22 栏",
            "本期实际应缴纳的增值税（正数为应补，负数为应退）",
            highlight=True,
        ),
    ]


def _surtax_lines(computed: dict[str, Any]) -> list[dict[str, Any]]:
    calc = computed["calc"]
    expected = computed["expected_surtax"]
    return [
        _line("1", "城市维护建设税", expected["city"], f"第 24 栏 × 7% × 六税两费减半", "县城/镇适用税率 5%，市区 7%，此处按综合口径估算，以申报表为准"),
        _line("2", "教育费附加", expected["education"], "第 24 栏 × 3% × 六税两费减半", "按实际缴纳增值税计征"),
        _line("3", "地方教育附加", expected["local_education"], "第 24 栏 × 2% × 六税两费减半", "按实际缴纳增值税计征"),
        _line("4", "附加税费合计", expected["total"], "城建税 + 教育费附加 + 地方教育附加", "本期应缴增值税为 0 时附加税费同步免征"),
    ]


def _attachment_lines(computed: dict[str, Any]) -> list[dict[str, Any]]:
    totals = computed["income_totals"]
    by_type = totals.get("by_invoice_type") or {}
    lines: list[dict[str, Any]] = []
    for index, invoice_type in enumerate(("专票", "普票", "未开票"), start=1):
        bucket = by_type.get(invoice_type) or {}
        lines.append(
            _line(
                str(index),
                f"{invoice_type}销售额（不含税）",
                bucket.get("exclusive") or 0.0,
                f"含税 {yuan(bucket.get('inclusive') or 0)} 元，{int(bucket.get('count') or 0)} 笔",
                "台账按发票类型汇总，供附列资料核对",
            )
        )
    lines.append(
        _line(
            "4",
            "合计销售额（不含税）",
            totals.get("total_exclusive") or 0.0,
            f"含税合计 {yuan(totals.get('total_inclusive') or 0)} 元，共 {int(totals.get('count') or 0)} 笔",
            "专票 + 普票 + 未开票 = 合计",
            highlight=True,
        )
    )
    if float(totals.get("total_inclusive") or 0) == 0:
        lines.append(
            _line(
                "5",
                "零申报提示",
                0.0,
                "本期无收入记录",
                "本期可进行增值税零申报，零申报也需在申报期内提交",
            )
        )
    return lines


def build_vat_return(period: Any, *, income_totals: dict[str, Any] | None = None) -> dict[str, Any]:
    """生成增值税及附加税费申报表 bundle。

    参数
    ----
    period : 所属季度，支持 ``2026Q1``、``2026-03``（自动换算到所属季度）等写法
    income_totals : 可选，手工指定销售额汇总（不传则从收入台账取数）

    返回
    ----
    dict：统一「申报表 bundle」结构（sections / totals / calc / checks / summary / guide /
    data / disclaimer 等字段齐全）。
    """
    computed = _compute(period, income_totals)
    calc = computed["calc"]
    totals = computed["income_totals"]
    expected = computed["expected_lines"]
    surtax = computed["expected_surtax"]
    bundle: dict[str, Any] = {
        "form_id": FORM_ID,
        "form_name": FORM_NAME,
        "category": CATEGORY,
        "period": computed["period"],
        "period_label": computed["period_label"],
        "frequency": FREQUENCY,
        "generated_at": config.now_text(),
        "status": "草稿",
        "sections": [
            {"title": "主表（增值税）", "lines": _main_lines(computed)},
            {"title": "附加税费", "lines": _surtax_lines(computed)},
            {"title": "附列资料（销售额拆分）", "lines": _attachment_lines(computed)},
        ],
        "totals": {
            "vat_payable": round2(calc["vat_payable"]),
            "vat_exempt": round2(calc["vat_exempt"]),
            "vat_at_standard": round2(calc["vat_at_standard"]),
            "vat_reduction": round2(calc["vat_reduction"]),
            "surtax": round2(calc["surtax"]),
            "surtax_city": surtax["city"],
            "surtax_education": surtax["education"],
            "surtax_local_education": surtax["local_education"],
            "total_tax": round2(calc["total_tax"]),
            "total_inclusive": totals["total_inclusive"],
            "total_exclusive": totals["total_exclusive"],
            "special_inclusive": totals["special_inclusive"],
            "special_exclusive": totals["special_exclusive"],
            "ordinary_inclusive": totals["ordinary_inclusive"],
            "ordinary_exclusive": totals["ordinary_exclusive"],
            "taxable_sales": expected["1"],
            "exempt_sales": expected["10"],
            "within_exemption_limit": calc["within_exemption_limit"],
            "line_18": expected["18"],
            "line_22": expected["22"],
            "line_24": expected["24"],
        },
        "calc": calc,
        "checks": [],
        "summary": {"level": "PASS", "text": ""},
        "guide": [],
        "data": {
            "income_totals": totals,
            "by_invoice_type": totals.get("by_invoice_type") or {},
            "source": totals.get("source") or "ledger",
            "start": computed["start"],
            "end": computed["end"],
            "levy_rate": calc["levy_rate"],
            "exemption_limit": calc["exemption_limit"],
        },
        "disclaimer": config.DISCLAIMER,
    }
    log_calculation(
        "增值税",
        computed["period"],
        {
            "quarterly_total_inclusive": totals["total_inclusive"],
            "special_invoice_inclusive": totals["special_inclusive"],
        },
        {
            "vat_payable": calc["vat_payable"],
            "vat_exempt": calc["vat_exempt"],
            "surtax": calc["surtax"],
            "total_tax": calc["total_tax"],
            "within_exemption_limit": calc["within_exemption_limit"],
            "note": calc["note"],
        },
    )
    return _finalize(bundle)


__all__ = ["FORM_ID", "FORM_NAME", "CATEGORY", "FREQUENCY", "build_vat_return"]
