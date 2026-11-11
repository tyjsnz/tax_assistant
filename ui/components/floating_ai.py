"""全局侧边抽屉式 AI 助手：任何页面右侧滑出，不打断当前页面操作。

设计：
* 右下角常驻 🤖 悬浮按钮；点击后右侧滑出抽屉（非模态，页面仍可操作）；
* 抽屉内是聊天界面：历史记录、输入框、发送、清空、跳转完整 AI 页面；
* 回复采用**渐进式输出**（逐段渲染），先给用户"正在生成"的连续反馈；
* 无 DeepSeek Key 时自动使用离线确定性回答，任何异常都不影响当前页面。
"""

from __future__ import annotations

import time
from typing import Iterator

import streamlit as st

import config

HISTORY_KEY = "floating_ai_history"
OPEN_KEY = "_floating_ai_drawer_open"
MAX_HISTORY = 20

_CSS = """
<style>
  /* 右下角悬浮按钮 */
  div.st-key-floating_ai {
    position: fixed !important;
    right: 24px !important;
    bottom: 26px !important;
    z-index: 100000 !important;
    width: auto !important;
  }
  div.st-key-floating_ai button {
    width: 58px !important; height: 58px !important; min-height: 58px !important;
    padding: 0 !important; border-radius: 50% !important; font-size: 24px !important;
    background: linear-gradient(135deg, #37e6b0 0%, #5b9dff 100%) !important;
    color: #06131c !important; border: none !important;
    box-shadow: 0 12px 32px rgba(55, 230, 176, 0.45), 0 4px 14px rgba(0, 0, 0, 0.3) !important;
  }
  div.st-key-floating_ai button:hover { transform: translateY(-2px); }

  /* 右侧抽屉 */
  div.st-key-floating_ai_drawer {
    position: fixed !important;
    top: 0 !important; right: 0 !important; bottom: 0 !important;
    width: min(430px, 94vw) !important;
    z-index: 1000000 !important;
    background: #0b1220 !important;
    border-left: 1px solid rgba(55, 230, 176, 0.25) !important;
    box-shadow: -18px 0 60px rgba(0, 0, 0, 0.55) !important;
    padding: 66px 16px 12px !important;
    overflow-y: auto !important;
    overflow-x: hidden !important;
    color: #e8eef8 !important;
    word-break: break-word !important;
  }
  div.st-key-floating_ai_drawer [data-testid="stAlert"],
  div.st-key-floating_ai_drawer [data-testid="stNotification"],
  div.st-key-floating_ai_drawer [data-testid="stMarkdownContainer"] { max-width: 100% !important; overflow-x: hidden !important; }
  div.st-key-floating_ai_drawer h3 { margin: 0 0 4px !important; font-size: 18px; color: #e8eef8 !important; }
  div.st-key-floating_ai_drawer [data-testid="stMarkdownContainer"] p,
  div.st-key-floating_ai_drawer [data-testid="stMarkdownContainer"] li,
  div.st-key-floating_ai_drawer [data-testid="stMarkdownContainer"] strong { color: #e8eef8 !important; }
  div.st-key-floating_ai_drawer input { color: #e8eef8 !important; }
  div.st-key-floating_ai_drawer [data-testid="stChatMessage"] { background: rgba(255, 255, 255, 0.06) !important; border: 1px solid rgba(255, 255, 255, 0.1) !important; }
  div.st-key-floating_ai_drawer .ai-drawer-sub { color: #9aa8bd; font-size: 12px; margin-bottom: 10px; }
  div.st-key-floating_ai_drawer [data-testid="stChatMessage"] { background: rgba(255,255,255,.03); border-radius: 12px; }
  @media (max-width: 620px) {
    div.st-key-floating_ai { right: 14px !important; bottom: 16px !important; }
    div.st-key-floating_ai button { width: 52px !important; height: 52px !important; min-height: 52px !important; }
    div.st-key-floating_ai_drawer { width: 100vw !important; }
  }
</style>
"""


def _history() -> list[dict[str, str]]:
    history = st.session_state.get(HISTORY_KEY)
    return list(history) if isinstance(history, list) else []


def _append(role: str, content: str, **extra: str) -> None:
    history = _history()
    item: dict[str, str] = {"role": role, "content": str(content)}
    item.update({key: str(value) for key, value in extra.items() if value})
    history.append(item)
    st.session_state[HISTORY_KEY] = history[-MAX_HISTORY:]


def _stream_chunks(text: str) -> Iterator[str]:
    """渐进式展示：按 2–4 字/段输出，兼顾速度与"正在生成"的连续感。"""
    content = str(text or "")
    if not content:
        yield "（无内容）"
        return
    step = 3
    for index in range(0, len(content), step):
        chunk = content[index : index + step]
        yield chunk
        time.sleep(0.012)


