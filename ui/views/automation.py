"""⚡ AI 代办：把确定性操作做成「预览 → 确认 → 一键执行」。

只代办本地操作（工资表、票据匹配入账、申报包、更正操作单），
不会登录电子税务局、不会自动提交申报或缴款。
"""

from __future__ import annotations

import streamlit as st

from automation import plans
from ui.components import nav, theme, ui


def _render_pipeline() -> None:
    st.caption(
        "按顺序执行：票据匹配入账 → 生成该季度各月工资表 → 扫描离职异常 → 生成并归档申报包；"
        "只备份一次，执行后逐步复核。**不会自动提交电子税务局/缴款。**"
    )
    period = st.text_input("季度（YYYYQn）", value=plans.quarter_period(), key="auto_pipeline_period")
    if st.button("🔍 预览整条流水线", key="auto_pipeline_preview", width="stretch"):
        try:
            st.session_state["auto_pipeline_preview"] = plans.preview_pipeline(period)
        except Exception as exc:  # noqa: BLE001
            st.error(f"预览失败：{exc}")
    preview = st.session_state.get("auto_pipeline_preview") or {}
    if preview:
        st.dataframe(
            [{"步骤": item.get("title"), "预览": item.get("summary")} for item in preview.get("steps") or []],
            width="stretch", hide_index=True,
        )
        filing = preview.get("filing") or {}
        cols = st.columns(2)
        with cols[0]:
            st.metric("申报表", f"{filing.get('form_count', 0)} 张")
        with cols[1]:
            st.metric("合计税费", f"¥{filing.get('total_tax', 0):,.2f}")
        for warning in preview.get("warnings") or []:
            st.warning(warning)
        confirmed = st.checkbox("我已核对预览，确认执行整条流水线", key="auto_pipeline_confirm")
        if st.button("🚀 执行季度流水线", key="auto_pipeline_execute", type="primary", disabled=not confirmed):
            try:
                result = plans.execute_pipeline(period)
                st.success("季度流水线执行完成，请逐步复核结果。" if result.get("ok") else "流水线执行完成，但有步骤失败，请查看明细。")
                for item in result.get("steps") or []:
                    if item.get("ok"):
                        note = f"（{item.get('note')}）" if item.get("skipped") else ""
                        st.caption(f"✅ {item.get('key')} {item.get('month') or ''}{note}")
                    else:
                        st.warning(f"❌ {item.get('key')} {item.get('month') or ''}：{item.get('error')}")
                st.caption(f"执行前备份：{result.get('backup', {}).get('name') or '已创建'}")
            except Exception as exc:  # noqa: BLE001
                st.error(f"执行失败：{exc}")
    st.caption("建议先预览再执行；失败步骤可在对应页面手工处理，不会自动重试。")


def _render_payroll() -> None:
    st.caption("按在职员工生成当月工资表草稿（累计预扣、社保公积金按档案）；生成后可在「👥 员工与工资」修改。")
    period = st.text_input("工资月份（YYYY-MM）", value=plans.month_period(), key="auto_payroll_period")
    cols = st.columns([1, 2, 2])
    with cols[0]:
        if st.button("🔍 预览", key="auto_payroll_preview", width="stretch"):
            try:
                st.session_state["auto_payroll_preview"] = plans.preview_payroll(period)
            except Exception as exc:  # noqa: BLE001
                st.error(f"预览失败：{exc}")
    preview = st.session_state.get("auto_payroll_preview") or {}
    if preview:
        with cols[1]:
            st.metric("在职员工", f"{preview.get('active_count', 0)} 人")
        with cols[2]:
            st.metric("待生成", f"{preview.get('pending_count', 0)} 人")
        if preview.get("pending_names"):
            st.caption("待生成：" + "、".join(preview["pending_names"]))
        for warning in preview.get("warnings") or []:
            st.warning(warning)
        confirmed = st.checkbox("我已核对预览，确认生成工资表", key="auto_payroll_confirm")
        if st.button("⚡ 执行代办", key="auto_payroll_execute", type="primary", disabled=not confirmed):
            try:
                result = plans.execute_payroll(period)
                st.success(f"已生成工资表：{result['period']}；备份：{result['backup'].get('name') or result['backup'].get('path') or '已创建'}")
                st.session_state["auto_payroll_preview"] = None
            except Exception as exc:  # noqa: BLE001
                st.error(f"执行失败：{exc}")


def _render_ingest() -> None:
    st.caption("先导入发票/流水，再生成匹配建议；勾选确认后，系统会把 ≥80 分的匹配自动入账（低于阈值保留人工确认）。")
    if st.button("🔍 预览待匹配情况", key="auto_ingest_preview", width="stretch"):
        try:
            st.session_state["auto_ingest_preview"] = plans.preview_ingest()
        except Exception as exc:  # noqa: BLE001
            st.error(f"预览失败：{exc}")
    preview = st.session_state.get("auto_ingest_preview") or {}
    summary = preview.get("summary") or {}
    if summary:
        cols = st.columns(4)
        with cols[0]:
            st.metric("发票", f"{summary.get('invoices_total', 0)} 张")
        with cols[1]:
            st.metric("待匹配发票", f"{summary.get('invoices_pending', 0)} 张")
        with cols[2]:
            st.metric("流水", f"{summary.get('transactions_total', 0)} 笔")
        with cols[3]:
            st.metric("待匹配流水", f"{summary.get('transactions_pending', 0)} 笔")
        for warning in preview.get("warnings") or []:
            st.warning(warning)
        threshold = st.slider("自动入账最低匹配分", 60, 100, 80, 5, key="auto_ingest_threshold")
        confirmed = st.checkbox("我已核对，确认生成建议并自动确认高置信匹配", key="auto_ingest_confirm")
        if st.button("⚡ 执行代办", key="auto_ingest_execute", type="primary", disabled=not confirmed):
            try:
                result = plans.execute_ingest(min_score=threshold)
                st.success(f"已确认入账 {result.get('confirmed_count', 0)} 条；跳过 {len(result.get('skipped') or [])} 条。")
                st.caption(f"备份：{result.get('backup', {}).get('name') or '已创建'}")
            except Exception as exc:  # noqa: BLE001
                st.error(f"执行失败：{exc}")
    st.caption("低于阈值的匹配仍留在「📥 票据与流水」人工确认；执行前会自动备份当前账套。")


