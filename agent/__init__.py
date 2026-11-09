"""Agent 编排层：DeepSeek 对话、意图路由、工具调用与本地降级回复。

对外常用入口：

* :func:`run_turn` / :func:`run_agent`：执行一轮对话（有 Key 走 DeepSeek + 工具循环，
  无 Key 自动回退本地规则模式）；
* :func:`local_reply`：无 API Key 的确定性回复；
* :func:`build_context`：企业画像 + 政策参数 + 台账摘要 + 截止日上下文；
* :func:`detect_intent` / :func:`route`：意图识别与工具路由；
* :func:`call_tool` / :func:`tool_specs`：13 个确定性工具；
* :class:`DeepSeekClient` / :func:`get_client` / :func:`parse_json`：DeepSeek 客户端。
"""

from __future__ import annotations

from agent.llm_client import DeepSeekClient, DeepSeekError, get_client, parse_json
from agent.prompt_templates import (
    DISCLAIMER,
    INTENT_PROMPT,
    PERSONA,
    SYSTEM_PROMPT,
    build_system_prompt,
)
from agent.intent_router import INTENTS, detect_intent, extract_entities, route
from agent.tools import TOOL_NAMES, call_tool, describe_tools, tool_specs
from agent.orchestrator import build_context, local_reply, run_agent, run_turn

__all__ = [
    # 客户端
    "DeepSeekClient",
    "DeepSeekError",
    "get_client",
    "parse_json",
    # Prompt
    "DISCLAIMER",
    "PERSONA",
    "SYSTEM_PROMPT",
    "INTENT_PROMPT",
    "build_system_prompt",
    # 意图
    "INTENTS",
    "detect_intent",
    "extract_entities",
    "route",
    # 工具
    "TOOL_NAMES",
    "tool_specs",
    "call_tool",
    "describe_tools",
    # 编排
    "build_context",
    "local_reply",
    "run_turn",
    "run_agent",
]
