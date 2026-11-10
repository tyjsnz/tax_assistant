"""月度工资表与个税累计预扣：生成、编辑、算税、凭证、申报标记与汇总。

核心口径
--------
* **累计预扣法**：调用 :func:`engine.calc_iit_withholding`，以「累计收入 − 累计减除费用 − 累计专项扣除
  − 累计专项附加扣除 − 累计其他扣除」计算累计应纳税额，再减去累计已预扣税额得到本期应预扣税额。
* **2026 扣缴端规则**：减除费用按**实际申报月份**累计——``months_declared = 目标月之前「已申报」月份数
  + 1（目标月本身，存在目标月工资行时）``；未申报月份额度作废，零工资月份也应做零申报。
* **历史累计**：目标月之前 ``status ∈ (已确认, 已申报)`` 的工资行累计 + 目标月当前行金额；
  累计已预扣税额只取历史行（不含当前行本期应预扣）。
* **工资凭证两张**（``source_ref=PAY-{period}``、``voucher_type=工资``、借贷平衡、幂等）：
  计提（借 管理费用—工资薪金 / 研发支出—人员人工，贷 应付职工薪酬）
  + 发放代扣（借 应付职工薪酬，贷 银行存款 / 其他应付款—社会保险费 / 其他应付款—住房公积金 /
  应交税费—应交个人所得税）。
* 已确认/已申报工资行禁止修改删除；期间锁生效（:func:`ledger.locks.ensure_open`）。
"""

from __future__ import annotations

import re
from typing import Any

import config
from engine import calc_iit_withholding, period_range
from engine.money import round2
from ledger import models
from ledger.audit import log_user_action
from ledger.store import table

from ledger import employees as employees_module
from ledger import journal, locks

TABLE_NAME = "payroll"
TABLE_LABEL = "月度工资表"

MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
LOCKED_PAYROLL_STATUSES: tuple[str, ...] = ("已确认", "已申报")
CONFIRMED_STATUSES: tuple[str, ...] = ("已确认", "已申报")

_EDITABLE_FIELDS: tuple[str, ...] = (
    "gross_salary",
    "bonus",
    "allowance",
    "social_insurance",
    "housing_fund",
    "special_deduction",
    "other_deduction",
    "note",
)
_MONEY_FIELDS: tuple[str, ...] = (
    "gross_salary",
    "bonus",
    "allowance",
    "social_insurance",
    "housing_fund",
    "special_deduction",
    "other_deduction",
)
_RD_KEYWORDS = ("研发",)


# =============================================================================
# 内部工具
# =============================================================================
def normalize_month(period: Any) -> str:
    """把期间规范为月份键 ``2026-11``；季度/非法值抛中文 ``ValueError``。"""
    text = models.normalize_period(period, default="")
    if MONTH_RE.match(text):
        return text
    if text and "Q" in text.upper():
        raise ValueError(f"工资与个税按月处理，请提供月份（如 2026-11），而不是季度 {text}。")
    raise ValueError(f"期间格式不正确：{period}，请使用月份 2026-11。")


def _period_end(period: str) -> str:
    _, end = period_range(period)
    return end.isoformat()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _row_income(row: dict[str, Any]) -> float:
    """本期应发合计 = 应发工资 + 奖金 + 补贴。"""
    return round2(
        round2(row.get("gross_salary")) + round2(row.get("bonus")) + round2(row.get("allowance"))
    )


def _payroll_rows(period: str | None = None) -> list[dict[str, Any]]:
    rows = table(TABLE_NAME).read()
    if period:
        text = normalize_month(period)
        rows = [row for row in rows if _text(row.get("period")) == text]
    return rows


def _is_rd_row(row: dict[str, Any]) -> bool:
    """研发人员判定：员工部门/岗位/备注含「研发」。"""
    employee = employees_module.get_employee(_text(row.get("employee_id"))) or {}
    text = " ".join(
        _text(employee.get(key)) for key in ("department", "position", "note")
    )
    return any(keyword in text for keyword in _RD_KEYWORDS)


