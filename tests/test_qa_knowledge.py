"""已审核问答知识库测试（v1.15）。"""

from __future__ import annotations

import pytest

from ledger import qa, qa_knowledge


def test_candidate_not_used_until_published():
    qa_knowledge.add_knowledge("测试问题：季度免税额度是多少？", "季度 30 万免税。", status="候选")
    assert qa_knowledge.search("季度免税额度是多少") == []
    entries = qa_knowledge.list_knowledge("候选")
    assert len(entries) == 1
    qa_knowledge.set_status(str(entries[0]["id"]), "已发布")
    hits = qa_knowledge.search("季度免税额度是多少")
    assert hits and hits[0]["score"] >= qa_knowledge.HIGH_CONFIDENCE
    assert "30 万" in hits[0]["answer"]


def test_similarity_and_exact():
    entry = {"normalized": qa.normalize_question("小规模纳税人季度30万免征增值税")}
    assert qa_knowledge.similarity("小规模纳税人季度30万免征增值税", entry) == 1.0
    assert qa_knowledge.similarity("小规模季度三十万免增值税", entry) < 1.0
    assert qa_knowledge.similarity("完全无关的问题", entry) < 0.2


def test_policy_version_invalidates():
    record = qa_knowledge.add_knowledge(
        "旧政策问题", "旧答案", status="已发布", note=""
    )
    # 手动把版本改成旧版，模拟政策升级
    from ledger import store

    store.table("qa_knowledge").update(str(record["id"]), {"policy_version": "old-version"})
    assert qa_knowledge.search("旧政策问题") == []


def test_promote_from_history_dedupes():
    history = qa.log_qa("公司离职员工多申报怎么办", "先停申报再逐月更正。", intent="payroll_correction")
    candidate = qa_knowledge.promote_from_history(str(history["id"]))
    assert candidate["status"] == "候选"
    assert candidate["source_qa_id"] == history["id"]
    again = qa_knowledge.promote_from_history(str(history["id"]))
    assert again["id"] == candidate["id"]
    assert len(qa_knowledge.list_knowledge("候选")) == 1


def test_format_context_filters_low_confidence():
    high = qa_knowledge.add_knowledge("小规模纳税人免税额度", "季度 30 万。", status="已发布")
    hits = qa_knowledge.search("小规模纳税人免税额度")
    context = qa_knowledge.format_context(hits)
    assert "已审核问答参考" in context
    assert "季度 30 万" in context
    assert qa_knowledge.format_context([{**high, "score": 0.1}]) == ""


def test_reject_and_expire():
    record = qa_knowledge.add_knowledge("待审核问题", "答案", status="候选")
    qa_knowledge.set_status(str(record["id"]), "已驳回")
    assert qa_knowledge.search("待审核问题") == []
    with pytest.raises(ValueError):
        qa_knowledge.set_status(str(record["id"]), "不存在的状态")
