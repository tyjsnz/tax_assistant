"""Agent 工具集：供 DeepSeek function calling 调用的 15 个固定工具。

核心原则
--------
1. **模型不参与算税**：所有金额计算都调用 :mod:`engine` 的确定性函数，
   所有台账 / 申报操作都调用 :mod:`ledger` / :mod:`filing` 的公开接口；
2. 统一返回 ``{"ok", "data", "display", "error"}``，其中 ``display`` 是可直接展示的中文摘要；
3. 参数缺失或非法时返回中文可操作错误，不抛异常给模型；
4. 所有算税 / 申报结果附带 ``disclaimer``（可能来自 engine，也可能在此补全）。

工具名称固定为 :data:`TOOL_NAMES`，与 ``docs/CONTRACTS.md`` 4.3 节一致。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

import config
import engine
from engine.money import pct, round2, yuan

DISCLAIMER = config.DISCLAIMER

#: 15 个固定工具名称
TOOL_NAMES: list[str] = [
    "calc_vat",
    "calc_corp_tax",
    "calc_cit_prepay",
    "calc_cit_settlement",
    "calc_rd_deduction",
    "calc_stamp_tax",
    "calc_disability_fund",
    "calc_iit",
    "payroll_correction",
    "add_income",
    "add_expense",
    "query_ledger",
    "build_filing",
    "check_filing",
    "get_filing_calendar",
    "search_policy",
    "export_filing",
]

#: 申报表编码 → 中文名（用于展示与默认值）
FORM_LABELS: dict[str, str] = {
    "VAT_QUARTERLY": "增值税及附加税费申报表",
    "CIT_PREPAY": "企业所得税季度预缴申报表",
    "STAMP_QUARTERLY": "印花税申报表",
    "FIN_REPORT": "财务报表",
    "IIT_MONTHLY": "个人所得税扣缴申报表",
}

INCOME_CATEGORIES = ("技术服务", "软件开发", "运维服务", "其他")
EXPENSE_CATEGORIES = ("工资", "社保", "房租", "办公", "差旅", "软件订阅", "研发材料", "折旧", "其他")
INVOICE_TYPES = ("普票", "专票", "未开票")

_RD_COST_KEYS = ("personnel_cost", "material_cost", "depreciation", "amortization", "design_cost", "other_cost")


# =============================================================================
# 统一返回与参数工具
# =============================================================================
def _ok(data: Any, display: str) -> dict[str, Any]:
    return {"ok": True, "data": data, "display": display, "error": None}


def _err(message: str, data: Any = None) -> dict[str, Any]:
    return {"ok": False, "data": data, "display": "", "error": message}


def _disclaimer(result: Any) -> Any:
    """确保结果带免责声明。"""
    if isinstance(result, dict):
        result.setdefault("disclaimer", DISCLAIMER)
    return result


def _to_float(value: Any, label: str = "金额") -> float:
    """把模型 / 用户传入的金额转换为 float（容忍千分位、¥、元、空串）。"""
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是数字，不能是布尔值")
    text = str(value).strip().replace(",", "").replace("，", "").replace("¥", "").replace("￥", "").replace("元", "")
    try:
        return round2(float(text))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}「{value}」不是有效数字，请提供以元为单位的金额，如 80000 或 8万。") from exc


def _need_float(args: dict[str, Any], key: str, label: str) -> float:
    if key not in args or args.get(key) in (None, ""):
        raise ValueError(
            f"缺少必填参数 {key}（{label}），请提供以元为单位的数值，例如 {key}=80000。"
        )
    return _to_float(args.get(key), label)


def _opt_float(args: dict[str, Any], key: str, default: float = 0.0) -> float:
    if key not in args or args.get(key) in (None, ""):
        return default
    return _to_float(args.get(key), key)


def _opt_int(args: dict[str, Any], key: str) -> int | None:
    value = args.get(key)
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"参数 {key} 应为整数，收到「{value}」。") from exc


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("是", "true", "1", "yes", "y", "on", "研发", "研发相关"):
        return True
    if text in ("否", "false", "0", "no", "n", "off", ""):
        return False
    return default


def _period(args: dict[str, Any], key: str = "period", *, default: str | None = None) -> str:
    """解析所属期间，默认当前季度；非法格式抛中文错误。"""
    raw = args.get(key)
    text = str(raw).strip() if raw not in (None, "") else ""
    if not text:
        text = default or engine.current_quarter()
    normalized = text
    try:
        from ledger.models import normalize_period

        normalized = normalize_period(text, default="") or text
    except Exception:  # noqa: BLE001 - 台账模型不可用时按原文本校验
        normalized = text
    try:
        engine.period_range(normalized)
    except ValueError as exc:
        raise ValueError(
            f"期间「{raw}」格式不正确，请使用季度（如 2026Q1）或月份（如 2026-11）。"
        ) from exc
    return normalized


def _normalize_choice(value: Any, choices: tuple[str, ...], default: str, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        return default
    if text in choices:
        return text
    for choice in choices:
        if text in choice or choice in text:
            return choice
    # 常见简称映射
    alias = {"专用发票": "专票", "普通发票": "普票", "未开发票": "未开票", "工资薪金": "工资", "研发": "研发材料"}
    if text in alias and alias[text] in choices:
        return alias[text]
    return default


def _safe_calc_log(calc_type: str, period: str, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    """写审计日志（台账模块未就绪或写入失败时静默降级，不影响主流程）。"""
    try:
        from ledger.audit import log_calculation

        log_calculation(calc_type, period, inputs, result)
    except Exception:  # noqa: BLE001 - 审计日志失败不阻断工具
        pass


def _period_label(period: str) -> str:
    try:
        return engine.period_label(period)
    except Exception:  # pragma: no cover
        return period


# =============================================================================
# 工具实现：算税
# =============================================================================
def _tool_calc_vat(args: dict[str, Any]) -> dict[str, Any]:
    total = _need_float(args, "quarterly_total_inclusive", "季度含税总销售额")
    special = _opt_float(args, "special_invoice_inclusive", 0.0)
    period = _period(args)
    result = _disclaimer(engine.calc_vat(total, special, period))
    _safe_calc_log("vat", period, {"quarterly_total_inclusive": total, "special_invoice_inclusive": special}, result)

    lines = [
        f"【增值税及附加税费 · {_period_label(period)}】",
        f"- 季度含税销售额：¥{yuan(result['total_inclusive'])}（其中专票 ¥{yuan(result['special_inclusive'])}）",
        f"- 不含税销售额：¥{yuan(result['total_exclusive'])}"
        f"（专票 ¥{yuan(result['special_exclusive'])}，普票+未开票 ¥{yuan(result['ordinary_exclusive'])}）",
        f"- 季度免税额度：¥{yuan(result['exemption_limit'])}，"
        f"{'未超过' if result['within_exemption_limit'] else '已超过'}免税标准",
        f"- 应缴增值税：¥{yuan(result['vat_payable'])}；免征增值税：¥{yuan(result['vat_exempt'])}",
        f"- 应缴附加税费（六税两费减半后）：¥{yuan(result['surtax'])}；合计应纳税费：¥{yuan(result['total_tax'])}",
        f"- 说明：{result.get('note', '')}",
        f"- 政策依据：{result.get('vat_policy_source') or '以主管税务机关公布的最新政策为准'}",
    ]
    for warning in result.get("warnings") or []:
        lines.append(f"⚠️ {warning}")
    lines.append(DISCLAIMER)
    return _ok(result, "\n".join(lines))


def _tool_calc_corp_tax(args: dict[str, Any]) -> dict[str, Any]:
    period = _period(args, default="")
    annual_profit: float | None = None
    if args.get("annual_profit") not in (None, ""):
        annual_profit = _to_float(args.get("annual_profit"), "年度利润总额")
    elif args.get("annual_revenue_exclusive") not in (None, "") and args.get("annual_cost") not in (None, ""):
        annual_profit = None  # 由 engine 用收入 - 成本计算
    else:
        raise ValueError(
            "请提供 annual_profit（年度利润总额），或同时提供 annual_revenue_exclusive（年度不含税收入）"
            "与 annual_cost（年度成本费用），单位：元。"
        )
    result = _disclaimer(
        engine.calc_corp_tax(
            annual_profit=annual_profit,
            rd_cost=_opt_float(args, "rd_cost", 0.0),
            annual_revenue_exclusive=_opt_float(args, "annual_revenue_exclusive", 0.0) if args.get("annual_revenue_exclusive") not in (None, "") else None,
            annual_cost=_opt_float(args, "annual_cost", 0.0) if args.get("annual_cost") not in (None, "") else None,
            employees=_opt_int(args, "employees"),
            total_assets=_opt_float(args, "total_assets", 0.0) if args.get("total_assets") not in (None, "") else None,
            rd_formed_intangible=_to_bool(args.get("rd_formed_intangible"), False),
            period=period,
        )
    )
    _safe_calc_log("corp_tax", period, dict(args), result)
    lines = [
        f"【企业所得税年度测算 · {period or '按年度口径'}】",
        f"- 会计利润：¥{yuan(result['annual_profit'])}；研发费用：¥{yuan(result['rd_cost'])}",
        f"- 研发加计扣除：¥{yuan(result['rd_deduction'])} → 应纳税所得额：¥{yuan(result['taxable_income'])}",
        f"- {'符合' if result['qualifies_small_low_profit'] else '不符合'}小型微利企业条件，"
        f"实际税负 {pct(result['effective_rate'])}",
        f"- 应缴企业所得税：¥{yuan(result['corp_tax'])}"
        f"（法定税率税额 ¥{yuan(result['gross_tax'])}，减免 ¥{yuan(result['tax_reduction'])}）",
        f"- 说明：{result.get('note', '')}",
        "- 政策依据：财政部 税务总局公告2023年第12号、2023年第7号",
        DISCLAIMER,
    ]
    return _ok(result, "\n".join(lines))


def _tool_calc_cit_prepay(args: dict[str, Any]) -> dict[str, Any]:
    quarter_profit = _need_float(args, "quarter_profit", "本季度利润总额")
    ytd_profit = _need_float(args, "ytd_profit", "本年累计利润总额")
    period = _period(args)
    result = _disclaimer(
        engine.calc_cit_prepay(
            quarter_profit,
            ytd_profit,
            ytd_cit_paid=_opt_float(args, "ytd_cit_paid", 0.0),
            period=period,
            rd_cost_ytd=_opt_float(args, "rd_cost_ytd", 0.0),
            employees=_opt_int(args, "employees"),
            total_assets=_opt_float(args, "total_assets", 0.0) if args.get("total_assets") not in (None, "") else None,
        )
    )
    _safe_calc_log("cit_prepay", period, dict(args), result)
    lines = [
        f"【企业所得税季度预缴 · {_period_label(period)}】",
        f"- 本季度利润：¥{yuan(result['quarter_profit'])}；本年累计利润：¥{yuan(result['ytd_profit'])}",
        f"- 本年累计研发加计扣除：¥{yuan(result['rd_deduction'])}；"
        f"累计应纳税所得额：¥{yuan(result['taxable_income_ytd'])}",
        f"- 累计应纳所得税：¥{yuan(result['cit_accrued'])}；已预缴：¥{yuan(result['cit_paid'])}",
        f"- 本期应预缴：¥{yuan(result['prepay_payable'])}",
        f"- 说明：{result.get('note', '')}",
        DISCLAIMER,
    ]
    return _ok(result, "\n".join(lines))


def _coerce_list_arg(value: Any, label: str) -> list[Any]:
    """把模型传入的列表参数（list / JSON 字符串 / dict 包裹）统一为 list。"""
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in ("items", "adjustments", "losses", "data", "list"):
                if isinstance(parsed.get(key), list):
                    return parsed[key]
        raise ValueError(f"{label} 需为数组（list），例如 [] 或 JSON 数组字符串，收到「{text[:80]}」。")
    raise ValueError(f"{label} 需为数组（list）。")


def _normalize_adjustments_arg(value: Any) -> list[dict[str, Any]]:
    """规范化 ``extra_adjustments``：``[{item_type, direction, adjustment_amount, ...}]``。"""
    items = _coerce_list_arg(value, "extra_adjustments")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"extra_adjustments 第 {index} 项应为对象，例如 "
                             '{"item_type": "罚款支出", "direction": "调增", "adjustment_amount": 5000}。')
        direction = str(item.get("direction") or item.get("方向") or "调增").strip()
        if direction not in ("调增", "调减"):
            direction = "调减" if direction in ("减少", "减", "扣除", "-") else "调增"
        amount = _to_float(
            item.get("adjustment_amount", item.get("amount", item.get("金额"))),
            f"第 {index} 条纳税调整金额",
        )
        if amount < 0:
            amount = abs(amount)
        normalized.append(
            {
                "item_type": str(item.get("item_type") or item.get("item") or item.get("项目") or "其他调整").strip(),
                "direction": direction,
                "adjustment_amount": amount,
                "accounting_amount": _to_float(item.get("accounting_amount"), "账面金额"),
                "tax_amount": _to_float(item.get("tax_amount"), "税收金额"),
                "basis": str(item.get("basis") or "").strip(),
                "source": str(item.get("source") or "").strip(),
                "note": str(item.get("note") or "").strip(),
            }
        )
    return normalized


def _normalize_losses_arg(value: Any, year: str) -> list[dict[str, Any]]:
    """规范化 ``losses``：``[{year, loss_amount, used_amount}]``。"""
    items = _coerce_list_arg(value, "losses")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            normalized.append({"year": str(int(year) - 1), "loss_amount": round2(float(item)), "used_amount": 0.0})
            continue
        if not isinstance(item, dict):
            raise ValueError(
                f"losses 第 {index} 项应为对象，例如 "
                '{"year": "2024", "loss_amount": 100000, "used_amount": 0}。'
            )
        raw_year = item.get("year", item.get("亏损年度"))
        if raw_year in (None, ""):
            loss_year = int(year) - 1
        else:
            try:
                loss_year = int(float(str(raw_year)))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"losses 第 {index} 项年度「{raw_year}」不合法，请提供 4 位年度。") from exc
        normalized.append(
            {
                "year": str(loss_year),
                "loss_amount": _to_float(item.get("loss_amount", item.get("amount", item.get("金额"))), "亏损金额"),
                "used_amount": _to_float(item.get("used_amount"), "已弥补金额"),
            }
        )
    return normalized


def _tool_calc_cit_settlement(args: dict[str, Any]) -> dict[str, Any]:
    """企业所得税年度汇算清缴辅助测算（限额类调增、手工调整、亏损弥补、小微优惠）。"""
    raw_year = args.get("year")
    if raw_year in (None, ""):
        year = str(config.today().year)
    else:
        match = re.search(r"(20\d{2})", str(raw_year))
        if not match:
            raise ValueError(f"year「{raw_year}」格式不正确，请提供 4 位纳税年度，如 2026。")
        year = match.group(1)

    revenue = _opt_float(args, "revenue_exclusive", 0.0)
    cost = _opt_float(args, "cost_total", 0.0)
    has_profit = args.get("accounting_profit") not in (None, "")
    accounting_profit = _opt_float(args, "accounting_profit", 0.0) if has_profit else None
    adjustments = _normalize_adjustments_arg(args.get("extra_adjustments"))
    losses = _normalize_losses_arg(args.get("losses"), year)
    if not has_profit and revenue == 0 and cost == 0 and not adjustments and not losses and not args.get("rd_deduction"):
        raise ValueError(
            "请至少提供年度数据再测算：revenue_exclusive（年度不含税收入）与 cost_total（成本费用），"
            "或直接提供 accounting_profit（会计利润总额），单位：元。"
        )

    result = _disclaimer(
        engine.calc_settlement(
            year=year,
            revenue_exclusive=revenue,
            cost_total=cost,
            accounting_profit=accounting_profit,
            salary_total=_opt_float(args, "salary_total", 0.0),
            entertainment_actual=_opt_float(args, "entertainment", 0.0),
            advertising_actual=_opt_float(args, "advertising", 0.0),
            welfare_actual=_opt_float(args, "welfare", 0.0),
            education_actual=_opt_float(args, "education", 0.0),
            union_actual=_opt_float(args, "union_actual", 0.0),
            rd_deduction=_opt_float(args, "rd_deduction", 0.0),
            extra_adjustments=adjustments,
            losses=losses,
            prepaid_tax=_opt_float(args, "prepaid_tax", 0.0),
            employees=_opt_int(args, "employees"),
            total_assets=_opt_float(args, "total_assets", 0.0) if args.get("total_assets") not in (None, "") else None,
        )
    )
    _safe_calc_log(
        "cit_settlement",
        year,
        {
            "revenue_exclusive": revenue,
            "cost_total": cost,
            "accounting_profit": accounting_profit,
            "prepaid_tax": result.get("prepaid_tax"),
        },
        result,
    )

    small = "符合" if result.get("qualifies_small_low_profit") else "不符合"
    lines = [
        f"【企业所得税年度汇算清缴（辅助测算）· {year} 年度】",
        f"- 年度不含税收入：¥{yuan(result.get('revenue_exclusive'))}；"
        f"成本费用：¥{yuan(result.get('cost_total'))}；会计利润：¥{yuan(result.get('accounting_profit'))}",
        f"- 纳税调整增加：¥{yuan(result.get('manual_adjustment_add', 0) + result.get('limit_adjustment_add', 0))}"
        f"（限额类 ¥{yuan(result.get('limit_adjustment_add'))}，手工调增 ¥{yuan(result.get('manual_adjustment_add'))}）；"
        f"纳税调整减少：¥{yuan(result.get('manual_adjustment_reduce'))}",
    ]
    for item in result.get("limit_adjustment_items") or []:
        if not item.get("actual"):
            continue
        lines.append(
            f"  · {item['label']}：发生额 ¥{yuan(item['actual'])}，税前限额 ¥{yuan(item['limit'])}，"
            f"可扣除 ¥{yuan(item['deductible'])}，调增 ¥{yuan(item['adjustment_add'])}"
        )
    for item in result.get("manual_adjustment_items") or []:
        lines.append(
            f"  · {item['item_type']}（{item['direction']}）：¥{yuan(item['adjustment_amount'])}"
            + (f"，依据：{item['basis']}" if item.get("basis") else "")
        )
    lines.append(f"- 研发费用加计扣除：¥{yuan(result.get('research_deduction'))}")
    loss_result = result.get("loss_carryforward") or {}
    if loss_result.get("lines"):
        for line in loss_result["lines"]:
            lines.append(
                f"  · {line['year']} 年度亏损：¥{yuan(line['loss_amount'])}，"
                f"本年弥补 ¥{yuan(line['used_this_year'])}，剩余 ¥{yuan(line['remaining_after'])}"
                f"（{line['status']}）"
            )
    lines.extend(
        [
            f"- 弥补以前年度亏损：¥{yuan(result.get('loss_used'))}（一般 5 年，高新技术/科技型中小企业 10 年）",
            f"- 应纳税所得额：¥{yuan(result.get('taxable_income'))}；"
            f"{small}小型微利企业条件，适用税率 {pct(result.get('applicable_rate'))}",
            f"- 应纳所得税额：¥{yuan(result.get('corporate_tax'))}"
            f"（小微减免 ¥{yuan(result.get('tax_reduction'))}）；已预缴：¥{yuan(result.get('prepaid_tax'))}",
            f"- **{result.get('payable_label') or '应补税额'}：¥{yuan(result.get('refund') or result.get('payable'))}**",
            f"- 说明：{result.get('note', '')}",
            f"- 申报期限：{int(year) + 1} 年 5 月 31 日前完成年度汇算清缴（多退少补）",
            "- 政策依据：",
        ]
    )
    for source in result.get("policy_sources") or []:
        lines.append(
            f"  · {source.get('source')}（{source.get('effective_date')} 起）：{source.get('title')}"
        )
    for warning in result.get("warnings") or []:
        lines.append(f"⚠️ {warning}")
    lines.append(DISCLAIMER)
    return _ok(result, "\n".join(lines))


def _tool_calc_rd_deduction(args: dict[str, Any]) -> dict[str, Any]:
    period = _period(args)
    formed_intangible = _to_bool(args.get("formed_intangible"), False)
    provided = {key: _opt_float(args, key, 0.0) for key in _RD_COST_KEYS}
    source = ""
    result: dict[str, Any]
    if any(value > 0 for value in provided.values()):
        result = engine.calc_rd_deduction(
            **provided,
            formed_intangible=formed_intangible,
            period=period,
            project_name=str(args.get("project_name") or "研发项目"),
        )
        source = "params"
    else:
        summary: dict[str, Any] | None = None
        try:
            from ledger.rd_auxiliary import summarize_rd

            summary = summarize_rd(period)
        except Exception:  # noqa: BLE001 - 台账模块未就绪时降级为 engine 空归集
            summary = None
        if isinstance(summary, dict) and "total_collectible" in summary:
            result = dict(summary)
            source = "ledger"
            if formed_intangible and not result.get("formed_intangible"):
                result = engine.calc_rd_deduction(
                    **{key: round2(result.get(key) or 0) for key in _RD_COST_KEYS},
                    formed_intangible=True,
                    period=period,
                )
        else:
            result = engine.calc_rd_deduction(formed_intangible=formed_intangible, period=period)
            source = "empty"
    result = _disclaimer(result)
    _safe_calc_log("rd_deduction", period, {"formed_intangible": formed_intangible, "source": source}, result)

    lines = [
        f"【研发费用归集与加计扣除 · {_period_label(period)}】",
    ]
    if result.get("items"):
        for label, amount in result["items"].items():
            lines.append(f"- {label}：¥{yuan(amount)}")
    lines.extend(
        [
            f"- 可加计扣除研发费用合计：¥{yuan(result.get('total_collectible'))}",
            f"- 加计扣除比例：{pct(result.get('deduction_ratio'))}；"
            f"加计扣除额：¥{yuan(result.get('deduction_amount'))}",
            f"- 说明：{result.get('note', '')}",
        ]
    )
    if source == "empty":
        lines.append("⚠️ 台账中没有本期已标记「研发相关」的费用记录，请先在费用台账标记研发费用。")
    lines.append("- 政策依据：财政部 税务总局公告2023年第7号；国家税务总局公告2021年第28号")
    lines.append(DISCLAIMER)
    return _ok(result, "\n".join(lines))


def _tool_calc_stamp_tax(args: dict[str, Any]) -> dict[str, Any]:
    period = _period(args)
    contracts: list[dict[str, Any]] = []
    try:
        from ledger import models, store

        start, end = engine.period_range(period)
        rows = store.table("contracts").filter(start=start.isoformat(), end=end.isoformat())
        for row in rows:
            contracts.append(
                {
                    "contract_type": row.get("contract_type"),
                    "contract_no": row.get("contract_no"),
                    "counterparty": row.get("counterparty"),
                    "date": row.get("date"),
                    "amount": round2(row.get("amount")),
                }
            )
    except Exception:  # noqa: BLE001 - 合同台账不可用时按空合同计算
        contracts = []
    result = _disclaimer(engine.calc_stamp_tax(contracts, small_scale=True, period=period))
    _safe_calc_log("stamp_tax", period, {"contract_count": len(contracts)}, result)

    lines = [
        f"【印花税 · {_period_label(period)}】",
        f"- 本期合同 {len(result.get('items') or [])} 份，计税金额合计 ¥{yuan(result.get('total_amount'))}",
        f"- 减免前税额：¥{yuan(result.get('total_before_discount'))}；"
        f"六税两费减半减免：¥{yuan(result.get('discount_amount'))}",
        f"- 应缴印花税：¥{yuan(result.get('total_payable'))}",
        f"- 说明：{result.get('note', '')}",
    ]
    for item in (result.get("items") or [])[:5]:
        lines.append(
            f"  · {item.get('contract_type')} {item.get('contract_no') or ''} "
            f"金额 ¥{yuan(item.get('amount'))} × {item.get('rate_text')} → ¥{yuan(item.get('tax_payable'))}"
        )
    lines.append("- 政策依据：《中华人民共和国印花税法》；财政部 税务总局公告2023年第12号")
    lines.append(DISCLAIMER)
    return _ok(result, "\n".join(lines))


def _tool_calc_disability_fund(args: dict[str, Any]) -> dict[str, Any]:
    """残疾人就业保障金测算工具实现。"""
    employees_raw = args.get("employees")
    if employees_raw in (None, ""):
        raise ValueError("缺少必填参数 employees（上年在职职工人数），请提供人数，例如 employees=31。")
    try:
        employees = int(float(str(employees_raw).replace(",", "")))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"employees「{employees_raw}」不是有效整数，请提供上年在职职工人数。") from exc
    average_wage = _opt_float(args, "average_wage", 0.0)
    disabled = _opt_float(args, "disabled_employed", 0.0)
    local_wage = _opt_float(args, "local_average_wage", 0.0)
    result = _disclaimer(
        engine.calc_disability_fund(
            employees=employees,
            average_wage=average_wage,
            disabled_employed=disabled,
            local_average_wage=local_wage or None,
        )
    )
    _safe_calc_log(
        "disability_fund",
        str(args.get("period") or engine.current_month()[:4]),
        {"employees": employees, "average_wage": average_wage, "disabled": disabled},
        result,
    )
    lines = [
        f"【残疾人就业保障金测算 · {result.get('year') if result.get('year') else ''}】".replace(" · 】", "】"),
        f"- 上年在职职工人数：{result.get('employees')} 人；实际安排残疾人：{round(float(result.get('disabled_employed') or 0), 2):g} 人；"
        f"安排比例：{round(float(result.get('actual_ratio') or 0) * 100, 2):.2f}%（法定 1.5%）",
        f"- 差额人数：{round(float(result.get('shortfall') or 0), 3):g} 人；年平均工资：¥{yuan(result.get('average_wage'))}",
        f"- 应缴基数：¥{yuan(result.get('base_amount'))}；分档减缴比例：{round(float(result.get('discount_rate') or 0) * 100):.0f}%",
        f"- **本期应缴残保金：¥{yuan(result.get('payable'))}**",
    ]
    if result.get("exempt"):
        lines.append("- 说明：在职职工 30 人（含）以下，按现行政策免征。")
    lines.append(f"- 说明：{result.get('note', '')}")
    for warning in result.get("warnings") or []:
        lines.append(f"⚠️ {warning}")
    lines.append("- 政策依据：")
    for source in result.get("policy_sources") or []:
        lines.append(f"  · {source.get('title')}（{source.get('source')}，{source.get('effective_date')} 起）")
    lines.append("- 提示：申报期限与残联审核流程以当地残联/税务公告为准。")
    lines.append(DISCLAIMER)
    return _ok(result, "\n".join(lines))


def _tool_calc_iit(args: dict[str, Any]) -> dict[str, Any]:
    """工资薪金个人所得税（累计预扣法）工具实现。"""
    income = _need_float(args, "cumulative_income", "本年累计工资薪金收入")
    months_raw = args.get("months_declared")
    if months_raw in (None, ""):
        raise ValueError(
            "缺少必填参数 months_declared（本年已实际申报月份数，0—12）。"
            "2026 年起减除费用按实际申报月份累计，请提供已申报月份数，例如 months_declared=11；"
            "零工资月份也建议零申报以保留额度。"
        )
    try:
        months = int(float(str(months_raw).replace(",", "")))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"months_declared「{months_raw}」不是有效整数，请提供 0—12 之间的已申报月份数。"
        ) from exc
    period = _period(args, default=engine.current_month())
    result = _disclaimer(
        engine.calc_iit_withholding(
            income,
            months_declared=months,
            cumulative_social_insurance=_opt_float(args, "cumulative_social_insurance", 0.0),
            cumulative_housing_fund=_opt_float(args, "cumulative_housing_fund", 0.0),
            cumulative_special_additional=_opt_float(args, "cumulative_special_additional", 0.0),
            cumulative_other_deduction=_opt_float(args, "cumulative_other_deduction", 0.0),
            cumulative_tax_withheld=_opt_float(args, "cumulative_tax_withheld", 0.0),
            one_time_basic_deduction=_to_bool(args.get("one_time_basic_deduction"), False),
            period=period,
        )
    )
    _safe_calc_log(
        "iit_withholding",
        period,
        {
            "cumulative_income": income,
            "months_declared": months,
            "cumulative_tax_withheld": result.get("cumulative_tax_withheld"),
        },
        result,
    )

    if result.get("one_time_basic_deduction"):
        basic_note = "一次性扣除 60,000 元"
    else:
        basic_note = f"5,000 元/月 × 已申报 {result.get('months_declared')} 个月"
    lines = [
        f"【个人所得税（工资薪金累计预扣）· {_period_label(period)}】",
        f"- 本年累计收入：¥{yuan(result.get('cumulative_income'))}",
        f"- 累计减除费用：¥{yuan(result.get('cumulative_basic_deduction'))}（{basic_note}）",
        f"- 累计专项扣除（社保+公积金）：¥{yuan((result.get('cumulative_social_insurance') or 0) + (result.get('cumulative_housing_fund') or 0))}；"
        f"累计专项附加扣除：¥{yuan(result.get('cumulative_special_additional'))}；"
        f"累计其他扣除：¥{yuan(result.get('cumulative_other_deduction'))}",
        f"- 累计应纳税所得额：¥{yuan(result.get('cumulative_taxable_income'))}",
        f"- 适用预扣率：{pct(result.get('applicable_rate'))}，"
        f"速算扣除数：¥{yuan(result.get('quick_deduction'))}；"
        f"累计应纳税额：¥{yuan(result.get('cumulative_tax'))}",
        f"- 累计已预扣：¥{yuan(result.get('cumulative_tax_withheld'))}；"
        f"**本期应预扣预缴：¥{yuan(result.get('tax_payable'))}**",
        f"- 说明：{result.get('note', '')}",
        "- 政策依据：",
    ]
    for source in result.get("policy_sources") or []:
        lines.append(
            f"  · {source.get('title')}（{source.get('source')}，{source.get('effective_date')} 起）"
        )
    for warning in result.get("warnings") or []:
        lines.append(f"⚠️ {warning}")
    if result.get("zero_declaration_note"):
        lines.append(f"⚠️ {result['zero_declaration_note']}")
    lines.append(DISCLAIMER)
    return _ok(result, "\n".join(lines))


# =============================================================================
# 工具实现：离职与申报纠错
# =============================================================================
def _tool_payroll_correction(args: dict[str, Any]) -> dict[str, Any]:
    """扫描离职后仍申报的工资，或测算单个员工的更正方案。"""
    from ledger import corrections

    employee = str(args.get("employee") or "").strip()
    scan = corrections.scan_departed_overpayments()
    items = scan.get("items") or []
    if not items:
        leave_month = str(args.get("leave_month") or "").strip()
        if employee and leave_month:
            from ledger import employees as employee_api
            all_people = employee_api.list_employees(status=None, include_left=True)
            candidate = next(
                (p for p in all_people if employee == str(p.get("id")) or employee in str(p.get("name") or "")),
                None,
            )
            if candidate:
                plan = corrections.build_correction_plan(str(candidate["id"]), leave_month=leave_month)
                totals = plan.get("totals") or {}
                lines = [
                    f"【手动更正方案 · {plan.get('employee_name')}】",
                    f"- 手动离职月份：{plan.get('leave_month')}（员工档案未登记离职日期）",
                    f"- 需更正月份：{'、'.join(plan.get('removed_periods') or []) or '无'}",
                    f"- 预计多缴（可退）{yuan(totals.get('tax_refund'))} 元；"
                    f"少缴（需补）{yuan(totals.get('tax_payable'))} 元",
                    "- 完整九步操作与逐月明细请在「员工与工资 → 离职与纠错」手动生成并导出 PDF 操作单。",
                    DISCLAIMER,
                ]
                return _ok({**scan, "plan": plan}, "\n".join(lines))
        data = {**scan, "plan": None}
        display = (
            "【离职与申报纠错扫描】\n"
            "- 未发现「已离职但离职后仍有工资/个税申报」的记录。\n"
            "- 如果员工实际已离职但系统里没登记离职日期/仍是「在职」，请告诉我员工姓名和离职月份"
            "（例如：张三 2026-02 离职），我按手动模式生成更正测算。\n"
            "- 如果员工在个税 APP 申诉，请先核实离职日期与银行流水；确认无误后在扣缴端异议处理中说明。\n"
            + DISCLAIMER
        )
        return _ok(data, display)

    if employee:
        target = next(
            (item for item in items if employee == item.get("employee_id") or employee in str(item.get("employee_name") or "")),
            None,
        )
        if not target:
            if args.get("leave_month"):
                from ledger import employees as employee_api
                all_people = employee_api.list_employees(status=None, include_left=True)
                candidate = next(
                    (p for p in all_people if employee == str(p.get("id")) or employee in str(p.get("name") or "")),
                    None,
                )
                if candidate:
                    plan = corrections.build_correction_plan(
                        str(candidate["id"]), leave_month=str(args.get("leave_month"))
                    )
                    totals = plan.get("totals") or {}
                    lines = [
                        f"【手动更正方案 · {plan.get('employee_name')}】",
                        f"- 手动离职月份：{plan.get('leave_month')}（员工档案未登记离职日期）",
                        f"- 需更正月份：{'、'.join(plan.get('removed_periods') or []) or '无'}",
                        f"- 预计多缴（可退）{yuan(totals.get('tax_refund'))} 元；"
                        f"少缴（需补）{yuan(totals.get('tax_payable'))} 元",
                        "- 完整九步操作与逐月明细请在「员工与工资 → 离职与纠错」手动生成并导出 PDF 操作单。",
                        DISCLAIMER,
                    ]
                    return _ok({**scan, "plan": plan}, "\n".join(lines))
            names = "、".join(item.get("employee_name", "") for item in items)
            raise ValueError(f"未在异常名单中找到「{employee}」。当前异常员工：{names}。")
        plan = corrections.build_correction_plan(
            str(target["employee_id"]), leave_month=str(args.get("leave_month") or "") or None
        )
        totals = plan.get("totals") or {}
        lines = [
            f"【离职多申报更正方案 · {plan.get('employee_name')}】",
            f"- 离职日期：{plan.get('leave_date')}；需更正月份：{'、'.join(plan.get('removed_periods') or []) or '无'}",
            f"- 原申报收入 {yuan(totals.get('original_income'))} → 更正后 {yuan(totals.get('corrected_income'))} 元",
            f"- 原已预扣个税 {yuan(totals.get('original_tax'))} → 更正后应预扣 {yuan(totals.get('corrected_tax'))} 元",
            f"- 预计多缴（可退）：{yuan(totals.get('tax_refund'))} 元；预计少缴（需补）：{yuan(totals.get('tax_payable'))} 元",
            f"- 企业所得税影响：小微 5% 约 {yuan((plan.get('cit_impact') or {}).get('small_low_profit_tax'))} 元；"
            f"一般 25% 约 {yuan((plan.get('cit_impact') or {}).get('standard_tax'))} 元",
            "",
            "操作步骤（照着做）：",
        ]
        for step in (plan.get("steps") or [])[:6]:
            lines.append(f"  {step}")
        lines.append("完整九步操作、逐月明细与留痕清单，可在「员工与工资 → 离职与纠错」导出 Excel/PDF 操作单。")
        lines.append(DISCLAIMER)
        return _ok({**scan, "plan": plan}, "\n".join(lines))

    summary = scan.get("summary") or {}
    lines = [
        "【离职与申报纠错扫描】",
        f"- 涉及员工 {summary.get('employees', 0)} 人，需更正月份 {summary.get('months', 0)} 个，"
        f"涉及工资 {yuan(summary.get('income'))} 元，已预扣个税 {yuan(summary.get('tax'))} 元。",
    ]
    for item in items[:8]:
        lines.append(
            f"- {item.get('employee_name')}（离职 {item.get('leave_date')}）："
            f"{'、'.join(item.get('periods') or [])}，工资 {yuan(item.get('income_total'))} 元"
        )
    lines.extend(
        [
            "",
            "下一步（零基础 3 步）：",
            "1. 先停申报：扣缴端人员信息采集把员工状态改为「离职」并填离职日期；",
            "2. 告诉我员工姓名，我生成逐月更正测算与《更正操作单》；",
            "3. 照操作单在扣缴端逐月更正，再处理退税/补税、企业所得税调增与账务调整。",
            "注意：员工撤销个税 APP 申诉不等于数据已更正，错误申报仍必须更正。",
            DISCLAIMER,
        ]
    )
    return _ok({**scan, "plan": None}, "\n".join(lines))


# =============================================================================
# 工具实现：记账与台账
# =============================================================================
def _tool_add_income(args: dict[str, Any]) -> dict[str, Any]:
    inclusive = _need_float(args, "inclusive_amount", "含税金额")
    if inclusive <= 0:
        raise ValueError("含税金额应大于 0，请提供正确的收款金额。")
    date = str(args.get("date") or config.today().isoformat()).strip()
    client = str(args.get("client") or "").strip()
    description = str(args.get("description") or "收入").strip()
    invoice_type = _normalize_choice(args.get("invoice_type"), INVOICE_TYPES, "普票", "发票类型")
    category = _normalize_choice(args.get("category"), INCOME_CATEGORIES, "技术服务", "业务类别")

    try:
        from ledger.income_ledger import add_income
    except Exception as exc:  # noqa: BLE001
        return _err(f"台账模块尚未就绪（ledger.income_ledger）：{exc}。请稍后重试或联系管理员。")
    try:
        record = add_income(
            date=date,
            client=client,
            inclusive_amount=inclusive,
            invoice_type=invoice_type,
            category=category,
            description=description,
            is_rd_related=_to_bool(args.get("is_rd_related"), False),
        )
    except ValueError as exc:
        return _err(f"记收入失败：{exc}")
    except Exception as exc:  # noqa: BLE001
        return _err(f"记收入失败：{exc}")

    journal = None
    journal_error = ""
    try:
        from ledger import journal

        entry = journal.build_income_entry(record)
        journal = journal.post_entry(entry)
    except Exception as exc:  # noqa: BLE001 - 分录生成失败不影响落账
        journal_error = str(exc)
    data = dict(record)
    data["journal"] = journal
    if journal_error:
        data["journal_error"] = journal_error
    _disclaimer(data)

    lines = [
        "✅ 已记入收入台账",
        f"- 日期：{record.get('date')}；客户：{record.get('client') or '未填写'}；"
        f"类别：{record.get('category')}；发票类型：{record.get('invoice_type')}",
        f"- 含税金额：¥{yuan(record.get('inclusive_amount'))}；"
        f"不含税：¥{yuan(record.get('exclusive_amount'))}；增值税：¥{yuan(record.get('vat_amount'))}",
        f"- 台账编号：{record.get('id')}",
        "- 分录（自动生成）：",
        f"  借：银行存款 ¥{yuan(record.get('inclusive_amount'))}",
        f"  贷：主营业务收入 ¥{yuan(record.get('exclusive_amount'))}",
        f"  贷：应交税费—应交增值税 ¥{yuan(record.get('vat_amount'))}",
    ]
    if journal:
        lines.append(f"- 凭证号：{journal.get('entry_no') or journal.get('id') or '已生成'}")
    elif journal_error:
        lines.append(f"⚠️ 分录未生成：{journal_error}")
    lines.append(DISCLAIMER)
    return _ok(data, "\n".join(lines))


def _tool_add_expense(args: dict[str, Any]) -> dict[str, Any]:
    amount = _need_float(args, "amount", "费用金额")
    if amount <= 0:
        raise ValueError("费用金额应大于 0，请提供正确的支出金额。")
    description = str(args.get("description") or "费用").strip()
    category = _normalize_choice(args.get("category"), EXPENSE_CATEGORIES, "办公", "费用类别")
    date = str(args.get("date") or config.today().isoformat()).strip()
    try:
        from ledger.expense_ledger import add_expense
    except Exception as exc:  # noqa: BLE001
        return _err(f"台账模块尚未就绪（ledger.expense_ledger）：{exc}。请稍后重试或联系管理员。")
    try:
        record = add_expense(
            date=date,
            description=description,
            amount=amount,
            category=category,
            is_rd_related=_to_bool(args.get("is_rd_related"), False),
            vendor=str(args.get("vendor") or "").strip(),
            receipt_no=str(args.get("receipt_no") or "").strip(),
        )
    except ValueError as exc:
        return _err(f"记费用失败：{exc}")
    except Exception as exc:  # noqa: BLE001
        return _err(f"记费用失败：{exc}")

    journal = None
    journal_error = ""
    try:
        from ledger import journal

        entry = journal.build_expense_entry(record)
        journal = journal.post_entry(entry)
    except Exception as exc:  # noqa: BLE001
        journal_error = str(exc)
    data = dict(record)
    data["journal"] = journal
    if journal_error:
        data["journal_error"] = journal_error
    _disclaimer(data)

    lines = [
        "✅ 已记入费用台账",
        f"- 日期：{record.get('date')}；类别：{record.get('category')}；"
        f"研发相关：{'是' if record.get('is_rd_related') else '否'}",
        f"- 费用描述：{record.get('description')}",
        f"- 金额：¥{yuan(record.get('amount'))}（小规模纳税人进项税不可抵扣，全额计入成本费用）",
        f"- 台账编号：{record.get('id')}",
    ]
    if journal:
        lines.append(f"- 已生成凭证：{journal.get('entry_no') or journal.get('id') or '已生成'}")
    elif journal_error:
        lines.append(f"⚠️ 分录未生成：{journal_error}")
    lines.append(DISCLAIMER)
    return _ok(data, "\n".join(lines))


def _ledger_income_summary(period: str) -> dict[str, Any]:
    try:
        from ledger.income_ledger import period_totals

        return period_totals(period)
    except Exception:  # noqa: BLE001 - 降级为直接读 store
        from ledger import store

        start, end = engine.period_range(period)
        rows = store.table("income").filter(start=start.isoformat(), end=end.isoformat())
        by_type: dict[str, dict[str, Any]] = {}
        total_inclusive = 0.0
        total_exclusive = 0.0
        special_inclusive = 0.0
        for row in rows:
            invoice_type = str(row.get("invoice_type") or "普票")
            bucket = by_type.setdefault(invoice_type, {"count": 0, "inclusive": 0.0, "exclusive": 0.0})
            bucket["count"] += 1
            bucket["inclusive"] = round2(bucket["inclusive"] + row.get("inclusive_amount", 0))
            bucket["exclusive"] = round2(bucket["exclusive"] + row.get("exclusive_amount", 0))
            total_inclusive = round2(total_inclusive + row.get("inclusive_amount", 0))
            total_exclusive = round2(total_exclusive + row.get("exclusive_amount", 0))
            if invoice_type == "专票":
                special_inclusive = round2(special_inclusive + row.get("inclusive_amount", 0))
        return {
            "period": period,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "count": len(rows),
            "total_inclusive": total_inclusive,
            "special_inclusive": special_inclusive,
            "ordinary_inclusive": round2(total_inclusive - special_inclusive),
            "total_exclusive": total_exclusive,
            "special_exclusive": round2(special_inclusive / 1.01),
            "ordinary_exclusive": round2(total_exclusive - special_inclusive / 1.01),
            "by_invoice_type": by_type,
        }


def _ledger_expense_summary(period: str) -> dict[str, Any]:
    try:
        from ledger.expense_ledger import period_totals

        return period_totals(period)
    except Exception:  # noqa: BLE001
        from ledger import store

        start, end = engine.period_range(period)
        rows = store.table("expense").filter(start=start.isoformat(), end=end.isoformat())
        by_category: dict[str, dict[str, Any]] = {}
        total = 0.0
        rd_total = 0.0
        for row in rows:
            category = str(row.get("category") or "其他")
            bucket = by_category.setdefault(category, {"amount": 0.0, "count": 0})
            bucket["count"] += 1
            bucket["amount"] = round2(bucket["amount"] + row.get("amount", 0))
            total = round2(total + row.get("amount", 0))
            if row.get("is_rd_related"):
                rd_total = round2(rd_total + row.get("amount", 0))
        return {
            "period": period,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "count": len(rows),
            "total": total,
            "rd_total": rd_total,
            "by_category": by_category,
        }


def _ledger_rd_summary(period: str) -> dict[str, Any]:
    try:
        from ledger.rd_auxiliary import summarize_rd

        return summarize_rd(period)
    except Exception:  # noqa: BLE001
        from ledger import store

        rows = store.table("rd").filter()
        rows = [row for row in rows if str(row.get("period") or "") == period]
        total = round2(sum(row.get("total", 0) for row in rows))
        deduction = round2(sum(row.get("deduction_amount", 0) for row in rows))
        return {
            "period": period,
            "record_count": len(rows),
            "total_collectible": total,
            "deduction_amount": deduction,
            "note": f"研发辅助账手工记录 {len(rows)} 条，合计 {yuan(total)} 元。",
        }


def _tool_query_ledger(args: dict[str, Any]) -> dict[str, Any]:
    kind = str(args.get("kind") or "income").strip().lower()
    mapping = {
        "income": ("income", "收入台账"),
        "收入": ("income", "收入台账"),
        "expense": ("expense", "费用台账"),
        "费用": ("expense", "费用台账"),
        "rd": ("rd", "研发辅助账"),
        "研发": ("rd", "研发辅助账"),
        "contracts": ("contracts", "合同台账"),
        "合同": ("contracts", "合同台账"),
    }
    if kind not in mapping:
        raise ValueError("kind 取值应为 income / expense / rd / contracts 之一。")
    normalized, label = mapping[kind]
    period = _period(args)
    data: dict[str, Any] = {"kind": normalized, "period": period, "label": label}
    lines = [f"【{label} · {_period_label(period)}】"]

    if normalized == "income":
        summary = _ledger_income_summary(period)
        data["summary"] = summary
        lines.append(
            f"- 收入笔数：{summary.get('count', 0)}；含税合计 ¥{yuan(summary.get('total_inclusive'))}；"
            f"不含税 ¥{yuan(summary.get('total_exclusive'))}"
        )
        by_type = summary.get("by_invoice_type") or {}
        for invoice_type, bucket in by_type.items():
            if isinstance(bucket, dict):
                lines.append(
                    f"  · {invoice_type}：{bucket.get('count', 0)} 笔，"
                    f"含税 ¥{yuan(bucket.get('inclusive'))}"
                )
    elif normalized == "expense":
        summary = _ledger_expense_summary(period)
        data["summary"] = summary
        lines.append(
            f"- 费用笔数：{summary.get('count', 0)}；合计 ¥{yuan(summary.get('total'))}；"
            f"其中研发相关 ¥{yuan(summary.get('rd_total'))}"
        )
        for category, bucket in (summary.get("by_category") or {}).items():
            if isinstance(bucket, dict):
                lines.append(f"  · {category}：¥{yuan(bucket.get('amount'))}（{bucket.get('count', 0)} 笔）")
    elif normalized == "rd":
        summary = _ledger_rd_summary(period)
        data["summary"] = summary
        lines.append(
            f"- 可加计扣除研发费用：¥{yuan(summary.get('total_collectible'))}；"
            f"加计扣除额：¥{yuan(summary.get('deduction_amount'))}"
        )
        if summary.get("note"):
            lines.append(f"- 说明：{summary['note']}")
    else:
        from ledger import store

        start, end = engine.period_range(period)
        rows = store.table("contracts").filter(start=start.isoformat(), end=end.isoformat())
        total = round2(sum(row.get("amount", 0) for row in rows))
        data["summary"] = {"count": len(rows), "total_amount": total}
        lines.append(f"- 合同份数：{len(rows)}；计税金额合计 ¥{yuan(total)}")
        for row in rows[:5]:
            lines.append(
                f"  · {row.get('contract_type')} {row.get('contract_no') or ''} "
                f"¥{yuan(row.get('amount'))}（{row.get('counterparty') or '—'}）"
            )
    lines.append(DISCLAIMER)
    data["disclaimer"] = DISCLAIMER
    return _ok(data, "\n".join(lines))


# =============================================================================
# 工具实现：申报
# =============================================================================
def _form_label(form_id: str) -> str:
    return FORM_LABELS.get(form_id.upper(), form_id)


def _tool_build_filing(args: dict[str, Any]) -> dict[str, Any]:
    form_id = str(args.get("form_id") or "VAT_QUARTERLY").strip().upper()
    period = _period(args)
    try:
        from filing.registry import build_form
    except Exception as exc:  # noqa: BLE001
        return _err(
            f"申报模块尚未就绪（filing.registry）：{exc}。"
            "申报表生成需要 filing 模块，请稍后重试；算税与台账功能不受影响。"
        )
    try:
        bundle = build_form(form_id, period)
    except KeyError as exc:
        return _err(f"未知申报表编码「{form_id}」，可选：{', '.join(FORM_LABELS)}。{exc}")
    except ValueError as exc:
        return _err(f"生成申报表失败：{exc}")
    except Exception as exc:  # noqa: BLE001
        return _err(f"生成申报表失败：{exc}")
    _disclaimer(bundle)
    totals = bundle.get("totals") or {}
    summary = bundle.get("summary") or {}
    lines = [
        f"【{bundle.get('form_name') or _form_label(form_id)} · {bundle.get('period_label') or period}】",
        f"- 状态：{bundle.get('status') or '草稿'}；校验：{summary.get('level') or 'PASS'} {summary.get('text') or ''}",
    ]
    for key, label in (("vat_payable", "应缴增值税"), ("surtax", "附加税费"), ("total_tax", "合计税费"), ("cit_payable", "应缴企业所得税"), ("stamp_payable", "应缴印花税")):
        if key in totals:
            lines.append(f"- {label}：¥{yuan(totals.get(key))}")
    for check in (bundle.get("checks") or [])[:6]:
        if check.get("status") not in ("PASS", None):
            lines.append(f"  ⚠️ {check.get('field')}：{check.get('message')}")
    lines.append("⚠️ 生成结果为草稿，请在电子税务局核对后提交（本系统不代替提交申报）。")
    lines.append(DISCLAIMER)
    return _ok(bundle, "\n".join(lines))


def _tool_check_filing(args: dict[str, Any]) -> dict[str, Any]:
    period = _period(args)
    fields = args.get("fields")
    text = args.get("text")
    result: dict[str, Any]
    try:
        from ocr import validator as ocr_validator
    except Exception as exc:  # noqa: BLE001
        return _err(f"申报核对模块尚未就绪（ocr.validator）：{exc}")
    if text:
        result = ocr_validator.check_from_text(str(text), period)
    elif fields:
        parsed = fields
        if isinstance(fields, str):
            try:
                from agent.llm_client import parse_json

                parsed = parse_json(fields)
            except Exception:  # noqa: BLE001
                parsed = {}
        if not isinstance(parsed, dict) or not parsed:
            return _err("fields 解析失败，请提供 JSON 对象，如 {\"line_10\": 101980.20}。")
        result = ocr_validator.check_vat_return(parsed, period)
    else:
        try:
            from filing.validator import validate_vat_return

            result = validate_vat_return(period)
        except Exception as exc:  # noqa: BLE001
            return _err(
                "申报核对需要提供申报表栏次数据（fields）或识别文本（text）；"
                f"直接校验草稿表失败：{exc}"
            )
    _disclaimer(result)
    details = result.get("details") or []
    lines = [
        f"【申报核对 · {_period_label(period)}】状态：{result.get('status', 'ERROR')}",
        f"- {result.get('summary') or ''}",
    ]
    for detail in details[:10]:
        lines.append(
            f"  · {detail.get('field')}：期望 {detail.get('expected')} / 实际 {detail.get('actual')}"
            f" → {detail.get('status')} {detail.get('message') or ''}"
        )
    lines.append(DISCLAIMER)
    return _ok(result, "\n".join(lines))


def _tool_get_filing_calendar(args: dict[str, Any]) -> dict[str, Any]:
    try:
        days = int(args.get("days") or 60)
    except (TypeError, ValueError):
        days = 60
    days = max(1, min(365, days))
    today = config.today()
    items = engine.upcoming_deadlines(today, lookahead_days=days)
    next_item = items[0] if items else None
    data = {
        "today": today.isoformat(),
        "days": days,
        "items": items,
        "next": next_item,
        "disclaimer": DISCLAIMER,
    }
    lines = [f"📅 未来 {days} 天申报事项（今天：{today.isoformat()}）"]
    if not items:
        lines.append("- 近期没有临近的申报事项，保持关注即可。")
    for item in items[:10]:
        lines.append(
            f"- {item['due_date']}（{item.get('label') or ''}）：{item['name']}；"
            f"税款所属期 {item.get('tax_period_label') or '—'}"
        )
    lines.append("⚠️ 请登录电子税务局完成申报，零申报也需按期提交。")
    lines.append(DISCLAIMER)
    return _ok(data, "\n".join(lines))


# =============================================================================
# 工具实现：政策 / 导出
# =============================================================================
def _tool_search_policy(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("请提供要检索的政策问题（query），例如「小规模纳税人免税额度」。")
    try:
        from knowledge.retriever import build_context, search
    except Exception as exc:  # noqa: BLE001
        return _err(f"政策知识库不可用：{exc}")
    try:
        top_k = int(args.get("top_k") or 4)
    except (TypeError, ValueError):
        top_k = 4
    matched = search(query, top_k=max(1, min(8, top_k)))
    if not matched:
        data = {"query": query, "matched": [], "context": "", "disclaimer": DISCLAIMER}
        return _ok(
            data,
            f"政策知识库中未检索到与「{query}」直接相关的条目。建议换用具体关键词"
            "（如「小规模 增值税 免税」「研发费用 加计扣除」），或咨询主管税务机关。\n" + DISCLAIMER,
        )
    context = build_context(query, top_k=min(4, len(matched)))
    data = {
        "query": query,
        "matched": [
            {
                "id": policy.get("id"),
                "title": policy.get("title"),
                "category": policy.get("category"),
                "score": policy.get("score"),
                "source": policy.get("source"),
                "effective_date": policy.get("effective_date"),
                "expiry_date": policy.get("expiry_date"),
                "last_verified": policy.get("last_verified"),
                "path": policy.get("path"),
            }
            for policy in matched
        ],
        "context": context,
        "disclaimer": DISCLAIMER,
    }
    lines = [f"【政策检索 · {query}】命中 {len(matched)} 条："]
    for policy in matched[:4]:
        lines.append(
            f"- {policy.get('title')}（来源：{policy.get('source')}；"
            f"生效日期：{policy.get('effective_date')}；执行至：{policy.get('expiry_date') or '长期'}）"
        )
        if policy.get("summary"):
            lines.append(f"  {policy['summary'][:160]}")
    lines.append("⚠️ 以上为知识库检索结果，具体执行以主管税务机关口径为准。")
    lines.append(DISCLAIMER)
    return _ok(data, "\n".join(lines))


def _tool_export_filing(args: dict[str, Any]) -> dict[str, Any]:
    period = _period(args)
    try:
        from filing.export import export_package_excel
    except Exception as exc:  # noqa: BLE001
        return _err(f"导出模块尚未就绪（filing.export）：{exc}。请稍后重试。")
    try:
        path = export_package_excel(period)
    except Exception as exc:  # noqa: BLE001
        return _err(f"导出申报包失败：{exc}")
    data = {"period": period, "path": str(path), "disclaimer": DISCLAIMER}
    return _ok(
        data,
        f"✅ 已导出 {_period_label(period)} 申报包：{path}\n"
        "该 Excel 含各申报表、填报指引与校验结果，仅供核对参考，正式申报请以电子税务局为准。\n" + DISCLAIMER,
    )


# =============================================================================
# 工具定义（OpenAI function calling schema）
# =============================================================================
def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


_MONEY = {"type": "number", "description": "金额（元）"}
_PERIOD = {"type": "string", "description": "所属期间，季度如 2026Q1，月份如 2026-11；缺省为当前季度"}

_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "calc_vat",
        "description": "计算小规模纳税人季度增值税及附加税费（确定性引擎，禁止模型自行计算）。",
        "parameters": _schema(
            {
                "quarterly_total_inclusive": {**_MONEY, "description": "季度含税总销售额（元）"},
                "special_invoice_inclusive": {**_MONEY, "description": "其中开具增值税专用发票的含税金额（元），默认 0"},
                "period": _PERIOD,
            },
            ["quarterly_total_inclusive"],
        ),
    },
    {
        "name": "calc_corp_tax",
        "description": "测算年度企业所得税（小型微利企业 5% 实际税负、研发加计扣除）。",
        "parameters": _schema(
            {
                "annual_profit": {**_MONEY, "description": "年度利润总额（元）；未提供时需同时提供收入与成本"},
                "annual_revenue_exclusive": {**_MONEY, "description": "年度不含税收入（元）"},
                "annual_cost": {**_MONEY, "description": "年度成本费用（元）"},
                "rd_cost": {**_MONEY, "description": "年度研发费用（元）"},
                "employees": {"type": "integer", "description": "从业人数"},
                "total_assets": {**_MONEY, "description": "资产总额（元）"},
                "rd_formed_intangible": {"type": "boolean", "description": "研发是否形成无形资产"},
                "period": _PERIOD,
            },
        ),
    },
    {
        "name": "calc_cit_prepay",
        "description": "计算企业所得税季度预缴额（本年累计口径）。",
        "parameters": _schema(
            {
                "quarter_profit": {**_MONEY, "description": "本季度利润总额（元）"},
                "ytd_profit": {**_MONEY, "description": "本年累计利润总额（元）"},
                "ytd_cit_paid": {**_MONEY, "description": "本年累计已预缴企业所得税（元）"},
                "rd_cost_ytd": {**_MONEY, "description": "本年累计研发费用（元）"},
                "period": _PERIOD,
            },
            ["quarter_profit", "ytd_profit"],
        ),
    },
    {
        "name": "calc_cit_settlement",
        "description": (
            "计算企业所得税年度汇算清缴（辅助测算）：业务招待费/广告费/三项经费限额类纳税调整、"
            "手工调增调减、研发费用加计扣除、以前年度亏损弥补、小型微利企业 5% 实际税负、"
            "应纳所得税额与应补（退）税额。"
        ),
        "parameters": _schema(
            {
                "year": {"type": "string", "description": "纳税年度，如 2026；缺省为当前年度"},
                "revenue_exclusive": {**_MONEY, "description": "年度不含税营业收入（元）"},
                "cost_total": {**_MONEY, "description": "年度成本费用合计（元）"},
                "accounting_profit": {**_MONEY, "description": "会计利润总额（元，可选，提供时优先于收入−成本）"},
                "salary_total": {**_MONEY, "description": "年度工资薪金总额（元，用于三项经费限额）"},
                "entertainment": {**_MONEY, "description": "业务招待费实际发生额（元）"},
                "advertising": {**_MONEY, "description": "广告费和业务宣传费实际发生额（元）"},
                "welfare": {**_MONEY, "description": "职工福利费实际发生额（元）"},
                "education": {**_MONEY, "description": "职工教育经费实际发生额（元）"},
                "union_actual": {**_MONEY, "description": "工会经费实际发生额（元）"},
                "rd_deduction": {**_MONEY, "description": "研发费用加计扣除额（元）"},
                "extra_adjustments": {
                    "type": "array",
                    "description": "其他纳税调整列表，如 [{\"item_type\":\"罚款支出\",\"direction\":\"调增\",\"adjustment_amount\":5000}]",
                    "items": {
                        "type": "object",
                        "properties": {
                            "item_type": {"type": "string", "description": "调整项目"},
                            "direction": {"type": "string", "enum": ["调增", "调减"]},
                            "adjustment_amount": {**_MONEY, "description": "调整金额（元）"},
                            "basis": {"type": "string", "description": "调整依据"},
                        },
                        "required": ["adjustment_amount"],
                    },
                },
                "losses": {
                    "type": "array",
                    "description": "可弥补以前年度亏损列表，如 [{\"year\":\"2024\",\"loss_amount\":100000,\"used_amount\":0}]",
                    "items": {
                        "type": "object",
                        "properties": {
                            "year": {"type": "string", "description": "亏损年度"},
                            "loss_amount": {**_MONEY, "description": "亏损金额（元）"},
                            "used_amount": {**_MONEY, "description": "已弥补金额（元）"},
                        },
                    },
                },
                "prepaid_tax": {**_MONEY, "description": "本年度已预缴企业所得税（元）"},
                "employees": {"type": "integer", "description": "从业人数（小型微利企业判定）"},
                "total_assets": {**_MONEY, "description": "资产总额（元，小型微利企业判定）"},
            },
            ["year"],
        ),
    },
    {
        "name": "calc_rd_deduction",
        "description": "归集研发费用并计算 100% 加计扣除（默认从研发辅助账取数）。",
        "parameters": _schema(
            {
                "period": _PERIOD,
                "formed_intangible": {"type": "boolean", "description": "是否形成无形资产（形成则按 200% 摊销）"},
                "personnel_cost": {**_MONEY, "description": "人员人工费用（可选，覆盖台账取数）"},
                "material_cost": {**_MONEY, "description": "直接投入费用（可选）"},
                "depreciation": {**_MONEY, "description": "折旧费用（可选）"},
                "amortization": {**_MONEY, "description": "无形资产摊销（可选）"},
                "design_cost": {**_MONEY, "description": "新产品设计费等（可选）"},
                "other_cost": {**_MONEY, "description": "其他相关费用（可选，受 10% 限额约束）"},
            },
        ),
    },
    {
        "name": "calc_stamp_tax",
        "description": "按合同台账计算印花税（六税两费减半后）。",
        "parameters": _schema({"period": _PERIOD}),
    },
    {
        "name": "calc_disability_fund",
        "description": (
            "测算残疾人就业保障金（残保金）：按 1.5% 法定比例、分档减缴（1%~1.5% 按 50%、低于 1% 按 90%）、"
            "30 人以下免征、年平均工资 2 倍封顶提示。"
        ),
        "parameters": _schema(
            {
                "employees": {"type": "integer", "description": "上年在职职工人数"},
                "average_wage": {**_MONEY, "description": "上年在职职工年平均工资（元）"},
                "disabled_employed": {**_MONEY, "description": "上年实际安排残疾人就业人数（可为小数，缺省 0）"},
                "local_average_wage": {**_MONEY, "description": "当地社会平均工资（元，可选，用于 2 倍封顶核对）"},
                "period": {"type": "string", "description": "所属年度，如 2026（缺省当前年度）"},
            },
            ["employees"],
        ),
    },
    {
        "name": "calc_iit",
        "description": (
            "计算工资薪金个人所得税本期应预扣预缴税额（居民个人累计预扣法）："
            "5,000 元/月基本减除费用、专项扣除（三险一金）、专项附加扣除、预扣率表。"
        ),
        "parameters": _schema(
            {
                "cumulative_income": {**_MONEY, "description": "本年累计工资薪金收入（元，含本期）"},
                "months_declared": {
                    "type": "integer",
                    "description": "本年已实际申报月份数（0—12）；2026 年起减除费用按实际申报月份累计，零工资月份也建议零申报",
                },
                "cumulative_social_insurance": {**_MONEY, "description": "累计专项扣除：社保（三险）个人部分（元）"},
                "cumulative_housing_fund": {**_MONEY, "description": "累计专项扣除：住房公积金个人部分（元）"},
                "cumulative_special_additional": {**_MONEY, "description": "累计专项附加扣除（元）"},
                "cumulative_other_deduction": {**_MONEY, "description": "累计其他扣除（企业年金、商业健康险、税延养老等，元）"},
                "cumulative_tax_withheld": {**_MONEY, "description": "本年累计已预扣预缴税额（元）"},
                "one_time_basic_deduction": {"type": "boolean", "description": "是否选择一次性扣除 60,000 元（仅符合条件的员工）"},
                "period": {"type": "string", "description": "税款所属月份，如 2026-11；缺省为当前月份"},
            },
            ["cumulative_income", "months_declared"],
        ),
    },
    {
        "name": "payroll_correction",
        "description": (
            "扫描公司「已离职但离职后月份仍在申报工资/个税」的记录，并生成更正测算："
            "按累计预扣法逐月重算、多缴（可退）/少缴（需补）个税、企业所得税调增、操作步骤。"
            "可传 employee（姓名或编号）查看单个员工。"
        ),
        "parameters": _schema(
            {
                "employee": {"type": "string", "description": "可选：员工姓名或编号；不传则扫描全公司"},
                "leave_month": {"type": "string", "description": "可选：手动离职月份 YYYY-MM（员工未登记离职日期时使用）"},
            },
            [],
        ),
    },
    {
        "name": "add_income",
        "description": "记一笔收入到收入台账，并自动生成会计分录。",
        "parameters": _schema(
            {
                "inclusive_amount": {**_MONEY, "description": "含税金额（元）"},
                "client": {"type": "string", "description": "客户名称（可脱敏）"},
                "invoice_type": {"type": "string", "enum": ["普票", "专票", "未开票"], "description": "发票类型"},
                "category": {"type": "string", "enum": ["技术服务", "软件开发", "运维服务", "其他"], "description": "业务类别"},
                "date": {"type": "string", "description": "收款日期 YYYY-MM-DD，缺省今天"},
                "description": {"type": "string", "description": "业务描述"},
            },
            ["inclusive_amount"],
        ),
    },
    {
        "name": "add_expense",
        "description": "记一笔费用到费用台账，并自动生成会计分录。",
        "parameters": _schema(
            {
                "amount": {**_MONEY, "description": "费用金额（元，含税）"},
                "description": {"type": "string", "description": "费用描述"},
                "category": {"type": "string", "enum": list(EXPENSE_CATEGORIES), "description": "费用类别"},
                "is_rd_related": {"type": "boolean", "description": "是否研发相关（可归集研发费用）"},
                "date": {"type": "string", "description": "发生日期 YYYY-MM-DD，缺省今天"},
                "vendor": {"type": "string", "description": "供应商"},
            },
            ["amount"],
        ),
    },
    {
        "name": "query_ledger",
        "description": "查询台账汇总（收入 / 费用 / 研发 / 合同）。",
        "parameters": _schema(
            {
                "kind": {"type": "string", "enum": ["income", "expense", "rd", "contracts"], "description": "台账类型"},
                "period": _PERIOD,
            },
            ["kind"],
        ),
    },
    {
        "name": "build_filing",
        "description": "生成指定期间申报表草稿（bundle 结构）。",
        "parameters": _schema(
            {
                "form_id": {"type": "string", "enum": list(FORM_LABELS), "description": "报表编码，默认 VAT_QUARTERLY"},
                "period": _PERIOD,
            },
        ),
    },
    {
        "name": "check_filing",
        "description": "核对申报表：对比 OCR/手工栏次值或校验草稿。",
        "parameters": _schema(
            {
                "period": _PERIOD,
                "fields": {"type": "object", "description": "申报表栏次值，如 {\"line_10\": 101980.20}"},
                "text": {"type": "string", "description": "申报表 OCR 识别文本或粘贴文本"},
            },
        ),
    },
    {
        "name": "get_filing_calendar",
        "description": "查询近期申报日历与截止日。",
        "parameters": _schema({"days": {"type": "integer", "description": "查询未来天数，默认 60"}}),
    },
    {
        "name": "search_policy",
        "description": "检索政策知识库，返回政策名称、文号、生效日期与摘要。",
        "parameters": _schema(
            {"query": {"type": "string", "description": "检索关键词或问题"}, "top_k": {"type": "integer", "description": "返回条数，默认 4"}},
            ["query"],
        ),
    },
    {
        "name": "export_filing",
        "description": "导出指定期间的申报包 Excel。",
        "parameters": _schema({"period": _PERIOD}),
    },
]


def tool_specs() -> list[dict[str, Any]]:
    """返回 OpenAI function calling 格式的工具定义列表。"""
    return [
        {
            "type": "function",
            "function": {
                "name": definition["name"],
                "description": definition["description"],
                "parameters": definition["parameters"],
            },
        }
        for definition in _TOOL_DEFS
    ]


def describe_tools() -> list[dict[str, Any]]:
    """供 UI 展示的工具清单：名称 / 用途 / 参数。"""
    return [
        {
            "name": definition["name"],
            "description": definition["description"],
            "parameters": definition["parameters"],
        }
        for definition in _TOOL_DEFS
    ]


# =============================================================================
# 分发
# =============================================================================
_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "calc_vat": _tool_calc_vat,
    "calc_corp_tax": _tool_calc_corp_tax,
    "calc_cit_prepay": _tool_calc_cit_prepay,
    "calc_cit_settlement": _tool_calc_cit_settlement,
    "calc_rd_deduction": _tool_calc_rd_deduction,
    "calc_stamp_tax": _tool_calc_stamp_tax,
    "calc_iit": _tool_calc_iit,
    "payroll_correction": _tool_payroll_correction,
    "calc_disability_fund": _tool_calc_disability_fund,
    "add_income": _tool_add_income,
    "add_expense": _tool_add_expense,
    "query_ledger": _tool_query_ledger,
    "build_filing": _tool_build_filing,
    "check_filing": _tool_check_filing,
    "get_filing_calendar": _tool_get_filing_calendar,
    "search_policy": _tool_search_policy,
    "export_filing": _tool_export_filing,
}


def call_tool(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """调用工具并统一返回 ``{"ok", "data", "display", "error"}``。

    参数
    ----
    name      : 工具名称（必须是 :data:`TOOL_NAMES` 之一）
    arguments : 工具参数字典（可为空）

    说明
    ----
    任何异常（参数错误、模块未就绪、数据非法）都转换为 ``ok=False`` 的中文错误，
    不向调用方抛出，避免打断 LLM 工具循环。
    """
    tool_name = str(name or "").strip()
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        return _err(f"未知工具「{name}」，可用工具：{', '.join(TOOL_NAMES)}。")
    args = arguments if isinstance(arguments, dict) else {}
    if not isinstance(arguments, dict) and arguments not in (None, ""):
        try:
            parsed = json.loads(str(arguments))
            args = parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            args = {}
    try:
        result = handler(args)
    except ValueError as exc:
        return _err(str(exc))
    except KeyError as exc:
        return _err(f"调用工具 {tool_name} 失败：缺少数据 {exc}。")
    except Exception as exc:  # noqa: BLE001 - 统一转为中文错误
        return _err(f"调用工具 {tool_name} 失败：{exc}。请稍后重试或联系管理员。")
    if not isinstance(result, dict) or "ok" not in result:
        return _err(f"工具 {tool_name} 返回格式异常。")
    if result.get("ok"):
        result["data"] = _disclaimer(result.get("data"))
    return result
