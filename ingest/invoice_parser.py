"""发票解析：数电票 XML、OFD（zip 内 XML）与 PDF（pypdf 文本 + 正则）解析入库。

解析原则
--------
* **容错优先**：命名空间、中文标签、字段缺失、金额千分位、日期多格式都不崩溃；
  无法识别的字段留空/0，并在 ``errors`` 里给出可操作的中文提示。
* **统一口径**：所有格式最终都走 :func:`normalize_invoice`，输出与 ``invoices`` 表一致的字段。
* **方向判定**：以 ``config.get_company()`` 的企业名称/简称/统一社会信用代码比对销售方、购买方，
  销售方含本企业 → 销项，购买方含本企业 → 进项；都判断不出时用名称相似度择一。
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

from engine.money import round2
from ledger import models
from ledger.store import table

from ingest._common import (
    clean_text,
    content_hash,
    detect_invoice_direction,
    detect_invoice_kind,
    parse_date,
    parse_number,
)

TABLE_NAME = "invoices"
TABLE_LABEL = "发票池"

#: XML / PDF 文本中的字段别名（按优先级排列）
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "invoice_no": ("InvoiceNumber", "InvoiceNo", "发票号码", "发票号", "票据号码"),
    "invoice_code": ("InvoiceCode", "发票代码", "票据代码"),
    "invoice_date": ("IssuTime", "IssueDate", "IssueTime", "开票日期", "日期"),
    "invoice_type": ("InvoiceType", "发票类型", "票据类型"),
    "specific_mark": ("SpecificInvoiceMark", "特殊发票标记"),
    "seller": ("SellerName", "销售方名称", "销售方", "销方名称"),
    "seller_id": ("SellerId", "销售方识别号", "销方识别号"),
    "buyer": ("BuyerName", "购买方名称", "购买方", "购方名称"),
    "buyer_id": ("BuyerId", "购买方识别号", "购方识别号"),
    "amount_exclusive": (
        "TotalAmWithoutTax",
        "AmountWithoutTax",
        "合计金额",
        "不含税金额",
        "金额合计",
    ),
    "tax_amount": ("TotalTaxAm", "TaxAmount", "合计税额", "税额合计", "税额"),
    "total_inclusive": (
        "TotalTax-includedAmount",
        "TotalTaxIncludedAmount",
        "价税合计",
        "含税金额",
        "价税合计（小写）",
    ),
    "item_name": ("ItemName", "项目名称", "货物或应税劳务名称"),
}

_SUPPORTED_FORMATS = ("XML", "OFD", "PDF")


# =============================================================================
# 通用工具
# =============================================================================
def _local_name(tag: str) -> str:
    """去除 XML 命名空间前缀，只保留本地标签名。"""
    return str(tag).rsplit("}", 1)[-1]


def _read_source(source: Any, filename: str | None) -> tuple[bytes, str]:
    """把路径 / bytes / 文件对象统一读成 ``(bytes, filename)``。"""
    if isinstance(source, (bytes, bytearray)):
        if not filename:
            raise ValueError("传入 bytes 时必须提供 filename 以判断文件格式")
        return bytes(source), str(filename)
    if isinstance(source, (str, Path)) or hasattr(source, "__fspath__"):
        path = Path(source)
        name = str(filename or path.name)
        try:
            return path.read_bytes(), name
        except OSError as exc:
            raise ValueError(f"读取文件失败：{path}（{exc}）") from exc
    if hasattr(source, "getvalue"):
        data = source.getvalue()
        name = str(filename or getattr(source, "name", "") or "上传文件")
        if isinstance(data, str):
            data = data.encode("utf-8")
        return bytes(data), name
    if hasattr(source, "read"):
        data = source.read()
        name = str(filename or getattr(source, "name", "") or "上传文件")
        if isinstance(data, str):
            data = data.encode("utf-8")
        return bytes(data), name
    raise ValueError("不支持的文件来源：请传入文件路径、bytes 或文件对象")


def _sniff_envelope(data: bytes) -> str:
    """在无法从扩展名判断时嗅探文件真实格式。"""
    head = data[:512].lstrip()
    if head.startswith(b"%PDF"):
        return "PDF"
    if head.startswith(b"PK\x03\x04"):
        return "OFD"
    if head.startswith(b"<?xml") or head.startswith(b"<"):
        return "XML"
    lowered = head.lower()
    if b"<einvoice" in lowered or b"<invoice" in lowered or "发票".encode("utf-8") in head:
        return "XML"
    return "UNKNOWN"


def _flatten_xml(data: bytes | str) -> tuple[dict[str, str], str]:
    """把 XML 扁平化为 ``{本地标签名: 文本}``，并返回拼接文本；解析失败抛 ValueError。"""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"XML 解析失败：{exc}") from exc
    flat: dict[str, str] = {}
    texts: list[str] = []
    for element in root.iter():
        text = (element.text or "").strip()
        if not text:
            continue
        texts.append(text)
        tag = _local_name(element.tag)
        flat.setdefault(tag, text)
    joined = " ".join(texts)
    if not flat:
        raise ValueError("XML 内容为空，未识别到任何字段")
    return flat, joined


def _first(flat: dict[str, str], aliases: tuple[str, ...]) -> str:
    """按别名顺序取第一个非空值。"""
    for alias in aliases:
        value = clean_text(flat.get(alias))
        if value:
            return value
    return ""


def _invoice_from_flat(flat: dict[str, str], text: str, *, source_format: str) -> dict[str, Any]:
    """把扁平化后的 XML/文本字段整理为 raw 字典并交给 normalize_invoice。"""
    raw = {
        "invoice_no": _first(flat, FIELD_ALIASES["invoice_no"]),
        "invoice_code": _first(flat, FIELD_ALIASES["invoice_code"]),
        "invoice_date": _first(flat, FIELD_ALIASES["invoice_date"]),
        "invoice_type": _first(flat, FIELD_ALIASES["invoice_type"]),
        "specific_mark": _first(flat, FIELD_ALIASES["specific_mark"]),
        "seller": _first(flat, FIELD_ALIASES["seller"]),
        "seller_id": _first(flat, FIELD_ALIASES["seller_id"]),
        "buyer": _first(flat, FIELD_ALIASES["buyer"]),
        "buyer_id": _first(flat, FIELD_ALIASES["buyer_id"]),
        "amount_exclusive": _first(flat, FIELD_ALIASES["amount_exclusive"]),
        "tax_amount": _first(flat, FIELD_ALIASES["tax_amount"]),
        "total_inclusive": _first(flat, FIELD_ALIASES["total_inclusive"]),
        "item_name": _first(flat, FIELD_ALIASES["item_name"]),
        "full_text": text,
        "raw_json": dict(flat),
    }
    return normalize_invoice(raw, source_format=source_format)


def _field_errors(invoice: dict[str, Any]) -> list[str]:
    """根据规范化后的发票记录给出缺失字段提示（缺失不阻塞入库）。"""
    errors: list[str] = []
    if not invoice.get("invoice_no"):
        errors.append("未识别到发票号码，请核对或手动录入。")
    if not invoice.get("invoice_date"):
        errors.append("未识别到开票日期，请核对或手动录入。")
    if not invoice.get("seller") and not invoice.get("buyer"):
        errors.append("未识别到销售方/购买方名称，请核对。")
    if round2(invoice.get("total_inclusive") or 0) == 0:
        errors.append("未识别到价税合计金额，请核对或手动录入。")
    return errors


# =============================================================================
# 发票记录规范化
# =============================================================================
def normalize_invoice(raw: dict[str, Any], *, source_format: str = "XML") -> dict[str, Any]:
    """把任意来源的发票原始字段统一为 ``invoices`` 表记录。

    统一字段：invoice_no/invoice_code/invoice_date/invoice_type/direction/seller/buyer/
    amount_exclusive/tax_amount/total_inclusive/status="待匹配"/source_format/raw_json。

    金额勾稽：三项金额允许 1 分钱误差，差额回填使 ``不含税 + 税额 = 价税合计``；
    缺失字段留空/0，不抛异常（由解析层汇总提示）。
    """
    if not isinstance(raw, dict):
        raise ValueError("发票原始数据必须是 dict")

    invoice_no = clean_text(raw.get("invoice_no"))
    invoice_code = clean_text(raw.get("invoice_code"))
    invoice_date = parse_date(raw.get("invoice_date"))
    seller = clean_text(raw.get("seller"))
    buyer = clean_text(raw.get("buyer"))
    seller_id = clean_text(raw.get("seller_id"))
    buyer_id = clean_text(raw.get("buyer_id"))
    amount_exclusive = parse_number(raw.get("amount_exclusive")) or 0.0
    tax_amount = parse_number(raw.get("tax_amount")) or 0.0
    total_inclusive = parse_number(raw.get("total_inclusive")) or 0.0

    note = clean_text(raw.get("note"))
    if total_inclusive == 0 and (amount_exclusive or tax_amount):
        total_inclusive = round2(amount_exclusive + tax_amount)
    elif amount_exclusive == 0 and total_inclusive and tax_amount:
        amount_exclusive = round2(total_inclusive - tax_amount)
    elif tax_amount == 0 and total_inclusive and amount_exclusive:
        tax_amount = round2(total_inclusive - amount_exclusive)
    elif total_inclusive and amount_exclusive and tax_amount:
        diff = round2(total_inclusive - (amount_exclusive + tax_amount))
        if abs(diff) <= 0.01:
            total_inclusive = round2(amount_exclusive + tax_amount)
        else:
            note = (
                f"{note}；金额勾稽不一致：不含税 {amount_exclusive:.2f} + 税额 {tax_amount:.2f} "
                f"≠ 价税合计 {total_inclusive:.2f}（差额 {diff:.2f} 元），请核对。"
            ).strip("；")

    direction = clean_text(raw.get("direction"))
    if direction not in models.INVOICE_DIRECTIONS:
        direction, direction_note = detect_invoice_direction(
            seller, buyer, seller_id=seller_id, buyer_id=buyer_id
        )
        if direction_note:
            note = f"{note}；{direction_note}".strip("；") if note else direction_note

    type_source = clean_text(raw.get("invoice_type")) or clean_text(raw.get("full_text"))
    kind = detect_invoice_kind(type_source, specific_mark=clean_text(raw.get("specific_mark")))
    if kind not in models.INVOICE_KINDS:
        kind = "其他"

    raw_json = raw.get("raw_json")
    if not isinstance(raw_json, dict):
        raw_json = {}

    return {
        "invoice_no": invoice_no,
        "invoice_code": invoice_code,
        "invoice_date": invoice_date,
        "invoice_type": kind,
        "direction": direction,
        "seller": seller,
        "buyer": buyer,
        "amount_exclusive": round2(amount_exclusive),
        "tax_amount": round2(tax_amount),
        "total_inclusive": round2(total_inclusive),
        "status": "待匹配",
        "source_format": str(source_format or "XML"),
        "raw_json": raw_json,
        "note": note,
    }


def _invoice_dedup_key(record: dict[str, Any]) -> str:
    """发票去重键：号码 + 开票日期；无号码时用销售方+金额+日期哈希。"""
    invoice_no = clean_text(record.get("invoice_no"))
    invoice_date = clean_text(record.get("invoice_date"))
    if invoice_no:
        return f"NO|{invoice_no}|{invoice_date}"
    return "HASH|" + content_hash(
        record.get("seller"),
        record.get("total_inclusive"),
        invoice_date,
        record.get("buyer"),
    )


def save_invoices(invoices: list[dict[str, Any]], source_file: str = "") -> dict[str, Any]:
    """发票去重入库，返回 ``{"saved": [...], "skipped": [{"invoice","reason"}]}``。"""
    if not isinstance(invoices, list):
        raise ValueError("invoices 必须是 list")
    invoice_table = table(TABLE_NAME)
    existing_keys = {_invoice_dedup_key(row) for row in invoice_table.read()}
    saved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for invoice in invoices:
        if not isinstance(invoice, dict):
            skipped.append({"invoice": invoice, "reason": "格式错误：不是 dict"})
            continue
        payload = dict(invoice)
        payload.setdefault("status", "待匹配")
        payload.setdefault("source_format", "XML")
        payload.setdefault("raw_json", {})
        if source_file and not clean_text(payload.get("source_file")):
            payload["source_file"] = source_file
        if payload.get("status") not in models.INGEST_STATUSES:
            payload["status"] = "待匹配"
        key = _invoice_dedup_key(payload)
        if key in existing_keys:
            skipped.append({"invoice": payload, "reason": "重复"})
            continue
        existing_keys.add(key)
        saved.append(invoice_table.append(payload))
    return {"saved": saved, "skipped": skipped}


# =============================================================================
# XML / OFD / PDF 解析
# =============================================================================
def parse_xml_invoice(data: bytes | str) -> dict[str, Any]:
    """解析单张数电票 XML，返回 ``{"ok","invoice","text","errors"}``。"""
    try:
        flat, text = _flatten_xml(data)
    except ValueError as exc:
        return {"ok": False, "invoice": None, "text": "", "errors": [str(exc)]}
    has_invoice_field = any(_first(flat, aliases) for aliases in FIELD_ALIASES.values())
    if not has_invoice_field:
        return {
            "ok": False,
            "invoice": None,
            "text": text[:4000],
            "errors": ["XML 中未识别到发票字段（需要如 InvoiceNumber/发票号码、价税合计等标签）。"],
        }
    invoice = _invoice_from_flat(flat, text, source_format="XML")
    return {
        "ok": True,
        "invoice": invoice,
        "text": text[:4000],
        "errors": _field_errors(invoice),
    }


def parse_ofd_invoice(data: bytes) -> dict[str, Any]:
    """解析 OFD 发票（zip 容器内查找含发票字段的 XML，优先 OriginalInvoice.xml）。"""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return {"ok": False, "invoice": None, "text": "", "errors": ["OFD 文件不是有效的 ZIP 容器，无法解析。"]}
    with archive:
        xml_names = [name for name in archive.namelist() if name.lower().endswith(".xml")]
        if not xml_names:
            return {"ok": False, "invoice": None, "text": "", "errors": ["OFD 内未找到 XML 文件。"]}
        # 优先 OriginalInvoice.xml / 含 invoice 的条目，其次按名称排序
        xml_names.sort(key=lambda name: (0 if "originalinvoice" in name.lower() else 1 if "invoice" in name.lower() else 2, name))
        texts: list[str] = []
        last_error = ""
        for name in xml_names:
            try:
                raw = archive.read(name)
            except (KeyError, OSError) as exc:
                last_error = f"读取 {name} 失败：{exc}"
                continue
            try:
                flat, text = _flatten_xml(raw)
            except ValueError as exc:
                last_error = f"{name}：{exc}"
                continue
            texts.append(text)
            if not (_first(flat, FIELD_ALIASES["invoice_no"]) or _first(flat, FIELD_ALIASES["total_inclusive"])):
                continue
            invoice = _invoice_from_flat(flat, text, source_format="OFD")
            return {
                "ok": True,
                "invoice": invoice,
                "text": "\n".join(texts)[:4000],
                "errors": _field_errors(invoice),
            }
    return {
        "ok": False,
        "invoice": None,
        "text": "\n".join(texts)[:4000],
        "errors": [last_error or "OFD 内未找到含发票号码/价税合计的 XML。"],
    }


_PDF_PATTERNS: dict[str, tuple[str, ...]] = {
    "invoice_no": (
        r"发票号码[:：]?\s*([0-9]{8,20})",
        r"Invoice\s*No[.:：]?\s*([0-9]{8,20})",
    ),
    "invoice_code": (r"发票代码[:：]?\s*([0-9]{8,20})",),
    "invoice_date": (
        r"(?:开票日期|开具日期)[:：]?\s*(\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)",
        r"(\d{4}年\d{1,2}月\d{1,2}日)",
    ),
    "seller": (
        r"销售方(?:名称)?[:：]?\s*([^\n\r]{2,60})",
        r"销方名称[:：]?\s*([^\n\r]{2,60})",
    ),
    "buyer": (
        r"购买方(?:名称)?[:：]?\s*([^\n\r]{2,60})",
        r"购方名称[:：]?\s*([^\n\r]{2,60})",
    ),
    "total_inclusive": (
        r"价税合计[^\d¥￥]{0,20}[¥￥]?\s*([\d,]+\.\d{2})",
        r"（小写）[¥￥]?\s*([\d,]+\.\d{2})",
        r"小写[^\d]{0,6}[¥￥]?\s*([\d,]+\.\d{2})",
    ),
    "amount_exclusive": (
        r"(?:合计金额|金额合计|不含税金额)[^\d¥￥]{0,12}[¥￥]?\s*([\d,]+\.\d{2})",
    ),
    "tax_amount": (
        r"(?:合计税额|税额合计|税额)[^\d¥￥]{0,12}[¥￥]?\s*([\d,]+\.\d{2})",
    ),
}


def _extract_pdf_fields(text: str) -> dict[str, str]:
    """用中文标签正则从 PDF 文本中提取发票字段（取第一个匹配）。"""
    fields: dict[str, str] = {}
    for key, patterns in _PDF_PATTERNS.items():
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                fields[key] = match.group(1).strip()
                break
    return fields


def parse_pdf_invoice(data: bytes) -> dict[str, Any]:
    """解析 PDF 发票：pypdf 提取文本 + 正则；不可用时友好降级。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        return {
            "ok": False,
            "invoice": None,
            "text": "",
            "errors": ["未安装 pypdf，无法解析 PDF；请使用 XML/OFD 文件或手动录入发票。"],
        }
    try:
        reader = PdfReader(io.BytesIO(data))
        pages_text = []
        for page in reader.pages:
            try:
                pages_text.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - 单页解析失败不影响其他页
                pages_text.append("")
        text = "\n".join(pages_text)
    except Exception as exc:  # noqa: BLE001 - pypdf 对损坏文件会抛多种异常
        return {
            "ok": False,
            "invoice": None,
            "text": "",
            "errors": [f"PDF 解析失败（{exc}）；请使用 XML/OFD 或手动录入。"],
        }
    if not text.strip():
        return {
            "ok": False,
            "invoice": None,
            "text": "",
            "errors": ["PDF 为图片扫描件，未提取到文字；请使用 XML/OFD 或手动录入。"],
        }
    fields = _extract_pdf_fields(text)
    if not fields.get("invoice_no") and not fields.get("total_inclusive"):
        return {
            "ok": False,
            "invoice": None,
            "text": text[:4000],
            "errors": ["PDF 文本中未识别到发票号码/价税合计，请使用 XML/OFD 或手动录入。"],
        }
    raw = {
        **fields,
        "invoice_type": "专用发票" if "专用发票" in text else ("普通发票" if "普通发票" in text else "电子发票"),
        "full_text": text,
        "raw_json": {"pdf_text": text[:4000]},
    }
    invoice = normalize_invoice(raw, source_format="PDF")
    return {
        "ok": True,
        "invoice": invoice,
        "text": text[:4000],
        "errors": _field_errors(invoice),
    }


