"""🏠 首页驾驶舱：企业名片、季度核心指标、申报提醒、收入趋势、台账笔数、最近业务、快捷入口。"""

from __future__ import annotations

from datetime import datetime

import config
import streamlit as st

from engine import current_quarter, deadline_status, format_date_cn, next_deadline, reminders
from ledger.models import mask_client
from ui.components import cache, charts, formatting as F, ledger_api, nav, theme, ui


def _load_dashboard() -> dict:
    """读取驾驶舱数据；失败时降级为可渲染的空结构。"""
    try:
        return cache.dashboard()
    except Exception as exc:  # noqa: BLE001
        ui.failure("驾驶舱数据读取失败，已降级为空数据展示。", exc)
        return {}


def _deadline(data: dict) -> dict:
    """最近申报截止事项（优先取台账汇总，其次 engine 实时计算）。"""
    item = data.get("next_deadline") or {}
    if not isinstance(item, dict) or not item:
        try:
            item = next_deadline() or {}
        except Exception:  # noqa: BLE001
            item = {}
    return item


def _deadline_metric(data: dict) -> dict:
    item = _deadline(data)
    if not item:
        return {"label": "申报倒计时", "value": "—", "delta": "暂无申报事项"}
    status = deadline_status(item.get("due_date"), config.today()) if item.get("due_date") else {}
    label = status.get("label") or item.get("label") or "—"
    delta = f"截止 {item.get('due_date_cn') or format_date_cn(item.get('due_date'))}"
    return {
        "label": "申报倒计时",
        "value": label,
        "delta": delta,
        "help": f"{item.get('name', '申报事项')}（税款所属期：{item.get('tax_period_label') or '—'}）",
    }


def _render_reminders(data: dict) -> None:
    items = data.get("reminders")
    if not isinstance(items, list) or not items:
        try:
            items = reminders(lookahead_days=45)
        except Exception:  # noqa: BLE001
            items = []
    ui.section("🔔 申报提醒", "按申报日历计算，遇周末顺延；法定节假日以税务机关公告为准。")
    if not items:
        st.success("近期没有即将到期的申报事项，保持关注即可。", icon="✅")
        return
    for item in items[:4]:
        days_left = item.get("days_left")
        label = item.get("label") or ""
        with st.container(border=True):
            left, middle, right = st.columns([3, 2, 2])
            with left:
                st.markdown(f"**{item.get('name') or '申报事项'}**")
                st.caption(item.get("description") or "")
            with middle:
                st.markdown(f"截止：{item.get('due_date_cn') or format_date_cn(item.get('due_date'))}")
                st.caption(f"税款所属期：{item.get('tax_period_label') or '—'}")
            with right:
                if days_left is not None and int(days_left) <= 3:
                    st.warning(label, icon="⚠️")
                else:
                    st.info(label, icon="📅")
                if st.button("去填报", key=f"dash_remind_{item.get('key')}_{item.get('due_date')}", width="stretch"):
                    nav.goto(nav.PAGE_FILING)


def _render_trend(data: dict) -> None:
    period = str(data.get("period") or current_quarter())
    try:
        year = int(period[:4])
    except ValueError:
        year = config.today().year
    rows: list[dict] = []
    try:
        rows = cache.monthly_summary(year)
    except Exception as exc:  # noqa: BLE001
        ui.failure("收入趋势数据读取失败。", exc, show_traceback=False)
    ui.section("📈 收入趋势", f"{year} 年逐月收入 / 费用 / 利润（元，按台账口径）。")
    has_data = any(
        any(float(row.get(key) or 0) for key in ("income_inclusive", "expense"))
        for row in rows
    )
    if not rows or not has_data:
        charts.empty_chart_hint("暂无月度数据，先录入收入 / 费用台账或加载示例数据。")
        return
    with st.container(border=True):
        charts.show(charts.income_trend(rows), alt=f"{year} 年月度收入、费用与利润趋势图")
        st.caption("口径说明：收入为不含税 / 含税销售额；费用按费用台账合计；利润为估算值。")
        theme.disclaimer()


def _render_counts(data: dict) -> None:
    counts = data.get("counts") or {}
    ui.section("🗂️ 台账笔数", "各台账当前记录数（数据全部保存在本地 CSV）。")
    ui.metric_row(
        [
            {"label": "收入台账（笔）", "value": f"{int(counts.get('income', 0))}", "icon": ":material/receipt_long:"},
            {"label": "费用台账（笔）", "value": f"{int(counts.get('expense', 0))}", "icon": ":material/payments:"},
            {"label": "研发辅助账（笔）", "value": f"{int(counts.get('rd', 0))}", "icon": ":material/science:"},
            {"label": "合同台账（笔）", "value": f"{int(counts.get('contracts', 0))}", "icon": ":material/description:"},
        ]
    )


