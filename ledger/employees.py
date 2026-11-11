"""员工档案：个税扣缴与工资表的基础数据（CRUD、脱敏与汇总）。

安全约定
--------
* **身份证号只保存脱敏值**（前 6 位 + ``****`` + 后 4 位），任何入口传入身份证原文都会先脱敏再落盘；
* 姓名必填；日期、状态、减除费用方式等非法值抛中文 ``ValueError``；
* 离职不删除历史工资记录：应把状态改为「离职」；存在工资记录的员工禁止删除；
* 专项附加扣除按 ``engine.calc_special_deduction`` 的月度标准计算。
"""

from __future__ import annotations

from typing import Any

import config
from engine.iit import calc_special_deduction
from engine.money import round2
from ledger import models
from ledger.audit import log_user_action
from ledger.store import table

TABLE_NAME = "employees"
TABLE_LABEL = "员工档案"

_FIELD_DEFAULTS: dict[str, Any] = {
    "employee_no": "",
    "name": "",
    "id_card_masked": "",
    "phone": "",
    "department": "",
    "position": "",
    "hire_date": "",
    "leave_date": "",
    "status": "在职",
    "social_insurance_base": 0.0,
    "housing_fund_base": 0.0,
    "special_deduction_json": {},
    "deduction_choice": "累计预扣",
    "is_deduction_qualified": True,
    "note": "",
}


# =============================================================================
# 内部工具
# =============================================================================
def _text(value: Any) -> str:
    return str(value or "").strip()


def _money(value: Any, label: str) -> float:
    amount = round2(value)
    if amount < 0:
        raise ValueError(f"{label}不能为负数")
    return amount


def _enum(value: Any, choices: tuple[str, ...], default: str, label: str) -> str:
    """枚举校验：空白取默认值，非法值抛中文 ``ValueError``。"""
    text = _text(value)
    if not text:
        return default
    if text in choices:
        return text
    for choice in choices:
        if text in choice or choice in text:
            return choice
    raise ValueError(f"{label}「{text}」不合法，可选：{'、'.join(choices)}")


def _non_empty_date(value: Any) -> str:
    """日期规范化；空值返回空字符串（不擅自填今天）。"""
    if value is None or _text(value) == "":
        return ""
    return models.normalize_date(value)


