"""企业所得税汇算清缴后端访问封装（``ledger.settlement`` / ``filing.cit_annual``）。

设计约定
--------
* 页面只依赖本模块签名；后端由队友并行开发，缺失时用 :mod:`ui.components.runtime`
  容错降级：列表类读取降级为直接读 ``ledger.store.table``，写操作抛中文 ``ValueError``；
* 年度台账汇总（``annual_ledger_data``）与限额类纳税调整（``calc_adjustment_limits``）
  在后端未就绪时可用 ``ledger_api`` + ``engine`` 本地兜底，保证页面可预览；
* 汇算结果计算、检查清单与年度申报辅助表必须走后端（确定性引擎已就绪，后端负责编排）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import ledger
from engine import (
    LOSS_CARRYFORWARD_YEARS,
    aggregate_rd_expenses,
    calc_adjustment_limits,
    calc_loss_carryforward,
    get_settlement_sources,
    round2,
)
from ledger import models
from ui.components import ledger_api as LA, runtime

#: 枚举（来自 ledger.models，只读引用）
ADJUSTMENT_DIRECTIONS = models.ADJUSTMENT_DIRECTIONS
SETTLEMENT_STATUSES = models.SETTLEMENT_STATUSES

#: 年度输入白名单（与后端 save_annual_inputs 一致）
ANNUAL_INPUT_KEYS: tuple[str, ...] = (
    "entertainment_actual",
    "advertising_actual",
    "welfare_actual",
    "education_actual",
    "union_actual",
    "employees",
    "total_assets",
    "prepaid_tax",
    "accounting_profit",
    "rd_deduction",
    "is_restricted_industry",
    "note",
)

#: 补充数据表单默认值
ANNUAL_INPUT_DEFAULTS: dict[str, Any] = {
    "entertainment_actual": 0.0,
    "advertising_actual": 0.0,
    "welfare_actual": 0.0,
    "education_actual": 0.0,
    "union_actual": 0.0,
    "employees": 0,
    "total_assets": 0.0,
    "prepaid_tax": 0.0,
    "accounting_profit": 0.0,
    "rd_deduction": 0.0,
    "is_restricted_industry": False,
}

#: 常用纳税调整项目（供表单下拉，也允许自定义）
ADJUSTMENT_ITEM_PRESETS: list[str] = [
    "业务招待费",
    "广告费和业务宣传费",
    "职工福利费",
    "职工教育经费",
    "工会经费",
    "税收滞纳金、罚款",
    "非公益性捐赠支出",
    "资产减值准备",
    "不征税收入",
    "免税收入",
    "其他纳税调整",
]


# =============================================================================
# 模块加载与就绪判断
# =============================================================================
def _load(name: str):
    return runtime.load(f"ledger.{name}")


def ledger_ready() -> bool:
    module = _load("settlement")
    return module is not None and hasattr(module, "annual_ledger_data")


def inputs_ready() -> bool:
    module = _load("settlement")
    return module is not None and hasattr(module, "save_annual_inputs")


def adjustments_ready() -> bool:
    module = _load("settlement")
    return module is not None and hasattr(module, "add_adjustment")


def losses_ready() -> bool:
    module = _load("settlement")
    return module is not None and hasattr(module, "add_loss")


def compute_ready() -> bool:
    module = _load("settlement")
    return module is not None and hasattr(module, "compute_settlement")


def checklist_ready() -> bool:
    module = _load("settlement")
    return module is not None and hasattr(module, "settlement_checklist")


def annual_return_ready() -> bool:
    module = runtime.load("filing.cit_annual")
    return module is not None and hasattr(module, "build_cit_annual_return")


def annual_export_ready() -> bool:
    module = runtime.load("filing.cit_annual")
    return module is not None and hasattr(module, "export_cit_annual_excel")


def any_ready() -> bool:
    return any(
        (
            ledger_ready(),
            inputs_ready(),
            compute_ready(),
            annual_return_ready(),
        )
    )


def missing_detail() -> str:
    return (
        runtime.error_of("ledger.settlement")
        or runtime.error_of("filing.cit_annual")
        or ""
    )


def _require(dotted: str, feature: str):
    module = runtime.load(dotted)
    if module is None:
        raise ValueError(f"{feature}暂不可用：{dotted} 模块尚未就绪（后端由队友并行开发中）。")
    return module


def _read_table(name: str) -> list[dict]:
    try:
        return list(ledger.table(name).read())
    except Exception:  # noqa: BLE001
        return []


# =============================================================================
# 年度工具
# =============================================================================
def current_year() -> str:
    return str(date.today().year)


def year_options(count: int = 6) -> list[str]:
    """最近若干年度（新→旧），如 ``["2026","2025",...]``。"""
    year = date.today().year
    return [str(year - index) for index in range(max(count, 1))]


def year_label(year: str) -> str:
    text = str(year or "").strip()
    return f"{text} 年度" if text else "—"


def sources() -> list[dict]:
    try:
        return list(get_settlement_sources())
    except Exception:  # noqa: BLE001
        return []


def loss_carryforward_years() -> int:
    try:
        return int(LOSS_CARRYFORWARD_YEARS)
    except Exception:  # noqa: BLE001
        return 5


# =============================================================================
# 年度数据 / 输入
# =============================================================================
def annual_ledger_data(year: str | int) -> dict:
    """年度台账汇总（后端优先，缺失时用 ledger_api + engine 兜底）。"""
    module = _load("settlement")
    if module is not None and hasattr(module, "annual_ledger_data"):
        try:
            return dict(module.annual_ledger_data(year))
        except Exception:  # noqa: BLE001
            pass
    return _fallback_annual_data(year)


def _fallback_annual_data(year: str | int) -> dict:
    """本地年度汇总（数据来源：台账 CSV + engine 研发归集 + 申报记录）。"""
    year_text = str(year)[:4]
    try:
        year_int = int(year_text)
    except ValueError:
        year_int = date.today().year
    start = f"{year_text}-01-01"
    end = f"{year_text}-12-31"
    try:
        months = LA.monthly_summary(year_int)
    except Exception:  # noqa: BLE001
        months = []
    income_rows = LA.list_income(start, end)
    expense_rows = LA.list_expenses(start, end)
    revenue_exclusive = round2(sum(float(row.get("exclusive_amount") or 0) for row in income_rows))
    revenue_inclusive = round2(sum(float(row.get("inclusive_amount") or 0) for row in income_rows))
    expense_total = round2(sum(float(row.get("amount") or 0) for row in expense_rows))
    expense_by_category: dict[str, float] = {}
    salary_total = 0.0
    for row in expense_rows:
        category = str(row.get("category") or "其他")
        amount = round2(row.get("amount"))
        expense_by_category[category] = round2(expense_by_category.get(category, 0.0) + amount)
        if category == "工资":
            salary_total = round2(salary_total + amount)
    rd_expenses = [row for row in expense_rows if models.parse_bool(row.get("is_rd_related"))]
    rd_cost = round2(sum(float(row.get("amount") or 0) for row in rd_expenses))
    rd_deduction = 0.0
    try:
        rd_deduction = round2(
            aggregate_rd_expenses(rd_expenses, project_name="费用台账自动归集", period=year_text)[
                "deduction_amount"
            ]
        )
    except Exception:  # noqa: BLE001
        rd_deduction = rd_cost
    return {
        "year": year_text,
        "start": start,
        "end": end,
        "revenue_exclusive": revenue_exclusive,
        "revenue_inclusive": revenue_inclusive,
        "expense_total": expense_total,
        "profit": round2(revenue_exclusive - expense_total),
        "expense_by_category": expense_by_category,
        "salary_total": salary_total,
        "rd_cost": rd_cost,
        "rd_deduction": rd_deduction,
        "prepaid_tax": _fallback_prepaid_tax(year_int),
        "counts": {
            "income": len(income_rows),
            "expense": len(expense_rows),
            "journal_entries": len(_read_table("journal_entries")),
            "invoices": len(_read_table("invoices")),
            "transactions": len(_read_table("bank_transactions")),
        },
        "monthly": [
            {
                "month": row.get("month"),
                "income_exclusive": round2(row.get("income_exclusive")),
                "expense": round2(row.get("expense")),
            }
            for row in months
            if str(row.get("month") or "").startswith(year_text)
        ],
        "warnings": ["年度台账汇总由本地台账兜底计算（ledger.settlement 尚未就绪）。"],
    }


def _fallback_prepaid_tax(year_int: int) -> float:
    """从申报记录中汇总该年度各季度企业所得税预缴税额。"""
    total = 0.0
    for row in _read_table("filings"):
        period = str(row.get("period") or "")
        if not period.startswith(str(year_int)):
            continue
        if str(row.get("form_id") or "") != "CIT_PREPAY":
            continue
        data = row.get("data_json") or {}
        totals = data.get("totals") if isinstance(data, dict) else {}
        totals = totals if isinstance(totals, dict) else {}
        for key in ("prepay_payable", "cit_payable", "payable"):
            value = totals.get(key)
            if isinstance(value, (int, float)):
                total += float(value)
                break
    return round2(total)


def get_annual_inputs(year: str | int) -> dict:
    """读取年度汇算补充输入（不存在返回空 dict）。"""
    module = _load("settlement")
    if module is not None and hasattr(module, "get_annual_inputs"):
        try:
            return dict(module.get_annual_inputs(year))
        except Exception:  # noqa: BLE001
            pass
    for row in _read_table("annual_settlements"):
        if str(row.get("year") or "") == str(year)[:4]:
            data = row.get("data_json") or {}
            return dict(data) if isinstance(data, dict) else {}
    return {}


def settlement_record(year: str | int) -> dict:
    """年度汇算记录（状态 / 备注 / 更新时间），后端无独立 getter，直接读表。"""
    for row in _read_table("annual_settlements"):
        if str(row.get("year") or "") == str(year)[:4]:
            return dict(row)
    return {}


def save_annual_inputs(year: str | int, data: dict, *, note: str = "") -> dict:
    """保存年度汇算补充输入（白名单键）。"""
    module = _require("ledger.settlement", "保存年度输入")
    if not hasattr(module, "save_annual_inputs"):
        raise ValueError("保存年度输入暂不可用：ledger.settlement.save_annual_inputs 未实现。")
    payload = {key: value for key, value in data.items() if key in ANNUAL_INPUT_KEYS}
    return dict(module.save_annual_inputs(year, payload, note=note))


def mark_settlement_status(year: str | int, status: str) -> dict:
    """标记年度汇算状态（草稿 / 已确认）。"""
    module = _require("ledger.settlement", "年度汇算状态")
    if not hasattr(module, "mark_settlement_status"):
        raise ValueError("年度汇算状态暂不可用：ledger.settlement.mark_settlement_status 未实现。")
    return dict(module.mark_settlement_status(year, status))


# =============================================================================
# 纳税调整
# =============================================================================
def adjustment_limits(year: str | int, *, annual: dict | None = None, inputs: dict | None = None) -> dict:
    """限额类纳税调整预览（确定性引擎，后端未就绪也可用）。"""
    annual = annual or annual_ledger_data(year)
    inputs = inputs or get_annual_inputs(year)
    try:
        return dict(
            calc_adjustment_limits(
                revenue_exclusive=float(annual.get("revenue_exclusive") or 0),
                salary_total=float(annual.get("salary_total") or 0),
                entertainment_actual=float(inputs.get("entertainment_actual") or 0),
                advertising_actual=float(inputs.get("advertising_actual") or 0),
                welfare_actual=float(inputs.get("welfare_actual") or 0),
                education_actual=float(inputs.get("education_actual") or 0),
                union_actual=float(inputs.get("union_actual") or 0),
            )
        )
    except Exception:  # noqa: BLE001
        return {"items": [], "total_adjustment_add": 0.0, "warnings": []}


def list_adjustments(year: str | int) -> list[dict]:
    module = _load("settlement")
    if module is not None and hasattr(module, "list_adjustments"):
        try:
            return list(module.list_adjustments(year))
        except Exception:  # noqa: BLE001
            pass
    rows = [row for row in _read_table("tax_adjustments") if str(row.get("year") or "") == str(year)[:4]]
    return sorted(rows, key=lambda row: str(row.get("created_at") or ""))


def add_adjustment(
    year: str | int,
    item_type: str,
    direction: str,
    adjustment_amount: float,
    *,
    accounting_amount: float = 0.0,
    tax_amount: float = 0.0,
    basis: str = "",
    source: str = "",
    note: str = "",
) -> dict:
    module = _require("ledger.settlement", "新增纳税调整")
    if not hasattr(module, "add_adjustment"):
        raise ValueError("新增纳税调整暂不可用：ledger.settlement.add_adjustment 未实现。")
    return dict(
        module.add_adjustment(
            year,
            item_type,
            direction,
            adjustment_amount,
            accounting_amount=accounting_amount,
            tax_amount=tax_amount,
            basis=basis,
            source=source,
            note=note,
        )
    )


def update_adjustment(adjustment_id: str, patch: dict) -> dict:
    module = _require("ledger.settlement", "修改纳税调整")
    if not hasattr(module, "update_adjustment"):
        raise ValueError("修改纳税调整暂不可用：ledger.settlement.update_adjustment 未实现。")
    return dict(module.update_adjustment(str(adjustment_id), patch))


def delete_adjustment(adjustment_id: str) -> bool:
    module = _require("ledger.settlement", "删除纳税调整")
    if not hasattr(module, "delete_adjustment"):
        raise ValueError("删除纳税调整暂不可用：ledger.settlement.delete_adjustment 未实现。")
    return bool(module.delete_adjustment(str(adjustment_id)))


# =============================================================================
# 亏损弥补
# =============================================================================
def list_losses(year: str | int | None = None) -> list[dict]:
    module = _load("settlement")
    if module is not None and hasattr(module, "list_losses"):
        try:
            return list(module.list_losses(year))
        except TypeError:
            try:
                return list(module.list_losses())
            except Exception:  # noqa: BLE001
                return []
        except Exception:  # noqa: BLE001
            pass
    rows = _read_table("loss_carryforwards")
    if year is not None:
        rows = [row for row in rows if str(row.get("year") or "") == str(year)[:4]]
    return sorted(rows, key=lambda row: str(row.get("year") or ""))


def add_loss(year: str | int, loss_amount: float, *, used_amount: float = 0.0, note: str = "") -> dict:
    module = _require("ledger.settlement", "新增亏损记录")
    if not hasattr(module, "add_loss"):
        raise ValueError("新增亏损记录暂不可用：ledger.settlement.add_loss 未实现。")
    return dict(module.add_loss(year, loss_amount, used_amount=used_amount, note=note))


def update_loss(loss_id: str, patch: dict) -> dict:
    module = _require("ledger.settlement", "修改亏损记录")
    if not hasattr(module, "update_loss"):
        raise ValueError("修改亏损记录暂不可用：ledger.settlement.update_loss 未实现。")
    return dict(module.update_loss(str(loss_id), patch))


def delete_loss(loss_id: str) -> bool:
    module = _require("ledger.settlement", "删除亏损记录")
    if not hasattr(module, "delete_loss"):
        raise ValueError("删除亏损记录暂不可用：ledger.settlement.delete_loss 未实现。")
    return bool(module.delete_loss(str(loss_id)))


def loss_preview(year: str | int, current_taxable_income: float) -> dict:
    """本年度可弥补亏损测算（确定性引擎）。"""
    try:
        return dict(
            calc_loss_carryforward(
                [dict(row) for row in list_losses()],
                float(current_taxable_income or 0),
                year=str(year)[:4],
            )
        )
    except Exception:  # noqa: BLE001
        return {"lines": [], "total_used": 0.0, "remaining_loss": 0.0, "warnings": []}


# =============================================================================
# 汇算结果 / 检查清单
# =============================================================================
def compute_settlement(year: str | int) -> dict:
    module = _require("ledger.settlement", "汇算结果计算")
    if not hasattr(module, "compute_settlement"):
        raise ValueError("汇算结果计算暂不可用：ledger.settlement.compute_settlement 未实现。")
    return dict(module.compute_settlement(year))


def settlement_checklist(year: str | int, settlement: dict) -> list[dict]:
    module = _require("ledger.settlement", "汇算检查清单")
    if not hasattr(module, "settlement_checklist"):
        raise ValueError("汇算检查清单暂不可用：ledger.settlement.settlement_checklist 未实现。")
    return list(module.settlement_checklist(year, settlement))


# =============================================================================
# 年度申报辅助表
# =============================================================================
def build_cit_annual_return(year: str | int, *, settlement: dict | None = None) -> dict:
    module = _require("filing.cit_annual", "年度申报辅助表")
    if not hasattr(module, "build_cit_annual_return"):
        raise ValueError("年度申报辅助表暂不可用：filing.cit_annual.build_cit_annual_return 未实现。")
    return dict(module.build_cit_annual_return(year, settlement=settlement))


def export_cit_annual_excel(bundle: dict, path: str | Path | None = None) -> Path:
    module = _require("filing.cit_annual", "导出年度申报辅助表 Excel")
    if not hasattr(module, "export_cit_annual_excel"):
        raise ValueError("导出年度申报辅助表 Excel 暂不可用：filing.cit_annual.export_cit_annual_excel 未实现。")
    return Path(module.export_cit_annual_excel(bundle, path=path))


__all__ = [
    "ADJUSTMENT_DIRECTIONS",
    "SETTLEMENT_STATUSES",
    "ANNUAL_INPUT_KEYS",
    "ANNUAL_INPUT_DEFAULTS",
    "ADJUSTMENT_ITEM_PRESETS",
    "ledger_ready",
    "inputs_ready",
    "adjustments_ready",
    "losses_ready",
    "compute_ready",
    "checklist_ready",
    "annual_return_ready",
    "annual_export_ready",
    "any_ready",
    "missing_detail",
    "current_year",
    "year_options",
    "year_label",
    "sources",
    "loss_carryforward_years",
    "annual_ledger_data",
    "get_annual_inputs",
    "settlement_record",
    "save_annual_inputs",
    "mark_settlement_status",
    "adjustment_limits",
    "list_adjustments",
    "add_adjustment",
    "update_adjustment",
    "delete_adjustment",
    "list_losses",
    "add_loss",
    "update_loss",
    "delete_loss",
    "loss_preview",
    "compute_settlement",
    "settlement_checklist",
    "build_cit_annual_return",
    "export_cit_annual_excel",
]
