"""🧮 算税工作台：增值税 / 企业所得税 / 研发加计 / 印花税 四个确定性算税工具。

所有税额均由 ``engine`` 计算（LLM 不参与），页面只负责取数、展示公式、分录与政策依据；
每个结果下方显示 ``config.DISCLAIMER``。
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

import config
from engine import (
    calc_corp_tax,
    calc_rd_deduction,
    calc_stamp_tax,
    calc_vat,
    current_quarter,
    describe_policy,
    format_date_cn,
    get_rate_table,
    period_range,
    period_label,
)
from ui.components import cache, formatting as F, ledger_api as LA, runtime, theme, ui


# =============================================================================
# 通用展示
# =============================================================================
def _formula_block(title: str, lines: list[str]) -> None:
    """计算公式说明块。"""
    body = "\n".join(f"- {line}" for line in lines if line)
    if body:
        with st.container(border=True):
            st.markdown(f"**{title}**")
            st.markdown(body)


def _policy_block(period: str) -> None:
    """政策依据（来源 + 生效日期）与适用税率表。"""
    try:
        described = describe_policy(period)
    except Exception:  # noqa: BLE001
        return
    with st.expander("📚 政策依据与适用税率", expanded=False):
        for source in described.get("sources") or []:
            st.markdown(
                f"- **{source.get('title')}**\n"
                f"  来源：{source.get('source')} · 生效：{source.get('effective_date')} · "
                f"到期：{source.get('expiry_date')}",
                unsafe_allow_html=False,
            )
        st.caption(f"政策参数版本：{described.get('policy_version')} · 最近核验：{described.get('last_verified')}")
        rates = described.get("rates") or []
        if rates:
            st.dataframe(pd.DataFrame(rates), hide_index=True, width="stretch")
        for warning in described.get("warnings") or []:
            st.warning(warning)


def _warnings(result: dict) -> None:
    for warning in result.get("warnings") or []:
        st.warning(warning)


def _render_journal(period: str, result: dict) -> None:
    """VAT 相关凭证（免征 / 缴纳）展示。"""
    journal = runtime.load("ledger.journal")
    if journal is None:
        ui.module_missing("会计分录展示", "ledger.journal", detail=runtime.error_of("ledger.journal"))
        return
    entries: list[dict] = []
    try:
        if result.get("vat_payable", 0) > 0 and hasattr(journal, "build_vat_payment_entry"):
            entry = journal.build_vat_payment_entry(period)
            if entry:
                entries.append(entry)
        elif hasattr(journal, "build_vat_exemption_entry"):
            entry = journal.build_vat_exemption_entry(period)
            if entry:
                entries.append(entry)
    except Exception as exc:  # noqa: BLE001
        ui.failure("生成会计分录失败（可直接跳过，不影响算税结果）。", exc, show_traceback=False)
        return
    if not entries:
        st.caption("本期无需要生成的增值税相关凭证。")
        return
    with st.container(border=True):
        st.markdown("**生成的会计分录（仅供参考，需会计确认）**")
        for entry in entries:
            st.caption(
                f"凭证类型：{entry.get('voucher_type') or '其他'} · 日期：{entry.get('date')} · "
                f"摘要：{entry.get('description') or '—'}"
            )
            lines = entry.get("lines") or []
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "会计科目": line.get("account"),
                            "方向": line.get("direction"),
                            "金额": F.fmt_money(line.get("amount")),
                        }
                        for line in lines
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
        st.caption(f"凭证需在台账页「凭证」中确认后入账（本系统仅生成建议分录）。")


def _source_caption(source: str) -> None:
    st.caption(f"数据来源：{source}")


# =============================================================================
# 增值税
# =============================================================================
def _prefill_vat() -> None:
    """「从台账取数」回调：把收入台账期间汇总写入输入框。"""
    period = st.session_state.get("calc_vat_period") or current_quarter()
    try:
        totals = LA.period_totals(period)
    except Exception as exc:  # noqa: BLE001
        st.session_state["_calc_vat_error"] = f"读取收入台账失败：{exc}"
        return
    st.session_state["calc_vat_total"] = float(totals.get("total_inclusive") or 0)
    st.session_state["calc_vat_special"] = float(totals.get("special_inclusive") or 0)
    st.session_state["_calc_vat_source"] = f"收入台账 {period}（{totals.get('count', 0)} 笔）"
    st.session_state.pop("_calc_vat_error", None)


def _render_vat() -> None:
    ui.section(
        "增值税及附加税费（按季申报）",
        "小规模纳税人 3% 征收率减按 1%；季度不含税销售额不超过 30 万元免征增值税。",
    )
    periods = LA.periods()
    period = st.selectbox("税款所属期", periods, index=0, key="calc_vat_period")
    cols = st.columns([2, 2, 2])
    with cols[0]:
        total = st.number_input(
            "季度含税销售额（元）", min_value=0.0, step=1000.0, format="%.2f", key="calc_vat_total"
        )
    with cols[1]:
        special = st.number_input(
            "其中：专用发票含税金额（元）", min_value=0.0, step=1000.0, format="%.2f", key="calc_vat_special"
        )
    with cols[2]:
        st.write("")
        st.button(
            "📥 从台账取数",
            key="calc_vat_prefill",
            on_click=_prefill_vat,
            width="stretch",
            help="读取收入台账中该季度的含税销售额与专票金额",
            type="primary",
        )
    if st.session_state.get("_calc_vat_error"):
        st.error(st.session_state["_calc_vat_error"])
    if st.session_state.get("_calc_vat_source"):
        _source_caption(st.session_state["_calc_vat_source"])

    try:
        result = calc_vat(total, special, period)
    except Exception as exc:  # noqa: BLE001
        ui.failure("增值税计算失败，请检查输入金额。", exc)
        return

    ui.metric_row(
        [
            {"label": "不含税销售额", "value": F.fmt_money(result["total_exclusive"])},
            {"label": "应缴增值税", "value": F.fmt_money(result["vat_payable"])},
            {"label": "附加税费", "value": F.fmt_money(result["surtax"])},
            {"label": "合计应纳税费", "value": F.fmt_money(result["total_tax"])},
            {"label": "免征增值税", "value": F.fmt_money(result["vat_exempt"])},
        ]
    )
    ui.status_alert(
        "PASS" if result.get("within_exemption_limit") else "WARNING",
        result.get("note") or "",
    )
    _warnings(result)

    with st.container(border=True):
        st.markdown("**计算明细**")
        ui.records_table(
            [{"项目": item["label"], "金额": F.fmt_money(item["value"])} for item in result.get("breakdown") or []],
            [("项目", "项目"), ("金额", "金额")],
            key="calc_vat_breakdown",
        )

    with st.expander("📋 对应申报表栏次（供填报参考）", expanded=True):
        lines = result.get("return_lines") or {}
        mapping = [
            ("line_1_taxable_sales", "第1栏 应征增值税不含税销售额（3%征收率）"),
            ("line_10_exempt_sales", "第10栏 小微企业免税销售额"),
            ("line_18_tax_amount", "第18栏 应纳税额"),
            ("line_19_tax_exempt_amount", "第19栏 本期免税额"),
            ("line_22_reduction", "第22栏 应纳税额减征额"),
            ("line_24_payable", "第24栏 本期应补（退）税额"),
            ("surtax_city", "附加：城市维护建设税"),
            ("surtax_education", "附加：教育费附加"),
            ("surtax_local_education", "附加：地方教育附加"),
        ]
        st.dataframe(
            pd.DataFrame(
                [
                    {"栏次": label, "金额": F.fmt_money(lines.get(key))}
                    for key, label in mapping
                    if key in lines
                ]
            ),
            hide_index=True,
            width="stretch",
        )

    _formula_block(
        "计算公式",
        [
            f"不含税销售额 = 含税销售额 ÷ (1 + 1%) = {F.fmt_money(total)} ÷ 1.01 = **{F.fmt_money(result['total_exclusive'])}**",
            f"免税判定：{result.get('limit_basis')}不含税 {F.fmt_money(result['total_exclusive'])} "
            f"{'≤' if result.get('within_exemption_limit') else '>'} 免税额度 {F.fmt_money(result.get('exemption_limit'))}",
            f"应缴增值税 = 应征销售额 × 1% = {F.fmt_money(result['vat_payable'])}",
            f"附加税费 = 应缴增值税 × 12% × 六税两费减半 50% = {F.fmt_money(result['surtax'])}",
            f"合计应纳税费 = 应缴增值税 + 附加税费 = {F.fmt_money(result['total_tax'])}",
        ],
    )

    ui.section("🧾 建议会计分录")
    _render_journal(period, result)

    _policy_block(period)
    theme.disclaimer()


# =============================================================================
# 企业所得税
# =============================================================================
def _prefill_cit() -> None:
    """「从台账取数」回调：按年度汇总收入、费用与研发费用。"""
    year = int(current_quarter()[:4])
    try:
        months = cache.monthly_summary(year)
        revenue = sum(float(row.get("income_exclusive") or 0) for row in months)
        expense = sum(float(row.get("expense") or 0) for row in months)
        rd_total = 0.0
        for quarter in (1, 2, 3, 4):
            summary = cache.rd_summary(f"{year}Q{quarter}")
            rd_total += float(summary.get("total_collectible") or 0)
    except Exception as exc:  # noqa: BLE001
        st.session_state["_calc_cit_error"] = f"读取台账失败：{exc}"
        return
    st.session_state["calc_cit_revenue"] = round(revenue, 2)
    st.session_state["calc_cit_cost"] = round(expense, 2)
    st.session_state["calc_cit_rd"] = round(rd_total, 2)
    st.session_state["_calc_cit_source"] = f"台账 {year} 年度累计"
    st.session_state.pop("_calc_cit_error", None)


def _render_cit() -> None:
    ui.section(
        "企业所得税年度测算",
        "年应纳税所得额不超过 300 万元的小型微利企业，减按 25% 计入应纳税所得额、按 20% 税率缴纳（实际税负 5%）。",
    )
    period = st.selectbox("测算期间", LA.periods(), index=0, key="calc_cit_period")
    cols = st.columns(4)
    with cols[0]:
        revenue = st.number_input(
            "年度不含税收入（元）", min_value=0.0, step=10000.0, format="%.2f", key="calc_cit_revenue"
        )
    with cols[1]:
        cost = st.number_input(
            "年度成本费用（元）", min_value=0.0, step=10000.0, format="%.2f", key="calc_cit_cost"
        )
    with cols[2]:
        rd_cost = st.number_input(
            "研发费用归集额（元）", min_value=0.0, step=1000.0, format="%.2f", key="calc_cit_rd"
        )
    with cols[3]:
        st.write("")
        st.button(
            "📥 从台账取数",
            key="calc_cit_prefill",
            on_click=_prefill_cit,
            width="stretch",
            type="primary",
            help="按当前年度汇总收入台账、费用台账与研发辅助账",
        )
    if st.session_state.get("_calc_cit_error"):
        st.error(st.session_state["_calc_cit_error"])
    if st.session_state.get("_calc_cit_source"):
        _source_caption(st.session_state["_calc_cit_source"])

    with st.expander("小型微利企业判定条件（可修改）", expanded=False):
        cols2 = st.columns(3)
        with cols2[0]:
            employees = st.number_input(
                "从业人数（人）", min_value=0, step=1, value=10, key="calc_cit_employees"
            )
        with cols2[1]:
            total_assets = st.number_input(
                "资产总额（元）", min_value=0.0, step=100000.0, format="%.2f",
                value=1_000_000.0, key="calc_cit_assets",
            )
        with cols2[2]:
            restricted = st.checkbox("属于国家限制或禁止行业", value=False, key="calc_cit_restricted")
            formed_intangible = st.checkbox("研发形成无形资产", value=False, key="calc_cit_intangible")

    annual_profit = round(revenue - cost, 2)
    st.caption(f"会计利润总额 = 年度收入 − 成本费用 = {F.fmt_money(revenue)} − {F.fmt_money(cost)} = **{F.fmt_money(annual_profit)}**")

    try:
        result = calc_corp_tax(
            annual_profit=annual_profit,
            rd_cost=rd_cost,
            employees=int(employees),
            total_assets=float(total_assets),
            is_restricted_industry=bool(restricted),
            rd_formed_intangible=bool(formed_intangible),
            period=period,
        )
    except Exception as exc:  # noqa: BLE001
        ui.failure("企业所得税测算失败，请检查输入。", exc)
        return

    ui.metric_row(
        [
            {"label": "利润总额", "value": F.fmt_money(result.get("annual_profit"))},
            {"label": "研发加计扣除", "value": F.fmt_money(result.get("rd_deduction"))},
            {"label": "应纳税所得额", "value": F.fmt_money(result.get("taxable_income"))},
            {"label": "实际税负率", "value": F.fmt_pct(result.get("effective_rate"))},
            {"label": "应纳企业所得税", "value": F.fmt_money(result.get("corp_tax"))},
        ]
    )
    ui.status_alert(
        "PASS" if result.get("qualifies_small_low_profit") else "WARNING",
        result.get("note") or "",
    )
    _warnings(result)

    with st.container(border=True):
        st.markdown("**计算明细**")
        st.dataframe(
            pd.DataFrame(
                [
                    {"项目": "会计利润总额", "金额": F.fmt_money(result.get("annual_profit"))},
                    {"项目": "研发费用加计扣除", "金额": F.fmt_money(result.get("rd_deduction"))},
                    {"项目": "应纳税所得额", "金额": F.fmt_money(result.get("taxable_income"))},
                    {"项目": "适用税率（实际税负）", "金额": F.fmt_pct(result.get("effective_rate"))},
                    {"项目": "应纳企业所得税", "金额": F.fmt_money(result.get("corp_tax"))},
                ]
            ),
            hide_index=True,
            width="stretch",
        )

    check = result.get("small_low_profit_check") or {}
    if check:
        with st.expander("小型微利企业条件判定明细", expanded=False):
            thresholds = check.get("thresholds") or {}
            st.dataframe(
                pd.DataFrame(
                    [
                        {"条件": "应纳税所得额 ≤ 300 万元", "是否满足": "是" if check.get("income_ok") else "否",
                         "阈值": F.fmt_money(thresholds.get("taxable_income"))},
                        {"条件": "从业人数 ≤ 300 人", "是否满足": "是" if check.get("employees_ok") else "否",
                         "阈值": f"{thresholds.get('employees')} 人"},
                        {"条件": "资产总额 ≤ 5,000 万元", "是否满足": "是" if check.get("assets_ok") else "否",
                         "阈值": F.fmt_money(thresholds.get("total_assets"))},
                        {"条件": "非国家限制或禁止行业", "是否满足": "是" if check.get("industry_ok") else "否", "阈值": "—"},
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
            st.caption("判定结果：" + ("符合小型微利企业条件" if check.get("qualifies") else "不符合：" + "；".join(check.get("reasons") or [])))

    _formula_block(
        "计算公式",
        [
            f"会计利润总额 = 收入 − 成本费用 = {F.fmt_money(annual_profit)}",
            f"应纳税所得额 = 利润总额 − 研发加计扣除 = {F.fmt_money(result.get('annual_profit'))} − {F.fmt_money(result.get('rd_deduction'))} = **{F.fmt_money(result.get('taxable_income'))}**",
            f"应纳企业所得税 = 应纳税所得额 × 实际税负率 = {F.fmt_money(result.get('taxable_income'))} × {F.fmt_pct(result.get('effective_rate'))} = **{F.fmt_money(result.get('corp_tax'))}**",
        ],
    )

    ui.section("🧾 账务处理提示")
    st.caption(
        "企业所得税为季度预缴、年度汇算清缴，本测算不直接生成凭证；"
        "账务上按季度计提「应交税费—应交企业所得税」，实际缴纳时冲减。"
    )
    _policy_block(period)
    theme.disclaimer()


# =============================================================================
# 研发费用加计扣除
# =============================================================================
_RD_FIELD_MAP = [
    ("人员人工费用", "calc_rd_personnel"),
    ("直接投入费用", "calc_rd_material"),
    ("折旧费用", "calc_rd_depreciation"),
    ("无形资产摊销", "calc_rd_amortization"),
    ("新产品设计费等", "calc_rd_design"),
    ("其他相关费用", "calc_rd_other"),
]


def _prefill_rd() -> None:
    """「从台账取数」回调：研发辅助账 + 费用台账归集。"""
    period = st.session_state.get("calc_rd_period") or current_quarter()
    try:
        summary = cache.rd_summary(period)
        items = summary.get("items") or {}
        mapping = dict(_RD_FIELD_MAP)
        for label, key in mapping.items():
            st.session_state[key] = float(items.get(label) or 0)
    except Exception as exc:  # noqa: BLE001
        st.session_state["_calc_rd_error"] = f"读取研发辅助账失败：{exc}"
        return
    st.session_state["_calc_rd_source"] = f"研发辅助账 + 费用台账 {period}"
    st.session_state.pop("_calc_rd_error", None)


def _render_rd() -> None:
    ui.section(
        "研发费用加计扣除",
        "未形成无形资产的研发费用按 100% 加计扣除；其他相关费用受 10% 限额约束。",
    )
    period = st.selectbox("所属期间", LA.periods(), index=0, key="calc_rd_period")
    cols = st.columns(3)
    values: dict[str, float] = {}
    order = [
        ("calc_rd_personnel", "人员人工费用（元）"),
        ("calc_rd_material", "直接投入费用（元）"),
        ("calc_rd_depreciation", "折旧费用（元）"),
        ("calc_rd_amortization", "无形资产摊销（元）"),
        ("calc_rd_design", "新产品设计费等（元）"),
        ("calc_rd_other", "其他相关费用（元）"),
    ]
    for index, (key, label) in enumerate(order):
        with cols[index % 3]:
            values[key] = st.number_input(label, min_value=0.0, step=1000.0, format="%.2f", key=key)
    with st.columns([1, 1, 1])[1]:
        st.button(
            "📥 从台账取数",
            key="calc_rd_prefill",
            on_click=_prefill_rd,
            width="stretch",
            type="primary",
            help="读取研发辅助账与费用台账中标记研发相关的费用",
        )
    if st.session_state.get("_calc_rd_error"):
        st.error(st.session_state["_calc_rd_error"])
    if st.session_state.get("_calc_rd_source"):
        _source_caption(st.session_state["_calc_rd_source"])

    cols2 = st.columns(2)
    with cols2[0]:
        formed_intangible = st.checkbox("研发形成无形资产（按 200% 摊销）", value=False, key="calc_rd_intangible")
    with cols2[1]:
        amortization_years = st.number_input(
            "无形资产摊销年限（年）", min_value=1, max_value=50, value=10, step=1, key="calc_rd_years"
        )

    try:
        result = calc_rd_deduction(
            personnel_cost=values["calc_rd_personnel"],
            material_cost=values["calc_rd_material"],
            depreciation=values["calc_rd_depreciation"],
            amortization=values["calc_rd_amortization"],
            design_cost=values["calc_rd_design"],
            other_cost=values["calc_rd_other"],
            formed_intangible=bool(formed_intangible),
            amortization_years=int(amortization_years),
            period=period,
            project_name="研发项目",
        )
    except Exception as exc:  # noqa: BLE001
        ui.failure("研发费用加计扣除计算失败，请检查输入。", exc)
        return

    ui.metric_row(
        [
            {"label": "五项费用合计", "value": F.fmt_money(result.get("five_total"))},
            {"label": "可加计扣除研发费用", "value": F.fmt_money(result.get("total_collectible"))},
            {"label": "加计扣除额", "value": F.fmt_money(result.get("deduction_amount"))},
            {"label": "其他费用限额", "value": F.fmt_money(result.get("other_cost_limit"))},
        ]
    )
    ui.status_alert("PASS" if result.get("total_collectible") else "WARNING", result.get("note") or "")
    _warnings(result)

    with st.container(border=True):
        st.markdown("**费用归集明细**")
        st.dataframe(
            pd.DataFrame(
                [
                    {"费用项目": item.get("item"), "金额": F.fmt_money(item.get("amount"))}
                    for item in result.get("items_detail") or []
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        st.caption(
            f"其他相关费用申报 {F.fmt_money(result.get('other_cost_claimed'))} 元，"
            f"限额 {F.fmt_money(result.get('other_cost_limit'))} 元，"
            f"按限额归集 {F.fmt_money(result.get('other_cost_allowed'))} 元。"
        )

    _formula_block(
        "计算公式",
        [
            "五项费用合计 = 人员人工 + 直接投入 + 折旧 + 无形资产摊销 + 新产品设计费",
            f"其他相关费用限额 = 五项费用合计 × 10% ÷ (1 − 10%) = {F.fmt_money(result.get('other_cost_limit'))}",
            f"可加计扣除研发费用 = 五项费用合计 + 其他费用（限额内）= {F.fmt_money(result.get('total_collectible'))}",
            f"加计扣除额 = 可加计扣除研发费用 × 100% = {F.fmt_money(result.get('deduction_amount'))}",
        ],
    )
    if result.get("formed_intangible"):
        st.info(
            f"形成无形资产：成本 {F.fmt_money(result.get('intangible_cost'))} 元，"
            f"按 {result.get('amortization_years')} 年摊销，每年摊销 {F.fmt_money(result.get('annual_amortization'))} 元，"
            f"并按 200% 税前摊销。",
            icon="🧾",
        )
    ui.section("🧾 账务处理提示")
    st.caption("研发费用加计扣除在年度汇算清缴时通过纳税调整享受，本测算用于预估可加计扣除金额，不直接生成凭证。")
    _policy_block(period)
    theme.disclaimer()


# =============================================================================
# 印花税
# =============================================================================
def _render_stamp() -> None:
    ui.section(
        "印花税（按季申报）",
        "按合同所列金额 × 适用税率计算，小规模纳税人享受「六税两费」减半征收。",
    )
    periods = LA.periods()
    period = st.selectbox("税款所属期", periods, index=0, key="calc_stamp_period")
    small_scale = st.checkbox("小规模纳税人（享受六税两费减半）", value=True, key="calc_stamp_small")
    if st.button("📥 从台账取数并重算", key="calc_stamp_refresh", type="primary"):
        cache.clear()
        st.rerun()

    try:
        start, end = period_range(period)
        contracts = LA.list_contracts(start.isoformat(), end.isoformat())
    except Exception as exc:  # noqa: BLE001
        ui.failure("读取合同台账失败。", exc)
        contracts = []
    _source_caption(f"合同台账 {period}（{len(contracts)} 份）")

    try:
        result = calc_stamp_tax(contracts, small_scale=bool(small_scale), period=period)
    except Exception as exc:  # noqa: BLE001
        ui.failure("印花税计算失败。", exc)
        return

    ui.metric_row(
        [
            {"label": "合同份数", "value": f"{len(result.get('items') or [])}"},
            {"label": "计税金额合计", "value": F.fmt_money(result.get("total_amount"))},
            {"label": "减免税额", "value": F.fmt_money(result.get("discount_amount"))},
            {"label": "应缴印花税", "value": F.fmt_money(result.get("total_payable"))},
        ]
    )
    ui.status_alert("PASS" if result.get("total_payable") is not None else "WARNING", result.get("note") or "")
    _warnings(result)

    if result.get("items"):
        with st.container(border=True):
            st.markdown("**分合同明细**")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "合同编号": item.get("contract_no") or "—",
                            "对方单位": item.get("counterparty") or "—",
                            "合同类型": item.get("contract_type"),
                            "计税金额": F.fmt_money(item.get("amount")),
                            "税率": item.get("rate_text"),
                            "税额（减免前）": F.fmt_money(item.get("tax_before_discount")),
                            "减免额": F.fmt_money(item.get("discount_amount")),
                            "应缴税额": F.fmt_money(item.get("tax_payable")),
                        }
                        for item in result["items"]
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
            _formula_block(
                "计算说明",
                [
                    f"计税金额合计 = {F.fmt_money(result.get('total_amount'))}",
                    f"减免前税额 = Σ(合同金额 × 税率) = {F.fmt_money(result.get('total_before_discount'))}",
                    f"六税两费减半（× {F.fmt_pct(result.get('discount_ratio'))}）= 减免 {F.fmt_money(result.get('discount_amount'))}",
                    f"应缴印花税 = {F.fmt_money(result.get('total_payable'))}",
                ],
            )
    else:
        st.info("本期无合同台账记录，无需缴纳印花税；若已签订应税合同，请先在台账页登记。")

    with st.expander("📚 印花税税率表", expanded=False):
        rates = result.get("rates") or {}
        st.dataframe(
            pd.DataFrame([{"合同类型": name, "税率": rate} for name, rate in rates.items()]),
            hide_index=True,
            width="stretch",
        )
    _policy_block(period)
    theme.disclaimer()


# =============================================================================
# 页面入口
# =============================================================================
def render() -> None:
    """渲染算税工作台。"""
    from ui.components import explainers

    ui.page_header(
        "算税工作台",
        "确定性算税引擎：所有税额由本地代码计算，AI 只负责解释，不参与计算。",
        icon="🧮",
        right_text=f"当前季度：{period_label(current_quarter())}",
    )
    explainers.basics()
    tabs = st.tabs(["增值税及附加", "企业所得税", "研发费用加计", "印花税"])
    with tabs[0]:
        _render_vat()
    with tabs[1]:
        _render_cit()
    with tabs[2]:
        _render_rd()
    with tabs[3]:
        _render_stamp()
