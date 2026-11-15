"""DeepSeek 工具 API 客户端（v1.30）：OCR / 网页搜索 / 网页抓取。

只封装只读工具；API Key 复用系统设置，文件/查询不会写入本地日志。
所有调用失败返回结构化错误，由 UI 降级提示，不抛给页面。
"""

from __future__ import annotations

from typing import Any

import requests

import config

DEFAULT_TIMEOUT = 60
OCR_TIMEOUT = 200
MAX_OCR_BYTES = 100 * 1024 * 1024


class ToolAPIError(RuntimeError):
    """工具 API 调用失败（已翻译为中文）。"""


def is_configured() -> bool:
    return bool(str(config.get_llm_config().get("api_key") or "").strip())


def _settings() -> tuple[str, str]:
    cfg = config.get_llm_config()
    key = str(cfg.get("api_key") or "").strip()
    base = str(cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")
    if not key:
        raise ToolAPIError("未配置 DeepSeek API Key，请到「设置 → API 与模型」填写。")
    return key, base


def _endpoint(base: str, path: str) -> str:
    base = base.rstrip("/")
    if base.endswith("/v1"):
        return base + path
    return base + "/v1" + path


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _translate_error(response: requests.Response) -> str:
    try:
        payload = response.json()
        message = str((payload.get("error") or {}).get("message") or payload.get("message") or "").strip()
    except Exception:  # noqa: BLE001
        message = ""
    if response.status_code == 402:
        return "DeepSeek 账户余额不足，请充值后重试。"
    if response.status_code == 401:
        return "DeepSeek API Key 无效或缺失，请到设置检查。"
    if response.status_code == 429:
        return "DeepSeek 请求过于频繁，请稍后重试。"
    if response.status_code in (408, 504):
        return "DeepSeek 处理超时，请稍后重试或换小一点的文件。"
    if response.status_code == 413:
        return "文件过大，DeepSeek 拒绝处理。"
    return f"DeepSeek 返回 HTTP {response.status_code}" + (f"：{message[:200]}" if message else "")


def ocr_file(data: bytes, filename: str, *, purpose: str = "text_recognition") -> dict[str, Any]:
    """调用 DeepSeek OCR，返回逐页文本。"""
    key, base = _settings()
    if not data:
        raise ToolAPIError("文件内容为空。")
    if len(data) > MAX_OCR_BYTES:
        raise ToolAPIError("文件超过 100 MB，DeepSeek OCR 无法处理。")
    if purpose not in ("text_recognition", "rich_layout_recognition"):
        raise ToolAPIError("OCR purpose 不合法。")
    try:
        response = requests.post(
            _endpoint(base, "/tools/ocr"),
            headers=_headers(key),
            files={"file": (filename or "document", data)},
            data={"purpose": purpose},
            timeout=OCR_TIMEOUT,
        )
    except requests.exceptions.RequestException as exc:
        raise ToolAPIError(f"无法连接 DeepSeek OCR：{exc}") from exc
    if response.status_code >= 400:
        raise ToolAPIError(_translate_error(response))
    try:
        payload = response.json()
    except ValueError as exc:
        raise ToolAPIError("DeepSeek OCR 返回内容不是 JSON。") from exc
    if isinstance(payload, dict) and payload.get("error"):
        raise ToolAPIError(str(payload["error"].get("message") or "OCR 识别失败"))
    pages = payload.get("pages") or []
    text = "\n".join(str(page.get("text") or "") for page in pages if isinstance(page, dict))
    return {"ok": True, "text": text, "pages": pages, "page_count": payload.get("page_count") or len(pages), "raw": payload}


def search(query: str, *, count: int = 5, need_content: bool = False) -> dict[str, Any]:
    """调用 DeepSeek Search，返回结构化网页结果。"""
    key, base = _settings()
    text = str(query or "").strip()
    if not text:
        raise ToolAPIError("搜索词不能为空。")
    payload = {"query": text[:128], "count": max(1, min(int(count), 50)), "need_content": bool(need_content)}
    try:
        response = requests.post(
            _endpoint(base, "/tools/search"), headers={**_headers(key), "Content-Type": "application/json"},
            json=payload, timeout=DEFAULT_TIMEOUT,
        )
    except requests.exceptions.RequestException as exc:
        raise ToolAPIError(f"无法连接 DeepSeek Search：{exc}") from exc
    if response.status_code >= 400:
        raise ToolAPIError(_translate_error(response))
    try:
        data = response.json()
    except ValueError as exc:
        raise ToolAPIError("DeepSeek Search 返回内容不是 JSON。") from exc
    return {"ok": True, "results": data.get("results") or [], "raw": data}


def fetch(url: str) -> dict[str, Any]:
    """调用 DeepSeek Fetch，把网页转为 Markdown 文本。"""
    key, base = _settings()
    text = str(url or "").strip()
    if not text.startswith(("http://", "https://")):
        raise ToolAPIError("抓取地址必须是 http(s) URL。")
    try:
        response = requests.post(
            _endpoint(base, "/tools/fetch"), headers={**_headers(key), "Content-Type": "application/json"},
            json={"url": text}, timeout=DEFAULT_TIMEOUT,
        )
    except requests.exceptions.RequestException as exc:
        raise ToolAPIError(f"无法连接 DeepSeek Fetch：{exc}") from exc
    if response.status_code >= 400:
        raise ToolAPIError(_translate_error(response))
    try:
        data = response.json()
    except ValueError as exc:
        raise ToolAPIError("DeepSeek Fetch 返回内容不是 JSON。") from exc
    return {"ok": True, "title": data.get("title") or "", "content": data.get("content") or "", "raw": data}


__all__ = ["ToolAPIError", "is_configured", "ocr_file", "search", "fetch", "MAX_OCR_BYTES"]
