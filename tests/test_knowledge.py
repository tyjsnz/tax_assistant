"""政策知识库测试：frontmatter 解析、政策文档格式与数量、BM25 检索、问答、核验时效。

不访问真实 DeepSeek API：有 Key 的分支用 fake client 模拟；无 Key 走本地检索摘要。
"""

from __future__ import annotations

import json

import pytest

import config
import engine
from knowledge import retriever, update_checker
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


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    try:
        config.save_user_settings({"api_key": ""})
    except OSError:  # pragma: no cover
        pass
    yield


# =============================================================================
# frontmatter 与加载
# =============================================================================
def test_parse_frontmatter():
    text = (
        "---\n"
        "title: 测试政策\n"
        "category: 增值税\n"
        "effective_date: 2023-01-01\n"
        "expiry_date: 2027-12-31\n"
        "source: 财政部 税务总局公告2023年第19号\n"
        "last_verified: 2026-11-01\n"
        "tags: [小规模纳税人, 增值税, 免税]\n"
        "---\n\n"
        "## 适用条件\n正文内容。\n"
    )
    meta, body = parse_frontmatter(text)
    assert meta["title"] == "测试政策"
    assert meta["category"] == "增值税"
    assert meta["tags"] == ["小规模纳税人", "增值税", "免税"]
    assert "正文内容" in body and "---" not in body


def test_parse_frontmatter_block_list_and_no_header():
    text = "---\ntitle: 块列表\ntags:\n  - 增值税\n  - 研发\n---\n正文"
    meta, body = parse_frontmatter(text)
    assert meta["tags"] == ["增值税", "研发"]
    assert body == "正文"
    meta2, body2 = parse_frontmatter("# 没有 frontmatter\n正文")
    assert meta2 == {}
    assert "没有 frontmatter" in body2


def test_policy_count_and_required_fields():
    policies = load_policies(force=True)
    assert len(policies) >= 10, f"政策文档不足 10 篇：{len(policies)}"
    required = ("title", "category", "effective_date", "expiry_date", "source", "last_verified", "tags")
    for policy in policies:
        for key in required:
            value = policy["meta"].get(key)
            assert value not in (None, "", []), f"{policy['id']} 缺少 frontmatter 字段 {key}"
        assert policy["last_verified"] == "2026-11-01", policy["id"]
        assert policy["source"], policy["id"]
        assert policy["summary"], policy["id"]
        assert policy["content"]


def test_policy_documents_standard_sections():
    required_sections = ("适用条件", "优惠内容", "办理方式", "留存资料", "注意事项")
    for policy in load_policies():
        for section in required_sections:
            assert section in policy["content"], f"{policy['id']} 缺少「{section}」章节"


def test_policy_files_cover_contract_list():
    ids = {policy["id"] for policy in load_policies()}
    expected = {
        "增值税/小规模纳税人起征点与1%征收率",
        "增值税/六税两费减半征收",
        "增值税/技术合同免征增值税",
        "企业所得税/小型微利企业优惠",
        "企业所得税/研发费用加计扣除",
        "其他税费/印花税",
        "其他税费/个人所得税扣缴",
        "申报流程/云南电子税务局_增值税申报",
        "申报流程/自然人扣缴端_个税申报",
        "申报流程/企业所得税_季度预缴与汇算清缴",
    }
    missing = expected - ids
    assert not missing, f"缺少政策文档：{missing}"


def test_policy_sources_and_key_figures():
    all_text = "\n".join(policy["meta"].get("source", "") + policy["content"] for policy in load_policies())
    # 关键政策文号必须准确出现
    for keyword in (
        "财政部 税务总局公告2023年第19号",
        "财政部 税务总局公告2023年第12号",
        "财政部 税务总局公告2023年第7号",
        "财税〔2016〕36号",
        "国家税务总局公告2018年第61号",
        "中华人民共和国印花税法",
    ):
        assert keyword in all_text, f"政策文号缺失：{keyword}"
    # 关键政策口径
    assert "30 万元" in all_text or "30万元" in all_text
    assert "减按 1%" in all_text or "减按1%" in all_text
    assert "2027-12-31" in all_text or "2027 年 12 月 31 日" in all_text
    assert "小型微利企业" in all_text


def test_list_get_and_categories():
    assert "增值税" in categories()
    assert len(list_policies("企业所得税")) >= 2
    policy = get_policy("增值税/小规模纳税人起征点与1%征收率")
    assert policy is not None
    assert "2023年第19号" in policy["source"]
    assert get_policy("不存在的政策") is None
    assert get_policy(policy["title"]) is not None


# =============================================================================
# 检索
# =============================================================================
def test_tokenize_chinese_bigram():
    tokens = tokenize("小规模纳税人")
    assert "小规" in tokens and "规模" in tokens
    assert "增值税" not in tokens  # 中文按 2-gram 切分


def test_search_hits_vat_exemption():
    hits = search("小规模纳税人 季度30万 免征增值税", top_k=3)
    assert hits, "检索无命中"
    assert hits[0]["score"] > 0
    assert hits[0]["category"] == "增值税"
    assert any("小规模" in hit["title"] for hit in hits[:2])


def test_search_research_deduction():
    hits = search("研发费用 加计扣除 比例 100%", top_k=3)
    assert hits
    assert hits[0]["category"] == "企业所得税"
    assert "加计扣除" in hits[0]["title"]