def _special_deduction_dict(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError("专项附加扣除明细必须是 dict（如 {\"children_education_children\": 1}）")
    return dict(value)


def mask_id_card(value: Any) -> str:
    """身份证号脱敏：前 6 位 + ``****`` + 后 4 位；已是脱敏值则原样返回。"""
    text = _text(value).replace(" ", "").replace("\u3000", "")
    if not text:
        return ""
    if "*" in text:
        return text
    if len(text) >= 10:
        return f"{text[:6]}****{text[-4:]}"
    if len(text) > 4:
        return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"
    return "*" * len(text)


def special_deduction_monthly(employee: dict[str, Any]) -> float:
    """按员工专项附加扣除明细计算月度可扣除金额（调确定性引擎）。"""
    if not isinstance(employee, dict):
        return 0.0
    breakdown = employee.get("special_deduction_json")
    if not isinstance(breakdown, dict):
        breakdown = {}
    return round2(calc_special_deduction(breakdown)["monthly_total"])


def _validate_leave_date(record: dict[str, Any]) -> None:
    """员工状态为「离职」时必须填写离职日期：用于停发工资、个税更正与扫描依据。"""
    if str(record.get("status") or "") == "离职" and not str(record.get("leave_date") or ""):
        raise ValueError(
            "员工状态为「离职」时必须填写离职日期（实际最后工作日）。"
            "离职日期用于停发工资、个税更正与离职后申报扫描；如确实忘记，请按银行流水/考勤/社保减员月份补录。"
        )


def _build_record(payload: dict[str, Any], *, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """把新增/修改的入参规范为可写库的员工记录。"""
    if not isinstance(payload, dict):
        raise ValueError("员工数据必须是 dict")
    source = dict(base or {})
    for key, value in payload.items():
        if key in ("id", "created_at", "updated_at", "id_card"):
            continue
        source[key] = value

    name = _text(source.get("name"))
    if not name:
        raise ValueError("员工姓名不能为空")
    status = _enum(source.get("status"), models.EMPLOYEE_STATUSES, "在职", "员工状态")
    hire_date_raw = source.get("hire_date")
    if _text(hire_date_raw):
        hire_date = models.normalize_date(hire_date_raw)
    elif base and _text(base.get("hire_date")):
        hire_date = _text(base.get("hire_date"))
    else:
        hire_date = models.normalize_date(None)
    leave_date = _non_empty_date(source.get("leave_date"))

    id_card_source = payload.get("id_card", source.get("id_card_masked"))
    id_card_masked = mask_id_card(id_card_source)

    return {
        "employee_no": _text(source.get("employee_no")),
        "name": name,
        "id_card_masked": id_card_masked,
        "phone": _text(source.get("phone")),
        "department": _text(source.get("department")),
        "position": _text(source.get("position")),
        "hire_date": hire_date,
        "leave_date": leave_date,
        "status": status,
        "social_insurance_base": _money(source.get("social_insurance_base"), "社保缴费基数"),
        "housing_fund_base": _money(source.get("housing_fund_base"), "公积金缴费基数"),
        "special_deduction_json": _special_deduction_dict(source.get("special_deduction_json")),
        "deduction_choice": _enum(
            source.get("deduction_choice"), models.BASIC_DEDUCTION_CHOICES, "累计预扣", "减除费用方式"
        ),
        "is_deduction_qualified": models.parse_bool(source.get("is_deduction_qualified"), default=True),
        "note": _text(source.get("note")),
    }


# =============================================================================
# CRUD
# =============================================================================
def add_employee(payload: dict[str, Any]) -> dict[str, Any]:
    """新增员工档案，返回保存后的记录；姓名必填，身份证只保存脱敏值。"""
    record = _build_record(payload)
    _validate_leave_date(record)
    added = table(TABLE_NAME).append(record)
    log_user_action(
        "新增员工",
        f"{added['name']}（{added.get('employee_no') or added['id']}），"
        f"{added.get('department') or '未填部门'}，状态 {added['status']}",
    )
    return added


def list_employees(status: str | None = "在职", *, include_left: bool = False) -> list[dict[str, Any]]:
    """查询员工档案。

    ``status`` 为空/``全部`` 时返回全部；``include_left=True`` 时在「在职」基础上把离职员工一并返回。
    """
    rows = table(TABLE_NAME).read()
    wanted = _text(status)
    if wanted in ("", "全部"):
        result = list(rows)
    else:
        wanted = _enum(wanted, models.EMPLOYEE_STATUSES, "在职", "员工状态")
        result = [row for row in rows if _text(row.get("status")) == wanted]
        if include_left and wanted == "在职":
            result += [row for row in rows if _text(row.get("status")) == "离职"]
    result.sort(key=lambda row: (_text(row.get("employee_no")) or str(row.get("id") or ""), str(row.get("name") or "")))
    return result


def get_employee(employee_id: str) -> dict[str, Any] | None:
    """按编号读取员工档案，不存在返回 ``None``。"""
    return table(TABLE_NAME).get(employee_id)


def update_employee(employee_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """修改员工档案，返回更新后的记录；状态改为「离职」时自动补离职日期。"""
    if not isinstance(patch, dict):
        raise ValueError("更新内容必须是 dict")
    current = get_employee(employee_id)
    if current is None:
        raise KeyError(f"{TABLE_LABEL}中不存在编号 {employee_id}")
    payload = {key: value for key, value in patch.items() if key not in ("id", "created_at", "updated_at")}
    if _text(payload.get("status")) == "在职" and "leave_date" not in payload:
        payload["leave_date"] = ""
    record = _build_record(payload, base=current)
    _validate_leave_date(record)
    updated = table(TABLE_NAME).update(employee_id, record)
    log_user_action(
        "修改员工",
        f"编号 {employee_id}：{updated['name']}，状态 {updated['status']}"
        + (f"，离职日期 {updated['leave_date']}" if updated.get("leave_date") else ""),
    )
    return updated


def delete_employee(employee_id: str) -> bool:
    """删除员工档案；已有工资记录的员工不允许删除（应改为离职）。"""
    current = get_employee(employee_id)
    if current is None:
        return False
    payroll_rows = [
        row for row in table("payroll").read() if str(row.get("employee_id") or "") == str(employee_id)
    ]
    if payroll_rows:
        raise ValueError(
            f"员工「{current.get('name') or employee_id}」已有 {len(payroll_rows)} 条工资记录，不能删除；"
            "请把状态改为「离职」以保留历史数据。"
        )
    deleted = table(TABLE_NAME).delete(employee_id)
    if deleted:
        log_user_action("删除员工", f"删除编号 {employee_id}（{current.get('name') or '—'}）")
    return deleted


# =============================================================================
# 汇总
# =============================================================================
def employees_without_leave_date() -> list[dict[str, Any]]:
    """状态为「离职」但未填写离职日期的员工（历史/导入数据兜底检查）。"""
    rows = [
        row
        for row in table(TABLE_NAME).read()
        if _text(row.get("status")) == "离职" and not _text(row.get("leave_date"))
    ]
    return sorted(rows, key=lambda row: str(row.get("name") or ""))


def summarize_employees() -> dict[str, Any]:
    """员工汇总：总人数、在职/离职人数、部门分布与在职员工社保/公积金基数合计。"""
    rows = table(TABLE_NAME).read()
    by_department: dict[str, int] = {}
    total_social = 0.0
    total_housing = 0.0
    active = 0
    left = 0
    for row in rows:
        department = _text(row.get("department")) or "未分配部门"
        by_department[department] = by_department.get(department, 0) + 1
        status = _text(row.get("status")) or "在职"
        if status == "在职":
            active += 1
            total_social = round2(total_social + round2(row.get("social_insurance_base")))
            total_housing = round2(total_housing + round2(row.get("housing_fund_base")))
        elif status == "离职":
            left += 1
    return {
        "total": len(rows),
        "active": active,
        "left": left,
        "by_department": {key: by_department[key] for key in sorted(by_department)},
        "total_social_base": total_social,
        "total_housing_fund_base": total_housing,
        "disclaimer": config.DISCLAIMER,
    }


def active_employees_for_month(period: str) -> list[dict[str, Any]]:
    """返回某月份应在工资表中的员工：入职日期 ≤ 月末，且离职日期为空或晚于月初。"""
    from engine.tax_calendar import period_range

    start, end = period_range(models.normalize_period(period))
    start_text = start.isoformat()
    end_text = end.isoformat()
    result = []
    for row in table(TABLE_NAME).read():
        hire_date = _text(row.get("hire_date"))
        leave_date = _text(row.get("leave_date"))
        if _text(row.get("status")) == "离职" and not leave_date:
            continue
        if hire_date and hire_date > end_text:
            continue
        if leave_date and leave_date <= start_text:
            continue
        result.append(row)
    result.sort(key=lambda row: (_text(row.get("employee_no")) or str(row.get("id") or ""), _text(row.get("name"))))
    return result


__all__ = [
    "TABLE_NAME",
    "add_employee",
    "list_employees",
    "get_employee",
    "update_employee",
    "delete_employee",
    "mask_id_card",
    "special_deduction_monthly",
    "summarize_employees",
    "active_employees_for_month",
    "employees_without_leave_date",
]
