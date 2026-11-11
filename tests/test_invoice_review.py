"""发票查验辅助/复核状态测试（v1.27）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ingest import invoice_review
from ingest.importer import import_invoice_files

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ingest" / "invoice_purchase.xml"


def _qr_png(payload: str) -> bytes:
    import cv2

    encoder = cv2.QRCodeEncoder.create()
    qr = encoder.encode(payload)
    image = cv2.resize(qr, (300, 300), interpolation=cv2.INTER_NEAREST)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def test_decode_official_qr():
    data = _qr_png("https://inv-veri.chinatax.gov.cn")
    result = invoice_review.decode_qr_image(data)
    assert result["ok"] is True
    info = invoice_review.inspect_payload(result["payload"])
    assert info["is_official"] is True


def test_non_official_qr_warns():
    data = _qr_png("https://example.com/verify")
    result = invoice_review.decode_qr_image(data)
    info = invoice_review.inspect_payload(result["payload"])
    assert info["is_official"] is False
    assert "非官方" in info["warning"]


def test_review_status_update():
    import_invoice_files([FIXTURE], auto_match=False)
    rows = invoice_review.list_invoices(10)
    assert rows
    invoice_id = str(rows[0]["id"])
    record = invoice_review.update_review(
        invoice_id, review_status="正常", verify_status="已查验-正常", review_note="已核对"
    )
    assert record["review_status"] == "正常"
    assert record["verify_status"] == "已查验-正常"
    assert record["verified_at"]
    summary = invoice_review.summary()
    assert summary["total"] >= 1


def test_invalid_status():
    with pytest.raises(ValueError):
        invoice_review.update_review("x", review_status="乱写")
