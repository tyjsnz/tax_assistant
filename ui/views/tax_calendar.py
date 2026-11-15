"""🗓️ 报税日历：按月/季/年看清要申报什么、什么时候截止、怎么做。"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import streamlit as st

import config
from engine.calendar_export import build_ics
from engine.tax_calendar import deadline_status, filing_deadlines
from ui.components import nav, theme, ui

_MONTHLY_KEYS = {"personal_tax_monthly", "social_insurance_monthly"}
_QUARTERLY_KEYS = {"vat_quarterly", "corp_tax_quarterly", "financial_report_quarterly", "stamp_tax_quarterly"}
_YEARLY_KEYS = {"annual_settlement", "annual_report", "disability_fund_annual"}

_ACTION_PAGE = {
    "personal_tax_monthly": nav.PAGE_PAYROLL,
    "annual_settlement": nav.PAGE_SETTLEMENT,
    "financial_report_quarterly": nav.PAGE_FILING,
    "social_insurance_monthly": nav.PAGE_FILING,
}
_HOW_TO = {
    "personal_tax_monthly": [
        "先到「👥 员工与工资」生成工资表、核对本月收入与专项附加扣除；",
        "到自然人电子税务局扣缴端：收入填写 → 税款计算 → 申报表报送；",
        "申报成功后完成缴款，保存申报/完税凭证；在本系统标记已申报。",
    ],
    "social_insurance_monthly": [
        "先确认本月社保缴费基数、人员增减（入职/离职）；",
        "到当地电子税务局/社保费申报入口，核对单位部分与个人部分；",
        "申报后完成缴费，保存缴费凭证；各地截止日不同，以当地公告为准。",
    ],
    "vat_quarterly": [
        "先到「📋 申报填报」生成增值税申报表并核对免税额；",
        "登录电子税务局 → 增值税及附加税费申报 → 填写/导入 → 提交；",
        "申报成功后缴款；到「🗄️ 结账与归档」保存申报包。",
    ],
    "corp_tax_quarterly": [
        "在「📋 申报填报」生成企业所得税季度预缴表；",
        "核对营业收入、成本、利润总额与小型微利优惠；",
        "到电子税务局提交并缴款（如有），保存申报表。",
    ],
    "financial_report_quarterly": [
        "先完成本季度账务与结账；",
        "生成资产负债表、利润表并核对勾稽；",
        "随增值税/企业所得税申报一并报送财务报表。",
    ],
    "stamp_tax_quarterly": [
        "整理本季度合同、产权转移书据等应税凭证；",
        "按合同金额和税目计算印花税；",
        "到电子税务局申报（按季或按次），保存凭证。",
    ],
    "annual_settlement": [
        "先完成年度结账，准备财务报表、总账明细账、发票、工资、亏损台账；",
        "做纳税调整底稿，核对小型微利/研发加计/亏损弥补；",
        "5 月 31 日前在电子税务局完成年度申报并补税/退税，归档申报包。",
    ],
    "annual_report": [
        "准备企业基本信息、股东出资、资产状况、社保人数等；",
        "登录国家企业信用信息公示系统填写年报；",
        "6 月 30 日前公示，保存提交回执。",
    ],
    "disability_fund_annual": [
        "确认上年度在职职工人数、残疾人就业人数、工资总额；",
        "按当地残联/税务口径计算残保金（30 人以下通常免征）；",
        "在规定期限内申报缴纳，保存凭证。",
    ],
}
_GENERIC_HOW = ["先确认税种、所属期和截止日；", "按系统提示准备数据/申报表；", "到电子税务局或对应平台提交，保存凭证。"]


def _task_month(item: dict[str, Any]) -> int:
    return int(str(item.get("due_date") or "")[5:7] or 0)


def _year_tasks(year: int) -> list[dict[str, Any]]:
    items = filing_deadlines(year)
    for item in items:
        item.update(deadline_status(item["due_date"]))
    return items


def _render_task_card(item: dict[str, Any], index: int) -> None:
    level = str(item.get("level") or "normal")
    icon = {"overdue": "🔴", "urgent": "🟠", "soon": "🟡"}.get(level, "🟢")
    with st.container(border=True):
        cols = st.columns([5, 2, 2])
        with cols[0]:
            st.markdown(f"**{icon} {item.get('name')}**")
            st.caption(
                f"{item.get('category') or ''} · 税款所属期：{item.get('tax_period_label') or item.get('tax_period') or '—'}"
            )
        with cols[1]:
            st.caption(f"截止：{item.get('due_date')}")
            st.caption(f"{item.get('label') or ''}")
        with cols[2]:
            page = _ACTION_PAGE.get(str(item.get("key")), nav.PAGE_FILING)
            if st.button("去处理 →", key=f"cal_task_{item.get('key')}_{item.get('due_date')}", width="stretch"):
                nav.goto(page)
        with st.expander("怎么做（3 步）", expanded=False):
            for step in _HOW_TO.get(str(item.get("key")), _GENERIC_HOW):
                st.markdown(f"- {step}")
            if st.button("📖 打开完整操作指引", key=f"cal_guide_{item.get('key')}_{item.get('due_date')}"):
                nav.goto(nav.PAGE_GUIDE)


def _render_summary() -> None:
    st.info(
        "先记结论：**每月**通常做个税扣缴、社保；**每季**做增值税/附加、企业所得税预缴、财务报表、印花税；"
        "**每年**做企业所得税汇算清缴（5/31）、工商年报（6/30）、残保金（多数 6–9 月）。"
        "具体以税种认定和主管税务机关通知为准。",
        icon="📌",
    )
    cols = st.columns(3)
    with cols[0]:
        with st.container(border=True):
            st.markdown("#### 📅 每月")
            st.markdown(
                "- **个人所得税扣缴申报**（有工资/员工；无员工按当地要求零申报）\n"
                "- **社会保险费申报缴纳（单位部分）**（单位部分 + 代扣个人部分）\n"
                "- 日常：记账、发票归集、银行流水核对"
            )
    with cols[1]:
        with st.container(border=True):
            st.markdown("#### 🗓️ 每季度")
            st.markdown(
                "- **增值税及附加税费**（小规模通常按季；税局核定按月则按月）\n"
                "- **企业所得税季度预缴**\n"
                "- **财务报表报送**\n"
                "- **印花税**（按季或按次）\n"
                "- 申报核对、归档"
            )
    with cols[2]:
        with st.container(border=True):
            st.markdown("#### 📆 每年")
            st.markdown(
                "- **企业所得税年度汇算清缴**（5 月 31 日前）\n"
                "- **工商年报公示**（6 月 30 日前）\n"
                "- **残疾人就业保障金**（多数地区 6–9 月）\n"
                "- 员工个税年度汇算（3–6 月，公司提醒）"
            )
    st.caption(
        "不一定每月都发生的：增值税/附加、企业所得税预缴、财务报表、印花税通常按季；"
        "无员工/无社保的单位可能不需要个税/社保申报，但零申报也要按当地要求确认。"
    )


def _render_month_view(year: int) -> None:
    today = date.today()
    month = st.selectbox(
        "选择月份",
        list(range(1, 13)),
        index=today.month - 1,
        format_func=lambda m: f"{year} 年 {m} 月",
        key="calendar_month",
    )
    items = [item for item in _year_tasks(year) if _task_month(item) == month]
    if not items:
        st.info("本月没有系统内置的申报事项；仍建议核对税种认定和当地通知。")
    for index, item in enumerate(items):
        _render_task_card(item, index)
    if items:
        st.download_button(
            "📅 导出本月日历",
            data=build_ics(items).encode("utf-8"),
            file_name=f"智小税_申报日历_{year}年{month}月.ics",
            mime="text/calendar",
            key="calendar_ics_month",
            width="stretch",
        )


def _render_year_view(year: int) -> None:
    items = _year_tasks(year)
    if not items:
        st.info("没有生成申报事项。")
        return
    rows = []
    for item in items:
        rows.append(
            {
                "月份": f"{_task_month(item)} 月",
                "申报事项": item.get("name"),
                "类型": "每月" if item.get("key") in _MONTHLY_KEYS else ("每季" if item.get("key") in _QUARTERLY_KEYS else "每年"),
                "截止日": item.get("due_date"),
                "剩余/逾期": item.get("label"),
            }
        )
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.download_button(
        "📅 导出全年日历 (.ics)",
        data=build_ics(items).encode("utf-8"),
        file_name=f"智小税_申报日历_{year}.ics",
        mime="text/calendar",
        key="calendar_ics_year",
        width="stretch",
    )
    st.caption("导入手机/电脑日历后，可由系统日历提前 3 天提醒；节假日以税务机关公告为准。")


def render() -> None:
    """渲染报税日历页。"""
    ui.page_header(
        "报税日历",
        "按月/季/年看清要申报什么、什么时候截止、怎么做；告别“功能太多不知道从哪下手”。",
        icon="🗓️",
    )
    today = date.today()
    year = st.selectbox("年份", [today.year, today.year + 1], index=0, key="calendar_year")
    tabs = st.tabs(["📌 先看必做", "📅 当月待办", "🗓️ 全年日历"])
    with tabs[0]:
        _render_summary()
    with tabs[1]:
        _render_month_view(year)
    with tabs[2]:
        _render_year_view(year)
    theme.disclaimer()
