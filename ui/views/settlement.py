"""📊 汇算清缴：年度数据 → 纳税调整 → 亏损弥补 → 年度申报辅助表 → 检查清单。

定位：企业所得税年度汇算的**数据准备与测算辅助**；系统不代替电子税务局年度申报，
最终以 A 类年度申报表与主管税务机关口径为准。全部税额由 ``engine.cit_settlement`` 计算。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import pandas as pd
import streamlit as st

from ui.components import cache, formatting as F, settlement_api as SA, theme, ui

FLASH_KEY = "_settlement_flash"
STATE_SETTLEMENT = "settlement_result"
STATE_BUNDLE = "settlement_annual_bundle"
STATE_EXPORT = "settlement_annual_export"

YEAR_KEY = "settlement_year"

ADJUSTMENT_TABLE_COLUMNS: list[tuple[str, str]] = [
    ("item_type", "调整项目"),
    ("direction", "方向"),
    ("accounting_amount", "会计金额"),
    ("tax_amount", "税法金额"),
    ("adjustment_amount", "调整金额"),
    ("basis", "计算依据"),
    ("source", "政策依据"),
    ("note", "备注"),
]

LOSS_TABLE_COLUMNS: list[tuple[str, str]] = [
    ("year", "亏损年度"),
    ("loss_amount", "可弥补亏损额"),
    ("used_amount", "已弥补金额"),
    ("remaining", "剩余可弥补"),
    ("expire_year", "到期年度"),
    ("note", "备注"),
]

BUNDLE_MONEY_KEYS = {
    "value",
    "accounting_amount",
    "tax_amount",
    "adjustment_amount",
    "loss_amount",
    "used_amount",
    "used_this_year",
    "remaining",
    "remaining_after",
    "revenue_exclusive",
    "cost_total",
    "accounting_profit",
    "taxable_income",
    "corporate_tax",
    "prepaid_tax",
    "payable",
    "refund",
    "total",
    "amount",
}

BUNDLE_FIELD_LABELS = {
    "item_type": "调整项目",
    "direction": "方向",
    "accounting_amount": "会计金额",
    "tax_amount": "税法金额",
    "adjustment_amount": "调整金额",
    "basis": "计算依据",
    "source": "政策依据",
    "year": "年度",
    "loss_amount": "可弥补亏损额",
    "used_amount": "已弥补金额",
    "used_this_year": "本年弥补",
    "remaining": "剩余可弥补",
    "remaining_after": "弥补后剩余",
    "expire_year": "到期年度",
    "status": "状态",
    "note": "备注",
}


# =============================================================================
# 提示
# =============================================================================
def _flash(message: str, level: str = "success") -> None:
    st.session_state.setdefault(FLASH_KEY, []).append({"message": message, "level": level})


def _show_flash() -> None:
    for item in st.session_state.pop(FLASH_KEY, []) or []:
        level = item.get("level", "success")
        if level == "warning":
            st.warning(item["message"])
        elif level == "error":
            st.error(item["message"])
        else:
            st.success(item["message"])


def _safe(func, default, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        ui.failure("汇算数据读取失败，请稍后重试。", exc, show_traceback=False)
        return default


def _settlement_state(year: str) -> dict | None:
    payload = st.session_state.get(STATE_SETTLEMENT) or {}
    if str(payload.get("year") or "") == str(year):
        return payload.get("settlement") or {}
    return None


def _compute_settlement(year: str) -> dict | None:
    try:
        settlement = SA.compute_settlement(year)
    except Exception as exc:  # noqa: BLE001
        st.error(f"计算汇算结果失败：{exc}")
        return None
    st.session_state[STATE_SETTLEMENT] = {"year": str(year), "settlement": settlement}
    cache.clear()
    _flash(f"{SA.year_label(year)}汇算结果已计算：应纳税所得额 {F.fmt_money(settlement.get('taxable_income'))}")
    for warning in settlement.get("warnings") or []:
        _flash(str(warning), "warning")
    st.rerun()
    return settlement


# =============================================================================
# Tab 1：年度数据
# =============================================================================
def _render_annual_metrics(annual: dict) -> None:
    ui.metric_row(
        [
            {"label": "全年收入（不含税）", "value": F.fmt_money(annual.get("revenue_exclusive"))},
            {"label": "全年费用", "value": F.fmt_money(annual.get("expense_total"))},
            {"label": "利润（台账口径）", "value": F.fmt_money(annual.get("profit"))},
            {"label": "工资薪金总额", "value": F.fmt_money(annual.get("salary_total"))},
            {"label": "研发费用", "value": F.fmt_money(annual.get("rd_cost"))},
            {"label": "已预缴企业所得税", "value": F.fmt_money(annual.get("prepaid_tax"))},
        ]
    )


def _render_monthly_detail(annual: dict) -> None:
    months = annual.get("monthly") or []
    if not months:
        st.caption("暂无月度明细（台账无本年度记录）。")
        return
    with st.expander("📅 月度收入 / 费用明细", expanded=False):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "月份": month.get("month"),
                        "含税收入": F.fmt_money(month.get("income_inclusive")),
                        "不含税收入": F.fmt_money(month.get("income_exclusive")),
                        "费用": F.fmt_money(month.get("expense")),
                        "利润": F.fmt_money(month.get("profit")),
                    }
                    for month in months
                ]
            ),
            hide_index=True,
            width="stretch",
        )


def _render_inputs_form(year: str, annual: dict, record: dict) -> None:
    """补充数据表单（保存后用 compute_settlement 计算）。"""
    inputs = SA.get_annual_inputs(year)
    note_default = str(record.get("note") or inputs.get("note") or "")
    profit_default = F.to_float(inputs.get("accounting_profit")) or F.to_float(annual.get("profit"))
    rd_default = F.to_float(inputs.get("rd_deduction")) or F.to_float(annual.get("rd_deduction"))
    with st.form(f"settlement_inputs_form_{year}"):
        st.markdown("**补充年度数据**（限额类纳税调整所需发生额、小微判定条件等）")
        cols = st.columns(5)
        with cols[0]:
            entertainment = st.number_input(
                "业务招待费（元）", min_value=0.0, step=1000.0, format="%.2f",
                value=F.to_float(inputs.get("entertainment_actual")),
                key=f"settlement_ent_{year}",
            )
            advertising = st.number_input(
                "广告费和业务宣传费（元）", min_value=0.0, step=1000.0, format="%.2f",
                value=F.to_float(inputs.get("advertising_actual")),
                key=f"settlement_adv_{year}",
            )
        with cols[1]:
            welfare = st.number_input(
                "职工福利费（元）", min_value=0.0, step=1000.0, format="%.2f",
                value=F.to_float(inputs.get("welfare_actual")),
                key=f"settlement_welfare_{year}",
            )
            education = st.number_input(
                "职工教育经费（元）", min_value=0.0, step=1000.0, format="%.2f",
                value=F.to_float(inputs.get("education_actual")),
                key=f"settlement_education_{year}",
            )
        with cols[2]:
            union = st.number_input(
                "工会经费（元）", min_value=0.0, step=1000.0, format="%.2f",
                value=F.to_float(inputs.get("union_actual")),
                key=f"settlement_union_{year}",
            )
            employees = st.number_input(
                "从业人数（人）", min_value=0, step=1,
                value=int(inputs.get("employees") or 0),
                key=f"settlement_employees_{year}",
            )
        with cols[3]:
            total_assets = st.number_input(
                "资产总额（元）", min_value=0.0, step=100000.0, format="%.2f",
                value=F.to_float(inputs.get("total_assets")),
                key=f"settlement_assets_{year}",
            )
            prepaid_tax = st.number_input(
                "本年已预缴企业所得税（元）", min_value=0.0, step=1000.0, format="%.2f",
                value=F.to_float(inputs.get("prepaid_tax")) or F.to_float(annual.get("prepaid_tax")),
                key=f"settlement_prepaid_{year}",
            )
        with cols[4]:
            is_restricted = st.checkbox(
                "属于国家限制或禁止行业",
                value=bool(inputs.get("is_restricted_industry")),
                key=f"settlement_restricted_{year}",
            )
            st.caption("小型微利企业：应纳税所得额 ≤300 万、从业人数 ≤300、资产总额 ≤5000 万。")
        override_cols = st.columns(2)
        with override_cols[0]:
            override_profit = st.checkbox(
                "覆盖会计利润（台账利润不准确时手动填写）",
                value=bool(inputs.get("accounting_profit")),
                key=f"settlement_override_profit_{year}",
            )
            accounting_profit = st.number_input(
                "会计利润总额（元）",
                min_value=0.0,
                step=1000.0,
                format="%.2f",
                value=profit_default,
                key=f"settlement_profit_{year}",
                disabled=not override_profit,
            )
        with override_cols[1]:
            override_rd = st.checkbox(
                "覆盖研发费用加计扣除（按辅助账手动填写）",
                value=bool(inputs.get("rd_deduction")),
                key=f"settlement_override_rd_{year}",
            )
            rd_deduction = st.number_input(
                "研发费用加计扣除（元）",
                min_value=0.0,
                step=1000.0,
                format="%.2f",
                value=rd_default,
                key=f"settlement_rd_{year}",
                disabled=not override_rd,
            )
        note = st.text_input("备注", value=note_default, key=f"settlement_note_{year}")
        submitted = st.form_submit_button("💾 保存年度输入", type="primary", width="content")
    if not submitted:
        return
    data: dict[str, Any] = {
        "entertainment_actual": float(entertainment),
        "advertising_actual": float(advertising),
        "welfare_actual": float(welfare),
        "education_actual": float(education),
        "union_actual": float(union),
        "employees": int(employees),
        "total_assets": float(total_assets),
        "prepaid_tax": float(prepaid_tax),
        "is_restricted_industry": bool(is_restricted),
    }
    if override_profit:
        data["accounting_profit"] = float(accounting_profit)
    # 研发加计扣除必须显式保存：后端 save_annual_inputs 会把未传的键默认成 0，
    # 导致 compute_settlement 误把「未覆盖」当成「覆盖为 0」。
    data["rd_deduction"] = float(rd_deduction if override_rd else F.to_float(annual.get("rd_deduction")))
    try:
        SA.save_annual_inputs(year, data, note=note.strip())
    except Exception as exc:  # noqa: BLE001
        st.error(f"保存年度输入失败：{exc}")
        return
    cache.clear()
    _flash(f"{SA.year_label(year)}补充数据已保存，请点击「计算汇算结果」刷新结果。")
    st.rerun()


def _render_settlement_result(year: str, settlement: dict) -> None:
    if not settlement:
        return
    net_adjustment = (
        F.to_float(settlement.get("limit_adjustment_add"))
        + F.to_float(settlement.get("manual_adjustment_add"))
        - F.to_float(settlement.get("manual_adjustment_reduce"))
    )
    payable_value = F.to_float(settlement.get("refund")) or F.to_float(settlement.get("payable"))
    payable_label = settlement.get("payable_label") or "应补（退）税额"
    ui.metric_row(
        [
            {"label": "会计利润", "value": F.fmt_money(settlement.get("accounting_profit"))},
            {"label": "纳税调整（净）", "value": F.fmt_money(net_adjustment)},
            {"label": "应纳税所得额", "value": F.fmt_money(settlement.get("taxable_income"))},
            {"label": "应纳所得税额", "value": F.fmt_money(settlement.get("corporate_tax"))},
            {"label": payable_label, "value": F.fmt_money(payable_value)},
        ]
    )
    ui.metric_row(
        [
            {"label": "研发费用加计扣除", "value": F.fmt_money(settlement.get("research_deduction"))},
            {"label": "弥补以前年度亏损", "value": F.fmt_money(settlement.get("loss_used"))},
            {"label": "本年已预缴", "value": F.fmt_money(settlement.get("prepaid_tax"))},
            {"label": "适用税率", "value": F.fmt_pct(settlement.get("applicable_rate"))},
            {"label": "小型微利企业", "value": "是" if settlement.get("qualifies_small_low_profit") else "否"},
        ]
    )
    status = str(settlement.get("payable_label") or "测算完成")
    ui.status_alert("WARNING" if settlement.get("warnings") else "PASS", settlement.get("note") or status)
    for warning in settlement.get("warnings") or []:
        st.warning(str(warning))
    if settlement.get("note"):
        st.caption(str(settlement.get("note")))
    with st.expander("📄 汇算结果明细", expanded=False):
        rows = [
            {"项目": "全年收入（不含税）", "金额": F.fmt_money(settlement.get("revenue_exclusive"))},
            {"项目": "全年成本费用", "金额": F.fmt_money(settlement.get("cost_total"))},
            {"项目": "会计利润总额", "金额": F.fmt_money(settlement.get("accounting_profit"))},
            {"项目": "限额类纳税调增", "金额": F.fmt_money(settlement.get("limit_adjustment_add"))},
            {"项目": "手工纳税调增", "金额": F.fmt_money(settlement.get("manual_adjustment_add"))},
            {"项目": "手工纳税调减", "金额": F.fmt_money(settlement.get("manual_adjustment_reduce"))},
            {"项目": "研发费用加计扣除", "金额": F.fmt_money(settlement.get("research_deduction"))},
            {"项目": "弥补亏损前应纳税所得额", "金额": F.fmt_money(settlement.get("taxable_income_before_loss"))},
            {"项目": "弥补以前年度亏损", "金额": F.fmt_money(settlement.get("loss_used"))},
            {"项目": "应纳税所得额", "金额": F.fmt_money(settlement.get("taxable_income"))},
            {"项目": "适用税率", "金额": F.fmt_pct(settlement.get("applicable_rate"))},
            {"项目": "应纳所得税额", "金额": F.fmt_money(settlement.get("corporate_tax"))},
            {"项目": "减免所得税额", "金额": F.fmt_money(settlement.get("tax_reduction"))},
            {"项目": "本年已预缴", "金额": F.fmt_money(settlement.get("prepaid_tax"))},
            {"项目": payable_label, "金额": F.fmt_money(payable_value)},
        ]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        check = settlement.get("small_low_profit_check") or {}
        if check:
            st.caption(
                "小型微利判定："
                + ("符合条件（实际税负 5%）" if check.get("qualifies") else "不符合：" + "；".join(check.get("reasons") or []))
            )
    theme.disclaimer(settlement.get("disclaimer"))


def _render_annual_tab(year: str) -> None:
    ui.section(
        "📅 年度数据",
        "从台账汇总全年收入 / 费用 / 工资 / 研发费用 / 预缴税额，并补充纳税调整所需的发生额。",
    )
    if not SA.ledger_ready():
        ui.module_missing("年度台账汇总", "ledger.settlement", detail=SA.missing_detail())
    annual = _safe(SA.annual_ledger_data, {}, year)
    _render_annual_metrics(annual)
    for warning in annual.get("warnings") or []:
        st.caption(f"⚠️ {warning}")
    _render_monthly_detail(annual)
    theme.disclaimer()

    record = _safe(SA.settlement_record, {}, year)
    _render_inputs_form(year, annual, record)

    st.markdown("---")
    ui.section("🧮 汇算结果", "保存补充数据后点击「计算汇算结果」（确定性引擎计算，AI 不参与）。")
    action_cols = st.columns([2, 2, 3, 3])
    with action_cols[0]:
        if st.button(
            "🧮 计算汇算结果",
            key="settlement_compute",
            type="primary",
            width="stretch",
            disabled=not SA.compute_ready(),
        ):
            _compute_settlement(year)
    with action_cols[1]:
        status = str(record.get("status") or "草稿")
        if st.button(
            f"✅ 标记已确认（当前：{status}）" if status != "已确认" else "✅ 已确认",
            key="settlement_mark_confirmed",
            width="stretch",
            disabled=status == "已确认" or not SA.inputs_ready(),
        ):
            try:
                SA.mark_settlement_status(year, "已确认")
            except Exception as exc:  # noqa: BLE001
                st.error(f"标记已确认失败：{exc}")
            else:
                cache.clear()
                _flash(f"{SA.year_label(year)}汇算结果已标记为「已确认」")
                st.rerun()
    with action_cols[2]:
        st.caption(f"当前状态：{status} · 更新时间：{record.get('updated_at') or '—'}")
    with action_cols[3]:
        st.caption("汇算结果为辅助测算，请与电子税务局 A 类年度申报表核对。")

    settlement = _settlement_state(year)
    if settlement:
        _render_settlement_result(year, settlement)
    else:
        st.info("尚未计算汇算结果。保存补充数据后点击「计算汇算结果」。", icon="🧮")
    theme.disclaimer()


# =============================================================================
# Tab 2：纳税调整
# =============================================================================
def _render_limits_table(year: str, annual: dict) -> None:
    limits = SA.adjustment_limits(year, annual=annual)
    items = limits.get("items") or []
    ui.section(
        "📏 限额类自动调整（只读）",
        "业务招待费按发生额 60% 与营业收入 5‰ 孰低；广告费 15%；福利费 14%、教育经费 8%、工会经费 2%。",
    )
    if not items:
        st.info("暂无自动限额调整：请先在「年度数据」补充业务招待费、广告费或三项经费发生额。", icon="📏")
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "调整项目": item.get("label"),
                        "本年发生额": F.fmt_money(item.get("actual")),
                        "税前扣除限额": F.fmt_money(item.get("limit")),
                        "可扣除金额": F.fmt_money(item.get("deductible")),
                        "纳税调增": F.fmt_money(item.get("adjustment_add")),
                        "计算规则": item.get("rule"),
                        "政策依据": item.get("basis"),
                    }
                    for item in items
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        st.caption(f"限额类纳税调增合计：{F.fmt_money(limits.get('total_adjustment_add'))}")
        for warning in limits.get("warnings") or []:
            st.caption(f"⚠️ {warning}")


def _render_adjustment_list(year: str) -> None:
    rows = _safe(SA.list_adjustments, [], year)
    ui.section("✍️ 手工纳税调整清单", "限额类以外的调整（如罚款、非公益性捐赠、不征税收入等）。")
    if rows:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "编号": row.get("id"),
                        "调整项目": row.get("item_type"),
                        "方向": row.get("direction"),
                        "会计金额": F.fmt_money(row.get("accounting_amount")),
                        "税法金额": F.fmt_money(row.get("tax_amount")),
                        "调整金额": F.fmt_money(row.get("adjustment_amount")),
                        "计算依据": row.get("basis"),
                        "政策依据": row.get("source"),
                        "备注": row.get("note"),
                    }
                    for row in rows
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        total_add = sum(
            F.to_float(row.get("adjustment_amount"))
            for row in rows
            if str(row.get("direction")) == "调增"
        )
        total_reduce = sum(
            F.to_float(row.get("adjustment_amount"))
            for row in rows
            if str(row.get("direction")) == "调减"
        )
        st.caption(f"手工调增合计 {F.fmt_money(total_add)} · 手工调减合计 {F.fmt_money(total_reduce)}")
        cols = st.columns([3, 2, 1, 3])
        with cols[0]:
            ids = [str(row.get("id")) for row in rows]
            labels = {
                str(row.get("id")): f"{row.get('item_type')}（{row.get('direction')} {F.fmt_money(row.get('adjustment_amount'))}）"
                for row in rows
            }
            selected = st.selectbox(
                "选择要删除的调整项",
                ids,
                format_func=lambda adjustment_id: labels.get(str(adjustment_id), str(adjustment_id)),
                key="settlement_adjustment_select",
            )
        with cols[1]:
            st.write("")
            if st.button(
                "🗑️ 删除调整",
                key="settlement_adjustment_delete",
                width="stretch",
                disabled=not SA.adjustments_ready(),
            ):
                try:
                    SA.delete_adjustment(str(selected))
                except Exception as exc:  # noqa: BLE001
                    st.error(f"删除纳税调整失败：{exc}")
                else:
                    cache.clear()
                    _flash("纳税调整项已删除", "warning")
                    st.rerun()
    else:
        st.info("暂无手工纳税调整项。", icon="✍️")

    with st.expander("➕ 新增手工纳税调整", expanded=not rows):
        presets = SA.ADJUSTMENT_ITEM_PRESETS
        with st.form(f"settlement_adjustment_form_{year}", clear_on_submit=True):
            cols = st.columns(3)
            with cols[0]:
                preset = st.selectbox("调整项目", presets, index=0, key=f"settlement_adj_preset_{year}")
                custom = st.text_input(
                    "自定义项目名称（选择「其他纳税调整」时使用）",
                    key=f"settlement_adj_custom_{year}",
                )
                direction = st.selectbox(
                    "方向", SA.ADJUSTMENT_DIRECTIONS, index=0, key=f"settlement_adj_direction_{year}"
                )
            with cols[1]:
                adjustment_amount = st.number_input(
                    "调整金额（元）", min_value=0.0, step=1000.0, format="%.2f",
                    key=f"settlement_adj_amount_{year}",
                )
                accounting_amount = st.number_input(
                    "会计金额（元）", min_value=0.0, step=1000.0, format="%.2f",
                    key=f"settlement_adj_accounting_{year}",
                )
                tax_amount = st.number_input(
                    "税法可扣除/确认金额（元）", min_value=0.0, step=1000.0, format="%.2f",
                    key=f"settlement_adj_tax_{year}",
                )
            with cols[2]:
                basis = st.text_input("计算依据", key=f"settlement_adj_basis_{year}")
                source = st.text_input(
                    "政策依据（文号）", key=f"settlement_adj_source_{year}", placeholder="如：企业所得税法实施条例第XX条"
                )
                note = st.text_input("备注", key=f"settlement_adj_note_{year}")
            submitted = st.form_submit_button("添加调整项", type="primary", width="content")
        if submitted:
            item_type = custom.strip() if preset == "其他纳税调整" and custom.strip() else preset
            if adjustment_amount <= 0:
                st.error("调整金额必须大于 0。")
            else:
                try:
                    SA.add_adjustment(
                        year,
                        item_type,
                        direction,
                        float(adjustment_amount),
                        accounting_amount=float(accounting_amount),
                        tax_amount=float(tax_amount),
                        basis=basis.strip(),
                        source=source.strip(),
                        note=note.strip(),
                    )
                except Exception as exc:  # noqa: BLE001
                    st.error(f"添加纳税调整失败：{exc}")
                else:
                    cache.clear()
                    _flash(f"已添加纳税调整：{item_type}（{direction}）")
                    st.rerun()


def _render_adjustments_tab(year: str) -> None:
    annual = _safe(SA.annual_ledger_data, {}, year)
    _render_limits_table(year, annual)
    st.markdown("---")
    _render_adjustment_list(year)
    with st.expander("📚 纳税调整政策依据", expanded=False):
        for source in SA.sources():
            st.markdown(
                f"- **{source.get('title')}**\n"
                f"  来源：{source.get('source')} · 生效：{source.get('effective_date')} · 到期：{source.get('expiry_date')}"
            )
    theme.disclaimer()


# =============================================================================
# Tab 3：亏损弥补
# =============================================================================
def _render_loss_list(year: str) -> None:
    rows = _safe(SA.list_losses, [], None)
    ui.section(
        "📉 亏损弥补台账",
        f"一般企业亏损可结转弥补 {SA.loss_carryforward_years()} 年；超过期限不得弥补。",
    )
    if not rows:
        st.info("暂无亏损弥补记录。", icon="📉")
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "编号": row.get("id"),
                        "亏损年度": row.get("year"),
                        "可弥补亏损额": F.fmt_money(row.get("loss_amount")),
                        "已弥补金额": F.fmt_money(row.get("used_amount")),
                        "剩余可弥补": F.fmt_money(row.get("remaining")),
                        "到期年度": row.get("expire_year"),
                        "备注": row.get("note"),
                    }
                    for row in rows
                ]
            ),
            hide_index=True,
            width="stretch",
        )

    action_cols = st.columns([2, 2, 2, 2])
    with action_cols[0]:
        with st.expander("➕ 新增亏损记录", expanded=not rows):
            year_options = [str(int(SA.current_year()) - index) for index in range(0, 6)]
            with st.form(f"settlement_loss_form_{year}", clear_on_submit=True):
                loss_year = st.selectbox("亏损年度", year_options, index=0, key=f"settlement_loss_year_{year}")
                loss_amount = st.number_input(
                    "可弥补亏损额（元）", min_value=0.0, step=1000.0, format="%.2f",
                    key=f"settlement_loss_amount_{year}",
                )
                used_amount = st.number_input(
                    "已弥补金额（元）", min_value=0.0, step=1000.0, format="%.2f",
                    key=f"settlement_loss_used_{year}",
                )
                note = st.text_input("备注", key=f"settlement_loss_note_{year}")
                submitted = st.form_submit_button("添加亏损记录", type="primary", width="content")
            if submitted:
                if loss_amount <= 0:
                    st.error("可弥补亏损额必须大于 0。")
                else:
                    try:
                        SA.add_loss(loss_year, float(loss_amount), used_amount=float(used_amount), note=note.strip())
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"添加亏损记录失败：{exc}")
                    else:
                        cache.clear()
                        _flash(f"已添加 {loss_year} 年度亏损记录")
                        st.rerun()
    with action_cols[1]:
        with st.expander("✏️ 编辑已弥补金额", expanded=False):
            if not rows:
                st.caption("暂无可编辑的亏损记录。")
            else:
                ids = [str(row.get("id")) for row in rows]
                edit_labels = {
                    str(row.get("id")): f"{row.get('year')} 年亏损（剩余 {F.fmt_money(row.get('remaining'))}）"
                    for row in rows
                }
                with st.form(f"settlement_loss_edit_form_{year}"):
                    selected = st.selectbox(
                        "选择亏损记录",
                        ids,
                        format_func=lambda loss_id: edit_labels.get(str(loss_id), str(loss_id)),
                        key=f"settlement_loss_edit_select_{year}",
                    )
                    target = next((row for row in rows if str(row.get("id")) == str(selected)), {})
                    used = st.number_input(
                        "已弥补金额（元）",
                        min_value=0.0,
                        step=1000.0,
                        format="%.2f",
                        value=F.to_float(target.get("used_amount")),
                        key=f"settlement_loss_edit_used_{year}",
                    )
                    note = st.text_input(
                        "备注", value=str(target.get("note") or ""), key=f"settlement_loss_edit_note_{year}"
                    )
                    submitted = st.form_submit_button("保存修改", type="primary", width="content")
                if submitted:
                    try:
                        SA.update_loss(str(selected), {"used_amount": float(used), "note": note.strip()})
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"修改亏损记录失败：{exc}")
                    else:
                        cache.clear()
                        _flash("亏损记录已更新")
                        st.rerun()
    with action_cols[2]:
        with st.expander("🗑️ 删除亏损记录", expanded=False):
            if not rows:
                st.caption("暂无可删除的亏损记录。")
            else:
                ids = [str(row.get("id")) for row in rows]
                delete_labels = {str(row.get("id")): f"{row.get('year')} 年亏损" for row in rows}
                selected = st.selectbox(
                    "选择亏损记录",
                    ids,
                    format_func=lambda loss_id: delete_labels.get(str(loss_id), str(loss_id)),
                    key=f"settlement_loss_delete_select_{year}",
                )
                confirm = st.checkbox("确认删除该亏损记录", key=f"settlement_loss_delete_confirm_{year}")
                if st.button(
                    "删除",
                    key="settlement_loss_delete",
                    width="stretch",
                    disabled=not confirm or not SA.losses_ready(),
                ):
                    try:
                        SA.delete_loss(str(selected))
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"删除亏损记录失败：{exc}")
                    else:
                        cache.clear()
                        _flash("亏损记录已删除", "warning")
                        st.rerun()
    with action_cols[3]:
        st.caption("已弥补金额更新后，剩余可弥补由后端自动重算。")


def _render_loss_preview(year: str, annual: dict) -> None:
    settlement = _settlement_state(year) or {}
    taxable = settlement.get("taxable_income_before_loss")
    basis_note = "汇算结果中的「弥补亏损前应纳税所得额」"
    if taxable is None:
        taxable = annual.get("profit") or 0.0
        basis_note = "台账利润（尚未计算汇算结果，仅供参考）"
    preview = _safe(SA.loss_preview, {}, year, F.to_float(taxable))
    ui.section("🧮 本年度可弥补亏损测算", f"按本年度可弥补应纳税所得额测算（来源：{basis_note}）。")
    ui.metric_row(
        [
            {"label": "可弥补应纳税所得额", "value": F.fmt_money(taxable)},
            {"label": "本年可弥补亏损", "value": F.fmt_money(preview.get("total_used"))},
            {"label": "弥补后剩余亏损", "value": F.fmt_money(preview.get("remaining_loss"))},
            {"label": "弥补后应纳税所得额", "value": F.fmt_money(preview.get("available_after"))},
        ]
    )
    lines = preview.get("lines") or []
    if lines:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "亏损年度": line.get("year"),
                        "可弥补亏损额": F.fmt_money(line.get("loss_amount")),
                        "已弥补": F.fmt_money(line.get("used_amount")),
                        "剩余": F.fmt_money(line.get("remaining")),
                        "本年弥补": F.fmt_money(line.get("used_this_year")),
                        "剩余可弥补": F.fmt_money(line.get("remaining_after")),
                        "状态": line.get("status"),
                        "到期年度": line.get("expire_year"),
                    }
                    for line in lines
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    for warning in preview.get("warnings") or []:
        st.warning(str(warning))
    st.caption("亏损弥补顺序：按到期年度先后先进先出弥补；超过期限的亏损不可再弥补。")
    theme.disclaimer()


def _render_losses_tab(year: str) -> None:
    annual = _safe(SA.annual_ledger_data, {}, year)
    _render_loss_list(year)
    st.markdown("---")
    _render_loss_preview(year, annual)


# =============================================================================
# Tab 4：申报辅助表
# =============================================================================
def _bundle_value(value: Any, key: str) -> Any:
    if key in BUNDLE_MONEY_KEYS:
        try:
            return F.fmt_money(value)
        except Exception:  # noqa: BLE001
            return value
    if key in {"rate", "applicable_rate"}:
        try:
            return F.fmt_pct(float(value))
        except (TypeError, ValueError):
            return value
    return value


def _render_bundle_section(section: dict) -> None:
    lines = section.get("lines") or []
    title = section.get("title") or "明细"
    if not lines:
        return
    st.markdown(f"**{title}**")
    if "政策" in title or "资料" in title:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "政策依据 / 资料": line.get("name") or "",
                        "来源 / 文号": line.get("source") or "",
                        "说明": line.get("rule") or "",
                    }
                    for line in lines
                    if isinstance(line, dict)
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        return
    if any(isinstance(line, dict) and ("adjustment_amount" in line or "item_type" in line) for line in lines):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "调整项目": line.get("item_type") or line.get("name") or "",
                        "方向": line.get("direction") or "",
                        "会计金额": F.fmt_money(line.get("accounting_amount")),
                        "税法金额": F.fmt_money(line.get("tax_amount")),
                        "调整金额": F.fmt_money(line.get("adjustment_amount")),
                        "依据": line.get("basis") or line.get("rule") or "",
                        "政策依据": line.get("source") or "",
                    }
                    for line in lines
                    if isinstance(line, dict)
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        return
    if any(isinstance(line, dict) and ("loss_amount" in line or "expire_year" in line) for line in lines):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "亏损年度": line.get("year") or "",
                        "可弥补亏损额": F.fmt_money(line.get("loss_amount")),
                        "已弥补": F.fmt_money(line.get("used_amount")),
                        "本年弥补": F.fmt_money(line.get("used_this_year")),
                        "剩余": F.fmt_money(line.get("remaining_after", line.get("remaining"))),
                        "到期年度": line.get("expire_year") or "",
                        "状态": line.get("status") or "",
                    }
                    for line in lines
                    if isinstance(line, dict)
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        return
    if any(isinstance(line, dict) and "line_no" in line for line in lines):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "栏次": ("⭐ " if line.get("highlight") else "") + str(line.get("line_no") or ""),
                        "名称": line.get("name") or "",
                        "金额": F.fmt_money(line.get("value")),
                        "来源": line.get("source") or "",
                        "口径": line.get("rule") or "",
                    }
                    for line in lines
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        return
    records = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        record = {}
        for key, value in line.items():
            if key in {"highlight", "line_no"}:
                continue
            record[BUNDLE_FIELD_LABELS.get(key, key)] = _bundle_value(value, key)
        records.append(record)
    if records:
        st.dataframe(pd.DataFrame(records), hide_index=True, width="stretch")


def _render_annual_bundle(year: str) -> None:
    payload = st.session_state.get(STATE_BUNDLE) or {}
    if str(payload.get("year") or "") != str(year):
        return
    bundle = payload.get("bundle") or {}
    if not bundle:
        return
    st.caption(
        f"{bundle.get('form_name') or '企业所得税年度纳税申报辅助表'} · "
        f"{bundle.get('period_label') or SA.year_label(year)} · 生成时间：{bundle.get('generated_at') or '—'}"
    )
    summary = bundle.get("summary") or {}
    if summary:
        ui.status_alert(summary.get("level") or "PASS", summary.get("text") or "辅助表已生成。")
    totals = bundle.get("totals") or {}
    if totals:
        items = []
        for key, value in totals.items():
            if not isinstance(value, (int, float)):
                continue
            label = {
                "accounting_profit": "会计利润",
                "adjustment_add": "纳税调增合计",
                "adjustment_reduce": "纳税调减合计",
                "rd_deduction": "研发加计扣除",
                "loss_used": "弥补亏损",
                "taxable_income": "应纳税所得额",
                "corporate_tax": "应纳所得税",
                "prepaid_tax": "已预缴",
                "payable": "应补税额",
                "refund": "应退税额",
            }.get(key, key)
            items.append({"label": label, "value": F.fmt_money(value)})
            if len(items) >= 6:
                break
        if items:
            ui.metric_row(items)
    for section in bundle.get("sections") or []:
        _render_bundle_section(section)
    checks = bundle.get("checks") or []
    if isinstance(checks, list) and checks:
        st.markdown("**校验结果**")
        ui.status_table(checks)
    guide = bundle.get("guide") or []
    if guide:
        with st.expander("📖 电子税务局 A 类年度申报操作步骤", expanded=False):
            for step in guide:
                st.markdown(f"- {step}")
    sources = bundle.get("policy_sources") or SA.sources()
    if sources:
        with st.expander("📚 政策依据", expanded=False):
            for source in sources:
                st.markdown(
                    f"- **{source.get('title')}**\n"
                    f"  来源：{source.get('source')} · 生效：{source.get('effective_date')} · "
                    f"到期：{source.get('expiry_date')}"
                )
    theme.disclaimer(bundle.get("disclaimer"))


def _build_annual_bundle(year: str) -> None:
    settlement = _settlement_state(year)
    try:
        bundle = SA.build_cit_annual_return(year, settlement=settlement)
    except Exception as exc:  # noqa: BLE001
        st.error(f"生成年度纳税申报辅助表失败：{exc}")
        return
    st.session_state[STATE_BUNDLE] = {"year": str(year), "bundle": bundle}
    st.session_state.pop(STATE_EXPORT, None)
    cache.clear()
    _flash(f"{SA.year_label(year)}年度纳税申报辅助表已生成")
    st.rerun()


def _export_annual_bundle(year: str) -> None:
    payload = st.session_state.get(STATE_BUNDLE) or {}
    bundle = payload.get("bundle") or {}
    if str(payload.get("year") or "") != str(year) or not bundle:
        st.warning("请先生成当年度的纳税申报辅助表，再导出 Excel。")
        return
    try:
        path = SA.export_cit_annual_excel(bundle)
    except Exception as exc:  # noqa: BLE001
        st.error(f"导出 Excel 失败：{exc}")
        return
    st.session_state[STATE_EXPORT] = str(path)
    st.rerun()


def _render_annual_return_tab(year: str) -> None:
    ui.section(
        "📄 年度申报辅助表",
        "生成企业所得税年度纳税申报辅助表（A 类关键行次 + 调整明细 + 亏损明细）；"
        "**实际申报请在电子税务局完成**，本系统仅辅助核对。",
    )
    if not SA.annual_return_ready():
        ui.module_missing("年度申报辅助表", "filing.cit_annual", detail=SA.missing_detail())
    cols = st.columns([2, 2, 5])
    with cols[0]:
        if st.button(
            "📋 生成辅助表",
            key="settlement_return_build",
            type="primary",
            width="stretch",
            disabled=not SA.annual_return_ready(),
        ):
            _build_annual_bundle(year)
    with cols[1]:
        if st.button(
            "📊 生成 Excel",
            key="settlement_return_export",
            width="stretch",
            disabled=not SA.annual_export_ready(),
        ):
            _export_annual_bundle(year)
    with cols[2]:
        st.caption("Excel 含主表、纳税调整明细、亏损弥补明细、政策依据与操作步骤。")
    export_path = st.session_state.get(STATE_EXPORT)
    if export_path and Path(str(export_path)).exists():
        path = Path(str(export_path))
        st.download_button(
            "⬇️ 下载年度汇算辅助表 Excel",
            data=path.read_bytes(),
            file_name=path.name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="settlement_return_download",
            width="content",
        )
    _render_annual_bundle(year)
    if not (st.session_state.get(STATE_BUNDLE) or {}):
        st.info(
            "点击「生成辅助表」后，可查看 A 类关键行次、纳税调整明细、亏损弥补明细、校验结果与操作步骤。",
            icon="📄",
        )
    st.caption(
        "说明：本系统不代替电子税务局申报。请于次年 5 月 31 日前完成 A 类年度申报，"
        "留存备查资料至少 5 年（研发加计、亏损弥补、资产损失等）。"
    )
    theme.disclaimer()


# =============================================================================
# Tab 5：检查清单
# =============================================================================
def _render_checklist_table(items: Sequence[dict]) -> None:
    if not items:
        st.caption("暂无检查项。")
        return
    frame = pd.DataFrame(
        [
            {
                "状态": str(item.get("status") or "").upper() if item.get("status") in {"PASS", "WARNING", "ERROR"} else str(item.get("status") or ""),
                "检查项": item.get("item"),
                "说明": item.get("message"),
                "建议操作": item.get("action") or "—",
            }
            for item in items
        ]
    )

    def _style(value: Any) -> str:
        text = str(value)
        if text == "PASS":
            return "background-color: #E8F5E9; color: #1B5E20; font-weight: 600;"
        if text == "WARNING":
            return "background-color: #FFF8E1; color: #8A6412; font-weight: 600;"
        if text == "ERROR":
            return "background-color: #FDECEA; color: #B3261E; font-weight: 600;"
        return ""

    try:
        styled = frame.style.map(_style, subset=["状态"])
    except AttributeError:  # pragma: no cover
        styled = frame.style.applymap(_style, subset=["状态"])
    st.dataframe(styled, hide_index=True, width="stretch")


def _render_checklist_tab(year: str) -> None:
    ui.section(
        "✅ 汇算检查清单",
        "逐项检查数据完整性、预缴核对、小型微利条件与资料留存，完成后再到电子税务局申报。",
    )
    settlement = _settlement_state(year) or {}
    if not settlement:
        st.info("请先在「📅 年度数据」点击「计算汇算结果」，再查看检查清单。", icon="✅")
        if st.button(
            "🧮 现在计算汇算结果",
            key="settlement_checklist_compute",
            type="primary",
            disabled=not SA.compute_ready(),
        ):
            _compute_settlement(year)
        return
    items = _safe(SA.settlement_checklist, [], year, settlement)
    if items:
        counts = {
            level: sum(1 for item in items if str(item.get("status")) == level)
            for level in ("PASS", "WARNING", "ERROR")
        }
        ui.metric_row(
            [
                {"label": "通过", "value": f"{counts['PASS']} 项"},
                {"label": "提示", "value": f"{counts['WARNING']} 项"},
                {"label": "需修复", "value": f"{counts['ERROR']} 项"},
            ],
            columns=3,
        )
        _render_checklist_table(items)
        if counts["ERROR"]:
            st.error("存在 ERROR 级问题，请先修复后再到电子税务局申报。")
        elif counts["WARNING"]:
            st.warning("存在提示项，建议人工复核后申报。")
        else:
            st.success("检查清单全部通过，可到电子税务局完成年度申报。", icon="✅")
    else:
        st.info("暂无检查项（后端未返回）。", icon="✅")

    with st.expander("📁 汇算资料留存清单（备查至少 5 年）", expanded=True):
        st.markdown(
            "- 年度财务报表（资产负债表、利润表）\n"
            "- 企业所得税年度纳税申报表（A 类）及附表\n"
            "- 纳税调整明细及计算依据（业务招待费、广告费、三项经费限额计算表）\n"
            "- 研发费用辅助账、研发项目立项与工时记录（享受加计扣除时）\n"
            "- 以前年度亏损弥补台账及亏损年度申报表\n"
            "- 预缴税款凭证（季度预缴申报表、完税凭证）\n"
            "- 小型微利企业条件证明材料（从业人数、资产总额）\n"
            "- 其他：资产损失资料、不征税收入文件、减免税备案/留存资料"
        )
    with st.expander("📚 政策依据", expanded=False):
        for source in SA.sources():
            st.markdown(
                f"- **{source.get('title')}**\n"
                f"  来源：{source.get('source')} · 生效：{source.get('effective_date')} · "
                f"到期：{source.get('expiry_date')}"
            )
    theme.disclaimer()


# =============================================================================
# 页面入口
# =============================================================================
def render() -> None:
    """渲染「📊 汇算清缴」页面。"""
    ui.page_header(
        "汇算清缴",
        "企业所得税年度汇算：年度数据 → 纳税调整 → 亏损弥补 → 年度申报辅助表 → 检查清单（不代替电子税务局申报）。",
        icon="📊",
        right_text=f"汇算截止：次年 5 月 31 日 · 当前年度 {SA.current_year()}",
    )
    _show_flash()
    if not SA.any_ready():
        ui.module_missing("汇算清缴", "ledger.settlement / filing.cit_annual", detail=SA.missing_detail())
    year = st.selectbox(
        "汇算年度",
        SA.year_options(6),
        index=0,
        format_func=SA.year_label,
        key=YEAR_KEY,
    )
    tabs = st.tabs(
        ["📅 年度数据", "✍️ 纳税调整", "📉 亏损弥补", "📄 申报辅助表", "✅ 检查清单"]
    )
    with tabs[0]:
        _render_annual_tab(year)
    with tabs[1]:
        _render_adjustments_tab(year)
    with tabs[2]:
        _render_losses_tab(year)
    with tabs[3]:
        _render_annual_return_tab(year)
    with tabs[4]:
        _render_checklist_tab(year)
