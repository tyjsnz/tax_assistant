"""📖 操作指引：面向小白的自绘图文步骤（不包含电子税务局截图）。

* 只提供自绘操作地图、字段说明、常见错误与官方入口链接；
* 支持勾选完成进度、导出 PDF 操作单；
* 不复制、不打包电子税务局页面，避免版权/时效/隐私风险。
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

import config
from guidebooks import content
from ui.components import nav, theme, ui

_CSS = """
<style>
  .guide-crumb {display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin:4px 0 8px;}
  .guide-crumb span.crumb {background:rgba(55,230,176,.10); border:1px solid rgba(55,230,176,.35);
    border-radius:999px; padding:3px 9px; font-size:12px; color:#0f766e;}
  .guide-crumb span.arrow {color:#94a3b8; font-size:12px;}
  .guide-step-no {display:inline-flex; width:24px; height:24px; border-radius:50%;
    align-items:center; justify-content:center; background:#0f766e; color:#fff;
    font-size:12px; font-weight:700; margin-right:6px;}
  .guide-note {font-size:12px; color:#64748b; line-height:1.6;}
</style>
"""


def _render_breadcrumb(path: str) -> None:
    parts = [part.strip() for part in str(path or "").split("→") if part.strip()]
    if not parts:
        return
    chips = []
    for index, part in enumerate(parts):
        if index:
            chips.append('<span class="arrow">→</span>')
        chips.append(f'<span class="crumb">{part}</span>')
    st.markdown('<div class="guide-crumb">' + "".join(chips) + "</div>", unsafe_allow_html=True)


def _render_step(workflow_id: str, index: int, step: dict) -> None:
    with st.container(border=True):
        st.markdown(
            f'<div><span class="guide-step-no">{index}</span><b>{step.get("title", "")}</b></div>',
            unsafe_allow_html=True,
        )
        st.caption("菜单路径（自绘示意，实际以页面为准）")
        _render_breadcrumb(step.get("menu_path") or "")
        fields = step.get("fields") or []
        if fields:
            st.markdown("**字段 / 怎么填**")
            rows = [{"字段/项目": name, "怎么填 / 注意什么": note} for name, note in fields]
            st.dataframe(rows, width="stretch", hide_index=True)
        errors = step.get("errors") or []
        if errors:
            st.markdown("**常见错误**")
            for item in errors:
                st.markdown(f"- ⚠️ {item}")
        tip = str(step.get("tips") or "").strip()
        if tip:
            st.info(f"💡 {tip}")
        done = st.checkbox(f"第 {index} 步已完成", key=f"guide_{workflow_id}_{index}")
        return done


def _render_workflow(workflow: dict) -> None:
    workflow_id = str(workflow.get("id"))
    st.markdown(_CSS, unsafe_allow_html=True)
    ui.section(
        f"{workflow.get('icon', '')} {workflow.get('title', '')}",
        str(workflow.get("subtitle") or ""),
    )
    links = workflow.get("official_links") or []
    if links:
        st.caption("官方入口（只提供链接，不复制官方页面）")
        link_cols = st.columns(len(links) + 1)
        for col, link in zip(link_cols, links):
            with col:
                st.link_button(link.get("label") or "官方入口", link.get("url") or "#", width="stretch")
        with link_cols[-1]:
            if st.button("📄 导出当前流程 PDF", key=f"guide_pdf_{workflow_id}", width="stretch"):
                try:
                    from guidebooks.pdf import build_guide_pdf

                    path = build_guide_pdf([workflow_id])
                    st.session_state[f"_guide_pdf_path_{workflow_id}"] = str(path)
                    st.success("已生成 PDF，点击下方按钮下载。")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"生成 PDF 失败：{exc}")
        pdf_path = st.session_state.get(f"_guide_pdf_path_{workflow_id}")
        if pdf_path and Path(pdf_path).exists():
            st.download_button(
                "⬇️ 下载操作指引 PDF",
                data=Path(pdf_path).read_bytes(),
                file_name=Path(pdf_path).name,
                mime="application/pdf",
                key=f"guide_pdf_download_{workflow_id}",
                width="stretch",
            )

    steps = workflow.get("steps") or []
    done_flags = []
    for index, step in enumerate(steps, start=1):
        done_flags.append(_render_step(workflow_id, index, step))
    done = sum(1 for flag in done_flags if flag)
    st.progress(done / max(len(steps), 1), text=f"本流程进度：{done} / {len(steps)} 步")
    if done == len(steps) and steps:
        st.success("✅ 本流程步骤已全部勾选。操作完成后建议保存申报表/缴款凭证并归档。")
    if workflow_id == "correction":
        st.info("配套工具：本系统「🧯 离职与纠错」可扫描离职后仍申报、生成逐月更正方案与操作单。")
        if st.button("🧯 去「离职与纠错」", key="guide_goto_correction", type="primary"):
            nav.goto(nav.PAGE_CORRECTION)
    st.caption(content.GUIDE_DISCLAIMER)


def render() -> None:
    """渲染操作指引页面。"""
    st.markdown(_CSS, unsafe_allow_html=True)
    ui.page_header(
        "图文操作指引",
        "给新手看的自绘步骤：点哪里、填什么、常见错误怎么办；不含电子税务局截图，避免版权与过期问题。",
        icon="📖",
    )
    workflows = content.list_workflows()
    labels = [f"{item.get('icon', '')} {item.get('title', '')}" for item in workflows]
    selected_label = st.radio("选择流程", labels, horizontal=True, key="guide_workflow")
    selected = workflows[labels.index(selected_label)]

    metric_cols = st.columns(3)
    with metric_cols[0]:
        st.metric("流程步骤", f"{len(selected.get('steps') or [])} 步")
    with metric_cols[1]:
        st.metric("官方入口", f"{len(selected.get('official_links') or [])} 个")
    with metric_cols[2]:
        st.metric("当前版本", f"v{config.APP_VERSION}")

    st.info(
        "为什么这里没有电子税务局截图：官方页面通常有版权限制、各省界面不同且改版频繁，"
        "截图容易过期或泄露企业信息。因此本页使用自绘菜单路径 + 字段说明 + 官方入口链接。",
        icon="📌",
    )
    _render_workflow(selected)

    st.markdown("---")
    st.caption(
        "建议把本页与「📚 政策知识库」「🧯 离职与纠错」配合使用："
        "政策用于确认口径，指引用于操作，纠错工具用于自动扫描与生成操作单。"
    )
    theme.disclaimer()
