"""报税日历个性化范围测试（v1.33）。"""

from __future__ import annotations

from engine import calendar_profile


def _items():
    return [
        {"key": "vat_quarterly"},
        {"key": "personal_tax_monthly"},
        {"key": "social_insurance_monthly"},
        {"key": "annual_settlement"},
        {"key": "disability_fund_annual"},
    ]


def test_save_and_filter():
    calendar_profile.save_profile({
        "vat": True, "cit": False, "financial": False, "iit": False,
        "social": True, "stamp": False, "annual_report": False,
        "disability": False, "has_employees": False, "pay_social": True,
    })
    profile = calendar_profile.load_profile()
    keys = [item["key"] for item in calendar_profile.filter_deadlines(_items(), profile)]
    assert "vat_quarterly" in keys
    assert "social_insurance_monthly" in keys
    assert "personal_tax_monthly" not in keys
    assert "annual_settlement" not in keys
    assert "disability_fund_annual" not in keys


def test_no_employees_hides_iit():
    profile = {"iit": True, "has_employees": False, "social": False, "pay_social": False,
               "vat": False, "cit": False, "financial": False, "stamp": False,
               "annual_report": False, "disability": False}
    keys = [item["key"] for item in calendar_profile.filter_deadlines(_items(), profile)]
    assert keys == []


def test_scope_text():
    text = calendar_profile.scope_text({"vat": True, "has_employees": True, "pay_social": True})
    assert "增值税" in text and "有员工" in text
