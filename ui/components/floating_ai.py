"""全局浮动 AI 助手：任何页面右下角常驻按钮，点击弹出对话框，不离开当前页面。

设计：
* 悬浮按钮通过 ``st.container(key="floating_ai")`` 的 ``st-key-floating_ai`` class 固定定位；
* 点击后打开 ``st.dialog``，内部复用 ``agent.orchestrator.run_turn``（无 Key 时自动离线回复）；
* 对话历史独立保存，不影响「🤖 AI 助手」页面的会话；
* 任何异常都不影响当前页面。
"""

from __future__ import annotations

import streamlit as st

import config

HISTORY_KEY = "floating_ai_history"
MAX_HISTORY = 20

_CSS = """
<style>
  div.st-key-floating_ai {
    position: fixed !important;
    right: 24px !important;
    bottom: 26px !important;
    z-index: 99999 !important;
    width: auto !important;
  }
  div.st-key-floating_ai button {
    width: 58px !important;
    height: 58px !important;
    min-height: 58px !important;
    padding: 0 !important;
    border-radius: 50% !important;
    font-size: 24px !important;
    background: linear-gradient(135deg, #37e6b0 0%, #5b9dff 100%) !important;
    color: #06131c !important;
    border: none !important;
    box-shadow: 0 12px 32px rgba(55, 230, 176, 0.45), 0 4px 14px rgba(0, 0, 0, 0.3) !important;
  }
  div.st-key-floating_ai button:hover {
    transform: translateY(-2px);
    box-shadow: 0 16px 40px rgba(55, 230, 176, 0.6) !important;
  }
  @media (max-width: 620px) {
    div.st-key-floating_ai { right: 14px !important; bottom: 16px !important; }
    div.st-key-floating_ai button { width: 52px !important; height: 52px !important; min-height: 52px !important; }
  }
</style>
"""


def _history() -> list[dict[str, str]]:
    history = st.session_state.get(HISTORY_KEY)
    return list(history) if isinstance(history, list) else []


def _append(role: str, content: str) -> None:
    history = _history()
    history.append({"role": role, "content": str(content)})
    st.session_state[HISTORY_KEY] = history[-MAX_HISTORY:]


def _ask(text: str) -> str:
    from agent import orchestrator

    history = [
        {"role": item.get("role") or "user", "content": item.get("content") or ""}
        for item in _history()[-10:]
        if item.get("content")
    ]
    try:
        result = orchestrator.run_turn(text, history=history)
        reply = str(result.get("reply") or "").strip()
        if not reply:
            reply = "暂时没有生成回复，请稍后再试。"
        return reply
    except Exception as exc:  # noqa: BLE001 - 对话框内失败不影响页面
        return f"AI 助手暂时不可用：{exc}"


@st.dialog("🤖 智小税 AI 助手 · 随时问", width="large")
def _dialog() -> None:
    st.caption(
        "无需离开当前页面：可以问算税、记账、工资个税、离职纠错、申报日历或政策。"
        "未配置 DeepSeek Key 时自动使用离线确定性回答。"
    )
    history = _history()
    if not history:
        st.info("试试：「这个季度开了 8 万普票，增值税怎么算？」「员工离职后还在申报个税怎么办？」")
    for message in history:
        role = str(message.get("role") or "assistant")
        with st.chat_message("user" if role == "user" else "assistant"):
            st.markdown(str(message.get("content") or ""))

    prompt = st.chat_input("问智小税…", key="floating_ai_prompt")
    if prompt:
        _append("user", prompt)
        with st.spinner("智小税思考中…"):
            reply = _ask(prompt)
        _append("assistant", reply)
        # 对话框内 st.chat_input 提交会触发整页 rerun，这里标记“自动重新打开”，
        # 由主页面 render() 在 rerun 后重新弹出对话框，用户视觉上不会离开对话。
        st.session_state["_floating_ai_reopen"] = True
        st.rerun()

    cols = st.columns([1, 1, 2])
    with cols[0]:
        if st.button("🧹 清空对话", key="floating_ai_clear", width="stretch"):
            st.session_state[HISTORY_KEY] = []
            st.session_state["_floating_ai_reopen"] = True
            st.rerun()
    with cols[1]:
        if st.button("↗ 完整 AI 页面", key="floating_ai_full_page", width="stretch"):
            from ui.components import nav

            nav.goto(nav.PAGE_ASSISTANT)
    with cols[2]:
        st.caption(config.DISCLAIMER)


def render() -> None:
    """在页面右下角渲染常驻 AI 助手按钮（任何页面均可用）。"""
    st.markdown(_CSS, unsafe_allow_html=True)
    with st.container(key="floating_ai"):
        clicked = st.button(
            "🤖", key="floating_ai_open", help="智小税 AI 助手：随时提问，不离开当前页面"
        )
    # 对话提交后整页 rerun 会关闭对话框，这里自动重新打开，保持“随时对话”的体验。
    if clicked or st.session_state.pop("_floating_ai_reopen", False):
        _dialog()
