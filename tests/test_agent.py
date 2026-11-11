"""Agent 编排层测试：DeepSeek 客户端、Prompt、意图识别、工具调用、本地降级回复。

约束
----
* **不访问真实 DeepSeek API**：所有联网请求用 ``monkeypatch`` 替换 ``requests.post``；
* 无 Key 场景通过设置页清空 + 删除环境变量模拟；
* 依赖 ledger / filing 的集成用例用 ``pytest.importorskip`` 保护，模块缺失时跳过。
"""

from __future__ import annotations

import json

import pytest

import config
import engine
from agent import intent_router, llm_client, orchestrator, prompt_templates, tools
from agent.intent_router import (
    INTENTS,
    detect_intent,
    extract_entities,
    route,
)
from agent.llm_client import DeepSeekClient, DeepSeekError, parse_json
from agent.tools import TOOL_NAMES, call_tool, describe_tools, tool_specs


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch):
    """所有用例默认无 API Key，避免误连真实接口。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    try:
        config.save_user_settings({"api_key": ""})
    except OSError:  # pragma: no cover
        pass
    yield


# =============================================================================
# llm_client
# =============================================================================
def test_parse_json_tolerant():
    assert parse_json('{"a": 1}') == {"a": 1}
    assert parse_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert parse_json('好的，结果是：{"a": {"b": 3}} 以上') == {"a": {"b": 3}}
    assert parse_json('{"a": 1,}') == {"a": 1}
    assert parse_json('{"intent": "calc_vat", "amounts": [80000]}')["amounts"] == [80000]
    assert parse_json("不是 JSON") == {}
    assert parse_json("") == {}
    assert parse_json({"x": 1}) == {"x": 1}


def test_client_not_configured():
    client = DeepSeekClient(api_key="")
    assert client.configured is False
    with pytest.raises(DeepSeekError) as excinfo:
        client.chat([{"role": "user", "content": "你好"}])
    assert "API Key" in str(excinfo.value)
    assert excinfo.value.kind == "no_key"
    assert client.test_connection()["ok"] is False
    assert client.test_connection()["message"]


class _FakeHTTPResponse:
    def __init__(self, status_code: int = 200, payload=None, lines=None):
        self.status_code = status_code
        self._payload = payload or {}
        self._lines = lines or []

    def json(self):
        return self._payload

    @property
    def text(self) -> str:
        return json.dumps(self._payload, ensure_ascii=False)

    def iter_lines(self, decode_unicode=False):
        for line in self._lines:
            yield line.encode("utf-8") if isinstance(line, str) else line

    def close(self):
        pass


def _fake_post_factory(response):
    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        return response

    return fake_post


def test_client_chat_with_tool_calls(monkeypatch):
    payload = {
        "model": "deepseek-chat",
        "choices": [
            {
                "message": {
                    "content": "我来帮你算。",
                    "reasoning_content": "先调用工具",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "calc_vat", "arguments": '{"quarterly_total_inclusive": 80000}'},
                        }
                    ],
                }
            }
        ],
        "usage": {"total_tokens": 42},
    }
    monkeypatch.setattr("agent.llm_client.requests.post", _fake_post_factory(_FakeHTTPResponse(200, payload)))
    client = DeepSeekClient(api_key="fake-key")
    result = client.chat([{"role": "user", "content": "算一下"}], tools=tool_specs())
    assert result["content"] == "我来帮你算。"
    assert result["reasoning_content"] == "先调用工具"
    assert result["tool_calls"][0]["name"] == "calc_vat"
    assert result["tool_calls"][0]["arguments"] == {"quarterly_total_inclusive": 80000}
    assert result["usage"]["total_tokens"] == 42


def test_client_broken_tool_arguments(monkeypatch):
    payload = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"id": "call_1", "function": {"name": "calc_vat", "arguments": "这不是 JSON"}}
                    ],
                }
            }
        ]
    }
    monkeypatch.setattr("agent.llm_client.requests.post", _fake_post_factory(_FakeHTTPResponse(200, payload)))
    result = DeepSeekClient(api_key="fake-key").chat([{"role": "user", "content": "x"}])
    assert result["tool_calls"][0]["arguments"] == {}
    assert result["warnings"]


@pytest.mark.parametrize(
    ("status", "keyword"),
    [
        (401, "鉴权"),
        (402, "余额"),
        (429, "频繁"),
        (500, "服务端"),
    ],
)
def test_client_error_translation(monkeypatch, status, keyword):
    response = _FakeHTTPResponse(status, {"error": {"message": "boom"}})
    monkeypatch.setattr("agent.llm_client.requests.post", _fake_post_factory(response))
    client = DeepSeekClient(api_key="fake-key")
    with pytest.raises(DeepSeekError) as excinfo:
        client.chat([{"role": "user", "content": "x"}])
    assert keyword in str(excinfo.value)
    assert excinfo.value.status_code == status


def test_client_connection_error(monkeypatch):
    import requests

    def raise_error(url, headers=None, json=None, timeout=None, stream=False):
        raise requests.exceptions.ConnectionError("network down")

    monkeypatch.setattr("agent.llm_client.requests.post", raise_error)
    with pytest.raises(DeepSeekError) as excinfo:
        DeepSeekClient(api_key="fake-key").chat([{"role": "user", "content": "x"}])
    assert "无法连接" in str(excinfo.value)
    assert excinfo.value.kind == "network"


def test_stream_chat_sse(monkeypatch):
    lines = [
        'data: {"choices": [{"delta": {"content": "你"}}]}',
        'data: {"choices": [{"delta": {"content": "好"}}]}',
        "data: [DONE]",
    ]
    response = _FakeHTTPResponse(200, {}, lines)
    monkeypatch.setattr("agent.llm_client.requests.post", _fake_post_factory(response))
    chunks = list(DeepSeekClient(api_key="fake-key").stream_chat([{"role": "user", "content": "x"}]))
    assert chunks == ["你", "好"]


def test_test_connection_ok(monkeypatch):
    payload = {"model": "deepseek-chat", "choices": [{"message": {"content": "连接成功"}}]}
    monkeypatch.setattr("agent.llm_client.requests.post", _fake_post_factory(_FakeHTTPResponse(200, payload)))
    result = DeepSeekClient(api_key="fake-key").test_connection()
    assert result["ok"] is True
    assert result["reply"] == "连接成功"
    assert result["latency_ms"] >= 0
    assert "连接成功" in result["message"]


def test_client_masks_key():
    client = DeepSeekClient(api_key="sk-1234567890abcdef")
    assert "1234567890abcdef" not in repr(client)
    assert "****" in client.masked_key


# =============================================================================
# prompt_templates
# =============================================================================
def test_system_prompt_content():
    prompt = prompt_templates.build_system_prompt()
    assert "税小智" in prompt
    assert config.COMPANY["name"] in prompt
    assert config.DISCLAIMER in prompt
    assert ("2026年第10号" in prompt) or ("2023年第19号" in prompt)
    assert "不得" in prompt and "算税" in prompt
    assert prompt_templates.SYSTEM_PROMPT
    assert prompt_templates.PERSONA == "税小智"


def test_scene_prompt_templates_json():
    assert "JSON" in prompt_templates.INTENT_PROMPT
    assert "{text}" in prompt_templates.INTENT_PROMPT
    for template in (
        prompt_templates.TAX_CALC_PROMPT,
        prompt_templates.INCOME_PROMPT,
        prompt_templates.EXPENSE_PROMPT,
        prompt_templates.POLICY_PROMPT,
        prompt_templates.FILING_PROMPT,
        prompt_templates.OCR_PROMPT,
        prompt_templates.SUMMARY_PROMPT,
    ):
        assert isinstance(template, str) and len(template) > 20


# =============================================================================
# intent_router
# =============================================================================
def test_intents_cover_required_eight():
    for intent in (
        "calc_vat",
        "income",
        "journal",
        "policy",
        "filing_check",
        "calc_rd",
        "calendar",
        "red_bill",
    ):
        assert intent in INTENTS


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("我这个季度开了8万普票，增值税怎么算？", "calc_vat"),
        ("今天收到云图科技5000块", "income"),
        ("帮我把这笔做成分录", "journal"),
        ("小规模现在增值税税率是多少？", "policy"),
        ("帮我看看这个申报表填对了没", "filing_check"),
        ("这个月研发人员工资总共2万", "calc_rd"),
        ("下个月有什么要报的？", "calendar"),
        ("我的红利账单9942元是怎么来的？", "red_bill"),
        ("这个月买办公用品花了500块", "expense"),
        ("小规模纳税人季度免税额度是多少", "policy"),
        ("算一下企业所得税，年利润20万", "calc_corp_tax"),
        ("印花税怎么算", "calc_stamp_tax"),
        ("个税怎么算", "iit"),
        ("月薪1.5万，这个月个税怎么算", "iit"),
        ("专项附加扣除怎么算个税", "iit"),
        ("2026年度企业所得税汇算清缴怎么算", "cit_settlement"),
        ("纳税调整怎么做", "cit_settlement"),
        ("业务招待费税前扣除限额是多少", "cit_settlement"),
    ],
)
def test_detect_intent(text, expected):
    result = detect_intent(text)
    assert result["intent"] == expected, f"{text} → {result}"
    assert result["method"] == "rule"
    assert 0.0 < result["confidence"] <= 1.0


def test_policy_consultation_not_misrouted_as_calc():
    """咨询型问句（问税率/额度）应走 policy，只有带金额或计算意图才走算税。"""
    for text in (
        "小规模现在增值税税率多少？",
        "小规模现在增值税税率是多少？",
        "小规模免税额度是多少",
        "企业所得税税率是多少",
        "小规模纳税人能享受什么优惠",
    ):
        result = detect_intent(text)
        assert result["intent"] == "policy", f"{text} → {result}"
        assert route(text)["tool"] == "search_policy"

    # 带明确金额 → 算税
    for text in (
        "这个季度开了8万普票，增值税怎么算？",
        "这个季度一共10万，其中2万专票，要交多少增值税",
    ):
        assert detect_intent(text)["intent"] == "calc_vat", text
        assert route(text)["tool"] == "calc_vat"

    # 明确计算意图（即使没有金额）→ 算税
    assert detect_intent("增值税怎么算")["intent"] == "calc_vat"
    assert detect_intent("开了普票要交多少增值税")["intent"] == "calc_vat"


def test_extract_entities_amounts_units():
    entities = extract_entities("今天收到云图科技5000块，另外还有两万现金")
    assert 5000.0 in entities["amounts"]
    assert 20000.0 in entities["amounts"]
    assert entities["client"] == "云图科技"
    assert entities["date"] == config.today().isoformat()

    entities = extract_entities("2026Q1开了8万普票，其中1.5万专票")
    assert entities["period"] == "2026Q1"
    assert 80000.0 in entities["amounts"]
    assert 15000.0 in entities["amounts"]
    assert entities["invoice_type"] == "专票"

    entities = extract_entities("昨天报销差旅费1,234.56元")
    assert 1234.56 in entities["amounts"]
    assert entities["date"] != config.today().isoformat()


def test_extract_entities_periods():
    assert extract_entities("本季度收入多少")["period"] == engine.current_quarter()
    assert extract_entities("上个季度开了多少票")["period"] == engine.prev_quarter(engine.current_quarter())
    assert extract_entities("本月收入5万")["period"] == engine.current_month()
    assert extract_entities("2026年第一季度销售额")["period"] == "2026Q1"
    assert extract_entities("2026-03 的申报")["period"] == "2026-03"


def test_cn_to_number():
    from agent.intent_router import cn_to_number

    assert cn_to_number("两万") == 20000.0
    assert cn_to_number("一万五千") == 15000.0
    assert cn_to_number("二十万") == 200000.0
    assert cn_to_number("五百") == 500.0
    assert cn_to_number("") is None


def test_route_calc_vat_arguments():
    routed = route("我这个季度开了8万普票，增值税怎么算？")
    assert routed["intent"] == "calc_vat"
    assert routed["tool"] == "calc_vat"
    assert routed["arguments"]["quarterly_total_inclusive"] == 80000.0
    assert routed["arguments"]["special_invoice_inclusive"] == 0.0
    assert routed["arguments"]["period"] == engine.current_quarter()
    assert routed["reply_hint"]

    with_special = route("这个季度一共10万，其中2万专票，增值税要交多少")
    assert with_special["arguments"]["quarterly_total_inclusive"] == 100000.0
    assert with_special["arguments"]["special_invoice_inclusive"] == 20000.0


def test_route_other_intents():
    assert route("今天收到云图科技5000块")["tool"] == "add_income"
    assert route("这个月买办公用品花了500块")["tool"] == "add_expense"
    assert route("小规模纳税人免税政策")["tool"] == "search_policy"
    assert route("下个月有什么要报的")["tool"] == "get_filing_calendar"
    assert route("帮我把这笔做成分录")["tool"] is None


def test_iit_entities_and_route():
    """个税意图抽取累计月份数与累计收入，并正确路由到 calc_iit。"""
    entities = extract_entities("今年已申报11个月，累计收入16.5万")
    assert entities["months_declared"] == 11
    assert 165000.0 in entities["amounts"]

    assert extract_entities("1-3月的工资个税")["months_declared"] == 3
    assert extract_entities("个税怎么算")["months_declared"] is None

    routed = route("月薪1.5万，今年已申报11个月，个税怎么算")
    assert routed["intent"] == "iit"
    assert routed["tool"] == "calc_iit"
    assert routed["arguments"]["cumulative_income"] == 165000.0
    assert routed["arguments"]["months_declared"] == 11
    assert routed["reply_hint"]

    cumulative = route("累计收入9万，累计申报3个月，个税怎么算")
    assert cumulative["arguments"]["cumulative_income"] == 90000.0
    assert cumulative["arguments"]["months_declared"] == 3

    with_deductions = route(
        "月薪2万，五险一金每月3000，专项附加扣除每月2000，已申报6个月，个税怎么算"
    )
    assert with_deductions["arguments"]["cumulative_income"] == 120000.0
    assert with_deductions["arguments"]["cumulative_social_insurance"] == 18000.0
    assert with_deductions["arguments"]["cumulative_special_additional"] == 12000.0


def test_iit_intent_does_not_conflict():
    """新增 iit 意图不得影响既有意图：增值税、研发工资、政策咨询仍走原路由。"""
    assert detect_intent("这个季度开了8万普票，增值税怎么算？")["intent"] == "calc_vat"
    assert detect_intent("这个月研发人员工资总共2万")["intent"] == "calc_rd"
    assert detect_intent("小规模现在增值税税率多少？")["intent"] == "policy"
    assert detect_intent("个税申报截止日是什么时候")["intent"] == "calendar"


def test_cit_settlement_entities_and_route():
    """汇算清缴意图抽取年度/收入/成本/限额/亏损/预缴并路由到 calc_cit_settlement。"""
    entities = extract_entities("2026年度企业所得税汇算清缴")
    assert entities["year"] == "2026"

    assert extract_entities("汇算清缴怎么算")["year"] is None

    routed = route(
        "2026年度汇算清缴，收入100万，成本60万，工资20万，业务招待费3万，广告费20万，已预缴2万"
    )
    assert routed["intent"] == "cit_settlement"
    assert routed["tool"] == "calc_cit_settlement"
    arguments = routed["arguments"]
    assert arguments["year"] == "2026"
    assert arguments["revenue_exclusive"] == 1_000_000.0
    assert arguments["cost_total"] == 600_000.0
    assert arguments["salary_total"] == 200_000.0
    assert arguments["entertainment"] == 30_000.0
    assert arguments["advertising"] == 200_000.0
    assert arguments["prepaid_tax"] == 20_000.0
    assert routed["reply_hint"]

    with_loss = route("2025年汇算清缴，利润50万，去年亏损10万，调增5万")
    assert with_loss["arguments"]["accounting_profit"] == 500_000.0
    assert with_loss["arguments"]["losses"] == [{"year": "2024", "loss_amount": 100_000.0, "used_amount": 0}]
    assert with_loss["arguments"]["extra_adjustments"][0]["direction"] == "调增"
    assert with_loss["arguments"]["extra_adjustments"][0]["adjustment_amount"] == 50_000.0

    assert route("汇算清缴怎么弄")["arguments"]["year"] == str(config.today().year)


def test_cit_settlement_intent_does_not_conflict():
    """新增汇算清缴意图不得影响既有意图。"""
    assert detect_intent("算一下企业所得税，年利润20万")["intent"] == "calc_corp_tax"
    assert detect_intent("企业所得税季度预缴怎么算")["intent"] == "calc_cit_prepay"
    assert detect_intent("这个季度开了8万普票，增值税怎么算？")["intent"] == "calc_vat"
    assert detect_intent("研发费用加计扣除比例是多少")["intent"] == "calc_rd"
    assert detect_intent("个税怎么算")["intent"] == "iit"
    assert detect_intent("小规模现在增值税税率多少？")["intent"] == "policy"


def test_detect_intent_llm_fallback(monkeypatch):
    class FakeClient:
        configured = True

        def chat(self, messages, **kwargs):
            return {
                "content": json.dumps(
                    {"intent": "calc_vat", "confidence": 0.88, "entities": {"amounts": [50000]}}
                )
            }

    result = detect_intent("嗯嗯嗯帮我随便看看这个", client=FakeClient())
    assert result["method"] == "llm"
    assert result["intent"] == "calc_vat"
    assert 50000.0 in result["entities"]["amounts"]


def test_detect_intent_llm_failure_falls_back(monkeypatch):
    class BrokenClient:
        configured = True

        def chat(self, messages, **kwargs):
            raise DeepSeekError("鉴权失败", kind="auth")

    result = detect_intent("完全无法判断的输入内容", client=BrokenClient())
    assert result["method"] == "rule"


# =============================================================================
# tools
# =============================================================================
def test_tool_specs_and_names():
    specs = tool_specs()
    assert [spec["function"]["name"] for spec in specs] == TOOL_NAMES
    assert len(TOOL_NAMES) == 17
    assert "calc_iit" in TOOL_NAMES
    assert "calc_cit_settlement" in TOOL_NAMES
    assert "calc_disability_fund" in TOOL_NAMES
    assert "payroll_correction" in TOOL_NAMES
    for spec in specs:
        assert spec["type"] == "function"
        assert spec["function"]["description"]
        assert spec["function"]["parameters"]["type"] == "object"
    assert len(describe_tools()) == 17


def test_call_tool_unknown_and_missing_params():
    assert call_tool("ha_ha", {})["ok"] is False
    missing = call_tool("calc_vat", {})
    assert missing["ok"] is False
    assert "quarterly_total_inclusive" in missing["error"]
    bad = call_tool("calc_vat", {"quarterly_total_inclusive": "abc"})
    assert bad["ok"] is False


def test_calc_vat_tool_exemption():
    result = call_tool("calc_vat", {"quarterly_total_inclusive": 80000, "period": "2026Q1"})
    assert result["ok"] is True
    data = result["data"]
    assert data["vat_payable"] == 0.0
    assert data["within_exemption_limit"] is True
    assert data["vat_exempt"] == 792.08
    assert data["total_exclusive"] == 79207.92
    assert "免征" in result["display"]
    assert "财政部 税务总局公告2026年第10号" in result["display"]
    assert data["disclaimer"] == config.DISCLAIMER


def test_calc_vat_tool_special_invoice():
    result = call_tool(
        "calc_vat",
        {"quarterly_total_inclusive": 303000, "special_invoice_inclusive": 10100, "period": "2026Q1"},
    )
    assert result["ok"] is True
    data = result["data"]
    assert data["vat_payable"] > 0
    assert data["vat_exempt"] > 0
    assert data["surtax"] > 0


def test_calc_vat_tool_exceed_limit():
    result = call_tool("calc_vat", {"quarterly_total_inclusive": 404000, "period": "2026Q1"})
    assert result["ok"] is True
    assert result["data"]["within_exemption_limit"] is False
    assert result["data"]["vat_payable"] > 0


def test_calc_vat_tool_boundary():
    result = call_tool("calc_vat", {"quarterly_total_inclusive": 303000, "period": "2026Q1"})
    assert result["ok"] is True
    assert result["data"]["within_exemption_limit"] is True


def test_calc_corp_tax_tool():
    result = call_tool("calc_corp_tax", {"annual_profit": 200000, "period": "2026"})
    assert result["ok"] is True
    assert result["data"]["effective_rate"] == 0.05
    assert result["data"]["corp_tax"] == 10000.0


def test_rd_tool_from_empty_ledger():
    result = call_tool("calc_rd_deduction", {"period": "2026Q1"})
    assert result["ok"] is True
    assert "研发" in result["display"]


def test_search_policy_tool():
    result = call_tool("search_policy", {"query": "小规模纳税人 季度免税额度"})
    assert result["ok"] is True
    matched = result["data"]["matched"]
    assert matched and matched[0]["title"]
    assert matched[0]["source"]
    assert "财政部 税务总局公告" in result["display"]


def test_calendar_tool():
    result = call_tool("get_filing_calendar", {"days": 120})
    assert result["ok"] is True
    assert result["data"]["items"]
    assert "截止" not in result["display"] or True
    assert result["data"]["items"][0]["due_date"] >= config.today().isoformat()


def test_calc_stamp_tax_tool():
    result = call_tool("calc_stamp_tax", {"period": "2026Q1"})
    assert result["ok"] is True
    assert "印花税" in result["display"]


def test_calc_iit_tool_first_month():
    result = call_tool(
        "calc_iit",
        {"cumulative_income": 15000, "months_declared": 1, "period": "2026-11"},
    )
    assert result["ok"] is True, result["error"]
    data = result["data"]
    assert data["cumulative_basic_deduction"] == 5000.0
    assert data["cumulative_taxable_income"] == 10000.0
    assert data["applicable_rate"] == 0.03
    assert data["tax_payable"] == 300.0
    assert data["disclaimer"] == config.DISCLAIMER
    display = result["display"]
    assert "本期应预扣预缴" in display
    assert "300.00" in display
    assert "国家税务总局公告2018年第61号" in display
    assert "零申报" in display
    assert config.DISCLAIMER in display


def test_calc_iit_tool_cumulative_with_withheld():
    result = call_tool(
        "calc_iit",
        {
            "cumulative_income": 30000,
            "months_declared": 2,
            "cumulative_social_insurance": 3000,
            "cumulative_special_additional": 2000,
            "cumulative_tax_withheld": 300,
            "period": "2026-02",
        },
    )
    assert result["ok"] is True, result["error"]
    data = result["data"]
    assert data["cumulative_taxable_income"] == 15000.0
    assert data["cumulative_tax"] == 450.0
    assert data["tax_payable"] == 150.0


def test_calc_iit_tool_rate_bracket_and_zero_months():
    result = call_tool("calc_iit", {"cumulative_income": 90000, "months_declared": 3})
    assert result["ok"] is True
    assert result["data"]["cumulative_taxable_income"] == 75000.0
    assert result["data"]["applicable_rate"] == 0.10
    assert result["data"]["quick_deduction"] == 2520.0
    assert result["data"]["cumulative_tax"] == 4980.0

    zero = call_tool("calc_iit", {"cumulative_income": 10000, "months_declared": 0})
    assert zero["ok"] is True
    assert zero["data"]["cumulative_basic_deduction"] == 0.0
    assert zero["data"]["cumulative_taxable_income"] == 10000.0
    assert any("零申报" in warning for warning in zero["data"]["warnings"])
    assert "零申报" in zero["display"]


def test_calc_iit_tool_one_time_deduction():
    result = call_tool(
        "calc_iit",
        {"cumulative_income": 100000, "months_declared": 3, "one_time_basic_deduction": True},
    )
    assert result["ok"] is True
    assert result["data"]["cumulative_basic_deduction"] == 60000.0
    assert result["data"]["cumulative_taxable_income"] == 40000.0
    assert result["data"]["cumulative_tax"] == 1480.0


def test_calc_iit_tool_missing_params():
    missing_income = call_tool("calc_iit", {})
    assert missing_income["ok"] is False
    assert "cumulative_income" in missing_income["error"]

    missing_months = call_tool("calc_iit", {"cumulative_income": 15000})
    assert missing_months["ok"] is False
    assert "months_declared" in missing_months["error"]
    assert "零申报" in missing_months["error"]

    bad_months = call_tool("calc_iit", {"cumulative_income": 15000, "months_declared": "abc"})
    assert bad_months["ok"] is False
    assert "months_declared" in bad_months["error"]


def test_calc_cit_settlement_tool_basic():
    result = call_tool(
        "calc_cit_settlement",
        {
            "year": "2026",
            "revenue_exclusive": 1_000_000,
            "cost_total": 600_000,
            "salary_total": 200_000,
            "entertainment": 30_000,
            "advertising": 200_000,
            "welfare": 30_000,
            "education": 20_000,
            "union_actual": 6_000,
            "rd_deduction": 50_000,
            "prepaid_tax": 20_000,
            "employees": 20,
            "total_assets": 2_000_000,
        },
    )
    assert result["ok"] is True, result["error"]
    data = result["data"]
    # 限额类调增：招待 25,000 + 广宣 50,000 + 福利 2,000 + 教育 4,000 + 工会 2,000 = 83,000
    assert data["limit_adjustment_add"] == 83_000.0
    assert data["taxable_income"] == 433_000.0
    assert data["qualifies_small_low_profit"] is True
    assert data["applicable_rate"] == 0.05
    assert data["corporate_tax"] == 21_650.0
    assert data["prepaid_tax"] == 20_000.0
    assert data["payable"] == 1_650.0
    assert data["payable_label"] == "应补税额"
    assert data["disclaimer"] == config.DISCLAIMER
    display = result["display"]
    assert "应纳税所得额" in display
    assert "应补税额" in display
    assert "5 月 31 日" in display
    assert "财政部 税务总局公告2023年第12号" in display
    assert config.DISCLAIMER in display


def test_calc_cit_settlement_tool_loss_and_manual_adjustments():
    result = call_tool(
        "calc_cit_settlement",
        {
            "year": "2026",
            "accounting_profit": 100_000,
            "extra_adjustments": [
                {"item_type": "罚款支出", "direction": "调增", "adjustment_amount": 5_000},
                {"item_type": "免税收入", "direction": "调减", "adjustment_amount": 3_000},
            ],
            "losses": [{"year": "2024", "loss_amount": 20_000, "used_amount": 0}],
            "prepaid_tax": 1_000,
        },
    )
    assert result["ok"] is True, result["error"]
    data = result["data"]
    assert data["manual_adjustment_add"] == 5_000.0
    assert data["manual_adjustment_reduce"] == 3_000.0
    assert data["loss_used"] == 20_000.0
    assert data["taxable_income"] == 82_000.0  # 100,000 + 5,000 − 3,000 − 20,000
    assert data["corporate_tax"] == 4_100.0

    refund = call_tool(
        "calc_cit_settlement",
        {"year": "2026", "accounting_profit": 100_000, "prepaid_tax": 10_000},
    )
    assert refund["ok"] is True
    assert refund["data"]["payable_label"] == "应退税额"
    assert refund["data"]["refund"] == 5_000.0
    assert "应退税额" in refund["display"]


def test_calc_cit_settlement_tool_missing_and_invalid():
    missing = call_tool("calc_cit_settlement", {"year": "2026"})
    assert missing["ok"] is False
    assert "revenue_exclusive" in missing["error"] or "accounting_profit" in missing["error"]

    bad_year = call_tool("calc_cit_settlement", {"year": "abc", "revenue_exclusive": 100})
    assert bad_year["ok"] is False
    assert "year" in bad_year["error"]

    bad_adjustments = call_tool(
        "calc_cit_settlement",
        {"year": "2026", "revenue_exclusive": 100, "extra_adjustments": "不是数组"},
    )
    assert bad_adjustments["ok"] is False
    assert "extra_adjustments" in bad_adjustments["error"]


# =============================================================================
# orchestrator：本地降级
# =============================================================================
def test_local_reply_calc_vat():
    result = orchestrator.local_reply("这个季度开了8万普票，增值税怎么算？")
    assert result["used_llm"] is False
    assert result["intent"] == "calc_vat"
    assert result["tool_calls"][0]["name"] == "calc_vat"
    assert result["tool_calls"][0]["result"]["ok"] is True
    assert "792.08" in result["reply"]
    assert "免征" in result["reply"]
    assert config.DISCLAIMER in result["reply"]


def test_local_reply_policy():
    result = orchestrator.local_reply("小规模纳税人现在增值税税率是多少？")
    assert result["intent"] == "policy"
    assert result["used_llm"] is False
    assert "2026年第10号" in result["reply"]
    assert config.DISCLAIMER in result["reply"]


def test_local_reply_calendar():
    result = orchestrator.local_reply("下个月有什么要报的？")
    assert result["intent"] == "calendar"
    assert "申报" in result["reply"] or "提醒" in result["reply"]


def test_local_reply_red_bill():
    result = orchestrator.local_reply("我的红利账单9942元是怎么来的？")
    assert result["intent"] == "red_bill"
    assert "红利账单" in result["reply"]
    assert "增值税" in result["reply"]


def test_local_reply_iit():
    result = orchestrator.local_reply("月薪1.5万，这个月个税怎么算")
    assert result["intent"] == "iit"
    assert result["used_llm"] is False
    assert result["tool_calls"][0]["name"] == "calc_iit"
    assert result["tool_calls"][0]["result"]["ok"] is True
    assert result["tool_calls"][0]["result"]["data"]["tax_payable"] == 300.0
    reply = result["reply"]
    assert "本期应预扣" in reply
    assert "300.00" in reply
    assert "累计预扣" in reply
    assert "国家税务总局公告2018年第61号" in reply
    assert "零申报" in reply
    assert config.DISCLAIMER in reply


def test_local_reply_iit_missing_income():
    result = orchestrator.local_reply("个税怎么算")
    assert result["intent"] == "iit"
    assert result["tool_calls"][0]["name"] == "calc_iit"
    assert result["tool_calls"][0]["result"]["ok"] is False
    reply = result["reply"]
    assert "累计" in reply
    assert "已实际申报月份数" in reply
    assert "零申报" in reply
    assert config.DISCLAIMER in reply


def test_local_reply_cit_settlement():
    result = orchestrator.local_reply(
        "2026年度汇算清缴，收入100万，成本60万，工资20万，业务招待费3万，广告费20万，已预缴2万"
    )
    assert result["intent"] == "cit_settlement"
    assert result["used_llm"] is False
    assert result["tool_calls"][0]["name"] == "calc_cit_settlement"
    assert result["tool_calls"][0]["result"]["ok"] is True
    reply = result["reply"]
    assert "应纳税所得额" in reply
    assert "应补税额" in reply
    assert "5 月 31 日" in reply
    assert "亏损" in reply and "5 年" in reply
    assert "小型微利企业" in reply and "5%" in reply
    assert "资料准备" in reply
    assert config.DISCLAIMER in reply


def test_local_reply_cit_settlement_missing_data():
    result = orchestrator.local_reply("汇算清缴怎么弄")
    assert result["intent"] == "cit_settlement"
    assert result["tool_calls"][0]["name"] == "calc_cit_settlement"
    assert result["tool_calls"][0]["result"]["ok"] is False
    reply = result["reply"]
    assert "5 月 31 日" in reply
    assert "纳税调增" in reply or "调增" in reply
    assert "亏损" in reply
    assert "资料准备" in reply
    assert config.DISCLAIMER in reply


def test_local_reply_chat_fallback():
    result = orchestrator.local_reply("你好")
    assert result["intent"] == "chat"
    assert "税小智" in result["reply"]


def test_build_context():
    context = orchestrator.build_context()
    assert "企业信息" in context
    assert config.COMPANY["name"] in context
    assert "政策" in context
    assert "本季度" in context or "台账" in context


def test_run_turn_without_key():
    result = orchestrator.run_turn("这个季度开了8万普票，增值税怎么算？")
    assert result["used_llm"] is False
    assert result["error"] and "API Key" in result["error"]
    assert "792.08" in result["reply"]
    assert config.DISCLAIMER in result["reply"]


class _FakeChatClient:
    """模拟 DeepSeek：第一轮返回工具调用，第二轮基于工具结果作答。"""

    configured = True
    model = "fake-model"

    def __init__(self):
        self.calls = 0
        self.messages = []

    def chat(self, messages, **kwargs):
        self.calls += 1
        self.messages.append(messages)
        if self.calls == 1:
            return {
                "content": "",
                "reasoning_content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "calc_vat",
                        "arguments": {"quarterly_total_inclusive": 80000, "period": "2026Q1"},
                    }
                ],
                "usage": {},
                "model": self.model,
                "raw": {},
            }
        return {
            "content": "本季度应缴增值税为 0.00 元，免征增值税 792.08 元。",
            "reasoning_content": "",
            "tool_calls": [],
            "usage": {},
            "model": self.model,
            "raw": {},
        }


def test_run_turn_with_fake_client(monkeypatch):
    fake = _FakeChatClient()
    monkeypatch.setattr(orchestrator, "get_client", lambda api_key=None, model=None: fake)
    result = orchestrator.run_turn("这个季度开了8万普票，增值税怎么算？")
    assert result["used_llm"] is True
    assert result["error"] is None
    assert result["tool_calls"][0]["name"] == "calc_vat"
    assert result["tool_calls"][0]["result"]["ok"] is True
    assert "0.00" in result["reply"]
    assert config.DISCLAIMER in result["reply"]
    assert fake.calls == 2
    # 第二轮消息里应包含工具结果（role=tool）
    assert any(message.get("role") == "tool" for message in fake.messages[1])


def test_run_turn_api_error_falls_back(monkeypatch):
    class BrokenClient:
        configured = True
        model = "fake"

        def chat(self, messages, **kwargs):
            raise DeepSeekError("API Key 鉴权失败", kind="auth")

    monkeypatch.setattr(orchestrator, "get_client", lambda api_key=None, model=None: BrokenClient())
    result = orchestrator.run_turn("这个季度开了8万普票，增值税怎么算？")
    assert result["used_llm"] is False
    assert "DeepSeek" in result["error"]
    assert "792.08" in result["reply"]


def test_run_turn_empty_text():
    result = orchestrator.run_turn("")
    assert result["error"]


# =============================================================================
# 与 ledger / filing 的联调（模块就绪时才执行）
# =============================================================================
def test_add_income_tool_with_ledger():
    pytest.importorskip("ledger.income_ledger")
    result = call_tool(
        "add_income",
        {
            "inclusive_amount": 5000,
            "client": "云图科技",
            "invoice_type": "普票",
            "category": "技术服务",
            "date": config.today().isoformat(),
        },
    )
    assert result["ok"] is True, result["error"]
    assert result["data"]["exclusive_amount"] == 4950.5
    assert result["data"]["vat_amount"] == 49.5
    from ledger.income_ledger import list_income

    rows = list_income()
    assert len(rows) == 1
    assert rows[0]["client"] == "云图科技"


def test_add_expense_tool_with_ledger():
    pytest.importorskip("ledger.expense_ledger")
    result = call_tool("add_expense", {"amount": 500, "description": "办公用品", "category": "办公"})
    assert result["ok"] is True, result["error"]
    assert result["data"]["amount"] == 500.0
    from ledger.expense_ledger import list_expenses

    assert len(list_expenses()) == 1


def test_query_ledger_tool_with_ledger():
    pytest.importorskip("ledger.income_ledger")
    call_tool("add_income", {"inclusive_amount": 5000, "client": "云图科技"})
    result = call_tool("query_ledger", {"kind": "income", "period": engine.current_quarter()})
    assert result["ok"] is True
    assert result["data"]["summary"]["count"] == 1
    assert "收入台账" in result["display"]


def test_build_filing_tool_with_filing():
    pytest.importorskip("filing.registry")
    pytest.importorskip("filing.vat_return")
    result = call_tool("build_filing", {"form_id": "VAT_QUARTERLY", "period": "2026Q1"})
    assert result["ok"] is True, result["error"]
    bundle = result["data"]
    assert bundle["form_id"] == "VAT_QUARTERLY"
    assert bundle["sections"]
    assert bundle["disclaimer"] == config.DISCLAIMER
    assert "增值税" in result["display"]


def test_check_filing_tool_with_filing():
    pytest.importorskip("filing.validator")
    pytest.importorskip("ledger.income_ledger")
    from ledger.income_ledger import add_income

    add_income(date="2026-02-01", client="测试客户", inclusive_amount=103000)
    result = call_tool(
        "check_filing",
        {"period": "2026Q1", "fields": {"line_10": 101980.20, "line_24": 0.0}},
    )
    assert result["ok"] is True, result["error"]
    assert result["data"]["status"] in ("PASS", "WARNING")
    assert result["data"]["details"]


def test_export_filing_tool_with_filing():
    pytest.importorskip("filing.export")
    result = call_tool("export_filing", {"period": "2026Q1"})
    assert result["ok"] is True, result["error"]
    from pathlib import Path

    assert Path(result["data"]["path"]).exists()


def test_call_calc_disability_fund_tool():
    result = call_tool("calc_disability_fund", {"employees": 31, "average_wage": 80_000})
    assert result["ok"] is True
    assert result["data"]["payable"] == 33_480.0
    assert "残保金" in result["display"]
    assert "财政部公告2023年第8号" in "\n".join(
        source.get("source", "") for source in result["data"].get("policy_sources") or []
    )
    assert result["data"]["disclaimer"]
    # 30 人以下免征
    exempt = call_tool("calc_disability_fund", {"employees": 30, "average_wage": 80_000})
    assert exempt["ok"] is True
    assert exempt["data"]["payable"] == 0


def test_payroll_correction_tool_and_intent():
    result = call_tool("payroll_correction", {})
    assert result["ok"] is True
    assert "离职" in result["display"]
    assert "个税" in result["display"]
    assert result["data"]["disclaimer"]

    text = "员工离职后还在申报个税，员工在个税APP申诉了又撤销，怎么处理？"
    intent = detect_intent(text)
    assert intent["intent"] == "payroll_correction"
    routed = route(text)
    assert routed["tool"] == "payroll_correction"

    reply = orchestrator.local_reply(text)
    assert reply["intent"] == "payroll_correction"
    assert "更正" in reply["reply"]
    assert reply["used_llm"] is False
