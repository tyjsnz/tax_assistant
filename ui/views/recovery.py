"""🆘 电脑坏了 / 资料找回：从电子税务局找回历史资料，重建账套并继续申报。"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from recovery import checklist, history
from ui.components import company_api, nav, theme, ui


def _goto(action: str) -> None:
    pages = {
        "recovery_goto_collector": nav.PAGE_COLLECTOR,
        "recovery_goto_settings": nav.PAGE_SETTINGS,
        "recovery_goto_ingest": nav.PAGE_INGEST,
        "recovery_goto_calendar": nav.PAGE_CALENDAR,
    }
    if action in pages:
        nav.goto(pages[action])


def _checklist_markdown() -> str:
    lines = ["# 税小智 · 电脑损坏/资料找回清单", "", "> 本清单为操作提示，不替代税务机关口径。", ""]
    for index, step in enumerate(checklist.STEPS, start=1):
        lines.append(f"## {index}. {step['title']}")
        lines.append(step["summary"])
        for bullet in step.get("bullets") or []:
            lines.append(f"- {bullet}")
        lines.append("")
    return "\n".join(lines)


def _render_steps(company_id: str, company: dict) -> None:
    """找回步骤清单。"""
    state = checklist.load_progress(company_id)
    steps = list(state.get("steps") or [False] * checklist.TOTAL_STEPS)
    done = sum(1 for item in steps if item)
    st.progress(done / checklist.TOTAL_STEPS, text=f"找回进度：{done} / {checklist.TOTAL_STEPS} 步")

    for index, step in enumerate(checklist.STEPS, start=1):
        with st.container(border=True):
            checked = steps[index - 1]
            st.markdown(f"### {'✅' if checked else '⬜'} 第 {index} 步 · {step['title']}")
            st.write(step["summary"])
            with st.expander("具体怎么做", expanded=not checked):
                for bullet in step.get("bullets") or []:
                    st.markdown(f"- {bullet}")
                links = step.get("links") or []
                if links:
                    cols = st.columns(len(links))
                    for col, (label, url) in zip(cols, links):
                        with col:
                            st.link_button(label, url, width="stretch")
                actions = step.get("actions") or []
                if actions:
                    cols = st.columns(len(actions))
                    for col, (label, action) in zip(cols, actions):
                        with col:
                            if st.button(label, key=f"recovery_action_{index}_{action}", width="stretch"):
                                _goto(action)
            value = st.checkbox(
                "这一步已完成", value=checked, key=f"recovery_step_{index}", help="完成后勾选，进度会自动保存"
            )
            if value != checked:
                steps[index - 1] = value
                checklist.save_progress(steps, company_id)

    done = sum(1 for item in steps if item)
    if done == checklist.TOTAL_STEPS:
        st.success("找回与重建步骤已全部勾选。建议到「设置 → 数据管理」创建备份，并以后每月备份一次。")
    st.download_button(
        "⬇️ 下载资料找回清单（Markdown）",
        data=_checklist_markdown().encode("utf-8"),
        file_name="税小智_资料找回清单.md",
        mime="text/markdown",
        key="recovery_download",
        width="stretch",
    )
    st.caption("找回过程中如不确定税种/所属期，先拨打 12366 或咨询主管税务机关，不要凭猜测补申报。")


def _render_completeness() -> None:
    """找回后的数据完整性检查。"""
    st.caption("检查本地账套是否具备继续申报的基础数据；不会访问电子税务局，也不上传任何数据。")
    if st.button("开始检查", key="recovery_completeness_run", type="primary", width="stretch"):
        try:
            st.session_state["_recovery_completeness"] = history.completeness()
        except Exception as exc:  # noqa: BLE001
            st.error(f"检查失败：{exc}")
    result = st.session_state.get("_recovery_completeness")
    if not isinstance(result, dict):
        st.info("点击「开始检查」，系统会检查公司信息、台账、工资、发票、历史申报和归档。")
        return
    ui.metric_row(
        [
            {"label": "完整度", "value": f"{result.get('score', 0)}%"},
            {"label": "已就绪", "value": f"{result.get('ok', 0)} / {result.get('total', 0)}"},
            {"label": "需补齐", "value": f"{result.get('total', 0) - result.get('ok', 0)}"},
        ],
        columns=3,
    )
    st.progress(int(result.get("score", 0)) / 100, text="继续申报的数据基础")
    for item in result.get("items") or []:
        with st.container(border=True):
            cols = st.columns([1, 4, 3])
            with cols[0]:
                st.markdown("✅" if item.get("status") == "ok" else "⚠️")
            with cols[1]:
                st.markdown(f"**{item.get('label')}**")
                st.caption(str(item.get("detail") or ""))
            with cols[2]:
                if item.get("status") != "ok":
                    page = str(item.get("page") or "")
                    if page in {"settings", "ledger", "payroll", "collector", "filing"}:
                        if st.button("去补齐", key=f"recovery_fix_{item.get('key')}", width="stretch"):
                            nav.goto(getattr(nav, f"PAGE_{page.upper()}", nav.PAGE_RECOVERY))
                    elif page == "recovery_history":
                        st.caption("在「历史申报补录」标签补录")
    if result.get("score", 0) >= 80:
        st.success("数据基础基本完整。仍建议对照电子税务局最近一期申报表逐项核对。")
    else:
        st.warning("还有关键数据未补齐，建议先补录历史申报、台账和发票，再继续申报。")


def _render_history() -> None:
    """历史申报补录与批量导入。"""
    st.caption("把电子税务局查到的历史申报补录到本地，便于找回后核对；不代替实际申报。")
    rows = history.list_filings()
    with st.container(border=True):
        st.markdown("**新增一条历史申报**")
        with st.form("recovery_history_form", clear_on_submit=True):
            cols = st.columns([2, 3, 2, 2])
            with cols[0]:
                period = st.text_input("所属期", placeholder="2026-03 或 2026Q1", key="recovery_history_period")
            with cols[1]:
                tax_type = st.selectbox("税种", history.TAX_TYPES, key="recovery_history_tax_type")
            with cols[2]:
                filed_date = st.text_input("申报日期", placeholder="2026-04-15", key="recovery_history_filed_date")
            with cols[3]:
                tax_amount = st.number_input("税额（元）", min_value=0.0, step=0.01, value=0.0, key="recovery_history_amount")
            cols = st.columns([2, 4])
            with cols[0]:
                status = st.selectbox("状态", history.STATUSES, key="recovery_history_status")
            with cols[1]:
                note = st.text_input("备注", placeholder="如：从电子税务局申报查询下载", key="recovery_history_note")
            if st.form_submit_button("保存补录", type="primary", width="stretch"):
                try:
                    history.add_filing(period, tax_type, filed_date=filed_date, tax_amount=tax_amount, status=status, note=note)
                    st.toast("已保存历史申报")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"保存失败：{exc}")

    if rows:
        frame = pd.DataFrame(rows)
        display = frame.rename(columns={
            "period": "所属期", "tax_type": "税种", "filed_date": "申报日期",
            "tax_amount": "税额", "status": "状态", "note": "备注", "source": "来源",
        })
        st.dataframe(
            display[["所属期", "税种", "申报日期", "税额", "状态", "备注", "来源"]],
            hide_index=True,
            alt="历史申报补录记录",
            column_config={"税额": st.column_config.NumberColumn("税额", format="¥%.2f")},
        )
        options = {f"{row.get('period')} · {row.get('tax_type')}": str(row.get("id")) for row in rows}
        with st.container(border=True):
            cols = st.columns([4, 2])
            with cols[0]:
                selected = st.selectbox("选择要删除的记录", list(options), key="recovery_history_delete_select")
            with cols[1]:
                st.write("")
                if st.button("删除", key="recovery_history_delete", width="stretch"):
                    try:
                        history.delete_filing(options[selected])
                        st.toast("已删除")
                        st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"删除失败：{exc}")
    else:
        st.info("还没有补录记录。可下载模板批量导入，或逐条新增。")

    cols = st.columns(2)
    with cols[0]:
        st.download_button(
            "⬇️ 下载补录模板 CSV",
            data=history.export_template(),
            file_name="历史申报补录模板.csv",
            mime="text/csv",
            key="recovery_history_template",
            width="stretch",
        )
    with cols[1]:
        uploaded = st.file_uploader("上传补录 CSV", type=["csv"], key="recovery_history_upload")
        if uploaded is not None and st.button("导入 CSV", key="recovery_history_import", width="stretch"):
            try:
                result = history.import_csv(uploaded.getvalue())
                st.success(f"导入完成：新增 {result['added']} 条；跳过 {len(result['skipped'])} 条。")
                if result["skipped"]:
                    st.warning("\n".join(result["skipped"][:5]))
                    st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(f"导入失败：{exc}")


def render() -> None:
    """渲染资料找回页。"""
    ui.page_header(
        "电脑坏了 / 资料找回",
        "旧电脑损坏、账套丢失时，按步骤从电子税务局找回资料，在新电脑重建账套并继续申报。",
        icon="🆘",
    )
    st.warning(
        "先别急着重新申报：先确认电子税务局最近所属期是否已经申报，避免重复申报。"
        "本页只做找回与重建指引，不代替税务机关处理。",
        icon="🛑",
    )
    try:
        company_id = company_api.active_company_id()
        company = company_api.active_company()
    except Exception:  # noqa: BLE001
        company_id = "current"
        company = {}
    st.caption(f"当前公司：{company.get('name') or company_id} · 清单进度会保存在当前账套。")

    tabs = st.tabs(["📋 找回步骤", "📊 完整性检查", "📥 历史申报补录"])
    with tabs[0]:
        _render_steps(company_id, company)
    with tabs[1]:
        _render_completeness()
    with tabs[2]:
        _render_history()
    theme.disclaimer()
