"""DeepSeek 大模型客户端（OpenAI 兼容协议）。

本模块是整个项目中**唯一**与 DeepSeek 官方 API 通信的地方，特性：

* ``requests`` 直连 ``{base_url}/chat/completions``，Header 使用 ``Authorization: Bearer <key>``；
* 支持 ``messages``、``tools``（function calling）、``response_format={"type": "json_object"}``、
  以及 SSE 流式输出；
* 所有网络 / 鉴权 / 额度 / 限流错误统一翻译为中文 :class:`DeepSeekError`，消息可直接展示给用户；
* **绝不打印、记录或落盘 API Key**（也不写入日志与异常消息）。

安全约定：调用方只能发送必要文本片段（用户问题、政策检索上下文、申报表文字、
台账汇总），不得上传客户名等敏感明细；算税、台账、申报表生成全部由本地确定性代码完成。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable, Iterator

import requests

import config

#: 默认单次请求超时（秒）
DEFAULT_TIMEOUT = 90

#: 未配置 Key 时的统一提示
MSG_NO_KEY = (
    "未配置 DeepSeek API Key：请在「设置」页面填写 Key 并点击「测试连接」，"
    "或设置环境变量 DEEPSEEK_API_KEY。未配置时仍可使用本地规则模式（算税、台账、申报核对不受影响）。"
)


class DeepSeekError(Exception):
    """DeepSeek API 调用错误（中文消息，可直接展示给用户）。

    属性
    ----
    kind : 错误类别，``no_key`` / ``auth`` / ``quota`` / ``rate_limit`` / ``timeout`` /
           ``network`` / ``bad_request`` / ``server`` / ``parse`` / ``unknown``
    status_code : HTTP 状态码（网络异常时为 None）
    retryable : 是否值得稍后重试
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "unknown",
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status_code = status_code
        self.retryable = retryable

    def __str__(self) -> str:  # pragma: no cover - 简单透传
        return self.message


# =============================================================================
# 内部工具
# =============================================================================
def _clean_base_url(base_url: str) -> str:
    """规范化接口地址：去空白、去尾部斜杠；若用户填了 ``/chat/completions`` 则截断。"""
    url = (base_url or "").strip().rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    return url or config.LLM_CONFIG["base_url"]


def _short_api_message(response: requests.Response) -> str:
    """从错误响应体提取简短错误描述（不包含任何请求头 / Key）。"""
    try:
        body = response.json()
    except ValueError:
        try:
            text = (response.text or "").strip()
        except Exception:  # pragma: no cover - 极端情况
            return ""
        return text[:200]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message") or ""
            if message:
                return str(message)[:200]
        message = body.get("message")
        if message:
            return str(message)[:200]
    return ""


def _translate_error(response: requests.Response) -> DeepSeekError:
    """把 HTTP 错误翻译成可操作的中文错误。"""
    status = response.status_code
    detail = _short_api_message(response)
    suffix = f"（接口返回：{detail}）" if detail else ""
    if status in (401, 403):
        return DeepSeekError(
            "DeepSeek API Key 鉴权失败：Key 无效、已过期或无权限。"
            "请到「设置」页面重新填写 Key，并点击「测试连接」确认。" + suffix,
            kind="auth",
            status_code=status,
        )
    if status == 402:
        return DeepSeekError(
            "DeepSeek 账户余额不足：请前往 DeepSeek 开放平台（platform.deepseek.com）充值后重试。" + suffix,
            kind="quota",
            status_code=status,
        )
    if status == 404:
        return DeepSeekError(
            "DeepSeek 接口地址不存在：请检查「设置」中的 base_url，默认应为 https://api.deepseek.com。" + suffix,
            kind="bad_request",
            status_code=status,
        )
    if status == 429:
        return DeepSeekError(
            "DeepSeek 请求过于频繁或并发超限：请稍等几秒后重试；如持续出现请降低调用频率。" + suffix,
            kind="rate_limit",
            status_code=status,
            retryable=True,
        )
    if status == 400:
        return DeepSeekError(
            "DeepSeek 请求参数不合法：请检查模型名称、消息格式或工具定义。" + suffix,
            kind="bad_request",
            status_code=status,
        )
    if status in (500, 502, 503, 504):
        return DeepSeekError(
            f"DeepSeek 服务端暂时不可用（HTTP {status}）：请稍后重试，或切换到其他模型。" + suffix,
            kind="server",
            status_code=status,
            retryable=True,
        )
    return DeepSeekError(
        f"DeepSeek 请求失败（HTTP {status}）：请检查网络与配置后重试。" + suffix,
        kind="unknown",
        status_code=status,
    )


