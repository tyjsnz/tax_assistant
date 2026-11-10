"""Agent 编排层：对话循环、工具调用、无 API Key 的本地确定性回复。

流程
----
1. :func:`run_turn`：有 API Key 时，用「税小智」System Prompt + 实时上下文 + 历史消息
   发起对话；模型返回 tool_calls 时调用 :func:`agent.tools.call_tool`，把结果回灌给模型，
   最多循环 ``max_rounds`` 轮，最终输出中文答复并附免责声明；
2. 无 API Key 或 DeepSeek 调用失败时，自动回退 :func:`local_reply`：
   基于 :mod:`agent.intent_router` + :mod:`engine` + :mod:`ledger` + :mod:`knowledge`
   生成确定性中文回复，``error`` 字段说明降级原因；
3. :func:`build_context` 汇总企业画像、现行政策参数、本季度台账摘要、最近申报截止日，
   作为 System Prompt 的实时上下文。

安全：算税数字全部来自工具（确定性引擎），模型只负责解释；输出统一附
``config.DISCLAIMER``。
"""

from __future__ import annotations

import json
from typing import Any

import config
import engine
from agent import intent_router, tools
from agent.llm_client import DeepSeekClient, DeepSeekError, get_client
from agent.prompt_templates import (
    DISCLAIMER,
    OFFLINE_HINT,
    build_system_prompt,
)
from engine.money import pct, round2, yuan

#: 工具结果回灌给模型时的最大字符数（避免上下文过长）
_MAX_TOOL_CONTENT = 6000


# =============================================================================
# 实时上下文
# =============================================================================
def _ledger_snapshot() -> tuple[list[str], dict[str, Any]]:
    """本季度台账摘要（优先 ledger.reports.dashboard，降级为直接读台账）。"""
    lines: list[str] = []
    data: dict[str, Any] = {}
    quarter = engine.current_quarter()
    try:
        from ledger.reports import quarterly_summary

        summary = quarterly_summary(quarter)
        data = summary
        lines.append(
            f"- 本季度（{engine.period_label(quarter)}）收入含税 ¥{yuan(summary.get('income_inclusive'))}，"
            f"不含税 ¥{yuan(summary.get('income_exclusive'))}，"
            f"费用 ¥{yuan(summary.get('expense'))}，利润约 ¥{yuan(summary.get('profit'))}"
        )
        lines.append(
            f"- 本季度增值税 ¥{yuan(summary.get('vat'))}，附加税费 ¥{yuan(summary.get('surtax'))}，"
            f"研发费用 ¥{yuan(summary.get('rd_cost'))}（加计扣除 ¥{yuan(summary.get('rd_deduction'))}）"
        )
        counts = {}
        try:
            from ledger.store import storage_stats

            counts = storage_stats()
        except Exception:  # noqa: BLE001
            counts = {}
        lines.append(
            f"- 台账笔数：收入 {counts.get('income', 0)} 笔、费用 {counts.get('expense', 0)} 笔、"
            f"研发 {counts.get('rd', 0)} 条、合同 {counts.get('contracts', 0)} 份"
        )
    except Exception:  # noqa: BLE001 - 台账报表未就绪时按收入/费用台账汇总
        try:
            from ledger.income_ledger import period_totals as income_totals

            income = income_totals(quarter)
            lines.append(
                f"- 本季度（{engine.period_label(quarter)}）收入 {income.get('count', 0)} 笔，"
                f"含税 ¥{yuan(income.get('total_inclusive'))}，不含税 ¥{yuan(income.get('total_exclusive'))}"
                f"（专票含税 ¥{yuan(income.get('special_inclusive'))}）"
            )
            data["income"] = income
        except Exception:  # noqa: BLE001
            try:
                from ledger import store

                start, end = engine.period_range(quarter)
                rows = store.table("income").filter(start=start.isoformat(), end=end.isoformat())
                total_inclusive = round2(sum(row.get("inclusive_amount", 0) for row in rows))
                total_exclusive = round2(sum(row.get("exclusive_amount", 0) for row in rows))
                data["income"] = {"count": len(rows), "total_inclusive": total_inclusive}
                lines.append(
                    f"- 本季度（{engine.period_label(quarter)}）收入 {len(rows)} 笔，"
                    f"含税 ¥{yuan(total_inclusive)}，不含税 ¥{yuan(total_exclusive)}"
                )
            except Exception:  # noqa: BLE001
                lines.append("- 本季度台账暂无可用数据（尚未记账或台账模块未就绪）")
        try:
            from ledger.expense_ledger import period_totals as expense_totals

            expense = expense_totals(quarter)
            data["expense"] = expense
            lines.append(
                f"- 本季度费用 {expense.get('count', 0)} 笔，合计 ¥{yuan(expense.get('total'))}"
                f"（研发相关 ¥{yuan(expense.get('rd_total'))}）"
            )
        except Exception:  # noqa: BLE001
            try:
                from ledger import store

                start, end = engine.period_range(quarter)
                rows = store.table("expense").filter(start=start.isoformat(), end=end.isoformat())
                total = round2(sum(row.get("amount", 0) for row in rows))
                data["expense"] = {"count": len(rows), "total": total}
                lines.append(f"- 本季度费用 {len(rows)} 笔，合计 ¥{yuan(total)}")
            except Exception:  # noqa: BLE001
                pass
    return lines, data


