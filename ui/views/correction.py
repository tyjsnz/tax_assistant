"""🧯 离职与纠错：离职后多申报扫描 / 更正方案 / 补偿金测算 / 个税异议申诉台账。

页面只做展示与调用编排，测算全部由 ``engine``/``ledger.corrections`` 完成；
实际更正申报仍在自然人电子税务局（扣缴端）完成。
"""

from __future__ import annotations

import streamlit as st

import config
from ui.components import nav, theme, ui
from ui.views import payroll as payroll_view


def render() -> None:
    """渲染「🧯 离职与纠错」页面（三个子标签，避免一屏堆太多内容）。"""
    ui.page_header(
        "离职与纠错",
        "员工离职后仍做工资/申报个税、员工个税 APP 异议申诉、离职补偿金测算：扫描 → 更正方案 → 照单操作 → 留痕。",
        icon="🧯",
        right_text=f"版本 {config.APP_VERSION}",
    )
    st.caption(
        "员工撤销个税 APP 申诉 ≠ 申报已更正；本页只做扫描、测算、操作单与台账，"
        "实际更正请在自然人电子税务局（扣缴端）完成。"
    )
    tabs = st.tabs(["🔍 扫描与更正", "💰 离职补偿金测算", "📬 个税异议申诉台账"])
    with tabs[0]:
        payroll_view._render_correction_scan_section()
    with tabs[1]:
        payroll_view._render_severance_section()
    with tabs[2]:
        payroll_view._render_disputes_section()
    theme.disclaimer()


__all__ = ["render"]
