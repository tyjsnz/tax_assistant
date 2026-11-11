"""离职员工多申报工资：扫描、更正方案与导出（P1-E）。

数据来源：员工档案（``employees.leave_date``）+ 月度工资表（``payroll``）。
本模块只做识别与测算、生成操作单；实际更正申报在自然人电子税务局（扣缴端）完成。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import config
from engine.payroll_correction import calc_correction_plan, correction_periods
from engine.money import round2, yuan
from ledger import employees, payroll, store
from ledger.audit import log_user_action

DISCLAIMER = config.DISCLAIMER


def _suspect_rows_for(employee: dict[str, Any]) -> list[dict[str, Any]]:
    """返回该员工离职月份之后仍有申报/工资的工资行。"""
    leave_date = str(employee.get("leave_date") or "")
    if not leave_date:
        return []
    leave_month = leave_date[:7]
    rows = [
        row
        for row in payroll.list_payroll(None)
        if str(row.get("employee_id")) == str(employee.get("id")) and str(row.get("period")) > leave_month
    ]
    suspects: list[dict[str, Any]] = []
    for row in rows:
        income = round2(
            round2(row.get("gross_salary")) + round2(row.get("bonus")) + round2(row.get("allowance"))
        )
        tax = round2(row.get("tax_withheld"))
        if income > 0 or tax > 0:
            suspects.append({**row, "income_total": income})
    return suspects


def scan_departed_overpayments() -> dict[str, Any]:
    """扫描「已离职但离职后月份仍有工资/个税申报」的员工。

    返回 ``{"items": [...], "summary": {"employees", "months", "income", "tax", "status_counts"}}``。
    """
    items: list[dict[str, Any]] = []
    for employee in employees.list_employees(status=None, include_left=True):
        leave_date = str(employee.get("leave_date") or "")
        if not leave_date:
            continue
        suspects = _suspect_rows_for(employee)
        if not suspects:
            continue
        income = round2(sum(item["income_total"] for item in suspects))
        tax = round2(sum(round2(item.get("tax_withheld")) for item in suspects))
        periods = sorted(str(item.get("period")) for item in suspects)
        items.append(
            {
                "employee_id": str(employee.get("id")),
                "employee_name": str(employee.get("name")),
                "leave_date": leave_date,
                "leave_month": leave_date[:7],
                "status": str(employee.get("status") or ""),
                "periods": periods,
                "month_count": len(periods),
                "income_total": income,
                "tax_withheld": tax,
                "row_statuses": sorted({str(item.get("status") or "") for item in suspects}),
                "rows": suspects,
            }
        )
    items.sort(key=lambda item: (-item["income_total"], item["employee_name"]))
    status_counts: dict[str, int] = {}
    for item in items:
        for status in item["row_statuses"]:
            status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "items": items,
        "summary": {
            "employees": len(items),
            "months": sum(item["month_count"] for item in items),
            "income": round2(sum(item["income_total"] for item in items)),
            "tax": round2(sum(item["tax_withheld"] for item in items)),
            "status_counts": status_counts,
        },
        "disclaimer": DISCLAIMER,
    }


def build_correction_plan(employee_id: str, *, late_fee_days: int = 0) -> dict[str, Any]:
    """为一个离职员工生成更正方案（逐月重算 + 企业所得税/社保影响 + 操作步骤）。"""
    employee = employees.get_employee(employee_id)
    if not employee:
        raise ValueError(f"未找到员工 {employee_id}")
    rows = [row for row in payroll.list_payroll(None) if str(row.get("employee_id")) == str(employee_id)]
    plan = calc_correction_plan(employee, rows, late_fee_days=late_fee_days)
    log_user_action(
        "生成离职多申报更正方案",
        f"{employee.get('name')}（{employee_id}）：更正 {plan['totals']['months_to_correct']} 个月，"
        f"预计多缴个税 {yuan(plan['totals']['tax_refund'])} 元",
    )
    return plan


def scan_and_plan_all() -> list[dict[str, Any]]:
    """扫描并为所有异常员工生成更正方案（供 CLI 批量导出）。"""
    scan = scan_departed_overpayments()
    return [build_correction_plan(item["employee_id"]) for item in scan["items"]]


def export_correction_plan(plan: dict[str, Any], path: str | Path | None = None) -> Path:
    """导出《离职员工多申报更正操作单》Excel（概览 + 逐月明细 + 操作步骤/风险）。"""
    from openpyxl import Workbook

    employee_name = str(plan.get("employee_name") or "员工")
    target = Path(path) if path else Path(config.EXPORT_DIR) / f"离职多申报更正操作单_{employee_name}.xlsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "更正概览"
    ws.append([f"离职员工多申报工资更正操作单 · {employee_name}"])
    ws.append(["系统只做核对与测算，实际更正申报请在自然人电子税务局（扣缴端）完成"])
    ws.append([DISCLAIMER])
    ws.append([])
    totals = plan.get("totals") or {}
    ws.append(["员工", employee_name])
    ws.append(["离职日期", plan.get("leave_date")])
    ws.append(["保留月份", "、".join(plan.get("kept_periods") or [])])
    ws.append(["需更正月份", "、".join(plan.get("removed_periods") or [])])
    ws.append(["原申报收入合计", totals.get("original_income")])
    ws.append(["更正后收入合计", totals.get("corrected_income")])
    ws.append(["原已预扣个税", totals.get("original_tax")])
    ws.append(["更正后应预扣个税", totals.get("corrected_tax")])
    ws.append(["预计多缴个税（可退）", totals.get("tax_refund")])
    ws.append(["预计少缴个税（需补）", totals.get("tax_payable")])
    ws.append(["涉及多列工资", totals.get("removed_wage_total")])
    cit = plan.get("cit_impact") or {}
    ws.append(["企业所得税影响（小微 5%）", cit.get("small_low_profit_tax")])
    ws.append(["企业所得税影响（一般 25%）", cit.get("standard_tax")])
    ws.append(["社保公积金个人部分退费参考", plan.get("social_refund_reference")])
    ws.append([])
    for warning in plan.get("warnings") or []:
        ws.append(["提示", warning])

    ws2 = wb.create_sheet("逐月更正明细")
    ws2.append(["所属期", "处理动作", "原申报收入", "更正后收入", "原已预扣", "更正后应预扣", "差额", "累计收入", "已申报月份", "说明"])
    for row in plan.get("rows") or []:
        ws2.append([
            row.get("period"), row.get("action"), row.get("original_income"), row.get("corrected_income"),
            row.get("original_tax"), row.get("corrected_tax"), row.get("tax_diff"),
            row.get("cumulative_income"), row.get("months_declared"), row.get("note"),
        ])

    ws3 = wb.create_sheet("操作步骤与留痕")
    ws3.append(["操作步骤"])
    for index, step in enumerate(plan.get("steps") or [], start=1):
        ws3.append([index, step])
    ws3.append([])
    ws3.append(["申诉与留痕"])
    ws3.append([plan.get("dispute_note") or ""])
    ws3.append([])
    ws3.append(["风险提示"])
    for warning in plan.get("warnings") or []:
        ws3.append([warning])
    wb.save(target)
    return target


# =============================================================================
# 个税异议申诉台账
# =============================================================================
DISPUTE_TABLE = "tax_disputes"
DISPUTE_STATUSES: tuple[str, ...] = ("待处理", "已沟通", "已撤销", "已更正", "已关闭")
DISPUTE_TYPES: tuple[str, ...] = ("任职受雇信息", "收入纳税明细", "其他")


def list_disputes(status: str | None = None) -> list[dict[str, Any]]:
    """列出申诉台账（可按状态过滤），按申诉日期倒序。"""
    rows = store.table(DISPUTE_TABLE).read()
    if status:
        rows = [row for row in rows if str(row.get("status")) == status]
    return sorted(rows, key=lambda row: str(row.get("filed_date") or ""), reverse=True)


def add_dispute(
    employee_id: str,
    dispute_type: str,
    filed_date: str,
    *,
    period: str = "",
    status: str = "待处理",
    resolution: str = "",
    note: str = "",
) -> dict[str, Any]:
    """新增一条个税异议申诉记录。"""
    employee = employees.get_employee(employee_id)
    if not employee:
        raise ValueError(f"未找到员工 {employee_id}")
    if dispute_type not in DISPUTE_TYPES:
        raise ValueError(f"申诉类型「{dispute_type}」不合法，可选：{'、'.join(DISPUTE_TYPES)}")
    if status not in DISPUTE_STATUSES:
        raise ValueError(f"处理状态「{status}」不合法，可选：{'、'.join(DISPUTE_STATUSES)}")
    record = store.table(DISPUTE_TABLE).append(
        {
            "employee_id": employee_id,
            "employee_name": str(employee.get("name") or ""),
            "period": str(period or ""),
            "dispute_type": dispute_type,
            "filed_date": str(filed_date or ""),
            "status": status,
            "withdrawn_date": "",
            "resolution": str(resolution or ""),
            "note": str(note or ""),
        }
    )
    log_user_action("登记个税异议申诉", f"{employee.get('name')}：{dispute_type}（{filed_date}）")
    return record


def update_dispute(dispute_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """更新申诉记录（状态/撤销日期/处理结果/备注）。"""
    allowed = {"period", "dispute_type", "filed_date", "status", "withdrawn_date", "resolution", "note"}
    clean = {key: value for key, value in patch.items() if key in allowed}
    if "status" in clean and clean["status"] not in DISPUTE_STATUSES:
        raise ValueError(f"处理状态「{clean['status']}」不合法，可选：{'、'.join(DISPUTE_STATUSES)}")
    if "dispute_type" in clean and clean["dispute_type"] not in DISPUTE_TYPES:
        raise ValueError(f"申诉类型「{clean['dispute_type']}」不合法，可选：{'、'.join(DISPUTE_TYPES)}")
    if "status" in clean and clean["status"] == "已撤销" and not clean.get("withdrawn_date"):
        clean["withdrawn_date"] = str(patch.get("withdrawn_date") or config.today().isoformat())
    record = store.table(DISPUTE_TABLE).update(dispute_id, clean)
    log_user_action("更新个税异议申诉", f"{dispute_id}：{clean}")
    return record


def delete_dispute(dispute_id: str) -> bool:
    """删除申诉记录（仅用于录错更正）。"""
    deleted = store.table(DISPUTE_TABLE).delete(dispute_id)
    if deleted:
        log_user_action("删除个税异议申诉", dispute_id)
    return deleted