def _recent_rows(rows, columns: list[tuple[str, str]]) -> list[dict]:
    """最近记录脱敏（隐私模式下客户名打码）。"""
    privacy = config.is_privacy_mode()
    result = []
    for row in rows or []:
        item = dict(row)
        if privacy and item.get("client"):
            item["client"] = mask_client(str(item["client"]))
        result.append(item)
    return result


def _render_recent(data: dict) -> None:
    ui.section("🧾 最近业务记录", "最近 5 笔收入与费用，按记账时间倒序展示。")
    left, right = st.columns(2)
    with left:
        with st.container(border=True):
            st.markdown("**最近收入**")
            rows = _recent_rows(data.get("recent_income"), [])
            if not rows:
                rows = _recent_rows(ledger_api.list_income()[-5:][::-1] if not ledger_api.is_empty() else [], [])
            ui.records_table(
                rows,
                [
                    ("date", "日期"),
                    ("client", "客户"),
                    ("description", "业务描述"),
                    ("inclusive_amount", "含税金额"),
                    ("invoice_type", "发票类型"),
                ],
                key="dash_recent_income",
                empty_text="暂无收入记录",
                money_fields={"inclusive_amount"},
            )
            if st.button("新增收入", key="dash_add_income", width="stretch"):
                nav.goto(nav.PAGE_LEDGER)
    with right:
        with st.container(border=True):
            st.markdown("**最近费用**")
            rows = _recent_rows(data.get("recent_expense"), [])
            if not rows:
                rows = _recent_rows(ledger_api.list_expenses()[-5:][::-1] if not ledger_api.is_empty() else [], [])
            ui.records_table(
                rows,
                [
                    ("date", "日期"),
                    ("description", "费用描述"),
                    ("category", "类别"),
                    ("amount", "金额"),
                    ("vendor", "供应商"),
                ],
                key="dash_recent_expense",
                empty_text="暂无费用记录",
                money_fields={"amount"},
            )
            if st.button("新增费用", key="dash_add_expense", width="stretch"):
                nav.goto(nav.PAGE_LEDGER)


def _render_quick_entries() -> None:
    ui.section("⚡ 快捷入口", "一键进入常用功能。")
    columns = st.columns(4)
    entries = [
        ("📒", "记一笔台账", nav.PAGE_LEDGER, "dash_quick_ledger"),
        ("🧮", "算增值税", nav.PAGE_CALCULATOR, "dash_quick_calc"),
        ("📋", "生成申报表", nav.PAGE_FILING, "dash_quick_filing"),
        ("🤖", "问 AI 助手", nav.PAGE_ASSISTANT, "dash_quick_ai"),
    ]
    for column, (icon, label, page, key) in zip(columns, entries):
        with column:
            if st.button(f"{icon} {label}", key=key, width="stretch", type="secondary"):
                nav.goto(page)


def render() -> None:
    """渲染首页驾驶舱。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    ui.page_header(
        "首页驾驶舱",
        f"{config.APP_SHORT_NAME} · 小规模纳税人季度税务总览（本地数据，不出内网）",
        icon="🏠",
        right_text=f"当前时间：{now}",
    )

    company = ui.company_snapshot()
    theme.company_card(company)

    if ledger_api.is_empty():
        ui.empty_state(
            "还没有台账数据",
            "可以手动录入，也可以一键加载 2026 年 Q1—Q4 示例数据（含收入、费用、研发、合同与申报记录）。",
            icon="🗂️",
            seed_key="dash_seed_demo",
        )

    data = _load_dashboard()
    period_label = ledger_api.quarter_label(data.get("period") or current_quarter())

    ui.section("📊 本季度核心指标", f"税款所属期：{period_label}（金额单位：元）")
    ui.metric_row(
        [
            {
                "label": "不含税收入",
                "value": F.fmt_money(data.get("quarter_income_exclusive")),
                "delta": f"含税 {F.fmt_money(data.get('quarter_income_inclusive'))}",
                "icon": ":material/account_balance_wallet:",
            },
            {
                "label": "增值税",
                "value": F.fmt_money(data.get("vat_payable")),
                "icon": ":material/receipt:",
            },
            {
                "label": "附加税费",
                "value": F.fmt_money(data.get("surtax")),
                "icon": ":material/percent:",
            },
            {
                "label": "合计税费",
                "value": F.fmt_money(data.get("total_tax")),
                "icon": ":material/savings:",
            },
            _deadline_metric(data),
        ]
    )
    st.caption(
        f"本年累计：不含税收入 {F.fmt_money(data.get('ytd_income'))} · "
        f"费用 {F.fmt_money(data.get('ytd_expense'))} · "
        f"利润 {F.fmt_money(data.get('ytd_profit'))}"
    )
    theme.disclaimer()

    _render_reminders(data)
    theme.disclaimer()

    _render_trend(data)
    _render_counts(data)
    _render_recent(data)
    _render_quick_entries()

    st.caption(
        f"数据更新时间：{data.get('updated_at') or '—'} · "
        f"所有数据保存在本地目录 {config.DATA_DIR}"
    )