def _coerce_messages(messages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """规范化消息列表：只保留协议允许的字段，内容统一为字符串。"""
    cleaned: list[dict[str, Any]] = []
    allowed = {"role", "content", "name", "tool_call_id", "tool_calls", "reasoning_content"}
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        entry: dict[str, Any] = {"role": role, "content": message.get("content") or ""}
        for key in ("name", "tool_call_id", "tool_calls", "reasoning_content"):
            if key in message and message[key] is not None:
                entry[key] = message[key]
        for key in list(entry):
            if key not in allowed:
                entry.pop(key, None)
        cleaned.append(entry)
    return cleaned


def _first_balanced_object(text: str) -> str:
    """截取文本中第一个配平的 JSON 对象（正确处理字符串内的花括号与转义）。"""
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


def parse_json(text: Any) -> dict[str, Any]:
    """容错解析模型返回的 JSON 文本。

    兼容：```json 代码块包裹、前后有解释文字、中文引号（“”‘’）、尾随逗号、
    以及模型常见的「单引号 JSON」。解析失败返回空 dict，**不抛异常**。
    """
    if isinstance(text, dict):
        return dict(text)
    if text is None:
        return {}
    raw = str(text).strip()
    if not raw:
        return {}

    stripped = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", raw)
    stripped = re.sub(r"\s*```$", "", stripped).strip()
    candidates: list[str] = []
    for candidate in (raw, stripped, _first_balanced_object(raw), _first_balanced_object(stripped)):
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value

    # 中文引号 / 尾随逗号 / 单引号修正后再试
    fixed = stripped
    for old, new in (("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'"), ("：", ":"), ("，", ",")):
        fixed = fixed.replace(old, new)
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
    block = _first_balanced_object(fixed)
    if block:
        try:
            value = json.loads(block)
            if isinstance(value, dict):
                return value
        except (ValueError, TypeError):
            pass
        try:
            # 单引号 JSON 的兜底：转成双引号再解析
            value = json.loads(block.replace("'", '"'))
            if isinstance(value, dict):
                return value
        except (ValueError, TypeError):
            pass
    return {}


# =============================================================================
# 客户端
# =============================================================================
class DeepSeekClient:
    """DeepSeek Chat Completions 客户端（OpenAI 兼容）。

    参数缺省时从 :func:`config.get_llm_config` 读取（设置页 > 环境变量 > .env > 默认值）。
    API Key 只保存在实例内存中，不会出现在 ``repr``、日志或异常消息里。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: int | float | None = None,
    ) -> None:
        llm = config.get_llm_config()
        self.api_key = str(api_key if api_key is not None else llm.get("api_key") or "").strip()
        self.base_url = _clean_base_url(str(base_url or llm.get("base_url") or ""))
        self.model = str(model or llm.get("model") or config.LLM_CONFIG["model"]).strip()
        try:
            self.temperature = float(temperature if temperature is not None else llm.get("temperature", 0.3))
        except (TypeError, ValueError):
            self.temperature = 0.3
        try:
            self.max_tokens = int(max_tokens if max_tokens is not None else llm.get("max_tokens", 2000))
        except (TypeError, ValueError):
            self.max_tokens = 2000
        try:
            self.timeout = float(timeout if timeout is not None else DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            self.timeout = float(DEFAULT_TIMEOUT)

    # ---------------------------------------------------------------- 基本信息
    @property
    def configured(self) -> bool:
        """是否已配置 API Key。"""
        return bool(self.api_key)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    @property
    def masked_key(self) -> str:
        """脱敏 Key，可安全展示。"""
        return config.mask_key(self.api_key)

    def __repr__(self) -> str:  # pragma: no cover - 防御性：不泄露 Key
        return f"DeepSeekClient(base_url={self.base_url!r}, model={self.model!r}, configured={self.configured})"

    # ---------------------------------------------------------------- HTTP
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _post(self, payload: dict[str, Any], *, stream: bool = False) -> requests.Response:
        """发送请求（独立方法便于测试注入 fake transport）。"""
        if not self.configured:
            raise DeepSeekError(MSG_NO_KEY, kind="no_key")
        try:
            response = requests.post(
                self.endpoint,
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
                stream=stream,
            )
        except requests.exceptions.Timeout as exc:
            raise DeepSeekError(
                f"DeepSeek API 请求超时（{int(self.timeout)} 秒）：请检查网络连接，"
                "或稍后在「设置」中调大超时时间后重试。",
                kind="timeout",
                retryable=True,
            ) from exc
        except requests.exceptions.SSLError as exc:
            raise DeepSeekError(
                "DeepSeek API 连接 SSL 证书校验失败：请检查网络代理或系统证书设置。",
                kind="network",
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise DeepSeekError(
                "无法连接 DeepSeek API：请检查网络、代理或 base_url 配置"
                "（默认 https://api.deepseek.com）。",
                kind="network",
                retryable=True,
            ) from exc
        except requests.exceptions.RequestException as exc:  # pragma: no cover - 兜底
            raise DeepSeekError(
                "DeepSeek API 请求异常：请检查网络后重试。",
                kind="network",
                retryable=True,
            ) from exc
        if response.status_code >= 400:
            raise _translate_error(response)
        return response

    def _payload(
        self,
        messages: Iterable[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | str | None = None,
        stream: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _coerce_messages(messages),
            "stream": bool(stream),
        }
        try:
            payload["temperature"] = float(self.temperature if temperature is None else temperature)
        except (TypeError, ValueError):
            payload["temperature"] = self.temperature
        try:
            payload["max_tokens"] = int(self.max_tokens if max_tokens is None else max_tokens)
        except (TypeError, ValueError):
            payload["max_tokens"] = self.max_tokens
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_format is not None:
            if isinstance(response_format, str):
                payload["response_format"] = {"type": response_format}
            else:
                payload["response_format"] = response_format
        return payload

    # ---------------------------------------------------------------- 主接口
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        """发起一次对话补全请求。

        返回
        ----
        ``{"content", "reasoning_content", "tool_calls", "usage", "model", "raw", "warnings"}``；
        ``tool_calls`` 为 ``[{"id", "name", "arguments": dict}]``，参数 JSON 解析失败时返回空 dict
        并在 ``warnings`` 中告警。
        """
        payload = self._payload(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )
        response = self._post(payload)
        try:
            data = response.json()
        except ValueError as exc:
            raise DeepSeekError(
                "DeepSeek 返回内容不是合法 JSON：可能是网关 / 代理返回了错误页面，"
                "请检查 base_url 是否正确。",
                kind="parse",
            ) from exc
        if not isinstance(data, dict):
            raise DeepSeekError("DeepSeek 返回内容格式异常（不是 JSON 对象）。", kind="parse")

        warnings: list[str] = []
        choices = data.get("choices") or []
        if not choices:
            error = data.get("error")
            detail = ""
            if isinstance(error, dict):
                detail = str(error.get("message") or "")
            raise DeepSeekError(
                "DeepSeek 未返回任何回答" + (f"（{detail[:200]}）" if detail else "") + "，请稍后重试。",
                kind="server",
            )

        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # 兼容多段内容
            content = "".join(
                str(part.get("text") or "") for part in content if isinstance(part, dict)
            )
        tool_calls: list[dict[str, Any]] = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            raw_arguments = function.get("arguments")
            arguments: dict[str, Any]
            if isinstance(raw_arguments, dict):
                arguments = raw_arguments
            else:
                arguments = parse_json(raw_arguments)
                if not arguments and raw_arguments:
                    warnings.append(
                        f"工具 {function.get('name') or '未知'} 的参数 JSON 解析失败，已按空参数处理。"
                    )
            tool_calls.append(
                {
                    "id": call.get("id"),
                    "name": str(function.get("name") or ""),
                    "arguments": arguments,
                }
            )

        return {
            "content": str(content or ""),
            "reasoning_content": str(message.get("reasoning_content") or ""),
            "tool_calls": tool_calls,
            "usage": data.get("usage") or {},
            "model": str(data.get("model") or self.model),
            "raw": data,
            "warnings": warnings,
        }

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        """流式对话补全：逐段 yield 文本增量（DeepSeek SSE / OpenAI 兼容格式）。

        说明：本方法只处理 ``delta.content``；工具调用请使用非流式 :meth:`chat`，
        由 Agent 编排层在工具执行完后对最终回答调用本方法。
        """
        payload = self._payload(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        payload["stream"] = True
        response = self._post(payload, stream=True)
        try:
            for raw_line in response.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8", "replace") if isinstance(raw_line, bytes) else str(raw_line)
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                error = event.get("error") if isinstance(event, dict) else None
                if isinstance(error, dict):
                    raise DeepSeekError(
                        "DeepSeek 流式返回错误：" + str(error.get("message") or "")[:200],
                        kind="api",
                    )
                choices = event.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield str(content)
        finally:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass


    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | str | None = None,
    ) -> Iterator[str]:
        """流式对话：逐段 ``yield`` 文本增量（SSE ``data:`` 行解析）。

        说明：流式模式下仅产出文本增量；工具调用（function calling）请使用 :meth:`chat`。
        """
        payload = self._payload(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            stream=True,
        )
        response = self._post(payload, stream=True)
        try:
            for raw_line in response.iter_lines(decode_unicode=False):
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8", "ignore") if isinstance(raw_line, bytes) else str(raw_line)
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data_text = line[len("data:") :].strip()
                if data_text == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_text)
                except ValueError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content")
                if isinstance(text, list):
                    text = "".join(
                        str(part.get("text") or "") for part in text if isinstance(part, dict)
                    )
                if text:
                    yield str(text)
        finally:
            try:
                response.close()
            except Exception:  # pragma: no cover - 关闭失败不影响结果
                pass

    def complete(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        """单轮补全，返回纯文本（便捷方法）。"""
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.chat(messages, **kwargs)["content"]

    def test_connection(self) -> dict[str, Any]:
        """用一次极短请求测试连通性并测量延迟。

        返回 ``{"ok", "model", "latency_ms", "message", "reply"}``；任何异常都转成 ``ok=False``
        的中文说明，不向调用方抛错。
        """
        result: dict[str, Any] = {
            "ok": False,
            "model": self.model,
            "latency_ms": 0,
            "message": "",
            "reply": "",
        }
        if not self.configured:
            result["message"] = MSG_NO_KEY
            return result
        started = time.perf_counter()
        try:
            data = self.chat(
                [{"role": "user", "content": "请只回复四个字：连接成功"}],
                temperature=0.0,
                max_tokens=16,
            )
            latency = int(round((time.perf_counter() - started) * 1000))
            reply = (data.get("content") or "").strip()
            result.update(
                {
                    "ok": True,
                    "latency_ms": latency,
                    "message": f"连接成功：DeepSeek API 可用（模型 {data.get('model') or self.model}，"
                    f"延迟约 {latency} 毫秒）。",
                    "reply": reply,
                }
            )
        except DeepSeekError as exc:
            result["latency_ms"] = int(round((time.perf_counter() - started) * 1000))
            result["message"] = str(exc)
        except Exception as exc:  # pragma: no cover - 防御性兜底
            result["latency_ms"] = int(round((time.perf_counter() - started) * 1000))
            result["message"] = f"测试连接失败：{exc}"
        return result


def get_client(api_key: str | None = None, model: str | None = None) -> DeepSeekClient:
    """创建客户端（缺省读取设置页 / 环境变量中的 Key 与模型）。"""
    return DeepSeekClient(api_key=api_key, model=model)
