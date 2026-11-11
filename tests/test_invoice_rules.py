"""发票合规判断规则测试（v1.19）。"""

from __future__ import annotations

from engine.invoice_rules import assess_invoice


def test_normal_special_invoice_small_scale_cannot_deduct_vat():
    result = assess_invoice("增值税专用发票")
    assert result["bookable"] is True
    assert result["vat_deductible"] is False
    assert result["cit_deductible"] is True


def test_personal_expense_not_cit_deductible():
    result = assess_invoice("增值税普通发票", purpose="用于集体福利/个人消费")
    assert result["bookable"] is True
    assert result["cit_deductible"] is False
    assert result["risk"] == "medium"


def test_wrong_buyer_high_risk():
    result = assess_invoice("增值税专用发票", buyer_is_company=False)
    assert result["bookable"] is False
    assert result["cit_deductible"] is False
    assert result["risk"] == "high"


def test_white_slip_not_deductible():
    result = assess_invoice("白条/无票支出")
    assert result["bookable"] is True
    assert result["cit_deductible"] is False


def test_cross_year_risk():
    result = assess_invoice("增值税普通发票", cross_year=True)
    assert result["risk"] in ("medium", "high")
    assert any("跨年度" in note for note in result["notes"])