def build_context() -> str:
    """构建 System Prompt 的实时上下文：企业画像、政策参数、台账摘要、最近截止日。"""
    parts: list[str] = []
    try:
        company = config.get_company()
        parts.append(
            "【企业信息】"
            f"{company.get('name')}（{company.get('taxpayer_type')}，{company.get('industry')}）；"
            f"主管税务机关：{company.get('tax_authority')}；"
            f"今天：{config.today().isoformat()}。"
        )
    except Exception:  # noqa: BLE001
        pass

    try:
        quarter = engine.current_quarter()
        describe = engine.describe_policy(quarter)
        rate_lines = []
        for item in (describe.get("rates") or [])[:6]:
            rate_lines.append(f"{item.get('项目')}：{item.get('取值')}（{item.get('依据')}）")
        parts.append(
            "【现行政策参数】" + f"当前期间 {engine.period_label(quarter)}；" + "；".join(rate_lines)
            + f"；最后核验：{describe.get('last_verified') or '—'}。"
        )
    except Exception:  # noqa: BLE001
        pass

    try:
        ledger_lines, _ = _ledger_snapshot()
        if ledger_lines:
            parts.append("【本季度台账摘要】\n" + "\n".join(ledger_lines))
    except Exception:  # noqa: BLE001
        pass

    try:
        next_item = engine.next_deadline()
        if next_item:
            parts.append(
                "【最近申报截止日】"
                f"{next_item.get('due_date')}（{next_item.get('label') or ''}）：{next_item.get('name')}；"
                f"税款所属期 {next_item.get('tax_period_label') or '—'}。零申报也需按期提交。"
            )
    except Exception:  # noqa: BLE001
        pass

    parts.append(
        "【安全约束】算税数字必须来自工具调用；政策回答必须标注文号与生效日期；"
        "不编造政策、不建议逃税避税、不主动建议转账；输出末尾附免责声明。"
    )
    return "\n".join(parts)


# =============================================================================
# 本地确定性回复
# =============================================================================
def _ensure_disclaimer(text: str) -> str:
    """确保回复末尾带统一免责声明。"""
    content = (text or "").strip()
    if not content:
        content = "抱歉，我暂时无法回答这个问题。请换个说法，或告诉我具体的收入、费用、期间等信息。"
    if DISCLAIMER not in content:
        content = content.rstrip() + "\n\n" + DISCLAIMER
    return content


def _get_ledger_vat(period: str) -> dict[str, Any] | None:
    """从收入台账取数并计算增值税（用于用户未提供金额的算税提问）。"""
    if not period:
        return None
    try:
        from ledger.income_ledger import period_totals

        totals = period_totals(period)
        total = totals.get("total_inclusive") or 0
        special = totals.get("special_inclusive") or 0
        count = totals.get("count") or 0
        if total <= 0 and count == 0:
            return None
        result = engine.calc_vat(total, special, period)
        result["_from_ledger"] = True
        result["_ledger_count"] = count
        return result
    except Exception:  # noqa: BLE001
        pass
    try:
        from ledger import store

        start, end = engine.period_range(period)
        rows = store.table("income").filter(start=start.isoformat(), end=end.isoformat())
        if not rows:
            return None
        total = round2(sum(row.get("inclusive_amount", 0) for row in rows))
        special = round2(
            sum(row.get("inclusive_amount", 0) for row in rows if str(row.get("invoice_type")) == "专票")
        )
        if total <= 0:
            return None
        result = engine.calc_vat(total, special, period)
        result["_from_ledger"] = True
        result["_ledger_count"] = len(rows)
        return result
    except Exception:  # noqa: BLE001
        return None


