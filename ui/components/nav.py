"""页面导航常量与跳转工具。

侧边栏使用 ``st.radio``（而不是 ``st.navigation``），以便 ``AppTest`` 可靠地
切换页面做冒烟测试。页面内「快捷入口」按钮通过 :func:`goto` 设置待跳转页面，
由入口脚本在下一次 rerun 时消费，避免直接改写已实例化 widget 的 session_state。
"""

from __future__ import annotations

from typing import Iterator

import streamlit as st

PAGE_DASHBOARD = "🏠 首页驾驶舱"
PAGE_ONBOARDING = "🚀 新手引导"
PAGE_LEDGER = "📒 台账管理"
PAGE_PAYROLL = "👥 员工与工资"
PAGE_CORRECTION = "🧯 离职与纠错"
PAGE_INGEST = "📥 票据与流水"
PAGE_CALCULATOR = "🧮 算税工作台"
PAGE_FILING = "📋 申报填报"
PAGE_SETTLEMENT = "📊 汇算清缴"
PAGE_CHECK = "🔍 申报核对"
PAGE_GUIDE = "📖 操作指引"
PAGE_POLICIES = "📚 政策知识库"
PAGE_ASSISTANT = "🤖 AI 助手"
PAGE_SETTINGS = "⚙️ 设置"

#: 侧边栏顺序（标签, 视图模块）
PAGES: list[tuple[str, str]] = [
    (PAGE_DASHBOARD, "ui.views.dashboard"),
    (PAGE_ONBOARDING, "ui.views.onboarding"),
    (PAGE_LEDGER, "ui.views.ledger"),
    (PAGE_PAYROLL, "ui.views.payroll"),
    (PAGE_CORRECTION, "ui.views.correction"),
    (PAGE_INGEST, "ui.views.ingest"),
    (PAGE_CALCULATOR, "ui.views.calculator"),
    (PAGE_FILING, "ui.views.filing"),
    (PAGE_SETTLEMENT, "ui.views.settlement"),
    (PAGE_CHECK, "ui.views.check"),
    (PAGE_GUIDE, "ui.views.guides"),
    (PAGE_POLICIES, "ui.views.policies"),
    (PAGE_ASSISTANT, "ui.views.assistant"),
    (PAGE_SETTINGS, "ui.views.settings"),
]

PAGE_LABELS: list[str] = [label for label, _ in PAGES]
PAGE_MODULES: dict[str, str] = dict(PAGES)

#: session_state 中的导航键
NAV_KEY = "nav"
GOTO_KEY = "_goto"


def goto(page: str) -> None:
    """请求跳转到指定页面并立即 rerun。"""
    if page in PAGE_MODULES:
        st.session_state[GOTO_KEY] = page
    st.rerun()


def resolve_goto() -> None:
    """在渲染 radio 之前消费待跳转页面（必须在 radio 实例化前调用）。"""
    pending = st.session_state.pop(GOTO_KEY, None)
    if pending in PAGE_MODULES:
        st.session_state[NAV_KEY] = pending


def iter_pages() -> Iterator[tuple[str, str]]:
    return iter(PAGES)
