"""研发辅助账：手工归集记录 + 从费用台账自动归集，计算加计扣除。

归集口径
--------
* 费用台账中 ``is_rd_related=True`` 的记录按 :data:`engine.rd_deduction.RD_CATEGORY_MAP`
  自动映射到六个研发费用科目（人员人工、直接投入、折旧、摊销、设计费、其他相关费用）。
* ``rd`` 表的手工记录用于补充未在费用台账逐笔登记的项目级归集（如折旧分摊、设计费）。
* 最终调用 :func:`engine.calc_rd_deduction` 做确定性计算（其他相关费用限额、100% 加计扣除）。
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any

import config
from engine.money import round2
from engine.rd_deduction import (
    RD_CATEGORY_MAP,
    RD_EXCLUDED_CATEGORIES,
    RD_ITEM_LABELS,
    RD_ITEMS,
    aggregate_rd_expenses,
    calc_rd_deduction,
)
from engine.tax_calendar import period_range, quarter_end, quarter_start
from ledger import models
from ledger.audit import log_user_action
from ledger.expense_ledger import list_expenses
from ledger.store import table

TABLE_NAME = "rd"
TABLE_LABEL = "研发辅助账"

_LABEL_TO_KEY = {label: key for key, label in RD_ITEM_LABELS.items()}


# =============================================================================
# 内部工具
# =============================================================================
def _period_bounds(period: Any) -> tuple[str, str, str]:
    """返回 ``(规范化期间, 起始日, 结束日)``。"""
    text = models.normalize_period(period, default="")
    if not text:
        raise ValueError("请提供所属期间，如 2026Q1")
    try:
        start, end = period_range(text)
    except ValueError as exc:
        raise ValueError(f"期间格式不正确：{period}，请使用 2026Q1 / 2026-03 / 2026 形式") from exc
    return text, start.isoformat(), end.isoformat()


def _quarter_text(period: Any) -> str:
    """规范化期间为季度字符串（月份期间自动换算到所属季度）。"""
    text = models.normalize_period(period, default="")
    if not text:
        raise ValueError("请提供所属期间，如 2026Q1")
    if len(text) == 7 and text[4] == "-":
        return f"{text[:4]}Q{(int(text[5:7]) - 1) // 3 + 1}"
    return text


def _manual_rows(period_text: str, project: str | None = None) -> list[dict[str, Any]]:
    """读取某期间的研发手工记录（可按项目名过滤）。"""
    rows = table(TABLE_NAME).read()
    result = []
    for row in rows:
        if models.normalize_period(row.get("period"), default="") != period_text:
            continue
        if project and str(row.get("project_name") or "") != str(project):
            continue
        result.append(row)
    return result


def _manual_items(rows: list[dict[str, Any]]) -> dict[str, float]:
    items = {key: 0.0 for key in RD_ITEMS}
    for row in rows:
        for key in RD_ITEMS:
            items[key] = round2(items[key] + round2(row.get(key)))
    return items


def _by_project(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按研发项目汇总手工记录。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("project_name") or "研发项目"), []).append(row)
    result = []
    for project_name, records in grouped.items():
        items = _manual_items(records)
        total = round2(sum(items.values()))
        calc = calc_rd_deduction(**items, period=str(records[0].get("period") or ""), project_name=project_name)
        result.append(
            {
                "project_name": project_name,
                "count": len(records),
                "total": total,
                "deduction_amount": calc["deduction_amount"],
                "items": {RD_ITEM_LABELS[key]: items[key] for key in RD_ITEMS},
                "records": records,
            }
        )
    return sorted(result, key=lambda item: item["total"], reverse=True)


def _export_default_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(config.EXPORT_DIR) / f"{TABLE_LABEL}_{stamp}.csv"


# =============================================================================
# CRUD
# =============================================================================
def add_rd_record(
    project_name: str = "研发项目",
    period: Any = None,
    *,
    personnel_cost: float = 0.0,
    material_cost: float = 0.0,
    depreciation: float = 0.0,
    amortization: float = 0.0,
    design_cost: float = 0.0,
    other_cost: float = 0.0,
    note: str = "",
) -> dict[str, Any]:
    """新增一条研发费用归集记录，返回写库后的完整记录。

    ``period`` 缺省时自动取当期所在季度；``total`` 与 ``deduction_amount`` 由系统计算。
    """
    record = models.normalize_rd(
        {
            "project_name": project_name,
            "period": period,
            "personnel_cost": personnel_cost,
            "material_cost": material_cost,
            "depreciation": depreciation,
            "amortization": amortization,
            "design_cost": design_cost,
            "other_cost": other_cost,
            "note": note,
        }
    )
    added = table(TABLE_NAME).append(record)
    log_user_action(
        "新增研发归集",
        f"{added['period']} {added['project_name']} 合计 {added['total']:.2f} 元，编号 {added['id']}",
    )
    return added


def list_rd_records(period: Any = None, project: str | None = None) -> list[dict[str, Any]]:
    """查询研发手工归集记录（可按期间、项目过滤），按期间与项目排序。"""
    text = ""
    if period not in (None, ""):
        text, _, _ = _period_bounds(period)
    rows = table(TABLE_NAME).read()
    result = []
    for row in rows:
        if text and models.normalize_period(row.get("period"), default="") != text:
            continue
        if project and str(row.get("project_name") or "") != str(project):
            continue
        result.append(row)
    return sorted(result, key=lambda row: (str(row.get("period") or ""), str(row.get("project_name") or "")))


def get_rd_record(rd_id: str) -> dict[str, Any] | None:
    """按编号读取一条研发归集记录。"""
    return table(TABLE_NAME).get(rd_id)


def update_rd_record(rd_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """按编号更新研发归集记录，返回更新后的记录；编号不存在时抛 ``KeyError``。"""
    if not isinstance(patch, dict):
        raise ValueError("更新内容必须是 dict")
    current = get_rd_record(rd_id)
    if current is None:
        raise KeyError(f"{TABLE_LABEL}中不存在编号 {rd_id}")
    merged = dict(current)
    for key, value in patch.items():
        if key in ("id", "created_at"):
            continue
        merged[key] = value
    if any(key in patch for key in RD_ITEMS) and "deduction_amount" not in patch:
        merged.pop("deduction_amount", None)
    record = models.normalize_rd(merged)
    updated = table(TABLE_NAME).update(rd_id, record)
    log_user_action("修改研发归集", f"编号 {rd_id}：{updated['project_name']} 合计 {updated['total']:.2f} 元")
    return updated


def delete_rd_record(rd_id: str) -> bool:
    """按编号删除研发归集记录，返回是否删除成功。"""
    deleted = table(TABLE_NAME).delete(rd_id)
    if deleted:
        log_user_action("删除研发归集", f"删除编号 {rd_id}")
    return deleted


# =============================================================================
# 归集与计算
# =============================================================================
def collect_from_expenses(period: Any) -> dict[str, Any]:
    """从费用台账自动归集某期间的研发费用（仅 ``is_rd_related=True`` 的记录）。

    返回 :func:`engine.aggregate_rd_expenses` 的结果，并补充 ``period/start/end``。
    """
    text, start, end = _period_bounds(period)
    expenses = list_expenses(start=start, end=end)
    result = aggregate_rd_expenses(expenses, project_name="费用台账自动归集", period=text)
    result["period"] = text
    result["start"] = start
    result["end"] = end
    return result


def summarize_rd(period: Any) -> dict[str, Any]:
    """汇总某期间的研发费用并计算加计扣除。

    返回内容 = :func:`engine.calc_rd_deduction` 的结果 + 归集来源明细：
    ``period/start/end``、``records_detail``（手工记录）、``from_expenses``（费用台账自动归集结果）、
    ``from_manual``（手工记录汇总）、``by_project``（按项目汇总）。
    """
    text, start, end = _period_bounds(period)
    from_expenses = collect_from_expenses(text)
    manual_rows = _manual_rows(text)
    manual_items = _manual_items(manual_rows)

    combined = {}
    for key in RD_ITEMS:
        auto_amount = round2((from_expenses.get("items") or {}).get(RD_ITEM_LABELS[key]))
        combined[key] = round2(manual_items[key] + auto_amount)
    base = calc_rd_deduction(**combined, period=text, project_name="全部研发项目")

    manual_total = round2(sum(manual_items.values()))
    by_project = _by_project(manual_rows)
    if float(from_expenses.get("total_collectible") or 0) > 0:
        by_project.insert(
            0,
            {
                "project_name": "费用台账自动归集",
                "count": int(from_expenses.get("source_count") or 0),
                "total": round2(from_expenses.get("total_collectible")),
                "deduction_amount": round2(from_expenses.get("deduction_amount")),
                "items": dict(from_expenses.get("items") or {}),
                "records": list(from_expenses.get("detail") or []),
            },
        )

    result = dict(base)
    result.update(
        {
            "period": text,
            "start": start,
            "end": end,
            "records_detail": manual_rows,
            "from_expenses": from_expenses,
            "from_manual": {
                "items": {RD_ITEM_LABELS[key]: manual_items[key] for key in RD_ITEMS},
                "total": manual_total,
                "count": len(manual_rows),
            },
            "by_project": by_project,
            "manual_total": manual_total,
            "from_expenses_total": round2(from_expenses.get("total_collectible")),
        }
    )
    return result


def ytd_summary(period: Any) -> dict[str, Any]:
    """按本年累计口径汇总研发费用（自年初至指定季度末），供企业所得税预缴取数。

    返回 ``rd_cost``（本年累计可加计扣除研发费用）、``rd_deduction``（本年累计加计扣除额），
    并附 ``period/start/end/quarters`` 明细。
    """
    text = _quarter_text(period)
    try:
        start_date = quarter_start(text)
        end_date = quarter_end(text)
    except ValueError as exc:
        raise ValueError(f"期间格式不正确：{period}，请使用 2026Q1 形式") from exc
    year = start_date.year
    current_quarter = (start_date.month - 1) // 3 + 1
    start = f"{year}-01-01"
    end = end_date.isoformat()

    expenses = list_expenses(start=start, end=end)
    from_expenses = aggregate_rd_expenses(expenses, project_name="费用台账自动归集", period=text)

    quarters: list[str] = [f"{year}Q{index}" for index in range(1, current_quarter + 1)]
    manual_rows = [row for row in table(TABLE_NAME).read() if models.normalize_period(row.get("period"), default="") in quarters]
    manual_items = _manual_items(manual_rows)
    combined = {}
    for key in RD_ITEMS:
        combined[key] = round2(
            manual_items[key] + round2((from_expenses.get("items") or {}).get(RD_ITEM_LABELS[key]))
        )
    base = calc_rd_deduction(**combined, period=text, project_name="本年累计研发项目")

    quarter_details = []
    for quarter in quarters:
        summary = summarize_rd(quarter)
        quarter_details.append(
            {
                "period": quarter,
                "total_collectible": summary["total_collectible"],
                "deduction_amount": summary["deduction_amount"],
            }
        )
    return {
        "period": text,
        "start": start,
        "end": end,
        "quarters": quarters,
        "quarter_details": quarter_details,
        "rd_cost": base["total_collectible"],
        "rd_deduction": base["deduction_amount"],
        "from_expenses": from_expenses,
        "records_detail": manual_rows,
        "items": base["items"],
        "note": base["note"],
        "warnings": base["warnings"],
        "disclaimer": base["disclaimer"],
    }


def import_rd_csv(path: Any) -> int:
    """从 CSV 导入研发辅助账（兼容中英文表头），返回成功导入条数。"""
    csv_path = Path(path)
    if not csv_path.exists():
        raise ValueError(f"文件不存在：{csv_path}")
    alias = {
        "project_name": ("project_name", "研发项目", "项目名称"),
        "period": ("period", "所属期间", "期间"),
        "personnel_cost": ("personnel_cost", "人员人工", "人员人工费用"),
        "material_cost": ("material_cost", "直接投入", "直接投入费用"),
        "depreciation": ("depreciation", "折旧费用", "折旧"),
        "amortization": ("amortization", "无形资产摊销", "摊销"),
        "design_cost": ("design_cost", "新产品设计费", "设计费"),
        "other_cost": ("other_cost", "其他相关费用", "其他费用"),
        "deduction_amount": ("deduction_amount", "加计扣除额"),
        "note": ("note", "备注"),
    }
    records = []
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                payload: dict[str, Any] = {}
                for field_name, aliases in alias.items():
                    for name in aliases:
                        if name in raw and str(raw[name]).strip() != "":
                            payload[field_name] = raw[name]
                            break
                try:
                    records.append(models.normalize_rd(payload))
                except ValueError:
                    continue
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        raise ValueError(f"读取 CSV 失败：{exc}") from exc
    added = table(TABLE_NAME).append_many(records)
    log_user_action("导入研发辅助账CSV", f"文件 {csv_path.name}，成功导入 {len(added)} 条")
    return len(added)


def export_rd_csv(path: Any = None) -> Path:
    """导出研发辅助账手工归集记录为 CSV（中文表头），返回文件路径。"""
    target = Path(path) if path else _export_default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    columns = models.TABLE_DEFS[TABLE_NAME]["columns"]
    rows = list_rd_records()
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([column["label"] for column in columns])
        for row in rows:
            values = []
            for column in columns:
                value = row.get(column["name"])
                if column["type"] == "money":
                    values.append(f"{round2(value):.2f}")
                else:
                    values.append(value if value is not None else "")
            writer.writerow(values)
    log_user_action("导出研发辅助账", f"文件 {target.name}，共 {len(rows)} 条")
    return target


__all__ = [
    "add_rd_record",
    "list_rd_records",
    "get_rd_record",
    "update_rd_record",
    "delete_rd_record",
    "collect_from_expenses",
    "summarize_rd",
    "ytd_summary",
    "import_rd_csv",
    "export_rd_csv",
    "RD_CATEGORY_MAP",
    "RD_EXCLUDED_CATEGORIES",
]
