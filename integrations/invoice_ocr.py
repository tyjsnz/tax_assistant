"""发票 OCR 识别（v1.30）：调用 DeepSeek OCR，把图片/扫描 PDF 转为可预览的发票字段。

注意：
* 只生成预览，不直接入账；用户确认后才写入发票池；
* 字段来自 OCR 文本正则，金额/税号必须人工核对；
* OCR 不能替代发票真伪查验。
"""

from __future__ import annotations

from typing import Any

from ingest.invoice_parser import _extract_pdf_fields, normalize_invoice

from integrations import deepseek_tools


def recognize_invoice(data: bytes, filename: str) -> dict[str, Any]:
    """OCR 识别一张发票，返回 ``{"ok","text","invoice","warnings"}``。"""
    result = deepseek_tools.ocr_file(data, filename)
    text = str(result.get("text") or "")
    if not text.strip():
        return {"ok": False, "text": "", "invoice": None, "errors": ["OCR 未识别到文字。"]}
    fields = _extract_pdf_fields(text)
    if not fields.get("invoice_no") and not fields.get("total_inclusive"):
        return {
            "ok": False,
            "text": text[:8000],
            "invoice": None,
            "errors": ["OCR 文本中未识别到发票号码/价税合计，请手动录入。"],
        }
    invoice_type = "专用发票" if "专用发票" in text else ("普通发票" if "普通发票" in text else "电子发票")
    raw = {
        **fields,
        "invoice_type": invoice_type,
        "full_text": text,
        "raw_json": {"ocr_text": text[:6000]},
    }
    invoice = normalize_invoice(raw, source_format="OCR")
    return {
        "ok": True,
        "text": text[:8000],
        "invoice": invoice,
        "pages": result.get("page_count") or 0,
        "warnings": ["OCR 识别结果仅供参考，请核对发票号码、日期、金额后再入账。"],
    }


def save_invoice(invoice: dict[str, Any], filename: str) -> dict[str, Any]:
    """把确认后的 OCR 发票写入发票池（重复会跳过）。"""
    from ingest.invoice_parser import save_invoices

    saved = save_invoices([invoice], source_file=f"OCR:{filename}")
    if not saved.get("saved"):
        raise ValueError("该发票已存在（发票号码+日期重复），未重复入账。")
    return saved["saved"][0]


__all__ = ["recognize_invoice", "save_invoice"]
