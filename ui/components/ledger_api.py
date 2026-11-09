"""台账访问封装：优先调用 ``ledger.*`` 公开接口，缺失时降级为直接读写 CSV 表。

设计目的
--------
1. 页面代码只依赖本模块的稳定签名，不关心队友模块是否已落盘；
2. 契约接口（``ledger.income_ledger`` / ``expense_ledger`` / ``rd_auxiliary`` /
   ``reports``）存在时使用之，不存在时用 ``ledger.store`` + ``engine`` 做等价降级，
   保证 UI 始终可用、不因缺模块白屏；
3. 所有写入后的缓存清理由调用方（页面）通过 ``ui.components.cache.clear()`` 完成。
"""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import config
import ledger
from engine import (
    aggregate_rd_expenses,
    calc_rd_deduction,
    calc_vat,
    current_quarter,
    month_options,
    next_deadline,
    period_range,
    quarter_of,
    quarter_options,
    reminders,
    round2,
    sum_money,
    upcoming_deadlines,
)
from ledger import models, store
from ui.components import runtime

INVOICE_TYPES = models.INVOICE_TYPES
INCOME_CATEGORIES = models.INCOME_CATEGORIES
EXPENSE_CATEGORIES = models.EXPENSE_CATEGORIES
CONTRACT_TYPES = models.CONTRACT_TYPES


# =============================================================================
# 通用小工具
# =============================================================================
def _table(name: str):
    return store.table(name)


def _sort_rows(rows: Iterable[dict], ascending: bool = True) -> list[dict]:
    return sorted(rows, key=lambda row: str(row.get("date") or ""), reverse=not ascending)


