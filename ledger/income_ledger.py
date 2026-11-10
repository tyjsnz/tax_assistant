"""收入台账 CRUD、汇总与 CSV 导入导出。

金额口径
--------
* 台账同时保存含税金额、不含税金额与增值税额；小规模纳税人征收率 1%，
  ``不含税 = 含税 ÷ (1 + 1%)``（见 :func:`ledger.models.derive_exclusive`）。
* 未显式提供不含税金额时自动推导，保证 ``不含税 + 增值税 = 含税``。

期间口径
--------
* ``period_totals`` 支持 ``2026Q1``、``2026-03``、``2026`` 三种期间写法，
  按日期区间从台账取数，专票 / 普票 / 未开票拆分供申报表使用。
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

TABLE_NAME = "income"
TABLE_LABEL = "收入台账"

#: 期间内每个发票类型的默认零值结构（保证申报表取数时键一定存在）
_EMPTY_INVOICE_BUCKET = {"count": 0, "inclusive": 0.0, "exclusive": 0.0}

#: CSV 导入时兼容的中英文字段别名（键为内部字段名）
_IMPORT_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "日期", "收款日期", "发生日期", "时间"),
    "client": ("client", "客户", "客户名称", "对方单位", "购买方"),
    "description": ("description", "业务描述", "摘要", "说明", "项目"),
    "category": ("category", "业务类别", "类别", "收入类别"),
    "invoice_type": ("invoice_type", "发票类型", "票据类型", "类型"),
    "invoice_no": ("invoice_no", "发票号码", "发票号", "票据号"),
    "inclusive_amount": ("inclusive_amount", "含税金额", "金额", "价税合计", "含税收入"),
    "exclusive_amount": ("exclusive_amount", "不含税金额", "不含税收入", "销售额"),
    "vat_amount": ("vat_amount", "增值税额", "税额", "增值税"),
    "is_rd_related": ("is_rd_related", "研发相关", "是否研发", "研发标记"),
    "note": ("note", "备注", "说明备注"),
}

_CATEGORY_ORDER = list(models.INCOME_CATEGORIES)


# =============================================================================
# 内部工具
# =============================================================================
def _rows() -> list[dict[str, Any]]:
    return table(TABLE_NAME).read()


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
    """返回 ``(规范化期间, 起始日, 结束日)``。"""
    text = _normalize_period_for_query(period)
    start, end = period_range(text)
    return text, start.isoformat(), end.isoformat()


def _client_text(client: Any) -> str:
    """按隐私模式决定是否对客户名称脱敏。"""
    text = str(client or "").strip()
    if text and config.is_privacy_mode():
        return models.mask_client(text)
    return text


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


def _invoice_bucket_empty() -> dict[str, dict[str, float | int]]:
    return {invoice_type: dict(_EMPTY_INVOICE_BUCKET) for invoice_type in models.INVOICE_TYPES}


def _export_default_path(prefix: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(config.EXPORT_DIR) / f"{prefix}_{stamp}.csv"


# =============================================================================
# CRUD
# =============================================================================
def add_income(
    date: Any = None,
    client: str = "",
    inclusive_amount: float = 0.0,
    *,
    invoice_type: str = "普票",
    category: str = "技术服务",
    description: str = "",
    invoice_no: str = "",
    is_rd_related: bool = False,
    note: str = "",
    exclusive_amount: float | None = None,
    vat_amount: float | None = None,
) -> dict[str, Any]:
    """新增一笔收入台账记录，返回写库后的完整记录（含自动编号与创建时间）。

    参数
    ----
    date : 收款日期，支持 ``2026-01-05``、``2026/1/5`` 等写法，缺省为今天
    inclusive_amount : 含税金额（元），不能为负数
    invoice_type : 发票类型，取 ``普票`` / ``专票`` / ``未开票``
    exclusive_amount / vat_amount : 可选，不填时按 1% 征收率自动推导
    """
    record = models.normalize_income(
        {
            "date": date,
            "client": _client_text(client),
            "description": description,
            "category": category,
            "invoice_type": invoice_type,
            "invoice_no": invoice_no,
            "inclusive_amount": inclusive_amount,
            "exclusive_amount": exclusive_amount,
            "vat_amount": vat_amount,
            "is_rd_related": is_rd_related,
            "note": note,
        }
    )
    locks.ensure_open(record["date"], action="新增收入")
    added = table(TABLE_NAME).append(record)
    log_user_action(
        "新增收入",
        f"{added['date']} {added['client'] or '（未填客户）'} "
        f"{added['invoice_type']} 含税 {added['inclusive_amount']:.2f} 元，编号 {added['id']}",
    )
    return added


def list_income(start: Any = None, end: Any = None, **filters: Any) -> list[dict[str, Any]]:
    """按日期区间与等值条件查询收入台账（按日期升序）。

    ``filters`` 可传 ``invoice_type``、``category``、``client``、``is_rd_related`` 等字段。
    """
    rows = table(TABLE_NAME).filter(start=start, end=end)
    return _sort_by_date_asc(_apply_filters(rows, filters))


def get_income(income_id: str) -> dict[str, Any] | None:
    """按编号读取一条收入记录，不存在返回 ``None``。"""
    return table(TABLE_NAME).get(income_id)


def update_income(income_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """按编号更新收入记录，返回更新后的记录；编号不存在时抛 ``KeyError``。

    若修改了含税金额而未同时给出不含税金额/增值税额，会自动重新推导，保证金额勾稽。
    """
    if not isinstance(patch, dict):
        raise ValueError("更新内容必须是 dict")
    current = get_income(income_id)
    if current is None:
        raise KeyError(f"{TABLE_LABEL}中不存在编号 {income_id}")
    locks.ensure_open(current["date"], action="修改收入")
    merged = dict(current)
    for key, value in patch.items():
        if key in ("id", "created_at"):
            continue
        merged[key] = value
    if "inclusive_amount" in patch and "exclusive_amount" not in patch and "vat_amount" not in patch:
        merged.pop("exclusive_amount", None)
        merged.pop("vat_amount", None)
    if "client" in patch:
        merged["client"] = _client_text(merged.get("client"))
    record = models.normalize_income(merged)
    locks.ensure_open(record["date"], action="修改收入")
    updated = table(TABLE_NAME).update(income_id, record)
    log_user_action(
        "修改收入",
        f"编号 {income_id}：{updated['date']} {updated['client'] or '（未填客户）'} "
        f"含税 {updated['inclusive_amount']:.2f} 元",
    )
    return updated


def delete_income(income_id: str) -> bool:
    """按编号删除收入记录，返回是否删除成功；已结账期间禁止删除。"""
    current = get_income(income_id)
    if current is None:
        return False
    locks.ensure_open(current["date"], action="删除收入")
    deleted = table(TABLE_NAME).delete(income_id)
    if deleted:
        log_user_action("删除收入", f"删除编号 {income_id}")
    return deleted


# =============================================================================
# CSV 导入 / 导出
# =============================================================================
def _extract_aliases(raw: dict[str, Any]) -> dict[str, Any]:
    """把一行 CSV（中/英文表头）映射为内部字段名。"""
    payload: dict[str, Any] = {}
    for field_name, aliases in _IMPORT_ALIASES.items():
        for alias in aliases:
            if alias in raw and str(raw[alias]).strip() != "":
                payload[field_name] = raw[alias]
                break
    return payload


def import_income_csv(path: Any) -> int:
    """从 CSV 导入收入台账，返回成功导入条数；兼容中英文字段别名。"""
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
            records.append(models.normalize_income(payload))
        except ValueError:
            continue
    for record in records:
        locks.ensure_open(record["date"], action="导入收入")
    added = table(TABLE_NAME).append_many(records)
    log_user_action("导入收入CSV", f"文件 {csv_path.name}，成功导入 {len(added)} 条")
    return len(added)


def export_income_csv(path: Any = None) -> Path:
    """导出收入台账为 CSV（中文表头，UTF-8 with BOM，Excel 可直接打开），返回文件路径。"""
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
    log_user_action("导出收入CSV", f"文件 {target.name}，共 {len(rows)} 条")
    return target


# =============================================================================
# 汇总与取数
# =============================================================================
def summarize_income(start: Any = None, end: Any = None) -> dict[str, Any]:
    """汇总指定日期区间内的收入台账（份数、含税/不含税、发票类型与类别拆分）。"""
    rows = table(TABLE_NAME).filter(start=start, end=end)
    by_invoice_type = _invoice_bucket_empty()
    by_category: dict[str, dict[str, float]] = {}
    total_inclusive = 0.0
    total_exclusive = 0.0
    vat_amount = 0.0
    rd_inclusive = 0.0
    max_single = 0.0
    for row in rows:
        inclusive = round2(row.get("inclusive_amount"))
        exclusive = round2(row.get("exclusive_amount"))
        vat = round2(row.get("vat_amount"))
        total_inclusive = round2(total_inclusive + inclusive)
        total_exclusive = round2(total_exclusive + exclusive)
        vat_amount = round2(vat_amount + vat)
        if models.parse_bool(row.get("is_rd_related")):
            rd_inclusive = round2(rd_inclusive + inclusive)
        max_single = max(max_single, inclusive)
        invoice_type = str(row.get("invoice_type") or "普票")
        bucket = by_invoice_type.setdefault(invoice_type, dict(_EMPTY_INVOICE_BUCKET))
        bucket["count"] = int(bucket["count"]) + 1
        bucket["inclusive"] = round2(float(bucket["inclusive"]) + inclusive)
        bucket["exclusive"] = round2(float(bucket["exclusive"]) + exclusive)
        category = str(row.get("category") or "其他")
        category_bucket = by_category.setdefault(category, {"inclusive": 0.0, "exclusive": 0.0})
        category_bucket["inclusive"] = round2(category_bucket["inclusive"] + inclusive)
        category_bucket["exclusive"] = round2(category_bucket["exclusive"] + exclusive)
    ordered_categories = {
        category: by_category[category]
        for category in _CATEGORY_ORDER
        if category in by_category
    }
    for category, bucket in by_category.items():
        ordered_categories.setdefault(category, bucket)
    return {
        "count": len(rows),
        "total_inclusive": total_inclusive,
        "total_exclusive": total_exclusive,
        "vat_amount": vat_amount,
        "by_invoice_type": by_invoice_type,
        "by_category": ordered_categories,
        "rd_inclusive": rd_inclusive,
        "max_single": round2(max_single),
    }


def period_totals(period: Any) -> dict[str, Any]:
    """按期间（季度/月/年）汇总收入台账，供增值税申报表取数。

    返回字段：``period``、``start``、``end``、含税/不含税合计、专票（special）与
    普票+未开票（ordinary）的含税/不含税拆分、笔数与发票类型明细。
    """
    text, start, end = _period_bounds(period)
    rows = table(TABLE_NAME).filter(start=start, end=end)
    by_invoice_type = _invoice_bucket_empty()
    total_inclusive = 0.0
    total_exclusive = 0.0
    special_inclusive = 0.0
    special_exclusive = 0.0
    for row in rows:
        inclusive = round2(row.get("inclusive_amount"))
        exclusive = round2(row.get("exclusive_amount"))
        total_inclusive = round2(total_inclusive + inclusive)
        total_exclusive = round2(total_exclusive + exclusive)
        invoice_type = str(row.get("invoice_type") or "普票")
        bucket = by_invoice_type.setdefault(invoice_type, dict(_EMPTY_INVOICE_BUCKET))
        bucket["count"] = int(bucket["count"]) + 1
        bucket["inclusive"] = round2(float(bucket["inclusive"]) + inclusive)
        bucket["exclusive"] = round2(float(bucket["exclusive"]) + exclusive)
        if invoice_type == "专票":
            special_inclusive = round2(special_inclusive + inclusive)
            special_exclusive = round2(special_exclusive + exclusive)
    ordinary_inclusive = round2(total_inclusive - special_inclusive)
    ordinary_exclusive = round2(total_exclusive - special_exclusive)
    return {
        "period": text,
        "start": start,
        "end": end,
        "total_inclusive": total_inclusive,
        "special_inclusive": special_inclusive,
        "ordinary_inclusive": ordinary_inclusive,
        "total_exclusive": total_exclusive,
        "special_exclusive": special_exclusive,
        "ordinary_exclusive": ordinary_exclusive,
        "count": len(rows),
        "by_invoice_type": by_invoice_type,
    }


__all__ = [
    "add_income",
    "list_income",
    "get_income",
    "update_income",
    "delete_income",
    "import_income_csv",
    "export_income_csv",
    "summarize_income",
    "period_totals",
]
