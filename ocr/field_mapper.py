"""申报表栏次映射：把 OCR 文本 / 粘贴文本映射为「栏次 → 金额」。

背景
----
DeepSeek 官方 API **没有视觉能力**：截图先由 :mod:`ocr.extractor` 本地 OCR 成文字，
本模块负责**规则优先**的字段映射；规则覆盖不足且已配置 API Key 时才让 DeepSeek 兜底
（:func:`llm_extract`），无 Key 时直接跳过，不影响使用。

排版兼容
--------
不同截图 / 手机拍照的排版差异较大，本模块同时支持：

1. 同一行：「第10栏 小微企业免税销售额 101,980.20」「10 101980.20」；
2. **跨行**（macOS Vision 常见）：「10小微企业免税销售额」与金额「101980.20」分属两行；
3. 粘贴表格：「栏次\\t金额」「10    101980.20」「10,101980.20」；
4. 中文括号、金额千分位、负数（本期应补（退）税额可能为负）。
"""

from __future__ import annotations

import re
from typing import Any

#: 增值税申报表（小规模纳税人适用）关键栏次定义
VAT_FIELD_DEFS: list[dict[str, Any]] = [
    {
        "key": "line_1",
        "line_no": "1",
        "name": "应征增值税不含税销售额（3%征收率）",
        "aliases": ["应征增值税不含税销售额", "应征增值税不含税销售额（3%征收率）", "第1栏"],
    },
    {
        "key": "line_2",
        "line_no": "2",
        "name": "税务机关代开增值税专用发票销售额",
        "aliases": ["税务机关代开增值税专用发票销售额", "代开增值税专用发票销售额", "第2栏"],
    },
    {
        "key": "line_10",
        "line_no": "10",
        "name": "小微企业免税销售额",
        "aliases": ["小微企业免税销售额", "免税销售额", "第10栏"],
    },
    {
        "key": "line_11",
        "line_no": "11",
        "name": "未达起征点销售额",
        "aliases": ["未达起征点销售额", "起征点销售额", "第11栏"],
    },
    {
        "key": "line_18",
        "line_no": "18",
        "name": "应纳税额",
        "aliases": ["应纳税额", "第18栏"],
    },
    {
        "key": "line_19",
        "line_no": "19",
        "name": "本期免税额",
        "aliases": ["本期免税额", "本期免税", "第19栏"],
    },
    {
        "key": "line_22",
        "line_no": "22",
        "name": "应纳税额减征额",
        "aliases": ["应纳税额减征额", "减征额", "第22栏"],
    },
    {
        "key": "line_24",
        "line_no": "24",
        "name": "本期应补（退）税额",
        "aliases": ["本期应补（退）税额", "本期应补退税额", "本期应补（退）", "应补（退）税额", "第24栏"],
    },
    {
        "key": "surtax_total",
        "line_no": "附加",
        "name": "附加税费合计",
        "aliases": ["附加税费合计", "附加税费", "城市维护建设税", "教育费附加", "地方教育附加"],
    },
    {
        "key": "total_payable",
        "line_no": "合计",
        "name": "本期应补（退）税费合计",
        "aliases": ["应补（退）税费合计", "本期应补（退）税费合计", "合计应补（退）"],
    },
]

_FIELD_KEYS = {definition["key"] for definition in VAT_FIELD_DEFS}
_LINE_NO_TO_KEY = {definition["line_no"]: definition["key"] for definition in VAT_FIELD_DEFS if definition["line_no"].isdigit()}

_FULLWIDTH = str.maketrans("０１２３４５６７８９．，－（）", "0123456789.,-()")