def _find_pay_entries(period: str) -> list[dict[str, Any]]:
    """读取本期已生成的工资凭证（source_ref=PAY-{period}）。"""
    from ledger.store import table as _table

    reference = f"PAY-{period}"
    headers = [row for row in _table("journal_entries").read() if _text(row.get("source_ref")) == reference]
    if not headers:
        return []
    lines_by_entry: dict[str, list[dict[str, Any]]] = {}
    for line in _table("journal_lines").read():
        lines_by_entry.setdefault(_text(line.get("entry_no")), []).append(line)
    result = []
    for header in headers:
        entry_no = _text(header.get("entry_no"))
        lines = sorted(lines_by_entry.get(entry_no, []), key=lambda item: int(item.get("line_no") or 0))
        result.append(
            {
                "entry_no": entry_no,
                "date": _text(header.get("date")),
                "description": _text(header.get("description")),
                "total": round2(header.get("total_amount")),
                "source_ref": _text(header.get("source_ref")),
                "lines": [
                    {
                        "account": _text(line.get("account")),
                        "direction": _text(line.get("direction")),
                        "amount": round2(line.get("amount")),
                    }
                    for line in lines
                ],
            }
        )
    result.sort(key=lambda item: item["entry_no"])
    return result


# =============================================================================
# 工资表生成 / 查询 / 编辑
# =============================================================================
def build_payroll(period: str, *, employee_ids: list[str] | None = None) -> dict[str, Any]:
    """为当月在职员工生成工资表草稿；同员工同期间已有记录则跳过。"""
    text = normalize_month(period)
    locks.ensure_open(text, action="生成工资表")
    candidates = employees_module.active_employees_for_month(text)
    if employee_ids:
        wanted = {str(item) for item in employee_ids}
        candidates = [row for row in candidates if str(row.get("id")) in wanted]
    existing = {_text(row.get("employee_id")) for row in _payroll_rows(text)}
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for employee in candidates:
        employee_id = _text(employee.get("id"))
        if employee_id in existing:
            skipped.append(
                {
                    "employee_id": employee_id,
                    "employee_name": _text(employee.get("name")),
                    "reason": "本期工资记录已存在",
                }
            )
            continue
        record = {
            "period": text,
            "employee_id": employee_id,
            "employee_name": _text(employee.get("name")),
            "gross_salary": 0.0,
            "bonus": 0.0,
            "allowance": 0.0,
            "social_insurance": 0.0,
            "housing_fund": 0.0,
            "special_deduction": employees_module.special_deduction_monthly(employee),
            "other_deduction": 0.0,
            "cumulative_income": 0.0,
            "cumulative_basic_deduction": 0.0,
            "cumulative_social": 0.0,
            "cumulative_special": 0.0,
            "cumulative_other": 0.0,
            "cumulative_taxable": 0.0,
            "cumulative_tax": 0.0,
            "tax_withheld": 0.0,
            "net_pay": 0.0,
            "zero_declaration": False,
            "status": "草稿",
            "journal_entry_no": "",
            "note": "",
        }
        created.append(table(TABLE_NAME).append(record))
    if created:
        log_user_action(
            "生成工资表",
            f"{text}：新增 {len(created)} 行草稿，跳过 {len(skipped)} 人（已有记录）",
        )
    return {"period": text, "created": len(created), "skipped": skipped, "rows": created}


def list_payroll(period: str | None = None) -> list[dict[str, Any]]:
    """查询工资表记录（可按月份过滤），按月份、员工排序。"""
    rows = _payroll_rows(period)
    return sorted(
        rows,
        key=lambda row: (_text(row.get("period")), _text(row.get("employee_name")), _text(row.get("employee_id"))),
    )


