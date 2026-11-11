"""已审核问答知识库（v1.15）：候选 → 审核发布 → 检索 → 注入上下文。

设计原则
--------
* **不自动信任 AI 回答**：从问答历史转来的条目默认是「候选」，需人工发布；
* **政策版本绑定**：政策版本变化后旧知识自动不参与检索；
* **本地检索**：中文 bigram + 意图加权，不依赖向量库；
* **小白可用**：管理动作只有「通过 / 驳回 / 过期」，不需要填标签术语。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from engine.policy_params import get_version
from ledger import store
from ledger.audit import log_user_action
from ledger.qa import find_by_id as find_qa
from ledger.qa import normalize_question

TABLE = "qa_knowledge"
STATUSES: tuple[str, ...] = ("候选", "已发布", "已驳回", "已过期")
SCOPES: tuple[str, ...] = ("company", "global")
HIGH_CONFIDENCE = 0.72
MEDIUM_CONFIDENCE = 0.45


def current_policy_version() -> str:
    try:
        return str(get_version().get("id") or "")
    except Exception:  # noqa: BLE001
        return ""


def _bigrams(text: str) -> set[str]:
    value = normalize_question(text)
    if len(value) < 2:
        return {value} if value else set()
    return {value[index : index + 2] for index in range(len(value) - 1)}


def similarity(question: str, knowledge: dict[str, Any]) -> float:
    """0–1 相似度：规范化完全相同为 1；否则用 bigram Dice + 意图匹配。"""
    left = normalize_question(question)
    right = str(knowledge.get("normalized") or "")
    if left and left == right:
        return 1.0
    left_set = _bigrams(question)
    right_set = _bigrams(right)
    if not left_set or not right_set:
        return 0.0
    dice = 2 * len(left_set & right_set) / (len(left_set) + len(right_set))
    return round(min(dice, 0.98), 4)


def add_knowledge(
    question: str,
    answer: str,
    *,
    intent: str = "",
    status: str = "候选",
    scope: str = "company",
    source_qa_id: str = "",
    note: str = "",
) -> dict[str, Any]:
    """新增知识条目；同一规范化问题已存在时更新该条目（避免重复）。"""
    if status not in STATUSES:
        raise ValueError(f"知识状态「{status}」不合法，可选：{'、'.join(STATUSES)}")
    if scope not in SCOPES:
        raise ValueError(f"知识范围「{scope}」不合法，可选：{'、'.join(SCOPES)}")
    normalized = normalize_question(question)
    if not normalized or not str(answer or "").strip():
        raise ValueError("问题与回答都不能为空")
    existing = next(
        (row for row in store.table(TABLE).read() if str(row.get("normalized") or "") == normalized),
        None,
    )
    payload = {
        "question": str(question).strip()[:1000],
        "normalized": normalized,
        "answer": str(answer).strip()[:8000],
        "intent": str(intent or ""),
        "status": status,
        "scope": scope,
        "source_qa_id": str(source_qa_id or ""),
        "policy_version": current_policy_version(),
        "note": str(note or ""),
    }
    if existing:
        record = store.table(TABLE).update(str(existing.get("id")), payload)
        log_user_action("更新问答知识", f"{record.get('id')}：{payload['question'][:40]}")
    else:
        record = store.table(TABLE).append(payload)
        log_user_action("新增问答知识", f"{record.get('id')}：{payload['question'][:40]}")
    return record


def promote_from_history(qa_id: str, *, note: str = "") -> dict[str, Any]:
    """把一条问答历史转为候选知识（不自动发布）。"""
    record = find_qa(qa_id)
    if not record:
        raise KeyError(f"问答记录不存在：{qa_id}")
    return add_knowledge(
        str(record.get("question") or ""),
        str(record.get("answer") or ""),
        intent=str(record.get("intent") or ""),
        status="候选",
        source_qa_id=str(qa_id),
        note=note,
    )


def list_knowledge(
    status: str | None = None,
    *,
    keyword: str = "",
) -> list[dict[str, Any]]:
    rows = store.table(TABLE).read()
    if status:
        rows = [row for row in rows if str(row.get("status")) == status]
    text = str(keyword or "").strip()
    if text:
        rows = [
            row
            for row in rows
            if text in str(row.get("question") or "") or text in str(row.get("answer") or "")
        ]
    return sorted(rows, key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)


def set_status(knowledge_id: str, status: str) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"知识状态「{status}」不合法，可选：{'、'.join(STATUSES)}")
    record = store.table(TABLE).update(str(knowledge_id), {"status": status})
    log_user_action("问答知识审核", f"{knowledge_id} → {status}")
    return record


def update_knowledge(knowledge_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    allowed = {"question", "answer", "intent", "scope", "note"}
    clean = {key: value for key, value in patch.items() if key in allowed}
    if "question" in clean:
        clean["normalized"] = normalize_question(str(clean["question"]))
    return store.table(TABLE).update(str(knowledge_id), clean)


def search(question: str, *, limit: int = 3, published_only: bool = True) -> list[dict[str, Any]]:
    """检索知识库，返回带 score 的条目（降序）。"""
    version = current_policy_version()
    rows = list_knowledge("已发布" if published_only else None)
    results: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("policy_version") or "") and str(row.get("policy_version")) != version:
            continue
        score = similarity(question, row)
        if score <= 0:
            continue
        item = dict(row)
        item["score"] = score
        results.append(item)
    results.sort(key=lambda item: float(item["score"]), reverse=True)
    return results[: max(int(limit), 1)]


def best_match(question: str) -> dict[str, Any] | None:
    hits = search(question, limit=1)
    return hits[0] if hits else None


def format_context(hits: list[dict[str, Any]], *, limit: int = 2) -> str:
    """把检索结果整理为可注入 LLM 的参考上下文（明确标注“历史审核问答，仅参考”）。"""
    selected = [item for item in hits if float(item.get("score") or 0) >= MEDIUM_CONFIDENCE][:limit]
    if not selected:
        return ""
    lines = ["【本地已审核问答参考（仅作参考，需结合当前公司数据与现行政策）】"]
    for item in selected:
        lines.append(f"问题：{item.get('question')}")
        lines.append(f"回答：{item.get('answer')}")
        if item.get("source_qa_id"):
            lines.append(f"来源：历史问答 {item.get('source_qa_id')}")
        lines.append("")
    return "\n".join(lines).strip()


def record_hit(knowledge_id: str) -> dict[str, Any]:
    rows = [row for row in store.table(TABLE).read() if str(row.get("id")) == str(knowledge_id)]
    if not rows:
        raise KeyError(f"知识条目不存在：{knowledge_id}")
    return store.table(TABLE).update(str(knowledge_id), {"hit_count": int(rows[0].get("hit_count") or 0) + 1})


def stats() -> dict[str, Any]:
    rows = store.table(TABLE).read()
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "候选")
        counts[status] = counts.get(status, 0) + 1
    return {"total": len(rows), "counts": counts, "hits": sum(int(row.get("hit_count") or 0) for row in rows)}


__all__ = [
    "STATUSES",
    "SCOPES",
    "HIGH_CONFIDENCE",
    "MEDIUM_CONFIDENCE",
    "add_knowledge",
    "promote_from_history",
    "list_knowledge",
    "set_status",
    "update_knowledge",
    "search",
    "best_match",
    "format_context",
    "record_hit",
    "stats",
    "similarity",
]