def _vat_reply(data: dict[str, Any], *, from_ledger: bool = False) -> str:
    """用自然语言解释 calc_vat 结果（数字全部来自引擎）。"""
    period = data.get("period") or ""
    label = engine.period_label(period) if period else "本季度"
    within = bool(data.get("within_exemption_limit"))
    source_line = "（数据来自本季度收入台账）" if from_ledger else ""
    lines = [f"{label}增值税测算{source_line}：", ""]
    lines.append(
        f"- 含税销售额 ¥{yuan(data.get('total_inclusive'))}，换算不含税 ¥{yuan(data.get('total_exclusive'))}；"
    )
    lines.append(
        f"- 其中专票不含税 ¥{yuan(data.get('special_exclusive'))}，"
        f"普票+未开票不含税 ¥{yuan(data.get('ordinary_exclusive'))}；"
    )
    lines.append(
        f"- {'未超过' if within else '已超过'}季度免税额度 ¥{yuan(data.get('exemption_limit'))}"
        f"（不含税销售额 {'≤' if within else '>'} 30 万元）；"
    )
    lines.append(
        f"- **应缴增值税 ¥{yuan(data.get('vat_payable'))}**；免征增值税 ¥{yuan(data.get('vat_exempt'))}；"
        f"应缴附加税费 ¥{yuan(data.get('surtax'))}（六税两费减半后）；"
        f"合计应纳税费 **¥{yuan(data.get('total_tax'))}**。"
    )
    note = data.get("note")
    if note:
        lines.extend(["", f"说明：{note}"])
    for warning in (data.get("warnings") or [])[:3]:
        lines.append(f"⚠️ {warning}")
    lines.extend(
        [
            "",
            "📌 政策依据："
            + str(data.get("vat_policy_source") or "以主管税务机关公布的最新政策为准")
            + "；六税两费减半按财政部 税务总局公告2023年第12号（2023-01-01 至 2027-12-31）。",
        ]
    )
    return "\n".join(lines)


def _income_amount_hint() -> str:
    return (
        "请告诉我具体金额，例如：「今天收到云图科技 5000 元技术服务费，开了普票」。"
        "金额支持「万 / 元 / 块」等说法，我会自动换算并记入收入台账、生成分录。"
    )


def _iit_reply(data: dict[str, Any]) -> str:
    """用自然语言解释工资薪金个税累计预扣结果（数字全部来自 engine）。"""
    period = data.get("period") or ""
    label = engine.period_label(period) if period else "本期"
    if data.get("one_time_basic_deduction"):
        basic_note = "按 60,000 元一次性扣除"
    else:
        basic_note = f"5,000 元/月 × 已实际申报 {data.get('months_declared')} 个月"
    social_total = (data.get("cumulative_social_insurance") or 0) + (data.get("cumulative_housing_fund") or 0)
    lines = [
        f"{label}工资薪金个人所得税（累计预扣法）：",
        "",
        f"- 本年累计收入 ¥{yuan(data.get('cumulative_income'))}；",
        f"- 累计减除费用 ¥{yuan(data.get('cumulative_basic_deduction'))}（{basic_note}）；",
        f"- 累计专项扣除（三险一金）¥{yuan(social_total)}，"
        f"累计专项附加扣除 ¥{yuan(data.get('cumulative_special_additional'))}，"
        f"累计其他扣除 ¥{yuan(data.get('cumulative_other_deduction'))}；",
        f"- 累计应纳税所得额 ¥{yuan(data.get('cumulative_taxable_income'))}，"
        f"适用预扣率 {pct(data.get('applicable_rate'))}、速算扣除数 ¥{yuan(data.get('quick_deduction'))}；",
        f"- 累计应纳税额 ¥{yuan(data.get('cumulative_tax'))}，"
        f"累计已预扣 ¥{yuan(data.get('cumulative_tax_withheld'))}；",
        f"- **本期应预扣预缴 ¥{yuan(data.get('tax_payable'))}**。",
    ]
    if data.get("note"):
        lines.extend(["", f"说明：{data['note']}"])
    sources = data.get("policy_sources") or []
    if sources:
        lines.extend(["", "📌 政策依据："])
        for source in sources[:4]:
            lines.append(
                f"- {source.get('source')}（{source.get('effective_date')} 起）：{source.get('title')}"
            )
    for warning in (data.get("warnings") or [])[:3]:
        lines.append(f"⚠️ {warning}")
    if data.get("zero_declaration_note"):
        lines.extend(["", f"⚠️ {data['zero_declaration_note']}"])
    return "\n".join(lines)


