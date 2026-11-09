"""申报表注册表：统一管理报表元数据、构造器与批量生成入口。

``FORMS`` 中每项包含：
``form_id`` / ``form_name`` / ``category`` / ``frequency`` / ``period_kind`` /
``description`` / ``builder``。构造器均返回统一的「申报表 bundle」结构
（见 ``docs/CONTRACTS.md`` 第 3.8 节）。
"""

from __future__ import annotations

from typing import Any, Callable

from ledger.reports import normalize_quarter
from filing import cit_prepay, fin_report, stamp_return, vat_return

FORMS: list[dict[str, Any]] = [
    {
        "form_id": vat_return.FORM_ID,
        "form_name": vat_return.FORM_NAME,
        "category": vat_return.CATEGORY,
        "frequency": vat_return.FREQUENCY,
        "period_kind": vat_return.PERIOD_KIND,
        "description": "小规模纳税人按季申报增值税及附加税费，享受季度 30 万元免税与 3% 减按 1% 征收。",
        "builder": vat_return.build_vat_return,
    },
    {
        "form_id": cit_prepay.FORM_ID,
        "form_name": cit_prepay.FORM_NAME,
        "category": cit_prepay.CATEGORY,
        "frequency": cit_prepay.FREQUENCY,
        "period_kind": cit_prepay.PERIOD_KIND,
        "description": "按本年累计实际利润额预缴企业所得税，享受小型微利企业 5% 实际税负与研发费用加计扣除。",
        "builder": cit_prepay.build_cit_prepay,
    },
    {
        "form_id": fin_report.FORM_ID,
        "form_name": fin_report.FORM_NAME,
        "category": fin_report.CATEGORY,
        "frequency": fin_report.FREQUENCY,
        "period_kind": fin_report.PERIOD_KIND,
        "description": "按小企业会计准则简化口径，以台账推算资产负债表与利润表（需与账面核对）。",
        "builder": fin_report.build_fin_report,
    },
    {
        "form_id": stamp_return.FORM_ID,
        "form_name": stamp_return.FORM_NAME,
        "category": stamp_return.CATEGORY,
        "frequency": stamp_return.FREQUENCY,
        "period_kind": stamp_return.PERIOD_KIND,
        "description": "按合同台账计算印花税，小规模纳税人享受「六税两费」减半征收。",
        "builder": stamp_return.build_stamp_return,
    },
]

#: ``list_forms`` 对外暴露的元数据字段（不含可调用对象，便于直接展示/序列化）
_METADATA_KEYS = ("form_id", "form_name", "category", "frequency", "period_kind", "description")


def list_forms() -> list[dict[str, Any]]:
    """返回全部报表的元数据列表（不含构造器函数）。"""
    return [{key: form[key] for key in _METADATA_KEYS} for form in FORMS]


def get_form(form_id: str) -> dict[str, Any]:
    """按报表编码获取报表定义（含构造器），不存在时抛 ``KeyError``。"""
    key = str(form_id or "").strip()
    for form in FORMS:
        if form["form_id"] == key:
            return dict(form)
    raise KeyError(f"未找到报表：{form_id}")


def build_form(form_id: str, period: Any, **overrides: Any) -> dict[str, Any]:
    """生成指定申报表 bundle。

    参数
    ----
    form_id : 报表编码，如 ``VAT_QUARTERLY``
    period  : 所属期间，如 ``2026Q1``（月度期间对季度报表会自动换算）
    overrides : 传给构造器的可选参数（如增值税的 ``income_totals``）

    生成后会补齐报表元数据，并读取已有申报记录中的状态（已保存的草稿/已确认/已申报）。
    """
    form = get_form(form_id)
    builder: Callable[..., dict[str, Any]] = form["builder"]
    if overrides and form["period_kind"] == "quarter":
        normalize_quarter(period)
    bundle = builder(period, **overrides)
    bundle.setdefault("form_id", form["form_id"])
    bundle.setdefault("form_name", form["form_name"])
    bundle.setdefault("category", form["category"])
    bundle.setdefault("frequency", form["frequency"])
    bundle["form_id"] = form["form_id"]
    bundle["form_name"] = form["form_name"]
    bundle["category"] = form["category"]
    bundle["frequency"] = form["frequency"]
    bundle.setdefault("status", "草稿")
    # 已保存过申报记录时，沿用其状态，避免页面显示与实际进度不一致
    from filing.progress import list_filings

    for record in list_filings(bundle.get("period")):
        if str(record.get("form_id") or "") == form["form_id"]:
            bundle["status"] = str(record.get("status") or bundle["status"])
            break
    return bundle


def build_all(period: Any) -> list[dict[str, Any]]:
    """生成某期间的全部申报表（增值税、企业所得税预缴、财务报表、印花税）。"""
    return [build_form(form["form_id"], period) for form in FORMS]


__all__ = ["FORMS", "list_forms", "get_form", "build_form", "build_all"]
