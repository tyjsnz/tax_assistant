"""Plotly 图表组件（统一配色、边距与中文标签）。

页面只需要传入结构化数据，避免各处重复写图表配置。
"""

from __future__ import annotations

from typing import Any, Sequence

import plotly.graph_objects as go
import streamlit as st

#: 与 .streamlit/config.toml 的 chartCategoricalColors 保持一致
COLORS = [
    "#1E5AA8",
    "#3B82C4",
    "#6FA8DC",
    "#2E8B57",
    "#E8A33D",
    "#D9534F",
    "#7E6BC4",
    "#4FB3B3",
    "#A8B8CC",
    "#8C6E4A",
]


def _base_layout(fig: go.Figure, height: int = 320) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=28, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="PingFang SC, Microsoft YaHei, sans-serif", size=12, color="#1D2129"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
    )
    fig.update_xaxes(showgrid=False, linecolor="#E3E9F2", tickfont=dict(size=11))
    fig.update_yaxes(gridcolor="#EDF1F7", zeroline=False, tickfont=dict(size=11))
    return fig


def income_trend(rows: Sequence[dict], *, height: int = 320) -> go.Figure:
    """月度收入趋势（柱：不含税收入 / 含税收入，线：费用与利润）。"""
    months = [str(row.get("month") or "") for row in rows]
    income_exclusive = [float(row.get("income_exclusive") or 0) for row in rows]
    income_inclusive = [float(row.get("income_inclusive") or 0) for row in rows]
    expense = [float(row.get("expense") or 0) for row in rows]
    profit = [float(row.get("profit") or 0) for row in rows]

    fig = go.Figure()
    fig.add_bar(name="不含税收入", x=months, y=income_exclusive, marker_color=COLORS[0], opacity=0.92)
    fig.add_bar(name="含税收入", x=months, y=income_inclusive, marker_color=COLORS[2], opacity=0.75)
    fig.add_scatter(
        name="费用",
        x=months,
        y=expense,
        mode="lines+markers",
        line=dict(color=COLORS[4], width=2),
        marker=dict(size=5),
    )
    fig.add_scatter(
        name="利润",
        x=months,
        y=profit,
        mode="lines+markers",
        line=dict(color=COLORS[3], width=2, dash="dot"),
        marker=dict(size=5),
    )
    fig.update_layout(barmode="group")
    return _base_layout(fig, height=height)


def expense_by_category(summary: dict, *, height: int = 300) -> go.Figure:
    """费用分类占比（水平柱状图）。"""
    categories = list((summary or {}).keys())
    amounts: list[float] = []
    for name in categories:
        value = (summary or {}).get(name)
        if isinstance(value, dict):
            value = value.get("amount", value.get("inclusive", 0))
        amounts.append(float(value or 0))
    fig = go.Figure(
        go.Bar(
            x=amounts,
            y=categories,
            orientation="h",
            marker_color=COLORS[: len(categories)] or COLORS[0],
            text=[f"¥{amount:,.2f}" for amount in amounts],
            textposition="auto",
        )
    )
    fig.update_layout(yaxis=dict(autorange="reversed"))
    return _base_layout(fig, height=height)


def show(fig: go.Figure, *, alt: str = "图表") -> None:
    """渲染 Plotly 图（随容器自适应宽度）。"""
    st.plotly_chart(fig, width="stretch", theme="streamlit", alt=alt)


def empty_chart_hint(message: str = "暂无数据，先录入台账或加载示例数据") -> None:
    st.caption(f"📊 {message}")
