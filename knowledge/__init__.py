"""政策知识库：Markdown 政策文件解析、中文 BM25 检索、DeepSeek 政策问答与核验时效检查。

对外常用入口：

* :func:`load_policies` / :func:`list_policies` / :func:`get_policy`：政策文件解析与查询；
* :func:`search` / :func:`build_context`：中文 2-gram BM25 检索与 LLM 上下文；
* :func:`answer`：政策问答（无 Key 返回检索摘要 + 原文片段，有 Key 让 DeepSeek 基于上下文作答）；
* :func:`stale_policies` / :func:`check_policies` / :func:`mark_verified` / :func:`policy_stats`：
  政策核验时效检查与更新日志。
"""

from __future__ import annotations

from knowledge.retriever import (
    POLICY_DIR,
    answer,
    build_context,
    categories,
    get_policy,
    list_policies,
    load_policies,
    parse_frontmatter,
    search,
    tokenize,
)
from knowledge.update_checker import (
    check_policies,
    mark_verified,
    policy_stats,
    stale_policies,
)

__all__ = [
    "POLICY_DIR",
    "load_policies",
    "list_policies",
    "get_policy",
    "search",
    "build_context",
    "answer",
    "categories",
    "tokenize",
    "parse_frontmatter",
    "stale_policies",
    "check_policies",
    "mark_verified",
    "policy_stats",
]
