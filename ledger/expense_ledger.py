"""费用台账 CRUD、汇总与 CSV 导入导出。

口径说明
--------
* 小规模纳税人进项税额不得抵扣，费用台账记录**含税金额**，即当期成本费用。
* ``is_rd_related=True`` 的费用会被 :mod:`ledger.rd_auxiliary` 自动归集进研发辅助账，
  按 :data:`engine.rd_deduction.RD_CATEGORY_MAP` 映射到研发费用六个归集科目。
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import config
from engine.money import round2
from engine.tax_calendar import period_range
from ledger import locks, models
from ledger.audit import log_user_action
from ledger.store import table

TABLE_NAME = "expense"
TABLE_LABEL = "费用台账"

_CATEGORY_ORDER = list(models.EXPENSE_CATEGORIES)

_IMPORT_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "日期", "发生日期", "时间"),
    "description": ("description", "费用描述", "摘要", "说明", "项目"),
    "category": ("category", "费用类别", "类别", "费用类型"),
    "amount": ("amount", "金额", "金额（含税）", "金额(含税)", "含税金额", "费用金额"),
    "is_rd_related": ("is_rd_related", "研发相关", "是否研发", "研发标记"),
    "vendor": ("vendor", "供应商", "对方单位", "收款方"),
    "receipt_no": ("receipt_no", "凭证号", "单据号", "发票号"),
    "note": ("note", "备注"),
}


# =============================================================================
# 内部工具
# =============================================================================
def _normalize_period_for_query(period: Any) -> str:
    """规范化并校验期间，返回 ``2026Q1`` / ``2026-03`` / ``2026``。"""
    text = models.normalize_period(period, default="")
    if not text:
        raise ValueError("请提供所属期间，如 2026Q1")
    try:
        period_range(text)
    except ValueError as exc:
        raise ValueError(f"期间格式不正确：{period}，请使用 2026Q1 / 2026-03 / 2026 形式") from exc
    return text


def _period_bounds(period: Any) -> tuple[str, str, str]:
    text = _normalize_period_for_query(period)
    start, end = period_range(text)
    return text, start.isoformat(), end.isoformat()


def _apply_filters(rows: list[dict[str, Any]], filters: dict[str, Any]) -> list[dict[str, Any]]:
    """对已按日期过滤的记录应用等值条件（布尔字段兼容 True/False/是/否）。"""
    if not filters:
        return rows
    valid_fields = {column["name"] for column in models.TABLE_DEFS[TABLE_NAME]["columns"]}
    result: list[dict[str, Any]] = []
    for row in rows:
        matched = True
        for key, value in filters.items():
            if value is None or value == "":
                continue
            if key in ("is_rd_related", "研发相关", "是否研发"):
                if models.parse_bool(row.get("is_rd_related")) != models.parse_bool(value):
                    matched = False
                    break
                continue
            if key not in valid_fields:
                matched = False
                break
            if str(row.get(key)) != str(value).strip():
                matched = False
                break
        if matched:
            result.append(row)
    return result


def _sort_by_date_asc(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: (str(row.get("date") or ""), str(row.get("id") or "")))


def _export_default_path(prefix: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(config.EXPORT_DIR) / f"{prefix}_{stamp}.csv"


def _extract_aliases(raw: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field_name, aliases in _IMPORT_ALIASES.items():
        for alias in aliases:
            if alias in raw and str(raw[alias]).strip() != "":
                payload[field_name] = raw[alias]
                break
    return payload


# =============================================================================
# CRUD
# =============================================================================
def add_expense(
    date: Any = None,
    description: str = "",
    amount: float = 0.0,
    *,
    category: str = "办公",
    is_rd_related: bool = False,
    vendor: str = "",
    receipt_no: str = "",
    note: str = "",
) -> dict[str, Any]:
    """新增一笔费用台账记录，返回写库后的完整记录（含自动编号与创建时间）。"""
    record = models.normalize_expense(
        {
            "date": date,
            "description": description,
            "category": category,
            "amount": amount,
            "is_rd_related": is_rd_related,
            "vendor": vendor,
            "receipt_no": receipt_no,
            "note": note,
        }
    )
    locks.ensure_open(record["date"], action="新增费用")
    added = table(TABLE_NAME).append(record)
    log_user_action(
        "新增费用",
        f"{added['date']} {added['category']} {added['description'] or '（未填摘要）'} "
        f"{added['amount']:.2f} 元，编号 {added['id']}",
    )
    return added


def list_expenses(start: Any = None, end: Any = None, **filters: Any) -> list[dict[str, Any]]:
    """按日期区间与等值条件查询费用台账（按日期升序）。

    ``filters`` 可传 ``category``、``is_rd_related``、``vendor`` 等字段。
    """
    rows = table(TABLE_NAME).filter(start=start, end=end)
    return _sort_by_date_asc(_apply_filters(rows, filters))


def get_expense(expense_id: str) -> dict[str, Any] | None:
    """按编号读取一条费用记录，不存在返回 ``None``。"""
    return table(TABLE_NAME).get(expense_id)


def update_expense(expense_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """按编号更新费用记录，返回更新后的记录；编号不存在时抛 ``KeyError``。"""
    if not isinstance(patch, dict):
        raise ValueError("更新内容必须是 dict")
    current = get_expense(expense_id)
    if current is None:
        raise KeyError(f"{TABLE_LABEL}中不存在编号 {expense_id}")
    locks.ensure_open(current["date"], action="修改费用")
    merged = dict(current)
    for key, value in patch.items():
        if key in ("id", "created_at"):
            continue
        merged[key] = value
    record = models.normalize_expense(merged)
    locks.ensure_open(record["date"], action="修改费用")
    updated = table(TABLE_NAME).update(expense_id, record)
    log_user_action(
        "修改费用",
        f"编号 {expense_id}：{updated['date']} {updated['category']} {updated['amount']:.2f} 元",
    )
    return updated


def delete_expense(expense_id: str) -> bool:
    """按编号删除费用记录，返回是否删除成功；已结账期间禁止删除。"""
    current = get_expense(expense_id)
    if current is None:
        return False
    locks.ensure_open(current["date"], action="删除费用")
    deleted = table(TABLE_NAME).delete(expense_id)
    if deleted:
        log_user_action("删除费用", f"删除编号 {expense_id}")
    return deleted


# =============================================================================
# CSV 导入 / 导出
# =============================================================================
def import_expense_csv(path: Any) -> int:
    """从 CSV 导入费用台账，返回成功导入条数；兼容中英文字段别名。"""
    csv_path = Path(path)
    if not csv_path.exists():
        raise ValueError(f"文件不存在：{csv_path}")
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            payloads = [_extract_aliases(dict(row)) for row in reader]
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        raise ValueError(f"读取 CSV 失败：{exc}") from exc
    records = []
    for payload in payloads:
        try:
            records.append(models.normalize_expense(payload))
        except ValueError:
            continue
    for record in records:
        locks.ensure_open(record["date"], action="导入费用")
    added = table(TABLE_NAME).append_many(records)
    log_user_action("导入费用CSV", f"文件 {csv_path.name}，成功导入 {len(added)} 条")
    return len(added)


def export_expense_csv(path: Any = None) -> Path:
    """导出费用台账为 CSV（中文表头，UTF-8 with BOM），返回文件路径。"""
    target = Path(path) if path else _export_default_path(TABLE_LABEL)
    target.parent.mkdir(parents=True, exist_ok=True)
    columns = models.TABLE_DEFS[TABLE_NAME]["columns"]
    rows = table(TABLE_NAME).sorted_by_date(descending=False)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([column["label"] for column in columns])
        for row in rows:
            values = []
            for column in columns:
                value = row.get(column["name"])
                if column["type"] == "money":
                    values.append(f"{round2(value):.2f}")
                elif column["type"] == "bool":
                    values.append(models.bool_text(value))
                else:
                    values.append(value if value is not None else "")
            writer.writerow(values)
    log_user_action("导出费用CSV", f"文件 {target.name}，共 {len(rows)} 条")
    return target


# =============================================================================
# 汇总与取数
# =============================================================================
def summarize_expenses(start: Any = None, end: Any = None) -> dict[str, Any]:
    """汇总指定日期区间内的费用台账（笔数、合计、研发相关金额与类别拆分）。"""
    rows = table(TABLE_NAME).filter(start=start, end=end)
    by_category: dict[str, dict[str, float | int]] = {}
    total = 0.0
    rd_total = 0.0
    for row in rows:
        amount = round2(row.get("amount"))
        total = round2(total + amount)
        if models.parse_bool(row.get("is_rd_related")):
            rd_total = round2(rd_total + amount)
        category = str(row.get("category") or "其他")
        bucket = by_category.setdefault(category, {"amount": 0.0, "count": 0})
        bucket["amount"] = round2(float(bucket["amount"]) + amount)
        bucket["count"] = int(bucket["count"]) + 1
    ordered = {category: by_category[category] for category in _CATEGORY_ORDER if category in by_category}
    for category, bucket in by_category.items():
        ordered.setdefault(category, bucket)
    return {
        "count": len(rows),
        "total": total,
        "rd_total": rd_total,
        "by_category": ordered,
    }


def period_totals(period: Any) -> dict[str, Any]:
    """按期间（季度/月/年）汇总费用台账，供利润表与研发归集取数。"""
    text, start, end = _period_bounds(period)
    summary = summarize_expenses(start=start, end=end)
    return {
        "period": text,
        "start": start,
        "end": end,
        "total": summary["total"],
        "rd_total": summary["rd_total"],
        "by_category": summary["by_category"],
        "count": summary["count"],
    }


__all__ = [
    "add_expense",
    "list_expenses",
    "get_expense",
    "update_expense",
    "delete_expense",
    "import_expense_csv",
    "export_expense_csv",
    "summarize_expenses",
    "period_totals",
]
