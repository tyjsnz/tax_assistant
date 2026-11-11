"""🏠 首页驾驶舱：企业名片、季度核心指标、申报提醒、收入趋势、台账笔数、最近业务、快捷入口。"""

from __future__ import annotations

from datetime import datetime

import config
import streamlit as st

from engine import current_quarter, deadline_status, format_date_cn, next_deadline, reminders
from ledger.models import mask_client
from ui.components import archive_api as AA, cache, charts, formatting as F, ledger_api, nav, payroll_api as PA, theme, ui


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


# =============================================================================
# 本月工资与个税提醒（P1-B，可选卡片）
# =============================================================================
def _render_payroll_card() -> None:
    """本月工资表 / 个税扣缴提醒卡片（无员工且无工资记录时不展示）。"""
    try:
        employees = PA.list_employees(status="在职")
        month = PA.current_month_key()
        rows = PA.list_payroll(month)
    except Exception:  # noqa: BLE001 - 工资模块异常不影响驾驶舱
        return
    if not employees and not rows:
        return
    summary = PA.summarize_period(month)
    paid_rows = [row for row in rows if str(row.get("employee_id")) != PA.ZERO_EMPLOYEE_ID]
    if not rows:
        status_text = "⬜ 未生成"
    elif any(str(row.get("status")) == "草稿" for row in rows):
        status_text = "📝 草稿待确认"
    elif paid_rows and all(str(row.get("status")) == "已申报" for row in paid_rows):
        status_text = "📤 已申报"
    else:
        status_text = "✅ 已确认"
    ui.section("💰 本月工资与个税", f"{PA.month_label(month)} 工资表与个税扣缴进度。")
    with st.container(border=True):
        ui.metric_row(
            [
                {"label": "在职员工", "value": f"{len(employees)} 人"},
                {"label": "工资表状态", "value": status_text},
                {"label": "本期应预扣个税", "value": F.fmt_money(summary.get("total_tax"))},
                {"label": "实发合计", "value": F.fmt_money(summary.get("total_net"))},
            ],
            columns=4,
        )
        if not rows:
            st.warning(
                f"{PA.month_label(month)} 尚未生成工资表：请到「员工与工资」生成工资表；"
                "若本月未发工资，建议做零申报以保留减除费用额度。",
                icon="💰",
            )
        elif any(str(row.get("status")) == "草稿" for row in rows):
            st.info("工资表存在草稿行：请核对金额、重新计算个税并确认生成凭证。", icon="📝")
        elif paid_rows and all(str(row.get("status")) == "已申报" for row in paid_rows):
            st.success(f"{PA.month_label(month)} 工资表已申报完成。", icon="📤")
        else:
            st.info("工资表已确认，请在扣缴端完成申报后回到「员工与工资」标记已申报。", icon="✅")
        if st.button("👥 去员工与工资", key="dash_go_payroll", width="content"):
            nav.goto(nav.PAGE_PAYROLL)
    theme.disclaimer()


# =============================================================================
# 期间结账卡片（P0-3）
# =============================================================================
CLOSE_DIALOG_KEY = "_dash_close_dialog_period"
REOPEN_OPEN_KEY = "_dash_reopen_confirm_open"

PERIOD_STATUS_ICONS = {
    "未结账": "⬜",
    "已结账": "🔒",
    "已申报": "📤",
    "已归档": "🗄️",
}


def _period_status_icon(status: str) -> str:
    return PERIOD_STATUS_ICONS.get(str(status or "").strip(), "ℹ️")


def _recent_month_options(current: str, count: int = 6) -> list[str]:
    """最近若干月份（新的在前），用于结账期间选择。"""
    options = [current] if current else []
    cursor = current
    for _ in range(max(count - 1, 0)):
        cursor = AA.prev_month(cursor)
        if not cursor:
            break
        options.append(cursor)
    return options