def _render_feedback(qa_id: str) -> None:
    """点赞/踩：用于决定本地缓存是否继续复用，并作为知识库候选。"""
    try:
        from ledger import qa as qa_store

        record = qa_store.find_by_id(qa_id) or {}
        current = str(record.get("feedback") or "")
    except Exception:  # noqa: BLE001
        current = ""
    cols = st.columns([1, 1, 1, 5])
    with cols[0]:
        if st.button("👍", key=f"qa_like_{qa_id}", help="有帮助，继续复用该回答"):
            from ledger import qa as qa_store

            qa_store.set_feedback(qa_id, "赞")
            st.rerun()
    with cols[1]:
        if st.button("👎", key=f"qa_dislike_{qa_id}", help="没帮助，下次不再复用"):
            from ledger import qa as qa_store

            qa_store.set_feedback(qa_id, "踩")
            st.rerun()
    with cols[2]:
        # 不用额外的成功提示（会撑高/撑宽抽屉）：直接检查是否已转候选，按钮状态即可反馈
        saved = False
        try:
            from ledger import qa as qa_store_check
            from ledger import qa_knowledge

            source = qa_store_check.find_by_id(qa_id) or {}
            normalized = str(source.get("normalized") or "")
            saved = any(
                str(item.get("source_qa_id") or "") == qa_id
                or (normalized and str(item.get("normalized") or "") == normalized)
                for item in qa_knowledge.list_knowledge(None)
            )
        except Exception:  # noqa: BLE001
            saved = False
        if saved:
            st.button(
                "✅ 已存为候选",
                key=f"qa_saved_{qa_id}",
                disabled=True,
                help="已进入候选知识；可在「📚 政策知识库 → 我的问答知识库」审核发布",
                width="stretch",
            )
        elif st.button(
            "📌 存为知识",
            key=f"qa_save_{qa_id}",
            help="转为候选知识，可在「📚 政策知识库 → 我的问答知识库」审核发布",
            width="stretch",
        ):
            try:
                from ledger import qa_knowledge

                qa_knowledge.promote_from_history(qa_id)
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.warning(f"保存失败：{exc}")
    with cols[3]:
        if current:
            st.caption(f"已反馈：{'👍 有帮助' if current == '赞' else '👎 没帮助'}")


def _stream_answer(text: str, history: list[dict[str, str]]) -> None:
    """调用本地优先的流式 Agent，并渲染来源标签、流式文本与反馈。"""
    from agent import orchestrator

    generator = orchestrator.run_turn_stream(text, history=history)
    try:
        meta = next(generator)
    except StopIteration:
        meta = {}
    source = str(meta.get("source") or "local")
    qa_id = str(meta.get("qa_id") or "")
    badge = {
        "local": "📗 本地计算/知识库", "cache": "♻️ 本地缓存",
        "knowledge": "🗂️ 已审核知识库", "llm": "🤖 AI 流式回答",
        "automation": "⚡ AI 代办",
    }.get(source, "💬 回答")
    with st.chat_message("assistant"):
        st.caption(f"{badge} · 仅供参考，以主管税务机关核定为准")
        reply = ""
        if source in ("local", "cache", "knowledge"):
            # 本地/缓存：一次性拿到答案后逐字展示，避免等待感
            events = list(generator)
            done = next((event for event in reversed(events) if event.get("type") == "done"), {})
            reply = str(done.get("reply") or "")
            qa_id = str(done.get("qa_id") or qa_id)
            st.write_stream(_stream_chunks(reply))
        else:
            placeholder = st.empty()
            buffer: list[str] = []
            finish: dict[str, object] = {}
            try:
                for event in generator:
                    if event.get("type") == "text":
                        buffer.append(str(event.get("text") or ""))
                        placeholder.markdown("".join(buffer) + "▌")
                    elif event.get("type") == "done":
                        finish = event
            except Exception as exc:  # noqa: BLE001
                st.warning(f"流式回答中断：{exc}")
            reply = str(finish.get("reply") or "".join(buffer))
            qa_id = str(finish.get("qa_id") or qa_id)
            placeholder.markdown(reply or "暂时没有生成回复。")
    _append("assistant", reply, qa_id=qa_id, source=source)