# =============================================================================
# 对外入口
# =============================================================================
def _wrap_single(result: dict[str, Any], fmt: str) -> dict[str, Any]:
    invoice = result.get("invoice")
    return {
        "ok": bool(result.get("ok")),
        "format": fmt,
        "invoices": [invoice] if (result.get("ok") and invoice) else [],
        "text": result.get("text", ""),
        "errors": list(result.get("errors") or []),
    }


def parse_invoice_bytes(data: bytes, filename: str) -> dict[str, Any]:
    """按文件名扩展名解析发票 bytes（XML / OFD / PDF，无法判断时嗅探）。"""
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("parse_invoice_bytes 的 data 必须是 bytes")
    data = bytes(data)
    suffix = Path(str(filename or "")).suffix.lower().lstrip(".")
    fmt = {"xml": "XML", "ofd": "OFD", "pdf": "PDF"}.get(suffix)
    if fmt is None:
        fmt = _sniff_envelope(data)
    if fmt == "XML":
        return _wrap_single(parse_xml_invoice(data), "XML")
    if fmt == "OFD":
        return _wrap_single(parse_ofd_invoice(data), "OFD")
    if fmt == "PDF":
        return _wrap_single(parse_pdf_invoice(data), "PDF")
    return {
        "ok": False,
        "format": "UNKNOWN",
        "invoices": [],
        "text": "",
        "errors": [f"不支持的文件类型：{filename}；仅支持 XML / OFD / PDF 发票文件。"],
    }


def parse_invoice_file(source: Any, filename: str | None = None) -> dict[str, Any]:
    """解析发票文件，返回 ``{"ok","format","invoices","text","errors"}``。

    参数
    ----
    source   : 文件路径（str/Path）、bytes 或文件对象
    filename : 传入 bytes 时必填，用于判断扩展名；传入路径时可省略
    """
    try:
        data, name = _read_source(source, filename)
    except ValueError as exc:
        return {"ok": False, "format": "UNKNOWN", "invoices": [], "text": "", "errors": [str(exc)]}
    result = parse_invoice_bytes(data, name)
    # 补齐来源文件信息，便于入库追溯
    for invoice in result["invoices"]:
        if not clean_text(invoice.get("source_file")):
            invoice["source_file"] = name
    return result


__all__ = [
    "FIELD_ALIASES",
    "parse_invoice_file",
    "parse_invoice_bytes",
    "parse_xml_invoice",
    "parse_ofd_invoice",
    "parse_pdf_invoice",
    "normalize_invoice",
    "save_invoices",
]
