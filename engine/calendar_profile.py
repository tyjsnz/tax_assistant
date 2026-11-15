"""报税日历个性化范围（v1.33）。

按公司保存：
* 涉及税种（增值税/企业所得税/财务报表/个税/社保/印花税/工商年报/残保金）；
* 是否有员工、是否缴社保；
* 用于隐藏不相关的申报事项，避免日历/待办信息过载。

文件：当前公司 ``DATA_DIR/calendar_profile.json``，不进入全局数据。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import config

SCOPE_KEYS: dict[str, str] = {
    "vat": "增值税及附加",
    "cit": "企业所得税",
    "financial": "财务报表",
    "iit": "个人所得税（工资）",
    "social": "社会保险费",
    "stamp": "印花税",
    "annual_report": "工商年报",
    "disability": "残保金",
}

DEFAULT_PROFILE: dict[str, Any] = {
    "vat": True,
    "cit": True,
    "financial": True,
    "iit": True,
    "social": True,
    "stamp": True,
    "annual_report": True,
    "disability": False,
    "has_employees": True,
    "pay_social": True,
}


def _path() -> Path:
    return Path(config.DATA_DIR) / "calendar_profile.json"


def load_profile() -> dict[str, Any]:
    profile = dict(DEFAULT_PROFILE)
    loaded = False
    try:
        path = _path()
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                profile.update({key: raw[key] for key in DEFAULT_PROFILE if key in raw})
                loaded = True
    except Exception:  # noqa: BLE001
        pass
    # 仅首次使用且没有保存过范围时，根据员工情况推断；之后尊重用户选择
    if not loaded:
        try:
            from ledger import employees

            active = employees.list_employees(status=None, include_left=False)
            if active:
                profile["has_employees"] = True
        except Exception:  # noqa: BLE001
            pass
    return profile


def save_profile(profile: dict[str, Any]) -> dict[str, Any]:
    data = dict(DEFAULT_PROFILE)
    data.update({key: bool(profile[key]) for key in DEFAULT_PROFILE if key in profile})
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def scope_text(profile: dict[str, Any]) -> str:
    labels = [label for key, label in SCOPE_KEYS.items() if profile.get(key)]
    if profile.get("has_employees"):
        labels.append("有员工")
    if profile.get("pay_social"):
        labels.append("缴社保")
    return "、".join(labels) if labels else "未选择任何事项"


def filter_deadlines(items: list[dict[str, Any]], profile: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """按范围过滤申报事项。"""
    data = profile or load_profile()
    result = []
    for item in items:
        key = str(item.get("key") or "")
        if key == "vat_quarterly" and not data.get("vat"):
            continue
        if key in ("corp_tax_quarterly", "annual_settlement") and not data.get("cit"):
            continue
        if key == "financial_report_quarterly" and not data.get("financial"):
            continue
        if key == "personal_tax_monthly" and not (data.get("iit") and data.get("has_employees")):
            continue
        if key == "social_insurance_monthly" and not (data.get("social") and data.get("pay_social")):
            continue
        if key == "stamp_tax_quarterly" and not data.get("stamp"):
            continue
        if key == "annual_report" and not data.get("annual_report"):
            continue
        if key == "disability_fund_annual" and not data.get("disability"):
            continue
        result.append(item)
    return result


__all__ = ["SCOPE_KEYS", "DEFAULT_PROFILE", "load_profile", "save_profile", "scope_text", "filter_deadlines"]