@st.dialog("期间结账检查")
def _close_period_dialog(period: str) -> None:
    """结账确认弹窗：展示预结账检查清单，ERROR 时禁用确认。"""
    st.caption(f"结账期间：{AA.month_label(period)}（{period}）")
    if not (AA.closing_checks_ready() and AA.closing_ready()):
        ui.module_missing("期间结账", "closing.checks / closing.close", detail=AA.missing_detail())
        return
    try:
        checks = AA.pre_close_checks(period)
    except Exception as exc:  # noqa: BLE001
        st.error(f"预结账检查失败：{exc}")
        return
    level = str(checks.get("level") or "WARNING")
    details = []
    for item in checks.get("checks") or []:
        message = str(item.get("message") or "")
        hint = str(item.get("fix_hint") or "")
        details.append(
            {
                "status": item.get("status"),
                "field": item.get("name") or item.get("key") or "检查项",
                "expected": "",
                "actual": "",
                "message": f"{message}；建议：{hint}" if hint and hint not in message else message,
            }
        )
    if details:
        ui.status_table(details)
    if level == "ERROR":
        st.error("存在 ERROR 级问题，请先按提示修复后再结账（ERROR 不可强制结账）。")
    elif level == "WARNING":
        st.warning("存在 WARNING 级提示，建议人工确认后继续结账。")
    else:
        st.success("检查通过，可以结账。")
    if checks.get("disclaimer"):
        theme.disclaimer(checks.get("disclaimer"))
    note = st.text_input("结账备注（可选）", key=f"dash_close_note_{period}", placeholder="如：2026-10 月结")
    if st.button(
        "确认结账",
        key=f"dash_close_confirm_{period}",
        type="primary",
        width="stretch",
        disabled=level == "ERROR",
    ):
        try:
            result = AA.close_period(period, note=note)
        except Exception as exc:  # noqa: BLE001
            st.error(str(exc))
        else:
            cache.clear()
            entries = result.get("entries") or []
            message = f"{AA.month_label(period)} 已结账。"
            if entries:
                message += f"生成 {len(entries)} 张损益结转凭证。"
            ui.flash(message)
            st.session_state.pop(CLOSE_DIALOG_KEY, None)
            st.rerun()
    if st.button("取消", key=f"dash_close_cancel_{period}", width="stretch"):
        st.session_state.pop(CLOSE_DIALOG_KEY, None)
        st.rerun()