def _render_automation_pending() -> None:
    """AI 识别到代办请求后，在抽屉里显示预览 + 确认执行卡。"""
    pending = st.session_state.get("_ai_automation_pending")
    if not pending:
        return
    intent = pending.get("intent") or {}
    preview = pending.get("preview") or {}
    action = str(intent.get("action") or "")
    with st.container(border=True):
        st.markdown(f"**⚡ 待确认代办：{intent.get('label') or ''}**")
        if action == "payroll":
            cols = st.columns(2)
            with cols[0]:
                st.metric("在职员工", f"{preview.get('active_count', 0)} 人")
            with cols[1]:
                st.metric("待生成", f"{preview.get('pending_count', 0)} 人")
            if preview.get("pending_names"):
                st.caption("待生成：" + "、".join(preview["pending_names"]))
        elif action == "ingest":
            summary = preview.get("summary") or {}
            cols = st.columns(2)
            with cols[0]:
                st.metric("待匹配发票", f"{summary.get('invoices_pending', 0)} 张")
            with cols[1]:
                st.metric("待匹配流水", f"{summary.get('transactions_pending', 0)} 笔")
        elif action == "filing":
            cols = st.columns(2)
            with cols[0]:
                st.metric("申报表", f"{preview.get('form_count', 0)} 张")
            with cols[1]:
                st.metric("合计税费", f"¥{preview.get('total_tax', 0):,.2f}")
        elif action == "correction":
            summary = preview.get("summary") or {}
            cols = st.columns(3)
            with cols[0]:
                st.metric("异常员工", f"{summary.get('employees', 0)} 人")
            with cols[1]:
                st.metric("涉及月份", f"{summary.get('months', 0)} 个")
            with cols[2]:
                st.metric("涉及工资", f"¥{summary.get('income', 0):,.2f}")
        for warning in preview.get("warnings") or []:
            st.warning(warning)
        st.caption("执行前自动备份；不会登录电子税务局、不会自动提交申报/缴款。")
        confirmed = st.checkbox("我已核对预览，确认执行", key=f"ai_auto_confirm_{action}")
        cols = st.columns(2)
        with cols[0]:
            if st.button("⚡ 确认执行", key=f"ai_auto_execute_{action}", type="primary",
                         disabled=not confirmed, width="stretch"):
                try:
                    from automation import intents

                    result = intents.execute(intent)
                    message = intents.result_text(action, result)
                    st.session_state.pop("_ai_automation_pending", None)
                    _append("assistant", message, source="automation")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"执行失败：{exc}")
        with cols[1]:
            if st.button("取消", key=f"ai_auto_cancel_{action}", width="stretch"):
                st.session_state.pop("_ai_automation_pending", None)
                _append("assistant", "已取消代办。需要时再告诉我。", source="automation")
                st.rerun()


def _drawer() -> None:
    with st.container(key="floating_ai_drawer"):
        head_cols = st.columns([8, 1])
        with head_cols[0]:
            st.markdown("### 🤖 智小税 AI 助手")
            st.markdown(
                '<div class="ai-drawer-sub">随时提问，不离开当前页面；无 Key 时自动离线回答。</div>',
                unsafe_allow_html=True,
            )
        with head_cols[1]:
            if st.button("✕", key="floating_ai_close", help="收起助手"):
                st.session_state[OPEN_KEY] = False
                st.rerun()

        history = _history()
        chat_cols = st.columns([1, 1])
        with chat_cols[0]:
            if st.button("🧹 清空", key="floating_ai_clear", width="stretch"):
                st.session_state[HISTORY_KEY] = []
                st.rerun()
        with chat_cols[1]:
            if st.button("↗ 完整页面", key="floating_ai_full_page", width="stretch"):
                from ui.components import nav

                st.session_state[OPEN_KEY] = False
                nav.goto(nav.PAGE_ASSISTANT)

        if not history:
            st.info("试试：「这个季度开了 8 万普票，增值税怎么算？」「员工离职后还在申报个税怎么办？」")
        for message in history:
            role = "user" if str(message.get("role") or "assistant") == "user" else "assistant"
            with st.chat_message(role):
                st.markdown(str(message.get("content") or ""))
                qa_id = str(message.get("qa_id") or "")
                if role == "assistant" and qa_id:
                    source = str(message.get("source") or "")
                    badge = {
                        "local": "📗 本地计算", "cache": "♻️ 本地缓存",
                        "knowledge": "🗂️ 已审核知识库", "llm": "🤖 AI 流式",
                        "automation": "⚡ AI 代办",
                    }.get(source, "💬 回答")
                    st.caption(f"{badge} · 仅供参考")
                    _render_feedback(qa_id)

        _render_automation_pending()
        with st.form("floating_ai_form", clear_on_submit=True):
            prompt = st.text_input(
                "问智小税…", key="floating_ai_prompt", label_visibility="collapsed",
                placeholder="例如：这个季度开了 8 万普票，增值税怎么算？",
            )
            submitted = st.form_submit_button("发送", type="primary", width="stretch")
        if submitted and str(prompt or "").strip():
            text = str(prompt).strip()
            previous_history = _history()[-10:]
            _append("user", text)
            with st.chat_message("user"):
                st.markdown(text)
            try:
                from automation import intents

                intent = intents.detect(text)
            except Exception:  # noqa: BLE001
                intent = None
            if intent:
                try:
                    preview = intents.preview(intent)
                    st.session_state["_ai_automation_pending"] = {
                        "intent": intent, "preview": preview,
                    }
                    _append(
                        "assistant",
                        f"⚡ 我可以帮你「{intent['label']}」。请先核对下面预览，确认后执行；执行前会自动备份。",
                        source="automation",
                    )
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.warning(f"代办预览失败，改为普通回答：{exc}")
            _stream_answer(text, previous_history)
            st.rerun()
        st.caption(config.DISCLAIMER)


def render() -> None:
    """全局渲染：悬浮按钮 + 可展开的侧边抽屉。"""
    st.markdown(_CSS, unsafe_allow_html=True)
    if st.session_state.get(OPEN_KEY):
        _drawer()
        return
    with st.container(key="floating_ai"):
        if st.button("🤖", key="floating_ai_open", help="智小税 AI 助手：随时提问，不离开当前页面"):
            st.session_state[OPEN_KEY] = True
            st.rerun()
