"""🤖 AI 助手：DeepSeek 多轮对话 + 工具调用轨迹展示。

无 API Key 时使用 ``agent.orchestrator.local_reply`` 的离线确定性回复
（算税 / 台账 / 日历仍可用），并在页面显著位置提示如何配置 Key。
"""

from __future__ import annotations

from typing import Any

import streamlit as st

import config
from ui.components import cache, formatting as F, runtime, theme, ui

MESSAGES_KEY = "assistant_messages"
MODEL_KEY = "assistant_model"

SAMPLE_QUESTIONS = [
    "我这个季度开了 8 万普票，增值税怎么算？",
    "研发费用加计扣除怎么算？需要留哪些资料？",
    "下个月有什么需要申报的？",
    "帮我看看这个季度申报表填得对不对？",
]


def _save_model() -> None:
    """模型切换后持久化到本地设置。"""
    model = st.session_state.get(MODEL_KEY)
    if model:
        try:
            config.save_user_settings({"model": model})
        except Exception:  # noqa: BLE001
            pass


def _offline_notice() -> None:
    if config.is_ai_configured():
        st.caption(
            f"已连接 DeepSeek：{config.mask_key()} · 当前模型 {config.get_llm_config().get('model')}"
        )
        return
    st.info(
        "**离线模式**：算税、台账、日历、申报等功能全部可用，AI 问答使用本地确定性回复。\n\n"
        "如需完整 AI 对话能力，请到「⚙️ 设置」页填写 DeepSeek API Key（仅保存在本地，不上传）。",
        icon="🔌",
    )


def _render_tool_calls(tool_calls: list[dict]) -> None:
    if not tool_calls:
        return
    with st.expander(f"🔧 工具调用轨迹（{len(tool_calls)} 次）", expanded=False):
        for index, call in enumerate(tool_calls, start=1):
            name = call.get("name") or "unknown"
            st.markdown(f"**{index}. `{name}`**")
            arguments = call.get("arguments") or {}
            if arguments:
                st.caption("参数：" + " · ".join(f"{key}={value}" for key, value in arguments.items()))
            outcome = call.get("result")
            if isinstance(outcome, dict):
                if outcome.get("display"):
                    st.caption("结果：" + str(outcome.get("display")))
                elif outcome.get("error"):
                    st.caption("错误：" + str(outcome.get("error")))
                else:
                    st.caption("结果：已返回数据")
            elif outcome is not None:
                st.caption("结果：" + str(outcome))


def _render_message(message: dict) -> None:
    role = message.get("role", "assistant")
    avatar = "🧑‍💼" if role == "user" else "🧮"
    with st.chat_message(role, avatar=avatar):
        st.markdown(message.get("content") or "（空回复）")
        _render_tool_calls(message.get("tool_calls") or [])
        if message.get("error"):
            st.warning(f"提示：{message['error']}")
        if role == "assistant" and message.get("used_llm") is not None:
            st.caption(
                ("🤖 DeepSeek 回复" if message.get("used_llm") else "🔌 离线确定性回复")
                + (f" · 意图：{message.get('intent')}" if message.get("intent") else "")
            )


def _history() -> list[dict]:
    """供 orchestrator 使用的历史消息（仅角色与文本，截取最近 20 条）。"""
    messages = st.session_state.get(MESSAGES_KEY) or []
    return [
        {"role": message.get("role", "assistant"), "content": str(message.get("content") or "")}
        for message in messages[-20:]
        if message.get("content")
    ]


def _render_automation_card() -> None:
    """AI 识别到代办请求后，在完整 AI 页面显示预览 + 确认执行卡。"""
    pending = st.session_state.get("_ai_automation_pending")
    if not pending:
        return
    from automation import intents

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
        elif action == "pipeline":
            filing = preview.get("filing") or {}
            payrolls = preview.get("payrolls") or []
            ingest = preview.get("ingest") or {}
            correction = preview.get("correction") or {}
            cols = st.columns(4)
            with cols[0]:
                st.metric("申报表", f"{filing.get('form_count', 0)} 张")
            with cols[1]:
                st.metric("工资待生成", f"{sum(item.get('pending_count', 0) for item in payrolls)} 人")
            with cols[2]:
                st.metric("待匹配发票", f"{(ingest.get('summary') or {}).get('invoices_pending', 0)} 张")
            with cols[3]:
                st.metric("离职异常", f"{(correction.get('summary') or {}).get('employees', 0)} 人")
        for warning in preview.get("warnings") or []:
            st.warning(warning)
        st.caption("执行前自动备份；不会登录电子税务局、不会自动提交申报/缴款。")
        confirmed = st.checkbox("我已核对预览，确认执行", key=f"assistant_auto_confirm_{action}")
        cols = st.columns(2)
        with cols[0]:
            if st.button("⚡ 确认执行", key=f"assistant_auto_execute_{action}", type="primary",
                         disabled=not confirmed, width="stretch"):
                try:
                    result = intents.execute(intent)
                    message = intents.result_text(action, result)
                    st.session_state.pop("_ai_automation_pending", None)
                    messages = st.session_state.setdefault(MESSAGES_KEY, [])
                    messages.append({"role": "assistant", "content": message, "used_llm": False, "intent": "automation"})
                    st.session_state[MESSAGES_KEY] = messages[-60:]
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"执行失败：{exc}")
        with cols[1]:
            if st.button("取消", key=f"assistant_auto_cancel_{action}", width="stretch"):
                st.session_state.pop("_ai_automation_pending", None)
                messages = st.session_state.setdefault(MESSAGES_KEY, [])
                messages.append({"role": "assistant", "content": "已取消代办。", "used_llm": False, "intent": "automation"})
                st.session_state[MESSAGES_KEY] = messages[-60:]
                st.rerun()