def _render_closing_card() -> None:
    """期间结账卡片：当前/上月状态、锁概览、待结账提醒、结账与反结账入口。"""
    ui.section(
        "🔒 期间结账",
        "结账后该期间台账与凭证被锁定，保证已出报表、申报表与归档包与账面一致；"
        "如需调整请先反结账（仅最近一个已结账期间可反结）。",
    )
    lock = AA.lock_summary()
    current = str(lock.get("current_month") or AA.current_month())
    previous = AA.prev_month(current)
    latest = str(lock.get("latest_closed_month") or "")
    current_status = AA.period_status(current)
    previous_status = AA.period_status(previous) if previous else "未结账"

    with st.container(border=True):
        cols = st.columns(4)
        with cols[0]:
            st.metric(
                f"当前月（{AA.month_label(current)}）",
                f"{_period_status_icon(current_status)} {current_status}",
                border=False,
                width="stretch",
            )
        with cols[1]:
            st.metric(
                f"上月（{AA.month_label(previous) if previous else '—'}）",
                f"{_period_status_icon(previous_status)} {previous_status}",
                border=False,
                width="stretch",
            )
        with cols[2]:
            st.metric(
                "最近已结账月份",
                AA.month_label(latest) + f"（{latest}）" if latest else "—",
                border=False,
                width="stretch",
            )
        with cols[3]:
            st.metric(
                "当前月记账状态",
                "🔒 已锁定" if lock.get("current_locked") else "✅ 可记账",
                border=False,
                width="stretch",
            )
        if lock.get("disclaimer"):
            st.caption(str(lock.get("disclaimer")))
        locked_rows = lock.get("locked_periods") or []
        if locked_rows:
            st.caption(
                "已锁定期间："
                + "、".join(f"{row.get('period')}（{row.get('status')}）" for row in locked_rows[:8])
            )
        if previous and previous_status == "未结账":
            st.info(
                f"📌 待结账提醒：{AA.month_label(previous)} 尚未结账。"
                "完成当月记账后建议及时结账，以锁定报表口径并避免跨期改动。",
                icon="📌",
            )
        elif latest:
            st.success(f"最近已结账期间：{AA.month_label(latest)}（{latest}），该期间已锁定。", icon="🔒")

    close_missing = not (AA.closing_checks_ready() and AA.closing_ready())
    options = _recent_month_options(current)
    control_cols = st.columns([2, 2, 2, 4])
    with control_cols[0]:
        target = st.selectbox(
            "结账期间",
            options,
            index=1 if len(options) > 1 else 0,
            format_func=AA.month_label,
            key="dash_close_target",
        )
    with control_cols[1]:
        st.write("")
        if st.button(
            "🔒 立即结账",
            key="dash_close_period",
            type="primary",
            width="stretch",
            disabled=close_missing,
            help="展示预结账检查清单，确认后生成损益结转凭证并锁定期间",
        ):
            st.session_state[CLOSE_DIALOG_KEY] = target
            st.rerun()
    with control_cols[2]:
        st.write("")
        if st.button(
            "🔓 反结账",
            key="dash_reopen_period",
            width="stretch",
            disabled=(not AA.closing_ready()) or not latest,
            help="仅最近一个已结账期间可反结账；会删除该期间的损益结转凭证",
        ):
            st.session_state[REOPEN_OPEN_KEY] = latest
            st.rerun()
    with control_cols[3]:
        st.caption("结账前会展示 pre_close_checks 检查清单；ERROR 项会阻止结账。反结账需二次确认。")
    if close_missing:
        ui.module_missing("期间结账", "closing.checks / closing.close", detail=AA.missing_detail())

    reopen_target = st.session_state.get(REOPEN_OPEN_KEY)
    if reopen_target:
        with st.container(border=True):
            st.warning(
                f"反结账将删除 {AA.month_label(reopen_target)}（{reopen_target}）的损益结转凭证"
                "并解锁该期间，是否继续？",
                icon="⚠️",
            )
            reason = st.text_input("反结账原因（可选）", key="dash_reopen_reason")
            confirm_cols = st.columns(2)
            with confirm_cols[0]:
                if st.button("确认反结账", key="dash_reopen_confirm", type="primary", width="stretch"):
                    try:
                        AA.reopen_period(reopen_target, reason=reason)
                    except Exception as exc:  # noqa: BLE001
                        st.error(str(exc))
                    else:
                        cache.clear()
                        st.session_state.pop(REOPEN_OPEN_KEY, None)
                        ui.flash(f"{AA.month_label(reopen_target)} 已反结账，期间已解锁。")
                        st.rerun()
            with confirm_cols[1]:
                if st.button("取消", key="dash_reopen_cancel", width="stretch"):
                    st.session_state.pop(REOPEN_OPEN_KEY, None)
                    st.rerun()

    # 弹窗需要跨 rerun 保持打开，直到用户确认 / 取消（否则按钮事件会丢失）
    pending = st.session_state.get(CLOSE_DIALOG_KEY)
    if pending:
        _close_period_dialog(str(pending))
    theme.disclaimer()


def _render_onboarding_banner() -> None:
    """首次使用引导入口：未完成新手引导时在首页提示。"""
    try:
        from onboarding import state as onboarding
        from ui.components import company_api

        company_id = company_api.active_company_id()
        if onboarding.is_complete(company_id):
            return
        done = onboarding.completed_count(company_id)
    except Exception:  # noqa: BLE001 - 引导不可用不影响驾驶舱
        return
    with st.container(border=True):
        cols = st.columns([5, 2])
        with cols[0]:
            st.markdown("#### 🚀 第一次使用？先完成 5 步新手引导")
            st.caption(
                f"已完成 {done}/5 步：公司信息 → 税务基础 → 第一笔账 → 第一次申报 → 完成。"
                "引导会帮你确认税务登记/税种认定/三方协议，避免一上来就报错。"
            )
        with cols[1]:
            if st.button("开始/继续引导 →", key="dashboard_onboarding", type="primary", width="stretch"):
                nav.goto(nav.PAGE_ONBOARDING)


