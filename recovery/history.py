"""资料找回：历史申报补录 + 数据完整性检查（v1.38）。"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

import config
from ledger import store

TABLE = "recovery_filings"
TAX_TYPES = [
    "增值税及附加", "企业所得税季度预缴", "企业所得税年度汇算",
    "个人所得税扣缴", "财务报表", "印花税", "社保费",
    "工商年报", "残保金", "其他",
]
STATUSES = ["已申报", "已缴款", "零申报", "已更正", "待核对"]
TEMPLATE_HEADERS = ["所属期", "税种", "申报日期", "税额", "状态", "备注"]


def _normalize_period(value: str) -> str:
    text = str(value or "").strip().upper().replace("年", "-").replace("月", "")
    if "Q" in text:
        return text
    if len(text) == 7 and text[4] == "-":
        return text
    if len(text) == 6 and text.isdigit():
        return f"{text[:4]}-{text[4:]}"
    raise ValueError("所属期格式应为 YYYY-MM 或 YYYYQn，例如 2026-03 / 2026Q1")


def list_filings() -> list[dict[str, Any]]:
    rows = store.table(TABLE).read()
    return sorted(rows, key=lambda row: str(row.get("period") or ""), reverse=True)


def add_filing(
    period: str, tax_type: str, *, filed_date: str = "", tax_amount: float = 0,
    status: str = "已申报", note: str = "", source: str = "手工补录",
) -> dict[str, Any]:
    period_text = _normalize_period(period)
    if tax_type not in TAX_TYPES:
        raise ValueError(f"税种不合法，可选：{'、'.join(TAX_TYPES)}")
    if status not in STATUSES:
        raise ValueError(f"状态不合法，可选：{'、'.join(STATUSES)}")
    if any(str(row.get("period")) == period_text and str(row.get("tax_type")) == tax_type for row in list_filings()):
        raise ValueError(f"{period_text} 的「{tax_type}」已存在，请勿重复补录。")
    return store.table(TABLE).append({
        "period": period_text,
        "tax_type": tax_type,
        "filed_date": str(filed_date or "")[:10],
        "tax_amount": float(tax_amount or 0),
        "status": status,
        "note": str(note or "")[:500],
        "source": source,
    })


def delete_filing(record_id: str) -> None:
    if not store.table(TABLE).get(str(record_id)):
        raise KeyError(f"记录不存在：{record_id}")
    store.table(TABLE).delete(str(record_id))


def export_template() -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(TEMPLATE_HEADERS)
    writer.writerow(["2026-03", "增值税及附加", "2026-04-15", "0", "零申报", "示例行，可删除"])
    return buffer.getvalue().encode("utf-8-sig")


def import_csv(data: bytes) -> dict[str, Any]:
    text = data.decode("utf-8-sig", "replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV 没有表头")
    required = {"所属期", "税种"}
    if not required.issubset(set(reader.fieldnames)):
        raise ValueError("CSV 至少需要「所属期」「税种」两列")
    added, skipped = 0, []
    for index, row in enumerate(reader, start=2):
        try:
            add_filing(
                str(row.get("所属期") or ""),
                str(row.get("税种") or "").strip(),
                filed_date=str(row.get("申报日期") or ""),
                tax_amount=float(str(row.get("税额") or "0").replace(",", "") or 0),
                status=str(row.get("状态") or "已申报").strip() or "已申报",
                note=str(row.get("备注") or ""),
                source="CSV导入",
            )
            added += 1
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"第 {index} 行：{exc}")
    return {"added": added, "skipped": skipped}


def completeness() -> dict[str, Any]:
    """检查找回后账套是否具备继续申报的数据基础。"""
    from archiving.filing_archive import list_archives
    from ledger import employees, payroll

    items: list[dict[str, Any]] = []

    try:
        company = config.get_company()
        name = str(company.get("name") or "")
        credit = str(company.get("unified_social_credit_code") or "")
        items.append(_item("company", "公司基本信息", bool(name and credit), "企业名称/统一社会信用代码已填写" if name and credit else "缺少企业名称或统一社会信用代码", "settings"))
    except Exception:  # noqa: BLE001
        items.append(_item("company", "公司基本信息", False, "无法读取公司信息", "settings"))

    stats = {}
    try:
        stats = store.storage_stats()
    except Exception:  # noqa: BLE001
        pass
    income = int(stats.get("income") or 0)
    expense = int(stats.get("expense") or 0)
    items.append(_item("ledger", "收入/费用台账", income > 0 or expense > 0,
                       f"收入 {income} 笔、费用 {expense} 笔" if income or expense else "台账为空，历史收入/费用尚未补录",
                       "ledger"))

    try:
        active = employees.list_employees(status=None, include_left=False)
        months = payroll.payroll_periods(limit=6)
        ok = not active or bool(months)
        detail = f"在职 {len(active)} 人，工资表 {len(months)} 个月" if active else "无在职员工"
        items.append(_item("payroll", "员工与工资", ok, detail, "payroll"))
    except Exception:  # noqa: BLE001
        items.append(_item("payroll", "员工与工资", False, "员工/工资数据不可读", "payroll"))

    try:
        invoices = store.table("invoices").count()
        items.append(_item("invoices", "发票数据", invoices > 0, f"发票池 {invoices} 张" if invoices else "发票池为空，尚未导入历史发票", "collector"))
    except Exception:  # noqa: BLE001
        items.append(_item("invoices", "发票数据", False, "发票数据不可读", "collector"))

    try:
        filings = list_filings()
        items.append(_item("filings", "历史申报补录", bool(filings), f"已补录 {len(filings)} 条" if filings else "尚未补录历史申报", "recovery_history"))
    except Exception:  # noqa: BLE001
        items.append(_item("filings", "历史申报补录", False, "历史申报数据不可读", "recovery_history"))

    try:
        archives = list_archives()
        items.append(_item("archives", "申报归档", bool(archives), f"归档 {len(archives)} 个期间" if archives else "暂无归档（不影响继续申报，但建议留档）", "filing"))
    except Exception:  # noqa: BLE001
        items.append(_item("archives", "申报归档", False, "归档数据不可读", "filing"))

    ok_count = sum(1 for item in items if item["status"] == "ok")
    score = round(ok_count / len(items) * 100) if items else 0
    return {"items": items, "score": score, "ok": ok_count, "total": len(items)}


def _item(key: str, label: str, ok: bool, detail: str, page: str) -> dict[str, Any]:
    return {"key": key, "label": label, "status": "ok" if ok else "warn", "detail": detail, "page": page}


__all__ = ["TAX_TYPES", "STATUSES", "TEMPLATE_HEADERS", "list_filings", "add_filing", "delete_filing", "export_template", "import_csv", "completeness"]