def get_payroll_row(payroll_id: str) -> dict[str, Any] | None:
    """按编号读取一条工资记录。"""
    return table(TABLE_NAME).get(payroll_id)


def update_payroll_row(payroll_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """修改工资行（仅草稿），返回更新后的记录；已确认/已申报禁止修改。"""
    if not isinstance(patch, dict):
        raise ValueError("更新内容必须是 dict")
    current = get_payroll_row(payroll_id)
    if current is None:
        raise KeyError(f"{TABLE_LABEL}中不存在编号 {payroll_id}")
    status = _text(current.get("status"))
    if status in LOCKED_PAYROLL_STATUSES:
        raise ValueError(f"工资记录当前状态为「{status}」，已确认/已申报的工资不能修改。")
    period = normalize_month(current.get("period"))
    locks.ensure_open(period, action="修改工资表")
    updated_patch: dict[str, Any] = {}
    for key, value in patch.items():
        if key not in _EDITABLE_FIELDS:
            continue
        if key in _MONEY_FIELDS:
            amount = round2(value)
            if amount < 0:
                raise ValueError("工资金额与扣除项不能为负数")
            updated_patch[key] = amount
        else:
            updated_patch[key] = _text(value)
    updated = table(TABLE_NAME).update(payroll_id, updated_patch)
    log_user_action(
        "修改工资表",
        f"{period} {updated.get('employee_name') or ''}：应发 {_row_income(updated):.2f} 元",
    )
    return updated


def delete_payroll_row(payroll_id: str) -> bool:
    """删除工资行（仅草稿）；已确认/已申报禁止删除。"""
    current = get_payroll_row(payroll_id)
    if current is None:
        return False
    status = _text(current.get("status"))
    if status in LOCKED_PAYROLL_STATUSES:
        raise ValueError(f"工资记录当前状态为「{status}」，已确认/已申报的工资不能删除。")
    period = normalize_month(current.get("period"))
    locks.ensure_open(period, action="删除工资表")
    deleted = table(TABLE_NAME).delete(payroll_id)
    if deleted:
        log_user_action("删除工资表", f"{period} 删除编号 {payroll_id}")
    return deleted


# =============================================================================
# 累计口径
# =============================================================================
def cumulative_for_employee(employee_id: str, period: str) -> dict[str, Any]:
    """计算员工截至目标月份的累计口径（供累计预扣法使用）。

    * 历史月份：目标月之前、``status ∈ (已确认, 已申报)`` 的工资行；
    * 目标月：若存在当前行，使用其本期金额；
    * ``months_declared`` = 历史「已申报」月份数 + 1（存在目标月当前行时；对应 2026 扣缴端
      「减除费用按实际申报月份累计」规则）；
    * ``cumulative_tax_withheld`` 只累计历史行，不含当前行本期应预扣税额。
    """
    text = normalize_month(period)
    employee_id = _text(employee_id)
    year = text[:4]
    rows = [
        row
        for row in table(TABLE_NAME).read()
        if _text(row.get("employee_id")) == employee_id and _text(row.get("period")).startswith(f"{year}-")
    ]
    history = [
        row
        for row in rows
        if _text(row.get("period")) < text and _text(row.get("status")) in CONFIRMED_STATUSES
    ]
    current = next((row for row in rows if _text(row.get("period")) == text), None)

    employee_declared_months = {
        _text(row.get("period")) for row in history if _text(row.get("status")) == "已申报"
    }
    zero_declared_months = {
        _text(row.get("period"))
        for row in table(TABLE_NAME).read()
        if _text(row.get("employee_id")) == "ZERO"
        and _text(row.get("status")) == "已申报"
        and _text(row.get("period")) < text
        and _text(row.get("period")).startswith(f"{year}-")
    }
    declared_before = len(employee_declared_months | zero_declared_months)
    months_declared = declared_before + (1 if current is not None else 0)

    cumulative_income = round2(sum(_row_income(row) for row in history))
    cumulative_social = round2(sum(round2(row.get("social_insurance")) for row in history))
    cumulative_housing = round2(sum(round2(row.get("housing_fund")) for row in history))
    cumulative_special = round2(sum(round2(row.get("special_deduction")) for row in history))
    cumulative_other = round2(sum(round2(row.get("other_deduction")) for row in history))
    cumulative_withheld = round2(sum(round2(row.get("tax_withheld")) for row in history))
    if current is not None:
        cumulative_income = round2(cumulative_income + _row_income(current))
        cumulative_social = round2(cumulative_social + round2(current.get("social_insurance")))
        cumulative_housing = round2(cumulative_housing + round2(current.get("housing_fund")))
        cumulative_special = round2(cumulative_special + round2(current.get("special_deduction")))
        cumulative_other = round2(cumulative_other + round2(current.get("other_deduction")))

    return {
        "employee_id": employee_id,
        "period": text,
        "year": year,
        "months_declared": months_declared,
        "declared_months_before": declared_before,
        "history_months": len(history),
        "has_current_row": current is not None,
        "cumulative_income": cumulative_income,
        "cumulative_social": cumulative_social,
        "cumulative_housing_fund": cumulative_housing,
        "cumulative_special_additional": cumulative_special,
        "cumulative_other": cumulative_other,
        "cumulative_tax_withheld": cumulative_withheld,
    }


# =============================================================================
# 重新计算个税
# =============================================================================
def recalc_period(period: str) -> dict[str, Any]:
    """按累计预扣法重算本期工资表（仅草稿行），写回累计字段、本期应预扣与实发工资。"""
    text = normalize_month(period)
    locks.ensure_open(text, action="重新计算个税")
    rows = list_payroll(text)
    updated_rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    total_tax = 0.0
    for row in rows:
        status = _text(row.get("status"))
        if status != "草稿":
            warnings.append(f"{row.get('employee_name') or row.get('employee_id')} 已{status}，跳过重算。")
            continue
        employee = employees_module.get_employee(_text(row.get("employee_id")))
        if employee is None:
            warnings.append(f"员工 {row.get('employee_name') or row.get('employee_id')} 档案不存在，跳过重算。")
            continue
        cumulative = cumulative_for_employee(_text(row.get("employee_id")), text)
        qualified = models.parse_bool(employee.get("is_deduction_qualified"), default=True)
        months_for_calc = cumulative["months_declared"] if qualified else 0
        one_time = _text(employee.get("deduction_choice")) == "一次性60000" and qualified
        result = calc_iit_withholding(
            cumulative["cumulative_income"],
            months_declared=months_for_calc,
            cumulative_social_insurance=cumulative["cumulative_social"],
            cumulative_housing_fund=cumulative["cumulative_housing_fund"],
            cumulative_special_additional=cumulative["cumulative_special_additional"],
            cumulative_other_deduction=cumulative["cumulative_other"],
            cumulative_tax_withheld=cumulative["cumulative_tax_withheld"],
            one_time_basic_deduction=one_time,
            period=text,
        )
        income = _row_income(row)
        social = round2(row.get("social_insurance"))
        housing = round2(row.get("housing_fund"))
        tax = round2(result["tax_payable"])
        net_pay = round2(income - social - housing - tax)
        row_warnings = list(result.get("warnings") or [])
        if net_pay < 0:
            row_warnings.append("实发工资为负数（代扣合计超过应发工资），已按 0 展示，请核对工资表。")
            net_pay = 0.0
        patch = {
            "cumulative_income": result["cumulative_income"],
            "cumulative_basic_deduction": result["cumulative_basic_deduction"],
            "cumulative_social": round2(result["cumulative_social_insurance"] + result["cumulative_housing_fund"]),
            "cumulative_special": result["cumulative_special_additional"],
            "cumulative_other": result["cumulative_other_deduction"],
            "cumulative_taxable": result["cumulative_taxable_income"],
            "cumulative_tax": result["cumulative_tax"],
            "tax_withheld": tax,
            "net_pay": net_pay,
        }
        updated = table(TABLE_NAME).update(str(row.get("id")), patch)
        warnings.extend(row_warnings)
        total_tax = round2(total_tax + tax)
        updated_rows.append(
            {
                **updated,
                "applicable_rate": result["applicable_rate"],
                "quick_deduction": result["quick_deduction"],
                "months_declared": result["months_declared"],
                "cumulative_total_deduction": result["cumulative_total_deduction"],
                "cumulative_tax_withheld": result["cumulative_tax_withheld"],
                "warnings": row_warnings,
            }
        )
    if updated_rows:
        log_user_action(
            "工资个税重算",
            f"{text}：重算 {len(updated_rows)} 行，本期应预扣合计 {total_tax:.2f} 元",
        )
    return {"period": text, "rows": updated_rows, "total_tax": total_tax, "warnings": warnings}


# =============================================================================
# 零申报 / 确认 / 申报标记
# =============================================================================
def mark_zero_declaration(period: str) -> dict[str, Any]:
    """无工资月份零申报：写入/更新一条 ``employee_id=ZERO``、状态「已申报」的零金额记录。"""
    text = normalize_month(period)
    locks.ensure_open(text, action="工资零申报")
    existing = next(
        (
            row
            for row in _payroll_rows(text)
            if _text(row.get("employee_id")) == "ZERO" or models.parse_bool(row.get("zero_declaration"))
        ),
        None,
    )
    note = "无工资月份零申报（2026 扣缴端：减除费用按实际申报月份累计）"
    if existing is not None:
        updated = table(TABLE_NAME).update(
            str(existing.get("id")),
            {
                "status": "已申报",
                "zero_declaration": True,
                "employee_name": "（零申报）",
                "note": note,
            },
        )
        record = updated
    else:
        record = table(TABLE_NAME).append(
            {
                "period": text,
                "employee_id": "ZERO",
                "employee_name": "（零申报）",
                "gross_salary": 0.0,
                "bonus": 0.0,
                "allowance": 0.0,
                "social_insurance": 0.0,
                "housing_fund": 0.0,
                "special_deduction": 0.0,
                "other_deduction": 0.0,
                "cumulative_income": 0.0,
                "cumulative_basic_deduction": 0.0,
                "cumulative_social": 0.0,
                "cumulative_special": 0.0,
                "cumulative_other": 0.0,
                "cumulative_taxable": 0.0,
                "cumulative_tax": 0.0,
                "tax_withheld": 0.0,
                "net_pay": 0.0,
                "zero_declaration": True,
                "status": "已申报",
                "journal_entry_no": "",
                "note": note,
            }
        )
    log_user_action("工资零申报", f"{text} 已标记零申报")
    return record


def _build_confirm_entries(period: str, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """构造工资计提与发放代扣凭证，返回 ``(entries, totals)``。"""
    effective = [row for row in rows if not models.parse_bool(row.get("zero_declaration"))]
    total_gross = round2(sum(_row_income(row) for row in effective))
    total_social = round2(sum(round2(row.get("social_insurance")) for row in effective))
    total_housing = round2(sum(round2(row.get("housing_fund")) for row in effective))
    total_tax = round2(sum(round2(row.get("tax_withheld")) for row in effective))
    net_total = round2(total_gross - total_social - total_housing - total_tax)
    totals = {
        "rows": len(effective),
        "gross": total_gross,
        "social": total_social,
        "housing": total_housing,
        "tax": total_tax,
        "net": net_total,
    }
    if total_gross <= 0:
        return [], totals
    if net_total < 0:
        raise ValueError(
            f"{period} 工资表代扣合计（社保 {total_social:.2f} + 公积金 {total_housing:.2f} + "
            f"个税 {total_tax:.2f}）超过应发工资 {total_gross:.2f} 元，请先修正工资表后再确认。"
        )
    rd_total = round2(sum(_row_income(row) for row in effective if _is_rd_row(row)))
    management_total = round2(total_gross - rd_total)
    entry_date = _period_end(period)
    reference = f"PAY-{period}"

    accrual_lines: list[dict[str, Any]] = []
    if rd_total > 0:
        accrual_lines.append({"account": "研发支出—人员人工", "direction": "借", "amount": rd_total})
    if management_total > 0:
        accrual_lines.append({"account": "管理费用—工资薪金", "direction": "借", "amount": management_total})
    accrual_lines.append({"account": "应付职工薪酬", "direction": "贷", "amount": total_gross})

    payment_lines: list[dict[str, Any]] = [
        {"account": "应付职工薪酬", "direction": "借", "amount": total_gross},
        {"account": "银行存款", "direction": "贷", "amount": net_total},
    ]
    if total_social > 0:
        payment_lines.append({"account": "其他应付款—社会保险费", "direction": "贷", "amount": total_social})
    if total_housing > 0:
        payment_lines.append({"account": "其他应付款—住房公积金", "direction": "贷", "amount": total_housing})
    if total_tax > 0:
        payment_lines.append({"account": "应交税费—应交个人所得税", "direction": "贷", "amount": total_tax})

    entries = [
        {
            "date": entry_date,
            "description": f"工资计提（{period}）",
            "voucher_type": "工资",
            "source_ref": reference,
            "lines": accrual_lines,
        },
        {
            "date": entry_date,
            "description": f"工资发放及代扣（{period}）",
            "voucher_type": "工资",
            "source_ref": reference,
            "lines": payment_lines,
        },
    ]
    return entries, totals


def confirm_period(
    period: str,
    *,
    post_journal: bool = True,
    operator: str = "system",
) -> dict[str, Any]:
    """确认本期工资表：生成「工资计提 + 发放代扣」两张凭证，状态→已确认并写审计日志。

    已确认/已申报行不重复入账；本期凭证已存在时跳过重复登记（幂等）；金额为 0 的行不入账。
    """
    text = normalize_month(period)
    locks.ensure_open(text, action="确认工资表")
    rows = list_payroll(text)
    entries, totals = _build_confirm_entries(text, rows)
    warnings: list[str] = []
    existing_entries = _find_pay_entries(text)
    posted_entries: list[dict[str, Any]] = []
    primary_entry_no = ""
    if not post_journal:
        posted_entries = [
            {
                "entry_no": "",
                "date": entry["date"],
                "description": entry["description"],
                "total": round2(
                    sum(line["amount"] for line in entry["lines"] if line["direction"] == "借")
                ),
                "source_ref": entry["source_ref"],
                "lines": entry["lines"],
            }
            for entry in entries
        ]
    elif entries:
        if existing_entries:
            posted_entries = existing_entries
            primary_entry_no = existing_entries[0]["entry_no"]
            warnings.append("本期工资凭证已存在，已跳过重复入账（如需重算请先反结账/删除凭证）。")
        else:
            for entry in entries:
                posted = journal.post_entry(entry)
                posted_entries.append(
                    {
                        "entry_no": posted.get("entry_no", ""),
                        "date": posted.get("date", entry["date"]),
                        "description": posted.get("description", entry["description"]),
                        "total": round2(posted.get("total_amount")),
                        "source_ref": entry["source_ref"],
                        "lines": list(posted.get("lines") or []),
                    }
                )
            primary_entry_no = posted_entries[0]["entry_no"] if posted_entries else ""
    else:
        warnings.append("本期无金额大于 0 的工资记录，未生成工资凭证。")

    draft_count = sum(1 for row in rows if _text(row.get("status")) == "草稿")
    confirmed_rows: list[dict[str, Any]] = []
    for row in rows:
        if _text(row.get("status")) == "草稿":
            updated = table(TABLE_NAME).update(
                str(row.get("id")), {"status": "已确认", "journal_entry_no": primary_entry_no}
            )
            confirmed_rows.append(updated)
        else:
            confirmed_rows.append(row)
    log_user_action(
        "工资表确认",
        f"{text}：本期 {len(rows)} 行（本次确认 {draft_count} 行），"
        f"应发 {totals['gross']:.2f} 元、代扣个税 {totals['tax']:.2f} 元，凭证 "
        f"{'、'.join(item['entry_no'] for item in posted_entries if item['entry_no']) or '（未生成）'}，操作人 {operator}",
    )
    return {
        "period": text,
        "rows": confirmed_rows,
        "entries": posted_entries,
        "totals": totals,
        "warnings": warnings,
    }


def mark_period_declared(period: str) -> dict[str, Any]:
    """在扣缴端完成申报后标记：已确认行 → 已申报，并写审计日志。"""
    text = normalize_month(period)
    locks.ensure_open(text, action="标记工资申报")
    rows = list_payroll(text)
    count = 0
    for row in rows:
        if _text(row.get("status")) == "已确认":
            table(TABLE_NAME).update(str(row.get("id")), {"status": "已申报"})
            count += 1
    log_user_action("工资个税申报标记", f"{text}：{count} 行标记为已申报")
    return {"period": text, "count": count}


# =============================================================================
# 汇总
# =============================================================================
def summarize_period(period: str) -> dict[str, Any]:
    """工资期间汇总（人数、应发、社保/公积金、专项附加、税额、实发与申报状态）。

    ``total_taxable`` 为累计应纳税所得额合计（累计预扣法口径）。
    """
    text = normalize_month(period)
    rows = list_payroll(text)
    effective = [row for row in rows if not models.parse_bool(row.get("zero_declaration"))]
    status_counts: dict[str, int] = {}
    for row in rows:
        status = _text(row.get("status")) or "草稿"
        status_counts[status] = status_counts.get(status, 0) + 1
    total_taxable = round2(sum(round2(row.get("cumulative_taxable")) for row in effective))
    total_tax = round2(sum(round2(row.get("tax_withheld")) for row in effective))
    declared = bool(rows) and all(_text(row.get("status")) == "已申报" for row in rows)
    return {
        "period": text,
        "headcount": len(effective),
        "total_gross": round2(sum(_row_income(row) for row in effective)),
        "total_social": round2(sum(round2(row.get("social_insurance")) for row in effective)),
        "total_housing_fund": round2(sum(round2(row.get("housing_fund")) for row in effective)),
        "total_special": round2(sum(round2(row.get("special_deduction")) for row in effective)),
        "total_taxable": total_taxable,
        "total_tax": total_tax,
        "total_net": round2(sum(round2(row.get("net_pay")) for row in effective)),
        "declared": declared,
        "status_counts": status_counts,
        "zero_declaration": any(models.parse_bool(row.get("zero_declaration")) for row in rows),
        "disclaimer": config.DISCLAIMER,
    }


def payroll_periods(limit: int = 12) -> list[str]:
    """已有工资表的月份列表（新 → 旧）。"""
    periods = {_text(row.get("period")) for row in table(TABLE_NAME).read() if _text(row.get("period"))}
    ordered = sorted(periods, reverse=True)
    if limit is not None:
        ordered = ordered[: max(int(limit), 0)]
    return ordered


__all__ = [
    "TABLE_NAME",
    "MONTH_RE",
    "LOCKED_PAYROLL_STATUSES",
    "normalize_month",
    "build_payroll",
    "list_payroll",
    "get_payroll_row",
    "update_payroll_row",
    "delete_payroll_row",
    "cumulative_for_employee",
    "recalc_period",
    "mark_zero_declaration",
    "confirm_period",
    "mark_period_declared",
    "summarize_period",
    "payroll_periods",
]