def _render_todo_card() -> None:
    """本月/本季度必做：按时间倒排，给出截止日、剩余天数与去处理入口。"""
    try:
        from engine.tax_calendar import upcoming_deadlines
        from engine.calendar_export import build_ics

        deadlines = [
            item for item in upcoming_deadlines(lookahead_days=45, include_overdue=True)
            if not item.get("is_overdue") or int(item.get("days_left") or 0) >= -30
        ]
    except Exception:  # noqa: BLE001 - 日历不可用不影响驾驶舱
        return
    if not deadlines:
        return
    urgent = [item for item in deadlines if str(item.get("level")) in ("overdue", "urgent")]
    soon = [item for item in deadlines if str(item.get("level")) == "soon"]
    with st.container(border=True):
        head_cols = st.columns([5, 2])
        with head_cols[0]:
            st.markdown("#### ✅ 本月 / 本季度必做")
            if urgent:
                st.warning(
                    f"有 {len(urgent)} 项已到期/即将到期：" +
                    "、".join(str(item.get("name")) for item in urgent[:3]) +
                    (" 等" if len(urgent) > 3 else ""),
                    icon="⏰",
                )
            elif soon:
                st.warning(f"有 {len(soon)} 项将在 7 天内到期，建议提前处理。", icon="⏳")
            else:
                st.caption("以下是最近 45 天内的申报事项；完成后在对应页面确认状态。")
        with head_cols[1]:
            try:
                ics = build_ics(deadlines)
                st.download_button(
                    "📅 导出到手机日历",
                    data=ics.encode("utf-8"),
                    file_name="智小税_申报日历.ics",
                    mime="text/calendar",
                    key="dashboard_ics_download",
                    width="stretch",
                    help="导入手机/电脑日历后，可由系统日历提前提醒（需保持日历同步）",
                )
            except Exception as exc:  # noqa: BLE001
                st.caption(f"日历导出不可用：{exc}")
        for index, item in enumerate(deadlines[:6]):
            status = str(item.get("label") or "")
            level = str(item.get("level") or "normal")
            icon = "🔴" if level == "overdue" else ("🟠" if level == "urgent" else ("🟡" if level == "soon" else "🟢"))
            row = st.columns([5, 2, 2])
            with row[0]:
                st.markdown(f"**{icon} {item.get('name')}**")
                st.caption(f"税款所属期：{item.get('tax_period_label') or item.get('tax_period') or '—'}")
            with row[1]:
                st.caption(f"截止：{item.get('due_date')}")
                st.caption(status)
            with row[2]:
                page = nav.PAGE_PAYROLL if item.get("key") == "personal_tax_monthly" else nav.PAGE_FILING
                if st.button("去处理 →", key=f"todo_goto_{item.get('key')}_{index}", width="stretch"):
                    nav.goto(page)
        if len(deadlines) > 6:
            st.caption(f"还有 {len(deadlines) - 6} 项未展示；可导出日历或到「📋 申报填报」查看。")
        st.caption(
            "提醒说明：本系统只在打开时提示，不会在关闭后推送；"
            "建议点「导出到手机日历」，让手机系统日历提前 3 天提醒。"
        )


def _render_zero_and_late_card(data: dict) -> None:
    """零申报提醒 + 滞纳金试算（只提示风险，不制造恐慌）。"""
    with st.container(border=True):
        st.markdown("#### 🗓️ 零申报与逾期风险")
        income = float(data.get("quarter_income_exclusive") or 0)
        if income <= 0:
            st.warning(
                "本季度暂无收入记录：如果确实没有业务，也要按期做零申报（增值税及附加、"
                "个税无员工也要零申报），不能“没收入就不管”。",
                icon="⚠️",
            )
        else:
            st.caption(f"本季度不含税收入 {F.fmt_money(income)} 元；有收入也要按期申报，免税不等于免申报。")
        cols = st.columns([2, 2, 2])
        with cols[0]:
            if st.button("📖 看「首次报税全流程」", key="dash_goto_first_filing", width="stretch"):
                st.session_state["guide_workflow"] = "🆕 首次报税全流程"
                nav.goto(nav.PAGE_GUIDE)
        with cols[1]:
            if st.button("🗓️ 查看申报日历", key="dash_goto_calendar", width="stretch"):
                nav.goto(nav.PAGE_FILING)
        with cols[2]:
            st.caption("零申报也要提交并确认“申报成功”。")
        with st.expander("🧮 滞纳金试算（仅供参考）", expanded=False):
            from datetime import date as _date

            late_cols = st.columns(3)
            with late_cols[0]:
                amount = st.number_input("滞纳税款（元）", min_value=0.0, value=0.0, step=100.0, key="late_fee_amount")
            with late_cols[1]:
                due = st.date_input("缴款期限", value=_date.today(), key="late_fee_due")
            with late_cols[2]:
                paid = st.date_input("实际/预计缴款日", value=_date.today(), key="late_fee_paid")
            if st.button("计算滞纳金", key="late_fee_calc", width="stretch"):
                try:
                    from engine.late_fee import calc_late_fee

                    result = calc_late_fee(amount, due, paid)
                    if result["days"] <= 0:
                        st.success("按所选日期，未产生滞纳金。")
                    else:
                        st.error(
                            f"逾期 {result['days']} 天，滞纳金约 {F.fmt_money(result['late_fee'])} 元，"
                            f"合计应缴约 {F.fmt_money(result['total'])} 元。{result['disclaimer']}"
                        )
                except Exception as exc:  # noqa: BLE001
                    st.error(f"计算失败：{exc}")
            st.caption("滞纳金 = 滞纳税款 × 0.05% × 滞纳天数；实际以主管税务机关核定为准，不替代专业意见。")


