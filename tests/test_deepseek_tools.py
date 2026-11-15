"""DeepSeek 工具 API 客户端测试（v1.30，全部 mock）。"""

from __future__ import annotations

import pytest

from integrations import deepseek_tools


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload


def test_ocr_posts_multipart(monkeypatch):
    monkeypatch.setattr(deepseek_tools.config, "get_llm_config", lambda: {"api_key": "sk-test", "base_url": "https://api.deepseek.com"})
    captured = {}

    def fake_post(url, headers=None, files=None, data=None, timeout=None, **kwargs):
        captured.update({"url": url, "headers": headers, "files": files, "data": data})
        return FakeResponse(payload={"page_count": 1, "pages": [{"index": 0, "text": "发票号码：12345678"}]})

    monkeypatch.setattr(deepseek_tools.requests, "post", fake_post)
    result = deepseek_tools.ocr_file(b"pdf-bytes", "invoice.pdf")
    assert result["ok"] is True
    assert "发票号码" in result["text"]
    assert captured["url"].endswith("/v1/tools/ocr")
    assert captured["data"]["purpose"] == "text_recognition"


def test_search_and_fetch(monkeypatch):
    monkeypatch.setattr(deepseek_tools.config, "get_llm_config", lambda: {"api_key": "sk-test", "base_url": "https://api.deepseek.com"})
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        calls.append({"url": url, "json": json})
        if url.endswith("/tools/search"):
            return FakeResponse(payload={"results": [{"title": "政策", "url": "https://www.chinatax.gov.cn/x"}]})
        return FakeResponse(payload={"title": "政策", "content": "# 标题\n正文"})

    monkeypatch.setattr(deepseek_tools.requests, "post", fake_post)
    search = deepseek_tools.search("小规模纳税人")
    assert search["results"][0]["title"] == "政策"
    fetched = deepseek_tools.fetch("https://www.chinatax.gov.cn/x")
    assert "正文" in fetched["content"]
    assert any(call["url"].endswith("/tools/fetch") for call in calls)


def test_missing_api_key(monkeypatch):
    monkeypatch.setattr(deepseek_tools.config, "get_llm_config", lambda: {"api_key": ""})
    with pytest.raises(deepseek_tools.ToolAPIError):
        deepseek_tools.search("test")


def test_error_translation(monkeypatch):
    monkeypatch.setattr(deepseek_tools.config, "get_llm_config", lambda: {"api_key": "sk-test", "base_url": "https://api.deepseek.com"})
    monkeypatch.setattr(deepseek_tools.requests, "post", lambda *a, **k: FakeResponse(status=402, payload={"error": {"message": "insufficient"}}))
    with pytest.raises(deepseek_tools.ToolAPIError) as exc:
        deepseek_tools.search("test")
    assert "余额不足" in str(exc.value)
