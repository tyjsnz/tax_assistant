"""🆘 电脑坏了 / 资料找回：从电子税务局找回历史资料，重建账套并继续申报。"""

from __future__ import annotations

import streamlit as st

from recovery import checklist
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
    theme.disclaimer()