#: 金额 token（支持千分位、负数、1-2 位小数）
_MONEY_TOKEN_RE = re.compile(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?(?!\d)")
#: 紧跟在这些字符后的数字不是金额（百分比、栏次、日期等）
_EXCLUDE_TAIL_RE = re.compile(r"^\s*(?:%|‰|栏|行|年|月|日|季度|号|项|个|位|次|份|张)")
#: 整行只有金额
_AMOUNT_ONLY_RE = re.compile(r"^\s*[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?\s*(?:元)?\s*$")
#: 行首栏次编号
_LINE_NO_RE = re.compile(r"^(?:第\s*)?(\d{1,2})\s*(?:栏|行|项)?\s*[.、:：]?\s*")
#: 纯栏次编号（OCR 可能把栏号单独识别成一行）
_BARE_LINE_NO_RE = re.compile(r"^\s*(?:第\s*)?(\d{1,2})\s*(?:栏|行|项)?\s*$")

_HEADER_HINTS = ("栏次", "申报表", "税款所属期", "纳税人名称", "纳税人识别号", "金额单位", "序号")


# =============================================================================
# 基础工具
# =============================================================================
def _normalize(text: Any) -> str:
    return str(text or "").translate(_FULLWIDTH).replace("（", "(").replace("）", ")")


def _parse_money(token: str) -> float | None:
    text = str(token or "").strip().replace(",", "").replace("¥", "").replace("￥", "").replace("元", "")
    if not text or text in ("-", "+"):
        return None
    try:
        value = round(float(text), 2)
    except (TypeError, ValueError):
        return None
    if abs(value) > 1e12:
        return None
    return value


def _amounts_in(text: str) -> list[float]:
    """抽取一段文字中的金额（排除百分比、栏次号、日期等非金额数字）。"""
    raw = _normalize(text)
    values: list[float] = []
    for match in _MONEY_TOKEN_RE.finditer(raw):
        start, end = match.span()
        if start > 0 and raw[start - 1] in "%‰":
            continue
        if _EXCLUDE_TAIL_RE.match(raw[end : end + 3]):
            continue
        value = _parse_money(match.group(0))
        if value is not None:
            values.append(value)
    return values


def _is_header(line: str) -> bool:
    return any(hint in line for hint in _HEADER_HINTS)


def _strip_line_no(line: str) -> tuple[str, str]:
    """去掉行首栏次编号，返回 ``(编号, 其余文本)``。"""
    match = _LINE_NO_RE.match(line)
    if not match:
        return "", line.strip()
    return match.group(1), line[match.end() :].strip()


def _definition_by_line_no(line_no: str) -> dict[str, Any] | None:
    key = _LINE_NO_TO_KEY.get(str(line_no))
    if not key:
        return None
    for definition in VAT_FIELD_DEFS:
        if definition["key"] == key:
            return definition
    return None


def _definition_by_name(text: str) -> dict[str, Any] | None:
    """按名称 / 别名匹配栏次，命中多个时取**最长别名**（避免「应纳税额」误吃「应纳税额减征额」）。"""
    best: tuple[int, dict[str, Any]] | None = None
    raw = _normalize(text)
    for definition in VAT_FIELD_DEFS:
        for alias in [definition["name"], *definition["aliases"]]:
            alias_norm = _normalize(alias)
            if alias_norm and alias_norm in raw:
                if best is None or len(alias_norm) > best[0]:
                    best = (len(alias_norm), definition)
    return best[1] if best else None


def _match_field(line: str) -> tuple[dict[str, Any], str] | None:
    """匹配一行文字对应的栏次，返回 ``(定义, 去掉栏号后的剩余文本)``。"""
    line_no, rest = _strip_line_no(line)
    if line_no:
        definition = _definition_by_line_no(line_no)
        if definition:
            return definition, rest
    definition = _definition_by_name(rest)
    if definition:
        return definition, rest
    return None


def _canonical_key(raw_key: Any) -> str | None:
    """把 ``line_10`` / ``第10栏`` / ``10`` / 栏次名 规范为字段 key。"""
    text = str(raw_key or "").strip()
    if not text:
        return None
    if text in _FIELD_KEYS:
        return text
    lowered = text.lower().replace("-", "_")
    if lowered in _FIELD_KEYS:
        return lowered
    if lowered.startswith("line_"):
        return lowered if lowered in _FIELD_KEYS else None
    match = re.search(r"(\d{1,2})", text)
    if match:
        definition = _definition_by_line_no(match.group(1))
        if definition:
            return definition["key"]
    definition = _definition_by_name(text)
    if definition:
        return definition["key"]
    return None


# =============================================================================
# 规则提取
# =============================================================================
def parse_pasted_table(text: str) -> dict[str, Any]:
    """解析用户粘贴的「栏次 金额」表格文本。

    支持 TAB / 多空格 / ``|`` 分隔的表格，以及 ``10,101980.20`` 形式的 CSV 行。
    返回 ``{"fields": {key: float}, "unmapped": [str]}``。
    """
    raw = _normalize(text)
    fields: dict[str, Any] = {}
    unmapped: list[str] = []
    for source_line in raw.splitlines():
        line = source_line.strip()
        if not line:
            continue
        cells = [cell.strip() for cell in re.split(r"\t+|\s{2,}|\s*\|\s*", line) if cell.strip()]
        if len(cells) < 2:
            # 单列：CSV 行或「10 101980.20」单空格行
            match = re.fullmatch(r"(?:第\s*)?(\d{1,2})\s*栏?\s*[,，]\s*(-?[\d,]+(?:\.\d+)?)", line)
            if match:
                definition = _definition_by_line_no(match.group(1))
                value = _parse_money(match.group(2))
                if definition and value is not None:
                    fields.setdefault(definition["key"], value)
                    continue
            match_result = _match_field(line)
            if match_result:
                definition, rest = match_result
                values = _amounts_in(rest)
                if values:
                    fields.setdefault(definition["key"], values[0])
                # 只有栏次名没有金额：属于跨行排版，由 regex_extract 的跨行逻辑处理
                continue
            if _amounts_in(line) and not _AMOUNT_ONLY_RE.match(line):
                unmapped.append(line[:100])
            continue
        if any(hint in cells[0] for hint in ("栏次", "序号", "项目")) and any("金额" in cell for cell in cells[1:]):
            continue
        name_cell = cells[0]
        amount_cell = cells[-1]
        definition = _definition_by_name(name_cell)
        if not definition and name_cell.isdigit():
            definition = _definition_by_line_no(name_cell)
        if definition is None:
            match = _BARE_LINE_NO_RE.match(name_cell)
            if match:
                definition = _definition_by_line_no(match.group(1))
        value = _parse_money(amount_cell) if _AMOUNT_ONLY_RE.match(amount_cell) else None
        if definition and value is not None:
            fields.setdefault(definition["key"], value)
        elif _amounts_in(line):
            unmapped.append(line[:100])
    return {"fields": fields, "unmapped": unmapped}


def regex_extract(text: str) -> dict[str, Any]:
    """正则规则提取申报表栏次金额。

    返回 ``{"fields": {key: float}, "unmapped": [str], "matched": [{"key","line"}]}``。
    兼容同一行与跨行（栏次名与金额分属两行）两种排版。
    """
    raw = _normalize(text)
    fields: dict[str, Any] = {}
    matched: list[dict[str, Any]] = []
    unmapped: list[str] = []
    pending_key: str | None = None
    pending_age = 0

    for source_line in raw.splitlines():
        line = source_line.strip()
        if not line:
            continue
        if _is_header(line):
            pending_key, pending_age = None, 0
            continue

        # 1) 整行只有金额：补到上一行「只有栏次名没有金额」的字段（跨行排版）
        if _AMOUNT_ONLY_RE.match(line):
            value = _parse_money(line)
            bare_line_no = _BARE_LINE_NO_RE.match(line)
            if pending_key and value is not None:
                fields.setdefault(pending_key, value)
                matched.append({"key": pending_key, "line": line})
                pending_key, pending_age = None, 0
            elif bare_line_no:
                # 单独一行的栏次编号（噪声），忽略
                pending_age += 1
            continue

        # 2) 同行「栏次名 + 金额」
        match_result = _match_field(line)
        if match_result:
            definition, rest = match_result
            values = _amounts_in(rest)
            if values:
                fields.setdefault(definition["key"], values[0])
                matched.append({"key": definition["key"], "line": line})
                pending_key, pending_age = None, 0
            else:
                pending_key, pending_age = definition["key"], 0
            continue

        # 3) 无法匹配栏次：记录未映射行
        if _amounts_in(line):
            unmapped.append(line[:100])
        pending_age += 1
        if pending_age > 3:
            pending_key, pending_age = None, 0

    # 表格粘贴兜底（TAB / 多空格 / 逗号分隔）
    table = parse_pasted_table(raw)
    for key, value in table["fields"].items():
        fields.setdefault(key, value)
    for line in table["unmapped"]:
        if line not in unmapped:
            unmapped.append(line)

    return {
        "fields": {key: round(float(value), 2) for key, value in fields.items() if key in _FIELD_KEYS},
        "unmapped": unmapped,
        "matched": matched,
    }


# =============================================================================
# LLM 兜底
# =============================================================================
def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = str(value or "").strip().replace(",", "").replace("¥", "").replace("￥", "").replace("元", "")
    text = text.replace("（", "(").replace("）", ")")
    try:
        return round(float(text), 2)
    except (TypeError, ValueError):
        return None


def llm_extract(text: str, client: Any) -> dict[str, Any]:
    """规则失败时用 DeepSeek 兜底做栏次映射（无 Key / 异常时返回空结果，不抛错）。

    返回 ``{"fields": {key: float}, "used_llm": bool, "error": str}``。
    """
    if client is None or not getattr(client, "configured", False):
        return {"fields": {}, "used_llm": False, "error": "未配置 DeepSeek API Key，已使用正则规则提取栏次。"}
    field_desc = "\n".join(
        f"- {definition['key']}（第{definition['line_no']}栏 {definition['name']}；"
        f"别名：{'、'.join(definition['aliases'])}）"
        for definition in VAT_FIELD_DEFS
    )
    prompt = (
        "下面是一张增值税及附加税费申报表（小规模纳税人适用）的 OCR 文字。\n"
        "请把文字中的金额映射到标准栏次，只输出 JSON 对象，不要解释。\n\n"
        f"标准栏次：\n{field_desc}\n\n"
        "输出格式：{\"fields\": {\"line_10\": 101980.20, \"line_24\": 0.00}}\n"
        "要求：只填写文字中明确出现的金额；金额为数字（元，保留 2 位小数）；"
        "「本期应补（退）税额」可能为负数；无法确定的栏次不要输出。\n\n"
        f"OCR 文字：\n{text[:4000]}"
    )
    try:
        from agent.llm_client import parse_json

        response = client.chat(
            [
                {"role": "system", "content": "你是中国增值税申报表栏次识别助手，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=800,
            response_format={"type": "json_object"},
        )
        data = parse_json(response.get("content") or "")
        raw_fields = data.get("fields") if isinstance(data.get("fields"), dict) else data
        fields: dict[str, float] = {}
        for key, value in (raw_fields or {}).items():
            canonical = _canonical_key(key)
            number = _coerce_number(value)
            if canonical and number is not None:
                fields[canonical] = number
        return {
            "fields": fields,
            "used_llm": True,
            "error": "" if fields else "DeepSeek 未识别出可用栏次金额，请手动粘贴「栏次 金额」文本。",
        }
    except Exception as exc:  # noqa: BLE001 - LLM 兜底失败不影响规则结果
        return {"fields": {}, "used_llm": False, "error": f"DeepSeek 栏次映射失败：{exc}"}


# =============================================================================
# 对外主接口
# =============================================================================
def map_fields(
    text: str,
    *,
    period: str | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """把申报表文本映射为栏次字典（规则优先，LLM 兜底）。

    参数
    ----
    text   : OCR 文本或用户粘贴文本
    period : 所属期间（仅透传，便于调用方追踪）
    client : 可选 :class:`agent.llm_client.DeepSeekClient`；无 Key 时不会调用

    返回
    ----
    ``{"fields": {key: float}, "method": "rule"|"llm", "unmapped": [str], "ok": bool,
       "raw": str, "period": str|None, "llm_error": str}``
    """
    raw = str(text or "")
    rule = regex_extract(raw)
    fields: dict[str, Any] = dict(rule["fields"])
    method = "rule"
    llm_error = ""
    if len(fields) < 3 and client is not None and getattr(client, "configured", False):
        llm_result = llm_extract(raw, client)
        llm_error = llm_result.get("error") or ""
        if llm_result.get("fields"):
            for key, value in llm_result["fields"].items():
                fields.setdefault(key, value)
            method = "llm"
    return {
        "fields": fields,
        "method": method,
        "unmapped": rule.get("unmapped", []),
        "ok": bool(fields),
        "raw": raw,
        "period": period,
        "llm_error": llm_error,
    }


def canonical_field_key(raw_key: Any) -> str | None:
    """对外公开的栏次 key 规范化函数（``第10栏`` / ``line_10`` / ``10`` → ``line_10``）。"""
    return _canonical_key(raw_key)