def test_search_category_filter():
    hits = search("加计扣除", top_k=5, category="企业所得税")
    assert hits
    assert all(hit["category"] == "企业所得税" for hit in hits)
    none_hits = search("加计扣除", top_k=5, category="申报流程")
    # 申报流程文档也可能提到加计扣除，但每篇分类必须正确
    assert all(hit["category"] == "申报流程" for hit in none_hits)


def test_search_empty_query():
    assert search("") == []
    assert search("   ") == []


def test_build_context_contains_sources_and_dates():
    context = build_context("研发费用加计扣除", top_k=2)
    assert "政策文号" in context
    assert "生效日期" in context
    assert "最后核验日期" in context
    assert "2023年第7号" in context
    empty_context = build_context("完全无关的词汇xyzzy", top_k=2)
    assert "未检索到" in empty_context


# =============================================================================
# 政策问答
# =============================================================================
def test_answer_without_api_key():
    result = answer("小规模纳税人季度免税额度是多少？")
    assert result["used_llm"] is False
    assert result["method"] == "retrieval"
    assert result["citations"]
    assert result["matched"]
    assert "2023年第19号" in result["answer"]
    assert result["disclaimer"] == config.DISCLAIMER
    assert config.DISCLAIMER in result["answer"]
    citation = result["citations"][0]
    assert citation["title"] and citation["source"] and citation["effective_date"]
    assert citation["path"]


def test_answer_no_match():
    result = answer("qqqqzzzz 伽马射线暴")
    assert result["used_llm"] is False
    assert result["citations"] == []
    assert "未检索到" in result["answer"]


class _FakeClient:
    configured = True

    def __init__(self, content=None, error=None):
        self._content = content or "根据知识库：季度 30 万元以下免征增值税（财政部 税务总局公告2023年第19号）。"
        self._error = error

    def chat(self, messages, **kwargs):
        if self._error:
            raise self._error
        return {"content": self._content}


def test_answer_with_llm_client():
    result = answer("小规模纳税人免税额度", client=_FakeClient())
    assert result["used_llm"] is True
    assert result["method"] == "llm"
    assert result["citations"]
    assert config.DISCLAIMER in result["answer"]


def test_answer_llm_error_falls_back():
    from agent.llm_client import DeepSeekError

    result = answer("小规模纳税人免税额度", client=_FakeClient(error=DeepSeekError("鉴权失败", kind="auth")))
    assert result["used_llm"] is False
    assert result["method"] == "retrieval_fallback"
    assert "2023年第19号" in result["answer"]
    assert result["error"]


# =============================================================================
# 更新检查
# =============================================================================
def test_policy_stats():
    stats = update_checker.policy_stats()
    assert stats["total"] >= 10
    assert stats["categories"]["增值税"] >= 1
    assert stats["latest_verified"] == "2026-11-01"
    assert stats["oldest"] is not None
    assert stats["oldest"]["last_verified"] == "2026-11-01"


def test_check_policies_and_log():
    result = update_checker.check_policies(days=90)
    assert result["checked_total"] >= 10
    assert result["latest_verified"] == "2026-11-01"
    assert isinstance(result["stale"], list)
    assert isinstance(result["advice"], list) and result["advice"]
    assert result["checked_at"]
    # 审计日志已写入
    from ledger.store import read_log

    logs = read_log("policy_check_log")
    assert logs, "policy_check_log 未写入"


def test_stale_policies_boundaries():
    policies = load_policies()
    # 全部政策 2026-11-01 核验，today 为 2026-11-09 → 90 天内不过期
    assert update_checker.stale_policies(days=90) == []
    # 把「今天」推到 2026-12-31 → 距今 60 天，按 30 天阈值全部过期
    stale = update_checker.stale_policies(days=30, today="2026-12-31")
    assert len(stale) == len(policies)
    for item in stale:
        assert item["id"] and item["title"]
        assert item["days"] == 60
    # 阈值足够大时不过期
    assert update_checker.stale_policies(days=3650) == []


def test_mark_verified_updates_frontmatter(tmp_path, monkeypatch):
    policy_root = tmp_path / "policy_files"
    policy_file = policy_root / "增值税" / "测试政策.md"
    policy_file.parent.mkdir(parents=True)
    policy_file.write_text(
        "---\n"
        "title: 测试政策\n"
        "category: 增值税\n"
        "effective_date: 2023-01-01\n"
        "expiry_date: 2027-12-31\n"
        "source: 财政部 税务总局公告2023年第19号\n"
        "last_verified: 2026-01-01\n"
        "tags: [测试]\n"
        "---\n\n## 适用条件\n测试正文。\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "POLICY_DIR", policy_root)
    # 清空缓存，确保从临时目录重新加载
    load_policies(force=True)

    updated = update_checker.mark_verified("增值税/测试政策", verified_date="2026-12-31")
    assert updated["last_verified"] == "2026-12-31"
    text = policy_file.read_text(encoding="utf-8")
    assert "last_verified: 2026-12-31" in text
    assert "title: 测试政策" in text  # 其他 frontmatter 未被破坏
    reloaded = get_policy("增值税/测试政策")
    assert reloaded["last_verified"] == "2026-12-31"

    from ledger.store import read_log

    assert read_log("policy_check_log"), "mark_verified 未写审计日志"

    with pytest.raises(KeyError):
        update_checker.mark_verified("不存在政策")
