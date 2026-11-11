"""AI 问答历史 / 本地缓存 / 用户反馈（v1.14，本地优先）。

* 每次提问与回答写入当前公司账套的 ``qa_history``；
* 相同问题（规范化 + 同政策版本 + 未被踩）可命中缓存，直接本地回答；
* 👍/👎 反馈用于决定缓存是否继续使用，并作为后续知识库候选；
* 数据按公司隔离（各公司独立 CSV），不上传、不跨公司共享。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any

import config
from engine import policy_params
from ledger import store
from ledger.audit import log_user_action

TABLE = "qa_history"
FEEDBACK_VALUES: tuple[str, ...] = ("", "赞", "踩")


def normalize_question(text: str) -> str:
    """问题规范化：去空白/标点、统一大小写，用于精确缓存匹配。"""
    value = str(text or "").strip().lower()
    value = re.sub(r"[\s\u3000]+", "", value)
    value = re.sub(r"[，。！？、；：,.!?;:\"“”‘’（）()\[\]【】<>《》~～]+", "", value)
    return value


def current_policy_version() -> str:
    try:
        return str(policy_params.get_version().get("id") or "")
    except Exception:  # noqa: BLE001 - 政策版本不可用时退化为空
        return ""


def _is_positive(record: dict[str, Any]) -> bool:
    return str(record.get("feedback") or "") != "踩"


def find_cached(
    question: str,
    *,
    policy_version: str | None = None,
    max_age_days: int = 120,
) -> dict[str, Any] | None:
    """查找可复用的本地答案：同规范化问题、未被踩、同政策版本且在有效期内。"""
    normalized = normalize_question(question)
    if not normalized:
        return None
    version = current_policy_version() if policy_version is None else str(policy_version)
    cutoff = (datetime.now() - timedelta(days=max_age_days)).strftime("%Y-%m-%d %H:%M:%S")
    candidates = [
        row
        for row in store.table(TABLE).read()
        if str(row.get("normalized") or "") == normalized
        and _is_positive(row)
        and str(row.get("policy_version") or "") == version
        and str(row.get("created_at") or "") >= cutoff
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda row: (str(row.get("feedback") or "") != "赞", str(row.get("created_at") or "")), reverse=True)
    return candidates[0]


def record_hit(qa_id: str) -> dict[str, Any]:
    """记录一次缓存命中（用于统计复用率）。"""
    rows = [row for row in store.table(TABLE).read() if str(row.get("id")) == str(qa_id)]
    if not rows:
        raise KeyError(f"问答记录不存在：{qa_id}")
    current = int(rows[0].get("hit_count") or 0)
    return store.table(TABLE).update(str(qa_id), {"hit_count": current + 1})


def log_qa(
    question: str,
    answer: str,
    *,
    intent: str = "",
    source: str = "local",
    model: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    policy_version: str | None = None,
) -> dict[str, Any]:
    """写入一条问答记录（默认当前公司），返回记录。"""
    record = store.table(TABLE).append(
        {
            "question": str(question or "").strip()[:2000],
            "normalized": normalize_question(question),
            "answer": str(answer or "")[:8000],
            "intent": str(intent or ""),
            "source": str(source or ""),
            "model": str(model or ""),
            "tool_calls_json": list(tool_calls or []),
            "feedback": "",
            "hit_count": 0,
            "policy_version": current_policy_version() if policy_version is None else str(policy_version),
        }
    )
    return record


def set_feedback(qa_id: str, feedback: str) -> dict[str, Any]:
    """设置反馈：``赞`` / ``踩`` / 空字符串（取消）。"""
    value = str(feedback or "").strip()
    if value not in FEEDBACK_VALUES:
        raise ValueError(f"反馈值「{value}」不合法，可选：赞、踩、空")
    updated = store.table(TABLE).update(str(qa_id), {"feedback": value})
    log_user_action("AI 问答反馈", f"{qa_id}：{value or '取消'}")
    return updated


def find_by_id(qa_id: str) -> dict[str, Any] | None:
    return store.table(TABLE).get(str(qa_id))


def list_recent(limit: int = 30) -> list[dict[str, Any]]:
    rows = sorted(store.table(TABLE).read(), key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return rows[: int(limit)]


def stats() -> dict[str, Any]:
    rows = store.table(TABLE).read()
    total_hits = sum(int(row.get("hit_count") or 0) for row in rows)
    return {
        "total": len(rows),
        "liked": sum(1 for row in rows if str(row.get("feedback") or "") == "赞"),
        "disliked": sum(1 for row in rows if str(row.get("feedback") or "") == "踩"),
        "cache_hits": total_hits,
        "local": sum(1 for row in rows if str(row.get("source") or "") == "local"),
        "llm": sum(1 for row in rows if str(row.get("source") or "") == "llm"),
    }


__all__ = [
    "normalize_question",
    "find_cached",
    "record_hit",
    "log_qa",
    "set_feedback",
    "find_by_id",
    "list_recent",
    "stats",
    "FEEDBACK_VALUES",
]
