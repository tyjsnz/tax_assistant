"""个税扣缴（员工与工资）后端访问封装。

覆盖三层后端（由队友并行实现，缺失时用 :mod:`ui.components.runtime` 容错降级）：

* ``ledger.employees``：员工档案 CRUD、脱敏、专项附加扣除；
* ``ledger.payroll``：月度工资表生成 / 编辑 / 累计预扣重算 / 确认凭证 / 零申报 / 申报标记；
* ``filing.iit_return``：扣缴申报辅助表 bundle 与 Excel 导出。

约定
----
* 读列表在模块缺失时降级为直接读 ``ledger.store.table``（页面仍可查看历史数据）；
* 写操作在后端缺失时抛中文 ``ValueError``；页面用「按钮禁用 + 模块提示」兜底；
* 专项附加扣除的**金额计算**直接复用 ``engine.calc_special_deduction``（确定性引擎），
  保证 UI 展示口径与后端一致。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Iterable

import config
import ledger
from engine import (
    BASIC_DEDUCTION_PER_MONTH,
    ZERO_DECLARATION_NOTE,
    calc_special_deduction,
    current_month,
    get_iit_sources,
    get_rate_table,
    get_special_deduction_standards,
    month_options,
)
from engine import iit as iit_engine
from ledger import models
from ui.components import runtime

#: 枚举（来自 ledger.models，只读引用）
EMPLOYEE_STATUSES = models.EMPLOYEE_STATUSES
PAYROLL_STATUSES = models.PAYROLL_STATUSES
BASIC_DEDUCTION_CHOICES = models.BASIC_DEDUCTION_CHOICES

#: 专项附加扣除明细的可选项（UI 下拉）
CONTINUING_EDUCATION_OPTIONS = ["不享受", "学历", "职业资格"]
HOUSING_OPTIONS = [
    "不享受",
    "住房贷款利息",
    "住房租金-直辖市/省会/计划单列市",
    "住房租金-市辖区户籍人口>100万",
    "住房租金-其他",
]
ELDERLY_OPTIONS = ["不享受", "独生", "非独生（分摊1500）"]

#: 住房租金档位 → engine 口径
HOUSING_RENT_TIERS = {
    "住房租金-直辖市/省会/计划单列市": "直辖市/省会/计划单列市",
    "住房租金-市辖区户籍人口>100万": "市辖区户籍人口>100万",
    "住房租金-其他": "其他",
}

#: 工资表可编辑字段（中文标签 → 字段名）
EDITABLE_PAYROLL_FIELDS: list[tuple[str, str]] = [
    ("应发工资", "gross_salary"),
    ("奖金", "bonus"),
    ("补贴", "allowance"),
    ("个人社保", "social_insurance"),
    ("个人公积金", "housing_fund"),
    ("专项附加扣除", "special_deduction"),
    ("其他扣除", "other_deduction"),
]

#: 工资表可编辑数字字段名集合
EDITABLE_FIELDS = {field for _, field in EDITABLE_PAYROLL_FIELDS}

#: 零申报行的员工编号
ZERO_EMPLOYEE_ID = "ZERO"


# =============================================================================
# 模块加载与就绪判断
# =============================================================================
def _employees_module():
    return runtime.load("ledger.employees")


def _payroll_module():
    return runtime.load("ledger.payroll")


def _iit_return_module():
    return runtime.load("filing.iit_return")


def employees_ready() -> bool:
    module = _employees_module()
    return module is not None and hasattr(module, "add_employee")


def payroll_ready() -> bool:
    module = _payroll_module()
    return module is not None and hasattr(module, "build_payroll")


def recalc_ready() -> bool:
    module = _payroll_module()
    return module is not None and hasattr(module, "recalc_period")


def confirm_ready() -> bool:
    module = _payroll_module()
    return module is not None and hasattr(module, "confirm_period")


def declare_ready() -> bool:
    module = _payroll_module()
    return module is not None and hasattr(module, "mark_period_declared")


def zero_declaration_ready() -> bool:
    module = _payroll_module()
    return module is not None and hasattr(module, "mark_zero_declaration")


def iit_return_ready() -> bool:
    module = _iit_return_module()
    return module is not None and hasattr(module, "build_iit_return")


def iit_export_ready() -> bool:
    module = _iit_return_module()
    return module is not None and hasattr(module, "export_iit_excel")


def any_ready() -> bool:
    """任一后端能力是否就绪。"""
    return any(
        (
            employees_ready(),
            payroll_ready(),
            iit_return_ready(),
        )
    )


def missing_detail() -> str:
    return (
        runtime.error_of("ledger.employees")
        or runtime.error_of("ledger.payroll")
        or runtime.error_of("filing.iit_return")
        or ""
    )


def _require(dotted: str, feature: str):
    module = runtime.load(dotted)
    if module is None:
        raise ValueError(f"{feature}暂不可用：{dotted} 模块尚未就绪（后端由队友并行开发中）。")
    return module


# =============================================================================
# 通用工具
# =============================================================================
def _read_table(name: str) -> list[dict]:
    try:
        return list(ledger.table(name).read())
    except Exception:  # noqa: BLE001
        return []


def month_options_list(count: int = 12) -> list[str]:
    """最近若干月份（新→旧），如 ``2026-11``。"""
    try:
        return list(month_options(count=count))
    except Exception:  # noqa: BLE001
        today = date.today()
        result = []
        year, month = today.year, today.month
        for _ in range(count):
            result.append(f"{year}-{month:02d}")
            month -= 1
            if month == 0:
                year, month = year - 1, 12
        return result


def current_month_key() -> str:
    try:
        return str(current_month())
    except Exception:  # noqa: BLE001
        today = date.today()
        return f"{today.year}-{today.month:02d}"


def month_label(month: str) -> str:
    """``2026-11`` → ``2026年11月``。"""
    text = str(month or "").strip()
    if len(text) >= 7 and text[4] == "-":
        try:
            return f"{int(text[:4])}年{int(text[5:7])}月"
        except ValueError:
            return text
    return text or "—"


def zero_declaration_note() -> str:
    return str(ZERO_DECLARATION_NOTE)


def basic_deduction_per_month() -> float:
    return float(BASIC_DEDUCTION_PER_MONTH)


def rate_table() -> list[dict]:
    """累计预扣率表（引擎已实现）。"""
    try:
        return list(get_rate_table())
    except Exception:  # noqa: BLE001
        return []


def special_standards() -> dict:
    try:
        return dict(get_special_deduction_standards())
    except Exception:  # noqa: BLE001
        return {}


def iit_sources() -> list[dict]:
    try:
        return list(get_iit_sources())
    except Exception:  # noqa: BLE001
        return []


def tax_rate_for(taxable: Any) -> str:
    """按累计应纳税所得额展示适用预扣率（仅展示用，确定性查表）。"""
    try:
        amount = float(taxable or 0)
    except (TypeError, ValueError):
        amount = 0.0
    rate = 0.45
    for upper, candidate, _quick in iit_engine.IIT_RATE_TABLE:
        if amount <= upper:
            rate = candidate
            break
    return f"{rate * 100:.2f}%"


# =============================================================================
# 员工档案
# =============================================================================
def list_employees(status: str | None = "在职", *, include_left: bool = False) -> list[dict]:
    """员工列表（默认在职；后端缺失时读 employees 表兜底）。"""
    module = _employees_module()
    if module is not None and hasattr(module, "list_employees"):
        try:
            return list(module.list_employees(status=status, include_left=include_left))
        except TypeError:
            try:
                rows = list(module.list_employees())
            except Exception:  # noqa: BLE001
                rows = []
            return _filter_employee_rows(rows, status, include_left)
        except Exception:  # noqa: BLE001
            pass
    return _filter_employee_rows(_read_table("employees"), status, include_left)


def _filter_employee_rows(rows: Iterable[dict], status: str | None, include_left: bool) -> list[dict]:
    records = [dict(row) for row in rows]
    if status and status not in ("全部", ""):
        records = [row for row in records if str(row.get("status") or "在职") == status]
    elif not include_left:
        records = [row for row in records if str(row.get("status") or "在职") != "离职"]
    return sorted(records, key=lambda row: (str(row.get("employee_no") or ""), str(row.get("name") or "")))


def get_employee(employee_id: str) -> dict | None:
    module = _employees_module()
    if module is not None and hasattr(module, "get_employee"):
        try:
            record = module.get_employee(str(employee_id))
            return dict(record) if record else None
        except Exception:  # noqa: BLE001
            pass
    for row in _read_table("employees"):
        if str(row.get("id")) == str(employee_id):
            return dict(row)
    return None


def add_employee(payload: dict) -> dict:
    module = _require("ledger.employees", "员工档案")
    if not hasattr(module, "add_employee"):
        raise ValueError("员工档案暂不可用：ledger.employees.add_employee 未实现。")
    return dict(module.add_employee(payload))


def update_employee(employee_id: str, patch: dict) -> dict:
    module = _require("ledger.employees", "员工档案")
    if not hasattr(module, "update_employee"):
        raise ValueError("员工档案暂不可用：ledger.employees.update_employee 未实现。")
    return dict(module.update_employee(str(employee_id), patch))


def delete_employee(employee_id: str) -> bool:
    module = _require("ledger.employees", "员工档案")
    if not hasattr(module, "delete_employee"):
        raise ValueError("员工档案暂不可用：ledger.employees.delete_employee 未实现。")
    return bool(module.delete_employee(str(employee_id)))


def mask_id_card(value: str) -> str:
    """身份证号脱敏（前端兜底；后端同样只保存脱敏值）。"""
    text = str(value or "").strip().replace(" ", "")
    if not text:
        return ""
    if len(text) <= 10:
        return text[:2] + "*" * max(len(text) - 2, 1)
    return f"{text[:6]}********{text[-4:]}"


def summarize_employees() -> dict:
    """员工汇总：在职/离职人数、部门分布、社保与公积金基数合计、专项附加合计。"""
    module = _employees_module()
    if module is not None and hasattr(module, "summarize_employees"):
        try:
            result = dict(module.summarize_employees())
            # 契约只要求社保/公积金基数合计；专项附加合计由 UI 侧用引擎补齐
            if "total_special_deduction" not in result:
                active = [row for row in _read_table("employees") if str(row.get("status") or "在职") != "离职"]
                result["total_special_deduction"] = round(
                    sum(monthly_special_deduction(row) for row in active), 2
                )
            return result
        except Exception:  # noqa: BLE001
            pass
    rows = _read_table("employees")
    active_rows = [row for row in rows if str(row.get("status") or "在职") != "离职"]
    by_department: dict[str, int] = {}
    for row in active_rows:
        department = str(row.get("department") or "未分配")
        by_department[department] = by_department.get(department, 0) + 1
    total_social = round(sum(float(row.get("social_insurance_base") or 0) for row in active_rows), 2)
    total_fund = round(sum(float(row.get("housing_fund_base") or 0) for row in active_rows), 2)
    total_special = round(sum(monthly_special_deduction(row) for row in active_rows), 2)
    return {
        "total": len(rows),
        "active": len(active_rows),
        "left": len(rows) - len(active_rows),
        "by_department": by_department,
        "total_social_base": total_social,
        "total_housing_fund_base": total_fund,
        "total_special_deduction": total_special,
    }


# =============================================================================
# 专项附加扣除（UI 表单 ↔ 明细 dict）
# =============================================================================
def special_deduction_json(
    *,
    children: int = 0,
    infants: int = 0,
    continuing_education: str = "不享受",
    housing: str = "不享受",
    elderly: str = "不享受",
) -> dict:
    """把 UI 表单字段组装为 ``special_deduction_json`` 明细 dict。"""
    payload: dict[str, Any] = {}
    try:
        children_count = max(int(children or 0), 0)
        infant_count = max(int(infants or 0), 0)
    except (TypeError, ValueError):
        children_count = infant_count = 0
    if children_count:
        payload["children_education_children"] = children_count
    if infant_count:
        payload["infant_care_children"] = infant_count
    if continuing_education in ("学历", "职业资格"):
        payload["continuing_education"] = continuing_education
    if housing == "住房贷款利息":
        payload["housing_loan_interest"] = True
    elif housing in HOUSING_RENT_TIERS:
        payload["housing_rent"] = HOUSING_RENT_TIERS[housing]
    if elderly == "独生":
        payload["elderly_care"] = "独生"
    elif elderly == "非独生（分摊1500）":
        payload["elderly_care"] = 1500.0
    return payload


def parse_special_deduction(employee: dict) -> dict:
    """把员工的 ``special_deduction_json`` 还原为 UI 表单字段。"""
    raw = employee.get("special_deduction_json") or {}
    if not isinstance(raw, dict):
        raw = {}
    try:
        children = int(raw.get("children_education_children") or 0)
    except (TypeError, ValueError):
        children = 0
    try:
        infants = int(raw.get("infant_care_children") or 0)
    except (TypeError, ValueError):
        infants = 0
    education = raw.get("continuing_education")
    continuing = "学历" if education in ("学历", True) else (
        "职业资格" if education else "不享受"
    )
    if raw.get("housing_loan_interest"):
        housing = "住房贷款利息"
    else:
        rent = str(raw.get("housing_rent") or "")
        housing = next((label for label, tier in HOUSING_RENT_TIERS.items() if tier == rent), "不享受")
    elderly_raw = raw.get("elderly_care")
    if str(elderly_raw) == "独生":
        elderly = "独生"
    elif elderly_raw:
        elderly = "非独生（分摊1500）"
    else:
        elderly = "不享受"
    return {
        "children": children,
        "infants": infants,
        "continuing_education": continuing,
        "housing": housing,
        "elderly": elderly,
    }


def monthly_special_deduction(employee: dict) -> float:
    """员工月度专项附加扣除合计（确定性引擎计算）。"""
    module = _employees_module()
    if module is not None and hasattr(module, "special_deduction_monthly"):
        try:
            return float(module.special_deduction_monthly(employee))
        except Exception:  # noqa: BLE001
            pass
    raw = employee.get("special_deduction_json") or {}
    if not isinstance(raw, dict):
        raw = {}
    try:
        return float(calc_special_deduction(raw).get("monthly_total") or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def special_deduction_text(employee: dict) -> str:
    """员工专项附加扣除的展示文案（如「子女教育 2,000 + 赡养老人 3,000」）。"""
    raw = employee.get("special_deduction_json") or {}
    if not isinstance(raw, dict):
        raw = {}
    try:
        result = calc_special_deduction(raw)
    except Exception:  # noqa: BLE001
        return "—"
    items = result.get("items") or []
    if not items:
        return "—"
    return " + ".join(
        f"{item.get('label')} {float(item.get('monthly') or 0):,.0f}" for item in items
    )


# =============================================================================
# 月度工资表
# =============================================================================
def list_payroll(period: str | None = None) -> list[dict]:
    """工资表记录（后端缺失时读 payroll 表兜底）。"""
    module = _payroll_module()
    if module is not None and hasattr(module, "list_payroll"):
        try:
            return list(module.list_payroll(period=period))
        except TypeError:
            try:
                rows = list(module.list_payroll())
            except Exception:  # noqa: BLE001
                rows = []
            return [row for row in rows if not period or str(row.get("period")) == str(period)]
        except Exception:  # noqa: BLE001
            pass
    rows = _read_table("payroll")
    if period:
        rows = [dict(row) for row in rows if str(row.get("period")) == str(period)]
    return sorted(rows, key=lambda row: str(row.get("employee_name") or ""))


def build_payroll(period: str, *, employee_ids: list[str] | None = None) -> dict:
    module = _require("ledger.payroll", "生成工资表")
    if not hasattr(module, "build_payroll"):
        raise ValueError("生成工资表暂不可用：ledger.payroll.build_payroll 未实现。")
    return dict(module.build_payroll(period, employee_ids=employee_ids))


def recalc_period(period: str) -> dict:
    module = _require("ledger.payroll", "重新计算个税")
    if not hasattr(module, "recalc_period"):
        raise ValueError("重新计算个税暂不可用：ledger.payroll.recalc_period 未实现。")
    return dict(module.recalc_period(period))


def confirm_period(period: str, *, post_journal: bool = True) -> dict:
    module = _require("ledger.payroll", "确认工资表")
    if not hasattr(module, "confirm_period"):
        raise ValueError("确认工资表暂不可用：ledger.payroll.confirm_period 未实现。")
    return dict(module.confirm_period(period, post_journal=bool(post_journal)))


def mark_period_declared(period: str) -> dict:
    module = _require("ledger.payroll", "标记已申报")
    if not hasattr(module, "mark_period_declared"):
        raise ValueError("标记已申报暂不可用：ledger.payroll.mark_period_declared 未实现。")
    return dict(module.mark_period_declared(period))


def mark_zero_declaration(period: str) -> dict:
    module = _require("ledger.payroll", "本期零申报")
    if not hasattr(module, "mark_zero_declaration"):
        raise ValueError("本期零申报暂不可用：ledger.payroll.mark_zero_declaration 未实现。")
    return dict(module.mark_zero_declaration(period))


def update_payroll_row(payroll_id: str, patch: dict) -> dict:
    module = _require("ledger.payroll", "工资表编辑")
    if not hasattr(module, "update_payroll_row"):
        raise ValueError("工资表编辑暂不可用：ledger.payroll.update_payroll_row 未实现。")
    allowed = {key: value for key, value in patch.items() if key in EDITABLE_FIELDS or key == "note"}
    return dict(module.update_payroll_row(str(payroll_id), allowed))


def delete_payroll_row(payroll_id: str) -> bool:
    module = _require("ledger.payroll", "删除工资行")
    if not hasattr(module, "delete_payroll_row"):
        raise ValueError("删除工资行暂不可用：ledger.payroll.delete_payroll_row 未实现。")
    return bool(module.delete_payroll_row(str(payroll_id)))


def summarize_period(period: str) -> dict:
    """期间工资汇总（后端优先，缺失时本地按工资行计算）。"""
    module = _payroll_module()
    if module is not None and hasattr(module, "summarize_period"):
        try:
            return dict(module.summarize_period(period))
        except Exception:  # noqa: BLE001
            pass
    rows = list_payroll(period)
    paid_rows = [row for row in rows if str(row.get("employee_id")) != ZERO_EMPLOYEE_ID]
    status_counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "草稿")
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "period": period,
        "headcount": len(paid_rows),
        "total_gross": round(sum(float(row.get("gross_salary") or 0) + float(row.get("bonus") or 0) + float(row.get("allowance") or 0) for row in rows), 2),
        "total_social": round(sum(float(row.get("social_insurance") or 0) for row in rows), 2),
        "total_housing_fund": round(sum(float(row.get("housing_fund") or 0) for row in rows), 2),
        "total_special": round(sum(float(row.get("special_deduction") or 0) for row in rows), 2),
        "total_taxable": round(sum(float(row.get("cumulative_taxable") or 0) for row in rows), 2),
        "total_tax": round(sum(float(row.get("tax_withheld") or 0) for row in rows), 2),
        "total_net": round(sum(float(row.get("net_pay") or 0) for row in rows), 2),
        "declared": bool(rows) and all(str(row.get("status")) == "已申报" for row in rows if str(row.get("employee_id")) != ZERO_EMPLOYEE_ID) if paid_rows else False,
        "status_counts": status_counts,
    }


def payroll_periods(limit: int = 12) -> list[str]:
    module = _payroll_module()
    if module is not None and hasattr(module, "payroll_periods"):
        try:
            return list(module.payroll_periods(limit=limit))
        except Exception:  # noqa: BLE001
            pass
    periods = sorted({str(row.get("period")) for row in _read_table("payroll") if row.get("period")}, reverse=True)
    return periods[:limit]


def cumulative_for_employee(employee_id: str, period: str) -> dict:
    module = _require("ledger.payroll", "累计预扣计算")
    if not hasattr(module, "cumulative_for_employee"):
        raise ValueError("累计预扣计算暂不可用：ledger.payroll.cumulative_for_employee 未实现。")
    return dict(module.cumulative_for_employee(str(employee_id), period))


# =============================================================================
# 扣缴申报辅助表
# =============================================================================
def build_iit_return(period: str, *, rows: list[dict] | None = None) -> dict:
    module = _require("filing.iit_return", "扣缴申报辅助表")
    if not hasattr(module, "build_iit_return"):
        raise ValueError("扣缴申报辅助表暂不可用：filing.iit_return.build_iit_return 未实现。")
    return dict(module.build_iit_return(period, rows=rows))


def export_iit_excel(bundle: dict, path: str | Path | None = None) -> Path:
    module = _require("filing.iit_return", "导出扣缴辅助表 Excel")
    if not hasattr(module, "export_iit_excel"):
        raise ValueError("导出扣缴辅助表 Excel 暂不可用：filing.iit_return.export_iit_excel 未实现。")
    return Path(module.export_iit_excel(bundle, path=path))


__all__ = [
    "EMPLOYEE_STATUSES",
    "PAYROLL_STATUSES",
    "BASIC_DEDUCTION_CHOICES",
    "CONTINUING_EDUCATION_OPTIONS",
    "HOUSING_OPTIONS",
    "ELDERLY_OPTIONS",
    "EDITABLE_PAYROLL_FIELDS",
    "EDITABLE_FIELDS",
    "ZERO_EMPLOYEE_ID",
    "employees_ready",
    "payroll_ready",
    "recalc_ready",
    "confirm_ready",
    "declare_ready",
    "zero_declaration_ready",
    "iit_return_ready",
    "iit_export_ready",
    "any_ready",
    "missing_detail",
    "month_options_list",
    "current_month_key",
    "month_label",
    "zero_declaration_note",
    "basic_deduction_per_month",
    "rate_table",
    "special_standards",
    "iit_sources",
    "tax_rate_for",
    "list_employees",
    "get_employee",
    "add_employee",
    "update_employee",
    "delete_employee",
    "mask_id_card",
    "summarize_employees",
    "special_deduction_json",
    "parse_special_deduction",
    "monthly_special_deduction",
    "special_deduction_text",
    "list_payroll",
    "build_payroll",
    "recalc_period",
    "confirm_period",
    "mark_period_declared",
    "mark_zero_declaration",
    "update_payroll_row",
    "delete_payroll_row",
    "summarize_period",
    "payroll_periods",
    "cumulative_for_employee",
    "build_iit_return",
    "export_iit_excel",
]