def _compose_local(intent: str, text: str, route: dict[str, Any], result: dict[str, Any] | None) -> str:
    """根据意图与工具结果组织本地中文回复。"""
    arguments = route.get("arguments") or {}

    if intent == "calc_vat":
        if result and result.get("ok"):
            return _vat_reply(result["data"])
        period = arguments.get("period") or engine.current_quarter()
        ledger_result = _get_ledger_vat(period)
        if ledger_result:
            return _vat_reply(ledger_result, from_ledger=True)
        if result and result.get("error"):
            if "缺少必填参数" in result["error"]:
                return (
                    "我可以直接用确定性算税引擎帮你算，请补充本季度含税销售额，"
                    "例如：「这个季度开了 8 万普票，增值税怎么算」。\n"
                    "如果你已经把收入记入台账，也可以说「这个季度增值税要交多少」，我会自动从台账取数。"
                )
            return f"增值税测算暂时失败：{result['error']}"
        return (
            f"台账中 {engine.period_label(period)} 暂无收入记录（可零申报）；"
            "如已发生收入，请先记账或直接告诉我季度含税销售额。"
        )

    if intent in ("calc_corp_tax", "calc_cit_prepay", "calc_rd", "calc_stamp_tax"):
        if result and result.get("ok"):
            return result["display"]
        if result and result.get("error"):
            return f"计算失败：{result['error']}"
        return "请提供计算的期间或金额信息，我会调用确定性算税引擎计算。"

    if intent == "iit":
        if result and result.get("ok"):
            return _iit_reply(result["data"])
        if result and result.get("error"):
            if "缺少必填参数" in result["error"]:
                return (
                    "个税（工资薪金累计预扣法）需要两个关键数据：\n"
                    "1. **本年累计工资薪金收入**（元），例如「月薪 1.5 万」；\n"
                    "2. **本年已实际申报月份数**（0—12），例如「今年已申报 11 个月」。\n"
                    "也可以把五险一金、专项附加扣除、已预扣税额一起告诉我，例如：\n"
                    "「月薪 1.5 万，五险一金每月 3000，专项附加扣除每月 2000，今年已申报 11 个月，个税怎么算」。\n\n"
                    "⚠️ 2026 年扣缴端升级后，5,000 元/月减除费用按**实际申报月份**累计："
                    "未申报的月份额度作废、不再补发；即使当月未发工资，也建议做零申报（收入填 0）以保留额度。"
                )
            return f"个税计算失败：{result['error']}"
        return (
            "请告诉我本年累计工资收入与已实际申报月份数，我会用累计预扣法计算本期应预扣个税；"
            "例如：「月薪 1.5 万，今年已申报 11 个月，个税怎么算」。"
        )

    if intent in ("income", "expense"):
        if result and result.get("ok"):
            return result["display"]
        if result and result.get("error"):
            if "缺少必填参数" in result["error"]:
                return ("请告诉我具体金额和用途，例如：" if intent == "income" else "请告诉我费用金额和用途，例如：") + (
                    "「今天收到云图科技 5000 元技术服务费，开了普票」"
                    if intent == "income"
                    else "「今天买了办公用品花了 500 元」"
                )
            return f"记账失败：{result['error']}"
        return _income_amount_hint()

    if intent == "policy":
        query = str(arguments.get("query") or text).strip()
        try:
            from knowledge.retriever import answer as policy_answer

            answer = policy_answer(query)
            reply = answer.get("answer") or ""
            if reply:
                return reply
        except Exception:  # noqa: BLE001 - 知识库异常时退回工具检索结果
            pass
        if result and result.get("ok"):
            return result["display"]
        if result and result.get("error"):
            return f"政策检索失败：{result['error']}"
        return "政策知识库暂时不可用，请稍后重试或咨询主管税务机关。"

    if intent == "filing_check":
        if result and result.get("ok"):
            return result["display"]
        if result and result.get("error"):
            return (
                "申报核对需要申报表截图（本地 OCR 识别）或栏次文本。当前暂时无法完成自动核对："
                f"{result['error']}\n"
                "你也可以把申报表栏次粘贴成「第10栏 101980.20」这样的文本发给我。"
            )
        return "请上传申报表截图，或粘贴「栏次 金额」文本，我来逐栏核对。"

    if intent == "calendar":
        if result and result.get("ok"):
            display = result["display"]
            if "近期没有" not in display:
                return display
        try:
            return engine.reminder_text()
        except Exception:  # noqa: BLE001
            return "近期申报提醒：每季度终了后 15 日内申报增值税及附加、企业所得税预缴；"
            "个税按月于次月 15 日内申报；年度汇算清缴于次年 5 月 31 日前完成。零申报也需按期提交。"

    if intent == "red_bill":
        amounts = (route.get("arguments") or {}).get("amounts") or []
        lines = [
            "“红利账单”反映的是你享受的税费减免合计，通常由以下几部分构成：",
            "",
            "1. **增值税免税**：季度不含税销售额不超过 30 万元的部分免征增值税"
            "（按 1% 口径约等于免税销售额 × 1%）；",
            "2. **3% 减按 1%**：开具专票或超过免税标准的部分，按 1% 而非 3% 缴纳，少缴的 2% 即为减征额；",
            "3. **六税两费减半**：城建税、教育费附加、地方教育附加、印花税等减半；",
            "4. **企业所得税小微优惠**：符合小型微利企业条件的，实际税负 5%（减免应纳税所得额的 20%）。",
            "",
        ]
        try:
            from knowledge.retriever import answer as policy_answer

            kb = policy_answer("红利账单 减免税额构成 小规模纳税人")
            if kb.get("answer"):
                lines.append("📚 知识库参考：")
                lines.append(kb["answer"].split("\n\n" + DISCLAIMER)[0][:800])
        except Exception:  # noqa: BLE001
            pass
        if amounts:
            try:
                sample = engine.calc_vat(amounts[0], 0.0, engine.current_quarter())
                lines.extend(
                    [
                        "",
                        f"以你提到的 ¥{yuan(amounts[0])} 为例（假设为该季度含税销售额、全部普票）：",
                        f"- 不含税销售额 ¥{yuan(sample['total_exclusive'])}，"
                        f"免征增值税 ¥{yuan(sample['vat_exempt'])}；",
                        f"- 若为专票或超过免税标准，则应缴增值税 ¥{yuan(sample['vat_payable'])}，"
                        f"减征额 ¥{yuan(sample['vat_reduction'])}。",
                    ]
                )
            except Exception:  # noqa: BLE001
                pass
        lines.append("")
        lines.append("如果你想精确核对某笔红利账单，可以告诉我所属季度和销售额，我用引擎逐项拆解。")
        return "\n".join(lines)

    if intent == "journal":
        lines = [
            "我按业务类型给你标准分录模板（金额按含税价 ÷ 1.01 换算）：",
            "",
            "**1. 收到服务费（开普票）**",
            "借：银行存款（含税金额）",
            "贷：主营业务收入（不含税金额）",
            "贷：应交税费—应交增值税（含税 − 不含税）",
            "",
            "**2. 支付办公费 / 房租（小规模进项不可抵扣）**",
            "借：管理费用—办公费 / 房租",
            "贷：银行存款",
            "",
            "**3. 计提工资**",
            "借：管理费用—工资",
            "贷：应付职工薪酬",
            "",
            "**4. 季度末免征增值税结转**",
            "借：应交税费—应交增值税",
            "贷：其他收益（或营业外收入）",
            "",
        ]
        amounts = (route.get("arguments") or {}).get("amounts") or []
        if amounts:
            try:
                from ledger.models import derive_exclusive

                inclusive = amounts[0]
                exclusive, vat = derive_exclusive(inclusive)
                lines.extend(
                    [
                        f"以你提到的 ¥{yuan(inclusive)} 为例：",
                        f"借：银行存款 ¥{yuan(inclusive)}",
                        f"贷：主营业务收入 ¥{yuan(exclusive)}",
                        f"贷：应交税费—应交增值税 ¥{yuan(vat)}",
                    ]
                )
            except Exception:  # noqa: BLE001
                pass
        return "\n".join(lines)

    # chat / 兜底
    return (
        f"你好，我是「税小智」，{config.COMPANY.get('short_name', '云睿科技')}的税务与记账助手。我可以帮你：\n"
        "1. 🧮 算税：增值税、企业所得税、研发加计扣除、印花税（确定性引擎计算）；\n"
        "2. 📒 记账：自然语言记收入 / 费用，自动生成分录；\n"
        "3. 📋 申报：生成申报表草稿、核对申报表（支持截图 OCR）；\n"
        "4. 📅 提醒：申报日历与截止日；\n"
        "5. 📚 政策：小规模纳税人、小微优惠、研发加计等政策检索（带文号与生效日期）。\n\n"
        "你可以直接说：「这个季度开了 8 万普票，增值税怎么算？」或「今天收到云图科技 5000 块」。\n\n"
        + OFFLINE_HINT
    )


