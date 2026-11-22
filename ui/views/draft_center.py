"""📝 申报草稿中心：每月/每季自动检查可准备的草稿与缺失数据（v1.55）。"""

from __future__ import annotations

from datetime import date

import streamlit as st

from drafting import center
from ledger import payroll
from ui.components import cache, nav, theme, ui

_STATUS = {
    "done": ("✅", "已完成"),
    "confirmed": ("✅", "已确认"),
    "draft": ("📝", "草稿待核对"),
    "action": ("⚡", "可自动准备"),
    "todo": ("📋", "待核对"),
    "warning": ("⚠️", "需补数据"),
    "info": ("ℹ️", "无需处理"),
}
_PAGES = {
    "payroll": nav.PAGE_PAYROLL,
    "filing": nav.PAGE_FILING,
    "settlement": nav.PAGE_SETTLEMENT,
    "ledger": nav.PAGE_LEDGER,
}


def _build_payroll(month: str) -> None:
    try:
        result = payroll.build_payroll(month)
        cache.clear()
        st.success(f"{month} 工资草稿已准备：新增 {result.get('created', 0)} 人，跳过 {len(result.get('skipped') or [])} 人。")
    except Exception as exc:  # noqa: BLE001
        st.error(f"生成工资草稿失败：{exc}")


def render() -> None:
    ui.page_header(
        "申报草稿中心",
        "按月/季检查：哪些申报可以自动准备草稿、哪些数据还缺、哪些需要人工确认。系统只准备草稿，不自动申报。",
        icon="📝",
    )
    today = date.today()
    result = center.build_center(today.strftime("%Y-%m"))
    summary = result["summary"]
    st.caption(f"当前月份：{result['month_label']} · 当前季度：{result['quarter_label']}")
    ui.metric_row(
        [
            {"label": "检查项", "value": f"{summary['total']}"},
            {"label": "可自动准备", "value": f"{summary['action']}"},
            {"label": "需补数据", "value": f"{summary['warning']}"},
            {"label": "已完成/已确认", "value": f"{summary['done']}"},
        ],
        columns=4,
    )
    st.info(
        "优先处理「⚠️ 需补数据」和「⚡ 可自动准备」；自动准备只是生成草稿，仍需逐项核对金额、人员和申报状态。",
        icon="📝",
    )
    for item in result["items"]:
        icon, label = _STATUS.get(str(item.get("status")), ("📋", "待核对"))
        with st.container(border=True):
            cols = st.columns([5, 2])
            with cols[0]:
                st.markdown(f"### {icon} {item['label']}")
                st.caption(f"{label} · 来源：{item.get('source') or '—'} · {item.get('scope') == 'quarterly' and '季度' or '月度'}")
                st.write(item.get("detail") or "")
                for warning in item.get("warnings") or []:
                    st.warning(str(warning))
            with cols[1]:
                page = _PAGES.get(str(item.get("page") or ""))
                if item.get("action") == "build_payroll" and page == nav.PAGE_PAYROLL:
                    if st.button(f"⚡ 生成 {result['month']} 工资草稿", key=f"draft_build_{item['key']}", width="stretch", type="primary"):
                        _build_payroll(result["month"])
                if page:
                    if st.button(f"去处理：{page}", key=f"draft_goto_{item['key']}", width="stretch"):
                        nav.goto(page)
    st.caption(result["disclaimer"])
    theme.disclaimer()
