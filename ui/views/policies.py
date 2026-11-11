"""📚 政策知识库：检索、分类筛选、政策详情与时效检查。

政策内容全部来自本地 ``knowledge/policy_files/**.md``，标注来源与生效日期；
无 API Key 时也能检索与阅读原文。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ui.components import cache, formatting as F, runtime, theme, ui


def _load_policies(force: bool = False) -> list[dict]:
    retriever = runtime.load("knowledge.retriever")
    if retriever is None:
        return []
    try:
        return list(retriever.load_policies(force=force))
    except Exception as exc:  # noqa: BLE001
        ui.failure("政策知识库读取失败，请检查 knowledge/policy_files 目录。", exc, show_traceback=False)
        return []


def _categories(policies: list[dict]) -> list[str]:
    categories = sorted({str(item.get("category") or "未分类") for item in policies})
    return ["全部"] + categories


def _search(query: str, category: str, top_k: int = 10) -> list[dict]:
    retriever = runtime.load("knowledge.retriever")
    if retriever is None:
        return []
    category_filter = None if category in ("", "全部") else category
    try:
        if query.strip() and hasattr(retriever, "search"):
            results = list(retriever.search(query.strip(), top_k=top_k, category=category_filter))
            if results:
                return results
        if hasattr(retriever, "list_policies"):
            return list(retriever.list_policies(category=category_filter))
    except Exception as exc:  # noqa: BLE001
        ui.failure("政策检索失败。", exc, show_traceback=False)
    return []


def _render_card(policy: dict, index: int) -> None:
    title = policy.get("title") or "未命名政策"
    category = policy.get("category") or "未分类"
    source = policy.get("source") or "来源未标注"
    effective = policy.get("effective_date") or "—"
    expiry = policy.get("expiry_date") or "—"
    verified = policy.get("last_verified") or "—"
    score = policy.get("score")
    tags = policy.get("tags") or []
    with st.container(border=True):
        st.markdown(f"**{F.status_icon('INFO')} {title}**")
        st.caption(
            f"分类：{category} · 来源：{source} · 生效日期：{F.fmt_date(effective)} · "
            f"失效日期：{expiry} · 最近核验：{F.fmt_date(verified)}"
            + (f" · 相关度：{float(score):.2f}" if isinstance(score, (int, float)) else "")
        )
        summary = policy.get("summary") or ""
        if summary:
            st.write(summary)
        if tags:
            st.caption("标签：" + "、".join(str(tag) for tag in tags))
        content = policy.get("content") or ""
        if content:
            with st.expander("📖 查看政策全文", expanded=False):
                st.markdown(content)
                st.caption(f"文件：{policy.get('path') or '—'}")
                theme.disclaimer()
        if policy.get("id"):
            st.caption(f"政策编号：{policy.get('id')}")


def _render_check_result(result: dict) -> None:
    ui.section("🕒 政策时效检查结果", "对比知识库 last_verified 与当前日期，提示可能过期的政策。")
    stale = result.get("stale") or []
    ui.metric_row(
        [
            {"label": "已检查政策", "value": f"{int(result.get('checked_total') or 0)}"},
            {"label": "可能过期", "value": f"{len(stale)}"},
            {"label": "最近核验日期", "value": F.fmt_date(result.get("latest_verified"))},
        ],
        columns=3,
    )
    if stale:
        st.warning(f"有 {len(stale)} 篇政策的最近核验日期较早，建议核对最新口径。", icon="⚠️")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "政策编号": item.get("id"),
                        "政策标题": item.get("title"),
                        "最近核验": F.fmt_date(item.get("last_verified")),
                        "距今天数": item.get("days"),
                    }
                    for item in stale
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    else:
        st.success("知识库政策均在有效核验期内。", icon="✅")
    for advice in result.get("advice") or []:
        st.markdown(f"- {advice}")
    st.caption(f"检查时间：{result.get('checked_at') or '—'}")
    theme.disclaimer(result.get("disclaimer"))


def _render_qa_knowledge_section() -> None:
    """问答知识库管理（v1.15）：候选审核发布 + 检索状态。"""
    st.markdown("---")
    ui.section(
        "🗂️ 我的问答知识库",
        "从历史问答沉淀的可复用答案：默认进入「候选」，人工「发布」后才参与回答与上下文注入；政策版本变化后自动失效。",
    )
    try:
        from ledger import qa as qa_store
        from ledger import qa_knowledge

        stats = qa_knowledge.stats()
        candidates = qa_knowledge.list_knowledge("候选")
        published = qa_knowledge.list_knowledge("已发布")
    except Exception as exc:  # noqa: BLE001
        st.warning(f"问答知识库不可用：{exc}")
        return
    counts = stats.get("counts") or {}
    ui.metric_row(
        [
            {"label": "知识条目", "value": f"{stats.get('total', 0)} 条"},
            {"label": "待审核候选", "value": f"{counts.get('候选', 0)} 条"},
            {"label": "已发布", "value": f"{counts.get('已发布', 0)} 条"},
            {"label": "知识命中", "value": f"{stats.get('hits', 0)} 次"},
        ],
        columns=4,
    )

    filter_cols = st.columns([2, 3, 2])
    with filter_cols[0]:
        status_filter = st.selectbox(
            "状态", ["全部", *qa_knowledge.STATUSES], key="qa_kb_status_filter"
        )
    with filter_cols[1]:
        keyword = st.text_input("关键词", key="qa_kb_keyword", placeholder="问题或回答关键词")
    with filter_cols[2]:
        st.write("")
        if st.button("🔄 刷新", key="qa_kb_refresh", width="stretch"):
            st.rerun()
    rows = qa_knowledge.list_knowledge(None if status_filter == "全部" else status_filter, keyword=keyword)
    if rows:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "编号": row.get("id"),
                        "状态": row.get("status"),
                        "问题": str(row.get("question") or "")[:60],
                        "回答": str(row.get("answer") or "")[:80],
                        "命中": row.get("hit_count"),
                        "来源问答": row.get("source_qa_id"),
                    }
                    for row in rows
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        options = {
            f"{row.get('question','')[:40]}（{row.get('id')} · {row.get('status')}）": row for row in rows
        }
        selected = st.selectbox("选择知识条目", list(options), key="qa_kb_selected")
        entry = options[selected]
        action_cols = st.columns(3)
        with action_cols[0]:
            if st.button("✅ 发布", key="qa_kb_publish", width="stretch"):
                try:
                    qa_knowledge.set_status(str(entry.get("id")), "已发布")
                    st.success("已发布，将参与高置信回答与上下文注入。")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"发布失败：{exc}")
        with action_cols[1]:
            if st.button("🚫 驳回", key="qa_kb_reject", width="stretch"):
                try:
                    qa_knowledge.set_status(str(entry.get("id")), "已驳回")
                    st.success("已驳回，不再参与回答。")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"驳回失败：{exc}")
        with action_cols[2]:
            if st.button("⏳ 标记过期", key="qa_kb_expire", width="stretch"):
                try:
                    qa_knowledge.set_status(str(entry.get("id")), "已过期")
                    st.success("已标记过期，不再参与回答。")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"标记失败：{exc}")
    else:
        st.info("暂无符合条件的知识条目。")

    with st.expander("➕ 从最近问答中存为候选", expanded=False):
        try:
            recent = qa_store.list_recent(20)
        except Exception:  # noqa: BLE001
            recent = []
        existing = {str(item.get("normalized") or "") for item in qa_knowledge.list_knowledge(None)}
        choices = {
            f"{str(item.get('question') or '')[:50]}（{item.get('created_at') or ''}）": item
            for item in recent
            if str(item.get("normalized") or "") not in existing
        }
        if not choices:
            st.caption("最近没有新的问答可沉淀（或都已存在知识库中）。")
        else:
            selected_qa = st.selectbox("问答记录", list(choices), key="qa_kb_source_qa")
            if st.button("📌 存为候选知识", key="qa_kb_from_qa", type="primary", width="stretch"):
                try:
                    record = qa_knowledge.promote_from_history(str(choices[selected_qa].get("id")))
                    st.success(f"已存为候选知识 {record.get('id')}，请在上方审核发布。")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"保存失败：{exc}")


def render() -> None:
    """渲染政策知识库页。"""
    ui.page_header(
        "政策知识库",
        "本地政策库检索：每条政策标注来源、生效日期与最近核验日期，避免引用过期口径。",
        icon="📚",
    )
    retriever = runtime.load("knowledge.retriever")
    if retriever is None:
        ui.module_missing("政策知识库", "knowledge.retriever", detail=runtime.error_of("knowledge.retriever"))
        theme.disclaimer()
        return

    try:
        all_policies = cache.policies()
    except Exception as exc:  # noqa: BLE001
        ui.failure("政策知识库加载失败。", exc, show_traceback=False)
        all_policies = []

    categories = _categories(all_policies)
    filter_cols = st.columns([3, 2, 2])
    with filter_cols[0]:
        query = st.text_input(
            "搜索政策", placeholder="如：小规模纳税人 起征点 / 研发加计扣除 / 印花税", key="policy_query"
        )
    with filter_cols[1]:
        category = st.selectbox("分类筛选", categories, index=0, key="policy_category")
    with filter_cols[2]:
        st.write("")
        refine = st.checkbox("精确检索（关键词打分）", value=True, key="policy_precise")

    if not all_policies:
        ui.empty_state(
            "政策知识库为空",
            "knowledge/policy_files 下暂无政策文件，请确认知识库模块已就绪。",
            icon="📚",
            show_seed=False,
        )
        theme.disclaimer()
        return

    results = _search(query, category) if refine else [
        policy for policy in all_policies
        if category in ("", "全部") or str(policy.get("category")) == category
    ]
    ui.section(
        "🔎 检索结果",
        f"共 {len(all_policies)} 篇政策，当前展示 {len(results)} 篇。",
    )
    if not results:
        st.info("没有匹配的政策，请调整关键词或分类。", icon="🔎")
    for index, policy in enumerate(results):
        _render_card(policy, index)

    if results:
        theme.disclaimer()

    st.markdown("---")
    ui.section("🕒 政策时效检查", "检查知识库中政策的 last_verified 是否超过 90 天。")
    check_cols = st.columns([2, 2, 3])
    with check_cols[0]:
        days = st.number_input("核验有效期（天）", min_value=7, max_value=730, value=90, step=7, key="policy_stale_days")
    with check_cols[1]:
        st.write("")
        run_check = st.button("检查政策时效", key="policy_check", type="primary", width="stretch")
    with check_cols[2]:
        stats = {}
        try:
            stats = cache.policy_stats()
        except Exception:  # noqa: BLE001
            stats = {}
        if stats:
            st.caption(f"知识库：共 {stats.get('total', 0)} 篇 · 分类 {len(stats.get('categories') or {})} 个")

    if run_check:
        checker = runtime.load("knowledge.update_checker")
        if checker is None or not hasattr(checker, "check_policies"):
            ui.module_missing("政策时效检查", "knowledge.update_checker", detail=runtime.error_of("knowledge.update_checker"))
        else:
            try:
                with st.spinner("正在检查政策时效…"):
                    result = dict(checker.check_policies(int(days)))
            except Exception as exc:  # noqa: BLE001
                ui.failure("政策时效检查失败。", exc)
            else:
                st.session_state["_policy_check_result"] = result

    result = st.session_state.get("_policy_check_result")
    if result:
        _render_check_result(result)
    else:
        st.caption("点击「检查政策时效」后展示检查结果。")
        theme.disclaimer()
    _render_qa_knowledge_section()