def _write_csv(path: Path, headers: list[str], rows: Iterable[dict], keys: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow({header: row.get(key, "") for header, key in zip(headers, keys)})
    return path


def _export_path(prefix: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(config.EXPORT_DIR) / f"{prefix}_{stamp}.csv"


def _alias_maps(table_name: str) -> dict[str, str]:
    """CSV 表头 → 字段名（同时支持英文字段名与中文列名）。"""
    mapping: dict[str, str] = {}
    for column in models.TABLE_DEFS[table_name]["columns"]:
        mapping[column["name"]] = column["name"]
        mapping[column["label"]] = column["name"]
    return mapping


def _import_csv(table_name: str, path: str | Path) -> int:
    """通用 CSV 导入（按表头中英文别名映射），返回成功条数。"""
    table = _table(table_name)
    alias = _alias_maps(table_name)
    normalizer = {
        "income": models.normalize_income,
        "expense": models.normalize_expense,
        "rd": models.normalize_rd,
        "contracts": models.normalize_contract,
    }.get(table_name)
    records: list[dict] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            payload: dict[str, Any] = {}
            for key, value in raw.items():
                field = alias.get((key or "").strip())
                if field:
                    payload[field] = (value or "").strip()
            if not payload:
                continue
            if normalizer is not None:
                try:
                    records.append(normalizer(payload))
                except ValueError:
                    continue
            else:
                records.append(payload)
    if not records:
        return 0
    table.append_many(records)
    return len(records)


# =============================================================================
# 收入台账
# =============================================================================
def add_income(payload: dict) -> dict:
    """新增收入记录（自动换算不含税金额与增值税额）。"""
    module = runtime.load("ledger.income_ledger")
    if module is not None and hasattr(module, "add_income"):
        return module.add_income(
            date=payload.get("date"),
            client=payload.get("client", ""),
            inclusive_amount=payload.get("inclusive_amount", 0.0),
            invoice_type=payload.get("invoice_type", "普票"),
            category=payload.get("category", "技术服务"),
            description=payload.get("description", ""),
            invoice_no=payload.get("invoice_no", ""),
            is_rd_related=payload.get("is_rd_related", False),
            note=payload.get("note", ""),
        )
    return _table("income").append(models.normalize_income(payload))


def list_income(start: str | None = None, end: str | None = None) -> list[dict]:
    module = runtime.load("ledger.income_ledger")
    if module is not None and hasattr(module, "list_income"):
        try:
            return list(module.list_income(start=start, end=end))
        except Exception:  # noqa: BLE001
            pass
    rows = _table("income").filter(start=start, end=end)
    return _sort_rows(rows)


def update_income(record_id: str, patch: dict) -> dict:
    module = runtime.load("ledger.income_ledger")
    if module is not None and hasattr(module, "update_income"):
        return module.update_income(record_id, patch)
    existing = _table("income").get(record_id) or {"id": record_id}
    merged = {**existing, **patch}
    if "inclusive_amount" in patch:
        # 含税金额变化时重新推导不含税金额与增值税额
        merged.pop("exclusive_amount", None)
        merged.pop("vat_amount", None)
    return _table("income").update(record_id, models.normalize_income(merged))


def delete_income(record_id: str) -> bool:
    module = runtime.load("ledger.income_ledger")
    if module is not None and hasattr(module, "delete_income"):
        return bool(module.delete_income(record_id))
    return _table("income").delete(record_id)


def import_income_csv(path: str | Path) -> int:
    module = runtime.load("ledger.income_ledger")
    if module is not None and hasattr(module, "import_income_csv"):
        return int(module.import_income_csv(path))
    return _import_csv("income", path)


def export_income_csv() -> Path:
    module = runtime.load("ledger.income_ledger")
    if module is not None and hasattr(module, "export_income_csv"):
        return Path(module.export_income_csv())
    table = _table("income")
    headers = [column["label"] for column in table.columns]
    keys = [column["name"] for column in table.columns]
    return _write_csv(_export_path("income_ledger"), headers, table.read(), keys)


def summarize_income(start: str | None = None, end: str | None = None) -> dict:
    module = runtime.load("ledger.income_ledger")
    if module is not None and hasattr(module, "summarize_income"):
        try:
            return dict(module.summarize_income(start=start, end=end))
        except Exception:  # noqa: BLE001
            pass
    rows = _sort_rows(_table("income").filter(start=start, end=end))
    by_invoice: dict[str, dict] = {
        invoice: {"count": 0, "inclusive": 0.0, "exclusive": 0.0} for invoice in INVOICE_TYPES
    }
    by_category: dict[str, dict] = {}
    for row in rows:
        inclusive = round2(row.get("inclusive_amount"))
        exclusive = round2(row.get("exclusive_amount"))
        invoice = str(row.get("invoice_type") or "普票")
        by_invoice.setdefault(invoice, {"count": 0, "inclusive": 0.0, "exclusive": 0.0})
        by_invoice[invoice]["count"] += 1
        by_invoice[invoice]["inclusive"] = round2(by_invoice[invoice]["inclusive"] + inclusive)
        by_invoice[invoice]["exclusive"] = round2(by_invoice[invoice]["exclusive"] + exclusive)
        category = str(row.get("category") or "其他")
        bucket = by_category.setdefault(category, {"inclusive": 0.0, "exclusive": 0.0})
        bucket["inclusive"] = round2(bucket["inclusive"] + inclusive)
        bucket["exclusive"] = round2(bucket["exclusive"] + exclusive)
    return {
        "count": len(rows),
        "total_inclusive": sum_money(row.get("inclusive_amount") for row in rows),
        "total_exclusive": sum_money(row.get("exclusive_amount") for row in rows),
        "vat_amount": sum_money(row.get("vat_amount") for row in rows),
        "by_invoice_type": by_invoice,
        "by_category": by_category,
        "rd_inclusive": sum_money(
            row.get("inclusive_amount") for row in rows if models.parse_bool(row.get("is_rd_related"))
        ),
        "max_single": max((round2(row.get("inclusive_amount")) for row in rows), default=0.0),
    }


def period_totals(period: str) -> dict:
    """期间收入汇总（含专票/普票拆分）。"""
    module = runtime.load("ledger.income_ledger")
    if module is not None and hasattr(module, "period_totals"):
        try:
            return dict(module.period_totals(period))
        except Exception:  # noqa: BLE001
            pass
    start, end = period_range(period)
    rows = _sort_rows(_table("income").filter(start=start.isoformat(), end=end.isoformat()))
    by_invoice: dict[str, dict] = {}
    total_inclusive = special_inclusive = total_exclusive = special_exclusive = 0.0
    for row in rows:
        inclusive = round2(row.get("inclusive_amount"))
        exclusive = round2(row.get("exclusive_amount"))
        invoice = str(row.get("invoice_type") or "普票")
        bucket = by_invoice.setdefault(invoice, {"count": 0, "inclusive": 0.0, "exclusive": 0.0})
        bucket["count"] += 1
        bucket["inclusive"] = round2(bucket["inclusive"] + inclusive)
        bucket["exclusive"] = round2(bucket["exclusive"] + exclusive)
        total_inclusive = round2(total_inclusive + inclusive)
        total_exclusive = round2(total_exclusive + exclusive)
        if invoice == "专票":
            special_inclusive = round2(special_inclusive + inclusive)
            special_exclusive = round2(special_exclusive + exclusive)
    return {
        "period": period,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total_inclusive": total_inclusive,
        "special_inclusive": special_inclusive,
        "ordinary_inclusive": round2(total_inclusive - special_inclusive),
        "total_exclusive": total_exclusive,
        "special_exclusive": special_exclusive,
        "ordinary_exclusive": round2(total_exclusive - special_exclusive),
        "count": len(rows),
        "by_invoice_type": by_invoice,
    }


# =============================================================================
# 费用台账
# =============================================================================
def add_expense(payload: dict) -> dict:
    module = runtime.load("ledger.expense_ledger")
    if module is not None and hasattr(module, "add_expense"):
        return module.add_expense(
            date=payload.get("date"),
            description=payload.get("description", ""),
            amount=payload.get("amount", 0.0),
            category=payload.get("category", "办公"),
            is_rd_related=payload.get("is_rd_related", False),
            vendor=payload.get("vendor", ""),
            receipt_no=payload.get("receipt_no", ""),
            note=payload.get("note", ""),
        )
    return _table("expense").append(models.normalize_expense(payload))


def list_expenses(start: str | None = None, end: str | None = None) -> list[dict]:
    module = runtime.load("ledger.expense_ledger")
    if module is not None and hasattr(module, "list_expenses"):
        try:
            return list(module.list_expenses(start=start, end=end))
        except Exception:  # noqa: BLE001
            pass
    return _sort_rows(_table("expense").filter(start=start, end=end))


def update_expense(record_id: str, patch: dict) -> dict:
    module = runtime.load("ledger.expense_ledger")
    if module is not None and hasattr(module, "update_expense"):
        return module.update_expense(record_id, patch)
    existing = _table("expense").get(record_id) or {"id": record_id}
    return _table("expense").update(record_id, models.normalize_expense({**existing, **patch}))


def delete_expense(record_id: str) -> bool:
    module = runtime.load("ledger.expense_ledger")
    if module is not None and hasattr(module, "delete_expense"):
        return bool(module.delete_expense(record_id))
    return _table("expense").delete(record_id)


def import_expense_csv(path: str | Path) -> int:
    module = runtime.load("ledger.expense_ledger")
    if module is not None and hasattr(module, "import_expense_csv"):
        return int(module.import_expense_csv(path))
    return _import_csv("expense", path)


def export_expense_csv() -> Path:
    module = runtime.load("ledger.expense_ledger")
    if module is not None and hasattr(module, "export_expense_csv"):
        return Path(module.export_expense_csv())
    table = _table("expense")
    headers = [column["label"] for column in table.columns]
    keys = [column["name"] for column in table.columns]
    return _write_csv(_export_path("expense_ledger"), headers, table.read(), keys)


def summarize_expenses(start: str | None = None, end: str | None = None) -> dict:
    module = runtime.load("ledger.expense_ledger")
    if module is not None and hasattr(module, "summarize_expenses"):
        try:
            return dict(module.summarize_expenses(start=start, end=end))
        except Exception:  # noqa: BLE001
            pass
    rows = _sort_rows(_table("expense").filter(start=start, end=end))
    by_category: dict[str, dict] = {}
    for row in rows:
        category = str(row.get("category") or "其他")
        bucket = by_category.setdefault(category, {"amount": 0.0, "count": 0})
        bucket["amount"] = round2(bucket["amount"] + round2(row.get("amount")))
        bucket["count"] += 1
    return {
        "count": len(rows),
        "total": sum_money(row.get("amount") for row in rows),
        "rd_total": sum_money(
            row.get("amount") for row in rows if models.parse_bool(row.get("is_rd_related"))
        ),
        "by_category": by_category,
    }


def period_expense_totals(period: str) -> dict:
    module = runtime.load("ledger.expense_ledger")
    if module is not None and hasattr(module, "period_totals"):
        try:
            return dict(module.period_totals(period))
        except Exception:  # noqa: BLE001
            pass
    start, end = period_range(period)
    summary = summarize_expenses(start.isoformat(), end.isoformat())
    summary.update({"period": period, "start": start.isoformat(), "end": end.isoformat()})
    return summary


# =============================================================================
# 研发辅助账
# =============================================================================
def add_rd_record(payload: dict) -> dict:
    module = runtime.load("ledger.rd_auxiliary")
    if module is not None and hasattr(module, "add_rd_record"):
        return module.add_rd_record(
            project_name=payload.get("project_name", "研发项目"),
            period=payload.get("period"),
            personnel_cost=payload.get("personnel_cost", 0.0),
            material_cost=payload.get("material_cost", 0.0),
            depreciation=payload.get("depreciation", 0.0),
            amortization=payload.get("amortization", 0.0),
            design_cost=payload.get("design_cost", 0.0),
            other_cost=payload.get("other_cost", 0.0),
            note=payload.get("note", ""),
        )
    return _table("rd").append(models.normalize_rd(payload))


def list_rd_records(period: str | None = None) -> list[dict]:
    module = runtime.load("ledger.rd_auxiliary")
    if module is not None and hasattr(module, "list_rd_records"):
        try:
            return list(module.list_rd_records(period=period))
        except Exception:  # noqa: BLE001
            pass
    rows = _table("rd").read()
    if period:
        rows = [row for row in rows if str(row.get("period")) == str(period)]
    return sorted(rows, key=lambda row: str(row.get("period") or ""))


def update_rd_record(record_id: str, patch: dict) -> dict:
    module = runtime.load("ledger.rd_auxiliary")
    if module is not None and hasattr(module, "update_rd_record"):
        return module.update_rd_record(record_id, patch)
    existing = _table("rd").get(record_id) or {"id": record_id}
    return _table("rd").update(record_id, models.normalize_rd({**existing, **patch}))


def delete_rd_record(record_id: str) -> bool:
    module = runtime.load("ledger.rd_auxiliary")
    if module is not None and hasattr(module, "delete_rd_record"):
        return bool(module.delete_rd_record(record_id))
    return _table("rd").delete(record_id)


def export_rd_csv() -> Path:
    module = runtime.load("ledger.rd_auxiliary")
    if module is not None and hasattr(module, "export_rd_csv"):
        return Path(module.export_rd_csv())
    table = _table("rd")
    headers = [column["label"] for column in table.columns]
    keys = [column["name"] for column in table.columns]
    return _write_csv(_export_path("rd_auxiliary"), headers, table.read(), keys)


def import_rd_csv(path: str | Path) -> int:
    module = runtime.load("ledger.rd_auxiliary")
    if module is not None and hasattr(module, "import_rd_csv"):
        return int(module.import_rd_csv(path))
    return _import_csv("rd", path)


def collect_from_expenses(period: str) -> dict:
    """从费用台账自动归集本期研发费用。"""
    module = runtime.load("ledger.rd_auxiliary")
    if module is not None and hasattr(module, "collect_from_expenses"):
        try:
            return dict(module.collect_from_expenses(period))
        except Exception:  # noqa: BLE001
            pass
    start, end = period_range(period)
    expenses = list_expenses(start.isoformat(), end.isoformat())
    return aggregate_rd_expenses(
        expenses,
        project_name="费用台账自动归集",
        period=period,
        include_unmarked=False,
    )


def summarize_rd(period: str) -> dict:
    """研发辅助账汇总（手工记录 + 费用台账归集）。"""
    module = runtime.load("ledger.rd_auxiliary")
    if module is not None and hasattr(module, "summarize_rd"):
        try:
            return dict(module.summarize_rd(period))
        except Exception:  # noqa: BLE001
            pass
    records = list_rd_records(period)
    totals = {
        key: sum_money(row.get(key) for row in records)
        for key in (
            "personnel_cost",
            "material_cost",
            "depreciation",
            "amortization",
            "design_cost",
            "other_cost",
        )
    }
    result = calc_rd_deduction(**totals, period=period, project_name="研发辅助账")
    from_expenses = collect_from_expenses(period)
    by_project: dict[str, float] = {}
    for row in records:
        project = str(row.get("project_name") or "研发项目")
        by_project[project] = round2(by_project.get(project, 0.0) + round2(row.get("total")))
    result.update(
        {
            "period": period,
            "records_detail": records,
            "from_expenses": from_expenses,
            "from_manual": {
                "count": len(records),
                "total": sum_money(row.get("total") for row in records),
            },
            "by_project": [
                {"project_name": name, "total": total} for name, total in by_project.items()
            ],
        }
    )
    return result


# =============================================================================
# 合同台账（印花税）
# =============================================================================
def list_contracts(start: str | None = None, end: str | None = None) -> list[dict]:
    return _sort_rows(_table("contracts").filter(start=start, end=end))


def add_contract(payload: dict) -> dict:
    return _table("contracts").append(models.normalize_contract(payload))


def update_contract(record_id: str, patch: dict) -> dict:
    existing = _table("contracts").get(record_id) or {"id": record_id}
    return _table("contracts").update(record_id, models.normalize_contract({**existing, **patch}))


def delete_contract(record_id: str) -> bool:
    return _table("contracts").delete(record_id)


def import_contracts_csv(path: str | Path) -> int:
    return _import_csv("contracts", path)


def export_contracts_csv() -> Path:
    table = _table("contracts")
    headers = [column["label"] for column in table.columns]
    keys = [column["name"] for column in table.columns]
    return _write_csv(_export_path("contracts"), headers, table.read(), keys)


# =============================================================================
# 汇总报表（驾驶舱）
# =============================================================================
def dashboard() -> dict:
    """驾驶舱数据：优先 ``ledger.reports.dashboard()``，否则本地降级汇总。"""
    module = runtime.load("ledger.reports")
    if module is not None and hasattr(module, "dashboard"):
        try:
            return dict(module.dashboard())
        except Exception:  # noqa: BLE001
            pass
    period = current_quarter()
    income = period_totals(period)
    expense = period_expense_totals(period)
    vat = calc_vat(income["total_inclusive"], income["special_inclusive"], period)
    profit = round2(income["total_exclusive"] - expense.get("total", 0.0))
    year = str(date.today().year)
    ytd_income = ytd_expense = 0.0
    for quarter in range(1, 5):
        candidate = f"{year}Q{quarter}"
        ytd_income = round2(ytd_income + period_totals(candidate)["total_exclusive"])
        ytd_expense = round2(ytd_expense + period_expense_totals(candidate).get("total", 0.0))
    counts = storage_stats()
    upcoming = upcoming_deadlines(lookahead_days=60)
    return {
        "period": period,
        "quarter_income_inclusive": income["total_inclusive"],
        "quarter_income_exclusive": income["total_exclusive"],
        "quarter_expense": expense.get("total", 0.0),
        "quarter_profit": profit,
        "vat_payable": vat["vat_payable"],
        "surtax": vat["surtax"],
        "total_tax": vat["total_tax"],
        "ytd_income": ytd_income,
        "ytd_expense": ytd_expense,
        "ytd_profit": round2(ytd_income - ytd_expense),
        "counts": {
            "income": counts.get("income", 0),
            "expense": counts.get("expense", 0),
            "rd": counts.get("rd", 0),
            "contracts": counts.get("contracts", 0),
        },
        "next_deadline": next_deadline() or {},
        "reminders": reminders(lookahead_days=45),
        "recent_income": list_income()[-5:][::-1],
        "recent_expense": list_expenses()[-5:][::-1],
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "fallback",
    }


def monthly_summary(year: int, months: list[int] | None = None) -> list[dict]:
    """月度收入/费用趋势。"""
    module = runtime.load("ledger.reports")
    if module is not None and hasattr(module, "monthly_summary"):
        try:
            return list(module.monthly_summary(year, months=months))
        except Exception:  # noqa: BLE001
            pass
    month_list = months or list(range(1, 13))
    rows: list[dict] = []
    for month in month_list:
        start = date(year, month, 1)
        next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        end = next_month - timedelta(days=1)
        income_rows = list_income(start.isoformat(), end.isoformat())
        expense_rows = list_expenses(start.isoformat(), end.isoformat())
        inclusive = sum_money(row.get("inclusive_amount") for row in income_rows)
        exclusive = sum_money(row.get("exclusive_amount") for row in income_rows)
        special = sum_money(
            row.get("inclusive_amount") for row in income_rows if str(row.get("invoice_type")) == "专票"
        )
        expense_total = sum_money(row.get("amount") for row in expense_rows)
        tax = calc_vat(inclusive, special, "")["total_tax"] if inclusive else 0.0
        rows.append(
            {
                "month": f"{year}-{month:02d}",
                "income_inclusive": inclusive,
                "income_exclusive": exclusive,
                "expense": expense_total,
                "profit": round2(exclusive - expense_total),
                "tax": tax,
            }
        )
    return rows


def income_expense_report(period: str) -> dict:
    """季度收入费用报表（含分类汇总）。"""
    module = runtime.load("ledger.reports")
    if module is not None and hasattr(module, "income_expense_report"):
        try:
            return dict(module.income_expense_report(period))
        except Exception:  # noqa: BLE001
            pass
    start, end = period_range(period)
    income = period_totals(period)
    expense = period_expense_totals(period)
    report = {
        "period": period,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "income_inclusive": income["total_inclusive"],
        "income_exclusive": income["total_exclusive"],
        "special_inclusive": income["special_inclusive"],
        "expense": expense.get("total", 0.0),
        "profit": round2(income["total_exclusive"] - expense.get("total", 0.0)),
        "rd_cost": expense.get("rd_total", 0.0),
        "rd_deduction": calc_rd_deduction(
            **{
                key: sum_money(row.get(key) for row in list_rd_records(period))
                for key in (
                    "personnel_cost",
                    "material_cost",
                    "depreciation",
                    "amortization",
                    "design_cost",
                    "other_cost",
                )
            },
            period=period,
        ).get("deduction_amount", 0.0),
        "vat": calc_vat(income["total_inclusive"], income["special_inclusive"], period)["vat_payable"],
        "count_income": income["count"],
        "count_expense": expense.get("count", 0),
        "by_category_income": {
            name: bucket.get("exclusive", 0.0)
            for name, bucket in summarize_income(start.isoformat(), end.isoformat())
            .get("by_category", {})
            .items()
        },
        "by_category_expense": {
            name: bucket.get("amount", 0.0)
            for name, bucket in expense.get("by_category", {}).items()
        },
    }
    report["surtax"] = calc_vat(
        income["total_inclusive"], income["special_inclusive"], period
    )["surtax"]
    return report


# =============================================================================
# 存储状态 / 示例数据
# =============================================================================
def storage_stats() -> dict[str, int]:
    try:
        return dict(store.storage_stats())
    except Exception:  # noqa: BLE001
        return {}


def is_empty() -> bool:
    """全部台账是否为空。"""
    module = runtime.load("ledger.seed")
    if module is not None and hasattr(module, "is_empty"):
        try:
            return bool(module.is_empty())
        except Exception:  # noqa: BLE001
            pass
    counts = storage_stats()
    return all(counts.get(name, 0) == 0 for name in ("income", "expense", "rd", "contracts"))


def seed_demo_data(reset: bool = False) -> dict:
    """加载示例数据；``ledger.seed`` 未就绪时抛出 ValueError 供页面提示。"""
    module = runtime.load("ledger.seed")
    if module is None or not hasattr(module, "seed_demo_data"):
        raise ValueError("示例数据模块（ledger.seed）尚未就绪，请稍后重试或手动录入。")
    return dict(module.seed_demo_data(reset=reset))


def clear_all() -> None:
    module = runtime.load("ledger.seed")
    if module is not None and hasattr(module, "clear_all"):
        module.clear_all()
        return
    for name in ("income", "expense", "rd", "contracts"):
        _table(name).replace_all([])


def backup_all(tag: str = "manual") -> list[Path]:
    return list(store.backup_all(tag=tag))


#: 供页面使用的期间选项
def periods(count: int = 8) -> list[str]:
    return list(quarter_options(count=count))


def months(count: int = 6) -> list[str]:
    return list(month_options(count=count))


def current_period() -> str:
    return current_quarter()


def quarter_label(period: str) -> str:
    """季度中文名。"""
    text = str(period or "").strip().upper()
    try:
        return f"{text[:4]}年第{'一二三四'[int(text[-1]) - 1]}季度"
    except (ValueError, IndexError):
        return text or "—"


__all__ = [
    "INVOICE_TYPES",
    "INCOME_CATEGORIES",
    "EXPENSE_CATEGORIES",
    "CONTRACT_TYPES",
    "add_income",
    "list_income",
    "update_income",
    "delete_income",
    "import_income_csv",
    "export_income_csv",
    "summarize_income",
    "period_totals",
    "add_expense",
    "list_expenses",
    "update_expense",
    "delete_expense",
    "import_expense_csv",
    "export_expense_csv",
    "summarize_expenses",
    "period_expense_totals",
    "add_rd_record",
    "list_rd_records",
    "update_rd_record",
    "delete_rd_record",
    "export_rd_csv",
    "import_rd_csv",
    "collect_from_expenses",
    "summarize_rd",
    "list_contracts",
    "add_contract",
    "update_contract",
    "delete_contract",
    "import_contracts_csv",
    "export_contracts_csv",
    "dashboard",
    "monthly_summary",
    "income_expense_report",
    "storage_stats",
    "is_empty",
    "seed_demo_data",
    "clear_all",
    "backup_all",
    "periods",
    "months",
    "current_period",
    "quarter_label",
]