def _render_filing() -> None:
    st.caption(
        "生成该季度全部申报表、校验、导出 Excel/PDF 并归档；**不会自动提交电子税务局**，"
        "归档后请到「📋 申报填报」核对并在电子税务局手工提交。"
    )
    period = st.text_input("季度（YYYYQn）", value=plans.quarter_period(), key="auto_filing_period")
    if st.button("🔍 预览申报表与税额", key="auto_filing_preview", width="stretch"):
        try:
            st.session_state["auto_filing_preview"] = plans.preview_filing(period)
        except Exception as exc:  # noqa: BLE001
            st.error(f"预览失败：{exc}")
    preview = st.session_state.get("auto_filing_preview") or {}
    if preview:
        cols = st.columns(2)
        with cols[0]:
            st.metric("申报表", f"{preview.get('form_count', 0)} 张")
        with cols[1]:
            st.metric("合计税费", f"¥{preview.get('total_tax', 0):,.2f}")
        if preview.get("forms"):
            st.dataframe(
                [{"申报表": item["form_name"], "合计税费": item["total_tax"], "校验": item["summary"]} for item in preview["forms"]],
                width="stretch", hide_index=True,
            )
        confirmed = st.checkbox("我已核对预览，确认生成并归档申报包", key="auto_filing_confirm")
        if st.button("⚡ 执行代办", key="auto_filing_execute", type="primary", disabled=not confirmed):
            try:
                result = plans.execute_filing(period)
                st.success(f"已生成并归档申报包：{result['period']}")
                st.caption(f"归档目录：{result['result'].get('dir')}；备份：{result['backup'].get('name') or '已创建'}")
            except Exception as exc:  # noqa: BLE001
                st.error(f"执行失败：{exc}")
    st.caption("申报表仍需在电子税务局手工提交；系统只负责算税、生成、校验与归档。")


def _render_correction() -> None:
    st.caption("扫描离职后仍申报的员工，为每位生成《更正操作单》PDF；不会自动修改扣缴端申报。")
    if st.button("🔍 预览异常员工", key="auto_correction_preview", width="stretch"):
        try:
            st.session_state["auto_correction_preview"] = plans.preview_correction()
        except Exception as exc:  # noqa: BLE001
            st.error(f"预览失败：{exc}")
    preview = st.session_state.get("auto_correction_preview") or {}
    summary = preview.get("summary") or {}
    if preview:
        cols = st.columns(3)
        with cols[0]:
            st.metric("异常员工", f"{summary.get('employees', 0)} 人")
        with cols[1]:
            st.metric("涉及月份", f"{summary.get('months', 0)} 个")
        with cols[2]:
            st.metric("涉及工资", f"¥{summary.get('income', 0):,.2f}")
        for warning in preview.get("warnings") or []:
            st.warning(warning)
        confirmed = st.checkbox("我已核对，确认生成更正操作单", key="auto_correction_confirm")
        if st.button("⚡ 执行代办", key="auto_correction_execute", type="primary", disabled=not confirmed):
            try:
                result = plans.execute_correction()
                st.success(f"已生成 {len(result.get('exported') or [])} 份操作单。")
                for item in result.get("exported") or []:
                    if item.get("error"):
                        st.warning(f"{item.get('employee')}：{item['error']}")
                    else:
                        st.caption(f"{item.get('employee')}：{item.get('path')}")
            except Exception as exc:  # noqa: BLE001
                st.error(f"执行失败：{exc}")
    st.caption("更正申报仍需在扣缴端手工操作；操作单可在「🧯 离职与纠错」查看与打印。")


def render() -> None:
    """渲染 AI 代办中心。"""
    ui.page_header(
        "AI 代办",
        "把重复操作做成「预览 → 确认 → 一键执行」；所有执行由本地确定性工具完成，AI 只负责理解与解释。",
        icon="⚡",
    )
    st.info(
        "可以自动：生成工资表、票据匹配入账、生成并归档申报包、导出离职更正操作单。"
        "**不可以自动：登录电子税务局、提交申报、缴款、开专票、社保申报**——这些没有公开 API，也涉及合规责任。",
        icon="🔐",
    )
    tabs = st.tabs(["🚀 一键季度流水线", "👥 工资个税", "🧾 票据入账", "📋 季度申报", "🧯 离职纠错"])
    with tabs[0]:
        _render_pipeline()
    with tabs[1]:
        _render_payroll()
    with tabs[2]:
        _render_ingest()
    with tabs[3]:
        _render_filing()
    with tabs[4]:
        _render_correction()
    st.caption("每次写操作前自动创建本地备份；执行结果请到对应页面复核。所有金额与操作仍以主管税务机关口径为准。")
    theme.disclaimer()