def _handle_prompt(prompt: str) -> None:
    """处理一轮用户输入并追加助手回复。"""
    messages: list[dict] = st.session_state.setdefault(MESSAGES_KEY, [])
    messages.append({"role": "user", "content": prompt})
    with st.chat_message("user", avatar="🧑‍💼"):
        st.markdown(prompt)

    # 优先识别「代办」请求：返回预览确认卡，不直接执行
    try:
        from automation import intents

        intent = intents.detect(prompt)
    except Exception:  # noqa: BLE001
        intent = None
    if intent:
        try:
            preview = intents.preview(intent)
            st.session_state["_ai_automation_pending"] = {"intent": intent, "preview": preview}
            messages.append(
                {
                    "role": "assistant",
                    "content": f"⚡ 我可以帮你「{intent['label']}」。请核对下方预览，确认后执行；执行前会自动备份。",
                    "used_llm": False,
                    "intent": "automation",
                }
            )
            st.session_state[MESSAGES_KEY] = messages[-60:]
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.warning(f"代办预览失败，改为普通回答：{exc}")

    orchestrator = runtime.load("agent.orchestrator")
    if orchestrator is None or not hasattr(orchestrator, "run_turn"):
        ui.module_missing("AI 助手", "agent.orchestrator", detail=runtime.error_of("agent.orchestrator"))
        messages.append(
            {
                "role": "assistant",
                "content": "AI 助手模块尚未就绪，暂时无法对话；算税、台账、申报等本地功能仍可正常使用。",
                "used_llm": False,
                "error": "agent.orchestrator 未就绪",
            }
        )
        return

    model = st.session_state.get(MODEL_KEY) or config.get_llm_config().get("model")
    history = [
        {"role": message.get("role", "assistant"), "content": str(message.get("content") or "")}
        for message in messages[:-1][-20:]
    ]
    with st.chat_message("assistant", avatar="🧮"):
        try:
            with st.spinner("税小智正在思考…"):
                result = dict(orchestrator.run_turn(prompt, history=history, model=model))
        except Exception as exc:  # noqa: BLE001
            ui.failure("AI 对话失败，请检查网络或 API 配置。", exc)
            reply = f"抱歉，本轮对话失败：{exc}"
            messages.append(
                {"role": "assistant", "content": reply, "used_llm": False, "error": str(exc)}
            )
            return
        reply = str(result.get("reply") or result.get("content") or "").strip()
        if not reply:
            reply = "（未能生成回复，请换个问法，或到设置页检查 API Key）"
        tool_calls = result.get("tool_calls") or []
        error = result.get("error")
        st.markdown(reply)
        _render_tool_calls(tool_calls)
        if error:
            st.warning(f"提示：{error}")
        st.caption(
            ("🤖 DeepSeek 回复" if result.get("used_llm") else "🔌 离线确定性回复")
            + (f" · 意图：{result.get('intent')}" if result.get("intent") else "")
        )
    messages.append(
        {
            "role": "assistant",
            "content": reply,
            "tool_calls": tool_calls,
            "used_llm": result.get("used_llm"),
            "intent": result.get("intent"),
            "error": error,
        }
    )
    st.session_state[MESSAGES_KEY] = messages[-60:]


def render() -> None:
    """渲染 AI 助手页。"""
    ui.page_header(
        "AI 助手 · 税小智",
        "算税、记账、政策、申报核对、研发归集与日历提醒，都可以直接问。",
        icon="🤖",
    )
    with st.sidebar:
        st.markdown("**🤖 AI 助手设置**")
        options = list(config.LLM_CONFIG.get("model_options") or ["deepseek-chat"])
        current = config.get_llm_config().get("model") or options[0]
        st.selectbox(
            "对话模型",
            options,
            index=options.index(current) if current in options else 0,
            key=MODEL_KEY,
            on_change=_save_model,
            help="deepseek-chat 响应快；deepseek-reasoner 推理更强但更慢。",
        )
        if st.button("🧹 清空对话", key="assistant_clear", width="stretch"):
            st.session_state[MESSAGES_KEY] = []
            st.rerun()
        st.caption("对话记录仅保存在当前浏览器会话，不写入磁盘。")

    _offline_notice()

    messages: list[dict] = st.session_state.setdefault(MESSAGES_KEY, [])
    if not messages:
        with st.container(border=True):
            st.markdown("**可以这样问我：**")
            for index, question in enumerate(SAMPLE_QUESTIONS):
                if st.button(question, key=f"assistant_sample_{index}", width="stretch"):
                    st.session_state["_assistant_pending"] = question
                    st.rerun()
            st.caption(
                "示例：增值税怎么算 / 分录怎么写 / 研发费用怎么归集 / 申报表对不对 / 下月申报日历。"
            )

    for message in messages:
        _render_message(message)
    _render_automation_card()

    tutorial = (
        "输入你的问题…（例如：这个季度开了 8 万普票，增值税怎么算？）"
        if config.is_ai_configured()
        else "输入你的问题…（当前离线模式：本地算税与台账可用）"
    )
    pending = st.session_state.pop("_assistant_pending", None)
    prompt = st.chat_input(tutorial, key="assistant_chat_input") or pending
    if prompt:
        _handle_prompt(str(prompt))

    theme.disclaimer()