def _render_hr_anomaly_card() -> None:
    """首页「离职与申诉异常」提醒卡片：汇总离职多申报、缺离职日期、待处理申诉。"""
    try:
        from ledger import corrections as corrections_module
        from ledger import employees as employees_module

        scan = corrections_module.scan_departed_overpayments()
        missing = employees_module.employees_without_leave_date()
        disputes = corrections_module.list_disputes()
    except Exception:  # noqa: BLE001 - 异常数据不影响驾驶舱
        return
    summary = scan.get("summary") or {}
    dispute_summary = corrections_module.summarize_disputes(disputes)
    pending = int(dispute_summary.get("pending") or 0)
    departed_people = int(summary.get("employees") or 0)
    departed_months = int(summary.get("months") or 0)
    missing_count = len(missing)
    if not (departed_people or missing_count or pending):
        st.caption("✅ 离职与申诉异常：未发现离职后仍申报、缺离职日期或待处理申诉。")
        return
    st.warning(
        f"检测到离职/申诉异常：离职后仍申报 {departed_people} 人（{departed_months} 个月）、"
        f"缺离职日期 {missing_count} 人、待处理申诉 {pending} 条，建议尽快处理。",
        icon="🧯",
    )
    ui.metric_row(
        [
            {"label": "离职后仍申报", "value": f"{departed_people} 人"},
            {"label": "涉及工资", "value": F.fmt_money(summary.get("income")),
             "delta": f"已预扣个税 {F.fmt_money(summary.get('tax'))}"},
            {"label": "缺离职日期", "value": f"{missing_count} 人"},
            {"label": "待处理申诉", "value": f"{pending} 条"},
        ],
        columns=4,
    )
    details = []
    if departed_people:
        names = "、".join(str(item.get("employee_name") or "") for item in (scan.get("items") or [])[:5])
        details.append(f"离职后仍申报：{names}")
    if missing_count:
        details.append("缺离职日期：" + "、".join(str(item.get("name") or "") for item in missing[:5]))
    if pending:
        details.append(f"待处理/已沟通申诉 {pending} 条")
    if details:
        st.caption("；".join(details) + "。")
    if st.button("🧯 去「离职与纠错」处理 →", key="dash_goto_correction", type="primary"):
        nav.goto(nav.PAGE_CORRECTION)


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
    try:
        region = config.get_region()
        rate = float(region.get("city_maintenance_tax_rate") or 0.07)
        full = float(region.get("surtax_rate_full") or 0.12)
        discounted = float(region.get("surtax_rate_discounted") or 0.06)
        st.caption(
            f"📍 地区：{region.get('label') or '未指定地区'} · 城建税 {rate * 100:.0f}% · "
            f"附加税费综合 {full * 100:.0f}%（六税两费减半后 {discounted * 100:.1f}%）"
        )
    except Exception:  # noqa: BLE001 - 地区信息缺失不影响驾驶舱
        pass

    if ledger_api.is_empty():
        ui.empty_state(
            "还没有台账数据",
            "可以手动录入，也可以一键加载 2026 年 Q1—Q4 示例数据（含收入、费用、研发、合同与申报记录）。",
            icon="🗂️",
            seed_key="dash_seed_demo",
        )

    data = _load_dashboard()
    period_label = ledger_api.quarter_label(data.get("period") or current_quarter())

    _render_onboarding_banner()
    _render_todo_card()
    _render_hr_anomaly_card()
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
    _render_zero_and_late_card(data)
    st.caption(
        f"本年累计：不含税收入 {F.fmt_money(data.get('ytd_income'))} · "
        f"费用 {F.fmt_money(data.get('ytd_expense'))} · "
        f"利润 {F.fmt_money(data.get('ytd_profit'))}"
    )
    theme.disclaimer()

    _render_closing_card()
    _render_payroll_card()

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