def local_reply(text: str) -> dict[str, Any]:
    """无 API Key 时的确定性回复（基于规则 + 工具 + 知识库）。

    返回
    ----
    ``{"reply", "intent", "tool_calls": [{"name", "arguments", "result"}], "used_llm": False,
       "error", "disclaimer"}``
    """
    user_text = str(text or "").strip()
    route = intent_router.route(user_text)
    intent = route.get("intent") or "chat"
    tool_name = route.get("tool")
    arguments = dict(route.get("arguments") or {})
    tool_calls: list[dict[str, Any]] = []
    result: dict[str, Any] | None = None

    if tool_name:
        result = tools.call_tool(tool_name, arguments)
        tool_calls.append({"name": tool_name, "arguments": arguments, "result": result})

    reply = _compose_local(intent, user_text, route, result)
    return {
        "reply": _ensure_disclaimer(reply),
        "intent": intent,
        "intent_label": intent_router.INTENTS.get(intent, intent),
        "tool_calls": tool_calls,
        "used_llm": False,
        "error": None,
        "disclaimer": DISCLAIMER,
        "reply_hint": route.get("reply_hint", ""),
    }


# =============================================================================
# LLM 对话循环
# =============================================================================
def _sanitize_history(history: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """只保留最近若干轮 user / assistant 文本消息（防止注入非法角色与超长历史）。"""
    cleaned: list[dict[str, str]] = []
    for message in (history or [])[-10:]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        content = message.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            continue
        cleaned.append({"role": role, "content": content[:4000]})
    return cleaned


def _tool_message(result: dict[str, Any]) -> str:
    """把工具结果压缩为回灌给模型的消息内容（保留 display 与标量数据）。"""
    data = result.get("data")
    brief: dict[str, Any] = {"ok": result.get("ok"), "display": result.get("display") or ""}
    if result.get("error"):
        brief["error"] = result["error"]
    if isinstance(data, dict):
        scalars = {
            key: value
            for key, value in data.items()
            if isinstance(value, (int, float, str, bool)) and key != "raw"
        }
        if scalars:
            brief["data"] = scalars
    text = json.dumps(brief, ensure_ascii=False, default=str)
    if len(text) > _MAX_TOOL_CONTENT:
        text = text[:_MAX_TOOL_CONTENT] + "…（内容过长已截断）"
    return text


def _build_messages(history: list[dict[str, Any]] | None, user_text: str) -> list[dict[str, Any]]:
    system = build_system_prompt() + "\n\n" + build_context()
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    messages.extend(_sanitize_history(history))
    messages.append({"role": "user", "content": user_text})
    return messages


def run_turn(
    user_text: str,
    history: list[dict[str, Any]] | None = None,
    *,
    api_key: str | None = None,
    model: str | None = None,
    allow_tools: bool = True,
    max_rounds: int = 4,
) -> dict[str, Any]:
    """执行一轮 Agent 对话。

    参数
    ----
    user_text : 用户输入
    history   : 历史消息（``[{"role": "user"|"assistant", "content": str}]``）
    api_key   : 覆盖 API Key（默认读设置页 / 环境变量）
    model     : 覆盖模型名（默认 deepseek-chat）
    allow_tools : 是否允许 function calling（默认 True）
    max_rounds  : 工具循环最大轮数（默认 4）

    返回
    ----
    ``{"reply", "intent", "tool_calls", "used_llm", "error", "disclaimer", "model"?}``
    """
    text = str(user_text or "").strip()
    if not text:
        fallback = local_reply("")
        fallback["reply"] = "请输入你的问题，例如：「这个季度开了 8 万普票，增值税怎么算？」\n\n" + DISCLAIMER
        fallback["error"] = "用户输入为空"
        return fallback

    intent_info = intent_router.detect_intent(text)
    client: DeepSeekClient = get_client(api_key=api_key, model=model)

    # ---- 无 Key：直接本地确定性回复 --------------------------------------
    if not client.configured:
        result = local_reply(text)
        result["intent"] = intent_info["intent"]
        result["error"] = "未配置 DeepSeek API Key，已使用本地规则模式回答（算税、台账、政策检索不受影响）。"
        return result

    messages = _build_messages(history, text)
    tool_calls_record: list[dict[str, Any]] = []
    try:
        final_content = ""
        rounds = max(1, int(max_rounds))
        for _ in range(rounds):
            response = client.chat(messages, tools=tools.tool_specs() if allow_tools else None)
            content = (response.get("content") or "").strip()
            calls = response.get("tool_calls") or []
            if not calls:
                final_content = content
                break
            assistant_tool_calls = []
            for index, call in enumerate(calls):
                assistant_tool_calls.append(
                    {
                        "id": call.get("id") or f"call_{index + 1}",
                        "type": "function",
                        "function": {
                            "name": call.get("name") or "",
                            "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                        },
                    }
                )
            messages.append({"role": "assistant", "content": content, "tool_calls": assistant_tool_calls})
            for index, call in enumerate(calls):
                name = str(call.get("name") or "")
                arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
                result = tools.call_tool(name, arguments)
                tool_calls_record.append({"name": name, "arguments": arguments, "result": result})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": assistant_tool_calls[index]["id"],
                        "content": _tool_message(result),
                    }
                )
        if not final_content and tool_calls_record:
            # 工具轮数用尽仍未给出文本：再要一次纯文本总结
            response = client.chat(
                messages
                + [{"role": "user", "content": "请基于以上工具结果，用中文简要总结结论与关键数字（保留 2 位小数）。"}],
                temperature=0.3,
            )
            final_content = (response.get("content") or "").strip()

        if not final_content:
            fallback = local_reply(text)
            fallback["error"] = "DeepSeek 未返回有效文本，已切换本地规则模式回答。"
            fallback["intent"] = intent_info["intent"]
            return fallback

        if intent_info["intent"] != "chat" :
            # 规则识别到的意图仅用于记录；以模型实际行为为准
            pass
        return {
            "reply": _ensure_disclaimer(final_content),
            "intent": intent_info["intent"],
            "tool_calls": tool_calls_record,
            "used_llm": True,
            "error": None,
            "disclaimer": DISCLAIMER,
            "model": client.model,
        }
    except DeepSeekError as exc:
        fallback = local_reply(text)
        fallback["intent"] = intent_info["intent"]
        fallback["used_llm"] = False
        fallback["error"] = f"DeepSeek 调用失败：{exc}（已切换本地规则模式回答）"
        return fallback
    except Exception as exc:  # noqa: BLE001 - 任何异常都降级，保证对话可用
        fallback = local_reply(text)
        fallback["intent"] = intent_info["intent"]
        fallback["used_llm"] = False
        fallback["error"] = f"AI 助手异常：{exc}（已切换本地规则模式回答）"
        return fallback


#: 兼容别名（CONTRACTS 4.5）
def run_agent(
    user_text: str,
    history: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """``run_turn`` 的兼容别名。"""
    return run_turn(user_text, history, **kwargs)
