"""只读数据缓存（``st.cache_data``，TTL 5 秒）。

CSV 数据量很小，但同一页面常需要重复读取（驾驶舱、申报页、政策页）。
写入数据后必须调用 :func:`clear` 立即失效缓存，避免用户看到旧数据。
"""

from __future__ import annotations

from typing import Any

import streamlit as st

TTL = 5


@st.cache_data(ttl=TTL, show_spinner=False)
def dashboard() -> dict:
    """驾驶舱汇总。"""
    from ui.components import ledger_api

    return ledger_api.dashboard()


@st.cache_data(ttl=TTL, show_spinner=False)
def monthly_summary(year: int) -> list[dict]:
    """月度收支趋势。"""
    from ui.components import ledger_api

    return ledger_api.monthly_summary(year)


@st.cache_data(ttl=TTL, show_spinner=False)
def income_summary(start: str | None = None, end: str | None = None) -> dict:
    from ui.components import ledger_api

    return ledger_api.summarize_income(start=start, end=end)


@st.cache_data(ttl=TTL, show_spinner=False)
def expense_summary(start: str | None = None, end: str | None = None) -> dict:
    from ui.components import ledger_api

    return ledger_api.summarize_expenses(start=start, end=end)


@st.cache_data(ttl=TTL, show_spinner=False)
def rd_summary(period: str) -> dict:
    from ui.components import ledger_api

    return ledger_api.summarize_rd(period)


@st.cache_data(ttl=TTL, show_spinner=False)
def income_period_totals(period: str) -> dict:
    from ui.components import ledger_api

    return ledger_api.period_totals(period)


@st.cache_data(ttl=TTL, show_spinner=False)
def income_expense_report(period: str) -> dict:
    from ui.components import ledger_api

    return ledger_api.income_expense_report(period)


@st.cache_data(ttl=TTL, show_spinner=False)
def policies(force: bool = False) -> list[dict]:
    """政策列表（``knowledge.retriever`` 缺失时抛异常，由页面捕获）。"""
    from knowledge import retriever

    return list(retriever.load_policies(force=force))


@st.cache_data(ttl=TTL, show_spinner=False)
def policy_stats() -> dict:
    from knowledge import update_checker

    return dict(update_checker.policy_stats())


def clear() -> None:
    """清空全部页面缓存（写入台账 / 设置变更后调用）。"""
    try:
        st.cache_data.clear()
    except Exception:  # noqa: BLE001
        pass


def get(key: str, loader, default: Any = None) -> Any:
    """执行 ``loader``，异常时返回 default（避免单点失败影响整页）。"""
    try:
        return loader()
    except Exception:  # noqa: BLE001
        return default
