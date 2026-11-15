"""发票 OCR 预览/入池测试（v1.30）。"""

from __future__ import annotations

from integrations import invoice_ocr


OCR_TEXT = """
电子发票（增值税普通发票）
发票号码：26530100000099999999
开票日期：2026-06-01
购买方名称：某科技有限公司
销售方名称：昆明某办公用品公司
合计金额：100.00
合计税额：13.00
价税合计（小写）¥113.00
"""


def test_recognize_invoice_from_ocr(monkeypatch):
    monkeypatch.setattr(invoice_ocr.deepseek_tools, "ocr_file", lambda data, filename: {"ok": True, "text": OCR_TEXT, "page_count": 1})
    result = invoice_ocr.recognize_invoice(b"img", "invoice.png")
    assert result["ok"] is True
    invoice = result["invoice"]
    assert invoice["invoice_no"] == "26530100000099999999"
    assert invoice["total_inclusive"] == 113.0
    assert invoice["direction"] in ("进项", "销项", "其他")


def test_recognize_without_fields(monkeypatch):
    monkeypatch.setattr(invoice_ocr.deepseek_tools, "ocr_file", lambda data, filename: {"ok": True, "text": "完全无关的文字", "page_count": 1})
    result = invoice_ocr.recognize_invoice(b"img", "x.png")
    assert result["ok"] is False


def test_save_ocr_invoice(monkeypatch):
    monkeypatch.setattr(invoice_ocr.deepseek_tools, "ocr_file", lambda data, filename: {"ok": True, "text": OCR_TEXT, "page_count": 1})
    result = invoice_ocr.recognize_invoice(b"img", "invoice.png")
    saved = invoice_ocr.save_invoice(result["invoice"], "invoice.png")
    assert saved["invoice_no"] == "26530100000099999999"
    assert saved.get("source_format") == "OCR"
