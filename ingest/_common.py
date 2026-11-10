"""ingest 包内部公共工具：日期/金额解析、名称归一化、公司方向判定与内容哈希。

这些工具服务于发票解析、银行流水解析与自动匹配，保证三处口径一致：
* 日期统一输出 ``YYYY-MM-DD``，无法识别时返回空字符串（不擅自用今天顶替）；
* 金额去除 ``¥/￥/千分位/空格/元`` 后四舍五入 2 位小数；
* 名称归一化（全角转半角、去空格标点、去常见企业后缀）后做相似度比较。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any

import config
from engine.money import round2

_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y%m%d",
    "%Y年%m月%d日",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M",
    "%Y年%m月%d日 %H:%M:%S",
)

_COMPANY_SUFFIXES: tuple[str, ...] = (
    "股份有限公司",
    "有限责任公司",
    "集团有限公司",
    "有限公司",
    "集团",
    "网络科技",
    "信息技术",
    "科技",
    "公司",
    "服务",
    "技术",
)

_PUNCTUATION = set("（）()【】[]{}<>《》〈〉“”\"'‘’、，,。.·:：;；!！?？-—_/\\| \t\r\n")


def clean_text(value: Any) -> str:
    """把任意值转为去首尾空白的字符串。"""
    if value is None:
        return ""
    return str(value).strip()


def parse_number(value: Any, *, absolute: bool = False) -> float | None:
    """解析金额/数字：去除货币符号、千分位、空格与「元」字，返回 2 位小数。

    无法解析（空值、``-``、``/``、纯文字）时返回 ``None``；
    ``absolute=True`` 时返回绝对值（用于流水金额统一存正数）。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = unicodedata.normalize("NFKC", str(value)).strip()
        text = (
            text.replace("¥", "")
            .replace("￥", "")
            .replace(",", "")
            .replace("，", "")
            .replace(" ", "")
            .replace("\u3000", "")
            .replace("元", "")
            .replace("整", "")
        )
        text = text.strip("（）()")
        if text in ("", "-", "--", "—", "/", "\\", "无", "null", "None"):
            return None
        try:
            number = float(text)
        except ValueError:
            match = re.search(r"-?\d+(?:\.\d+)?", text)
            if not match:
                return None
            number = float(match.group(0))
    if absolute:
        number = abs(number)
    return round2(number)


def parse_date(value: Any) -> str:
    """解析常见日期写法，返回 ``YYYY-MM-DD``；无法识别时返回空字符串。

    支持 ``2026-01-15``、``2026/1/5``、``2026.01.15``、``20260115``、
    ``2026年1月15日``、``2026-01-15 10:23:45``、Excel 日期序列与 ``datetime`` 对象。
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        serial = float(value)
        if 20_000 <= serial <= 80_000:  # Excel 日期序列号
            try:
                return (date(1899, 12, 30) + timedelta(days=int(serial))).isoformat()
            except (OverflowError, ValueError):
                return ""
        value = str(int(serial)) if serial.is_integer() else str(serial)
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text:
        return ""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    match = re.search(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})", text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return ""
    return ""


def normalize_name(value: Any) -> str:
    """企业/个人名称归一化：全角转半角、去空格标点、小写、去常见企业后缀。

    用于发票对方名称与流水对方户名的相似度比较，避免「有限公司」「科技」等后缀干扰。
    """
    text = unicodedata.normalize("NFKC", clean_text(value)).lower()
    if not text:
        return ""
    text = "".join(char for char in text if char.isalnum() or "\u4e00" <= char <= "\u9fff")
    changed = True
    while changed and text:
        changed = False
        for suffix in _COMPANY_SUFFIXES:
            if text.endswith(suffix) and len(text) > len(suffix):
                text = text[: -len(suffix)]
                changed = True
                break
    return text


def name_similarity(left: Any, right: Any) -> float:
    """两个名称归一化后的相似度（0~1），使用 :class:`difflib.SequenceMatcher`。"""
    from difflib import SequenceMatcher

    a = normalize_name(left)
    b = normalize_name(right)
    if not a or not b:
        return 0.0
    return round(SequenceMatcher(None, a, b).ratio(), 4)


def company_names() -> list[str]:
    """返回本企业可用于方向判定的名称/简称/统一社会信用代码（去空）。"""
    company = config.get_company()
    candidates = [
        clean_text(company.get("name")),
        clean_text(company.get("short_name")),
        clean_text(company.get("unified_social_credit_code")),
    ]
    return [name for name in candidates if name]


def _matches_company(text: str) -> bool:
    """判断名称/代码文本是否指向本企业（归一化后包含或被包含）。"""
    normalized = normalize_name(text)
    if not normalized:
        return False
    for name in company_names():
        target = normalize_name(name)
        if not target:
            continue
        if target in normalized or normalized in target:
            return True
    return False


def detect_invoice_direction(
    seller: Any,
    buyer: Any,
    *,
    seller_id: str = "",
    buyer_id: str = "",
) -> tuple[str, str]:
    """按销售方/购买方与公司的关系判定发票方向，返回 ``(方向, 备注)``。

    规则：销售方含本企业 → 销项；购买方含本企业 → 进项；
    都判断不出时用名称相似度择一并给出备注；仍无法判断默认销项并提示待确认。
    """
    seller_text = f"{clean_text(seller)} {clean_text(seller_id)}".strip()
    buyer_text = f"{clean_text(buyer)} {clean_text(buyer_id)}".strip()
    seller_hit = _matches_company(seller_text)
    buyer_hit = _matches_company(buyer_text)
    if seller_hit and not buyer_hit:
        return "销项", ""
    if buyer_hit and not seller_hit:
        return "进项", ""
    if seller_hit and buyer_hit:
        return "销项", "销售方与购买方均包含本企业名称，已按销项处理，请人工核对。"

    company = company_names()[0] if company_names() else ""
    seller_ratio = name_similarity(company, seller_text)
    buyer_ratio = name_similarity(company, buyer_text)
    if max(seller_ratio, buyer_ratio) >= 0.35:
        if seller_ratio >= buyer_ratio:
            return "销项", "未精确匹配本企业名称，按销售方相似度判定为销项，请核对。"
        return "进项", "未精确匹配本企业名称，按购买方相似度判定为进项，请核对。"
    return "销项", "无法根据销售方/购买方判断开票方向，已默认销项，请人工确认。"


def detect_invoice_kind(type_text: Any, *, specific_mark: str = "") -> str:
    """根据发票类型文本（及数电票特殊标记）判定发票类型。"""
    text = clean_text(type_text)
    mark = clean_text(specific_mark)
    if "专用" in text:
        return "专票"
    if "普通" in text:
        return "普票"
    if any(keyword in text for keyword in ("数电票", "电子发票", "全电发票", "电子票")):
        return "数电票"
    if mark == "01":
        return "专票"
    if mark == "02":
        return "普票"
    if text and ("发票" in text):
        return "其他"
    return "数电票" if not text else "其他"


def content_hash(*parts: Any) -> str:
    """把若干字段拼成稳定的短哈希（用于无号码时的去重键）。"""
    material = "|".join(clean_text(part) if not isinstance(part, float) else f"{part:.2f}" for part in parts)
    return hashlib.md5(material.encode("utf-8")).hexdigest()[:16]


def now_text() -> str:
    """当前时间字符串，供匹配确认等时间戳字段使用。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


__all__ = [
    "clean_text",
    "parse_number",
    "parse_date",
    "normalize_name",
    "name_similarity",
    "company_names",
    "detect_invoice_direction",
    "detect_invoice_kind",
    "content_hash",
    "now_text",
]
