"""问答历史/本地缓存/反馈测试（v1.14）。"""

from __future__ import annotations

import pytest

from ledger import qa


def test_normalize_question():
    assert qa.normalize_question(" 这个季度开了 8 万普票，增值税怎么算？ ") == "这个季度开了8万普票增值税怎么算"
    assert qa.normalize_question("How to  报税？") == "howto报税"


def test_log_cache_feedback():
    record = qa.log_qa("这个季度增值税怎么算？", "本地回答：免征。", intent="calc_vat", source="local")
    assert record["id"].startswith("QA")
    cached = qa.find_cached("这个季度增值税怎么算？")
    assert cached is not None and cached["id"] == record["id"]

    qa.record_hit(record["id"])
    qa.set_feedback(record["id"], "踩")
    assert qa.find_cached("这个季度增值税怎么算？") is None

    qa.set_feedback(record["id"], "赞")
    assert qa.find_cached("这个季度增值税怎么算？") is not None


def test_cache_invalidated_by_policy_version():
    record = qa.log_qa("测试政策问题", "旧版本答案", policy_version="old-version")
    assert qa.find_cached("测试政策问题", policy_version="new-version") is None
    assert qa.find_cached("测试政策问题", policy_version="old-version") is not None


def test_invalid_feedback():
    record = qa.log_qa("测试", "答案")
    with pytest.raises(ValueError):
        qa.set_feedback(record["id"], "一般")


def test_stats():
    qa.log_qa("问题A", "答案A", source="local")
    qa.log_qa("问题B", "答案B", source="llm")
    stats = qa.stats()
    assert stats["total"] == 2
    assert stats["local"] == 1 and stats["llm"] == 1
