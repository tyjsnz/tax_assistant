"""企业所得税年度汇算清缴：年度数据汇总、纳税调整、亏损弥补与汇算测算。

定位
----
本模块负责**年度数据准备 + 纳税调整测算 + 亏损弥补 + 汇算结果测算 + 检查清单**，
计算全部调用 :func:`engine.calc_settlement`（确定性代码）；系统**不代替**电子税务局
年度申报，最终以 A 类年度申报表与主管税务机关口径为准。

数据来源
--------
* 收入 / 费用 / 工资总额 / 凭证笔数：从 ``income`` / ``expense`` / ``journal_entries`` 台账按年汇总；
* 研发费用与加计扣除：费用台账 ``is_rd_related`` 记录 + ``rd`` 手工归集记录，调 ``engine.calc_rd_deduction``；
* 预缴税额：优先读 ``filings`` 表 ``CIT_PREPAY`` 季度申报快照合计，缺失时回退年度输入的 ``prepaid_tax``；
* 年度输入（限额类实际发生额、从业人数、资产总额、会计利润覆盖等）存 ``annual_settlements``。
"""

from __future__ import annotations

from typing import Any

import config
from engine import calc_rd_deduction, calc_settlement, period_range, round2
from engine.rd_deduction import RD_ITEM_LABELS, RD_ITEMS
from ledger import models
from ledger.audit import log_calculation, log_user_action
from ledger.expense_ledger import summarize_expenses
from ledger.income_ledger import summarize_income
from ledger.rd_auxiliary import collect_from_expenses, list_rd_records
from ledger.store import table

ADJUSTMENT_TABLE = "tax_adjustments"
LOSS_TABLE = "loss_carryforwards"
SETTLEMENT_TABLE = "annual_settlements"
FILING_TABLE = "filings"

#: ``annual_settlements.data_json`` 允许的输入键（白名单）
INPUT_KEYS: tuple[str, ...] = (
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

_MONEY_INPUT_KEYS: tuple[str, ...] = (
    "entertainment_actual",
    "advertising_actual",
    "welfare_actual",
    "education_actual",
    "union_actual",
    "total_assets",
)
#: 可为空（未填）的金额键：prepaid_tax/accounting_profit/rd_deduction 留空表示「按台账取数」
_OPTIONAL_MONEY_KEYS: tuple[str, ...] = ("prepaid_tax", "accounting_profit", "rd_deduction")


# =============================================================================
# 年度与输入工具
# =============================================================================
def normalize_year(year: Any) -> str:
    """把年度规范为 4 位字符串 ``2026``；非法值抛中文 ``ValueError``。"""
    text = str(year if year is not None else "").strip()
    digits = "".join(char for char in text if char.isdigit())
    if len(digits) < 4:
        raise ValueError(f"年度格式不正确：{year}，请使用 2026 形式")
    return digits[:4]


def _money(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    amount = round2(value)
    return amount


def _input_defaults() -> dict[str, Any]:
    """年度输入的默认值（未保存时返回）。"""
    return {
        "entertainment_actual": 0.0,
        "advertising_actual": 0.0,
        "welfare_actual": 0.0,
        "education_actual": 0.0,
        "union_actual": 0.0,
        "employees": None,
        "total_assets": 0.0,
        "prepaid_tax": None,
        "accounting_profit": None,
        "rd_deduction": None,
        "is_restricted_industry": False,
        "note": "",
    }


def _normalize_inputs(data: dict[str, Any], *, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """白名单校验与类型规范化（金额 2 位、人数/资产非负、布尔解析）。"""
    if not isinstance(data, dict):
        raise ValueError("年度输入数据必须是 dict")
    record = {**_input_defaults(), **(base or {})}
    for key, value in data.items():
        if key not in INPUT_KEYS:
            continue  # 非白名单键忽略，避免误写入
        if key in _MONEY_INPUT_KEYS:
            amount = _money(value)
            if amount is None:
                amount = 0.0
            if amount < 0:
                raise ValueError(f"年度输入「{key}」不能为负数")
            record[key] = amount
        elif key in _OPTIONAL_MONEY_KEYS:
            amount = _money(value)
            if amount is not None and amount < 0:
                raise ValueError(f"年度输入「{key}」不能为负数")
            record[key] = amount
        elif key == "employees":
            if value in (None, ""):
                record[key] = None
            else:
                try:
                    number = int(float(value))
                except (TypeError, ValueError) as exc:
                    raise ValueError("从业人数必须是整数") from exc
                if number < 0:
                    raise ValueError("从业人数不能为负数")
                record[key] = number
        elif key == "is_restricted_industry":
            record[key] = models.parse_bool(value)
        elif key == "note":
            record[key] = str(value or "").strip()
    return record


def _find_by(table_name: str, key: str, value: Any) -> dict[str, Any] | None:
    for row in table(table_name).read():
        if str(row.get(key) or "") == str(value):
            return row
    return None


# =============================================================================
# 年度台账数据
# =============================================================================
def _annual_rd(year: str) -> dict[str, Any]:
    """全年研发费用归集与加计扣除（费用台账研发标记 + rd 手工归集，按年一次限额）。"""
    auto = collect_from_expenses(year)
    manual_rows = [
        row for row in list_rd_records() if str(row.get("period") or "").startswith(str(year))
    ]
    label_to_key = {label: key for key, label in RD_ITEM_LABELS.items()}
    buckets: dict[str, float] = {key: 0.0 for key in RD_ITEMS}
    for key in RD_ITEMS:
        auto_amount = round2(round2((auto.get("items") or {}).get(RD_ITEM_LABELS[key])))
        manual_amount = round2(sum(round2(row.get(key)) for row in manual_rows))
        buckets[key] = round2(auto_amount + manual_amount)
    result = calc_rd_deduction(**buckets, period=str(year), project_name="全年研发费用")
    return {
        "rd_cost": round2(result["total_collectible"]),
        "rd_deduction": round2(result["deduction_amount"]),
        "items": result["items"],
        "from_expenses": auto,
        "manual_count": len(manual_rows),
        "manual_rows": manual_rows,
        "label_to_key": label_to_key,
        "warnings": result.get("warnings") or [],
        "note": result.get("note", ""),
    }


def _prepaid_from_filings(year: str) -> tuple[float, int]:
    """从 filings 表读取该年度 CIT_PREPAY 季度预缴快照合计，返回 ``(金额, 记录数)``。"""
    total = 0.0
    count = 0
    for quarter in range(1, 5):
        period = f"{year}Q{quarter}"
        row = None
        for candidate in table(FILING_TABLE).read():
            if str(candidate.get("form_id") or "") == "CIT_PREPAY" and str(candidate.get("period") or "") == period:
                row = candidate
                break
        if row is None:
            continue
        data = row.get("data_json")
        totals = data.get("totals") if isinstance(data, dict) else None
        amount = round2((totals or {}).get("prepay_payable"))
        total = round2(total + amount)
        count += 1
    return total, count


def annual_ledger_data(year: Any) -> dict[str, Any]:
    """从台账汇总某年度的汇算基础数据（收入/费用/利润/工资/研发/预缴/月度明细）。"""
    text = normalize_year(year)
    start, end = f"{text}-01-01", f"{text}-12-31"
    income = summarize_income(start=start, end=end)
    expense = summarize_expenses(start=start, end=end)
    rd = _annual_rd(text)
    expense_by_category = {
        category: round2(bucket.get("amount")) for category, bucket in (expense.get("by_category") or {}).items()
    }
    salary_total = round2(expense_by_category.get("工资", 0.0))
    revenue_exclusive = round2(income.get("total_exclusive"))
    expense_total = round2(expense.get("total"))
    profit = round2(revenue_exclusive - expense_total)

    filings_prepaid, filing_count = _prepaid_from_filings(text)
    inputs = get_annual_inputs(text)
    input_prepaid = inputs.get("prepaid_tax")
    if filings_prepaid > 0:
        prepaid_tax = filings_prepaid
        prepaid_source = "filings"
    elif input_prepaid not in (None, 0, 0.0):
        prepaid_tax = round2(input_prepaid)
        prepaid_source = "inputs"
    else:
        prepaid_tax = 0.0
        prepaid_source = "none"

    monthly: list[dict[str, Any]] = []
    for month in range(1, 13):
        month_text = f"{text}-{month:02d}"
        month_start, month_end = period_range(month_text)
        month_income = summarize_income(start=month_start.isoformat(), end=month_end.isoformat())
        month_expense = summarize_expenses(start=month_start.isoformat(), end=month_end.isoformat())
        monthly.append(
            {
                "month": month_text,
                "income_inclusive": round2(month_income.get("total_inclusive")),
                "income_exclusive": round2(month_income.get("total_exclusive")),
                "expense": round2(month_expense.get("total")),
                "profit": round2(
                    month_income.get("total_exclusive") - month_expense.get("total")
                ),
            }
        )

    warnings: list[str] = []
    if income.get("count") == 0 and expense.get("count") == 0:
        warnings.append(f"{text} 年度无收入与费用记录，请确认是否为零申报年度。")
    if filing_count == 0:
        warnings.append(f"未读取到 {text} 年度的企业所得税季度预缴申报记录，请核对预缴税额。")
    if rd["rd_cost"] > 0 and rd["rd_deduction"] == 0:
        warnings.append("全年有研发费用归集但加计扣除为 0，请检查研发辅助账。")

    return {
        "year": text,
        "start": start,
        "end": end,
        "revenue_exclusive": revenue_exclusive,
        "revenue_inclusive": round2(income.get("total_inclusive")),
        "expense_total": expense_total,
        "profit": profit,
        "expense_by_category": expense_by_category,
        "salary_total": salary_total,
        "rd_cost": rd["rd_cost"],
        "rd_deduction": rd["rd_deduction"],
        "rd_detail": rd,
        "prepaid_tax": prepaid_tax,
        "prepaid_source": prepaid_source,
        "counts": {
            "income": int(income.get("count") or 0),
            "expense": int(expense.get("count") or 0),
            "journal_entries": len(table("journal_entries").filter(start=start, end=end)),
            "invoices": len(table("invoices").filter(start=start, end=end)),
            "transactions": len(table("bank_transactions").filter(start=start, end=end)),
        },
        "monthly": monthly,
        "warnings": warnings,
        "disclaimer": config.DISCLAIMER,
    }


# =============================================================================
# 年度输入（annual_settlements）
# =============================================================================
def get_settlement_record(year: Any) -> dict[str, Any] | None:
    """读取 annual_settlements 原始行（不存在返回 ``None``），供状态展示使用。"""
    text = normalize_year(year)
    return _find_by(SETTLEMENT_TABLE, "year", text)


def get_annual_inputs(year: Any) -> dict[str, Any]:
    """读取年度汇算输入（不存在返回白名单默认值）。"""
    text = normalize_year(year)
    row = _find_by(SETTLEMENT_TABLE, "year", text)
    data = dict(_input_defaults())
    if row is not None and isinstance(row.get("data_json"), dict):
        data = _normalize_inputs(row["data_json"], base=data)
    return data


def _input_view(row: dict[str, Any]) -> dict[str, Any]:
    """把 annual_settlements 行展开为「行字段 + 扁平年度输入」，供 API/CLI 直接读取。

    ``data_json`` 中的白名单键会提升到顶层（如 ``inputs["entertainment_actual"]``），
    同时保留 ``id`` / ``year`` / ``status`` / ``note`` / ``data_json`` 等行字段。
    """
    view = dict(row or {})
    data = view.get("data_json")
    if isinstance(data, dict):
        for key in INPUT_KEYS:
            if key in data:
                view[key] = data[key]
    return view


def save_annual_inputs(year: Any, data: dict[str, Any], *, note: str = "") -> dict[str, Any]:
    """按年度 upsert 汇算输入（只允许白名单键），金额 2 位并写审计日志。"""
    text = normalize_year(year)
    if not isinstance(data, dict):
        raise ValueError("年度输入数据必须是 dict")
    current = _find_by(SETTLEMENT_TABLE, "year", text)
    base = current.get("data_json") if current and isinstance(current.get("data_json"), dict) else {}
    record = _normalize_inputs(data, base=base)
    if note:
        record["note"] = str(note).strip()
    if current is not None:
        updated = table(SETTLEMENT_TABLE).update(str(current.get("id")), {"data_json": record, "note": record["note"]})
        log_user_action("保存年度汇算输入", f"{text} 年度：更新输入数据")
        return _input_view(updated)
    created = table(SETTLEMENT_TABLE).append(
        {"year": text, "data_json": record, "status": "草稿", "note": record["note"]}
    )
    log_user_action("保存年度汇算输入", f"{text} 年度：新建输入数据")
    return _input_view(created)


def mark_settlement_status(year: Any, status: str) -> dict[str, Any]:
    """更新年度汇算状态（草稿 / 已确认）。"""
    text = normalize_year(year)
    status_text = str(status or "").strip()
    if status_text not in models.SETTLEMENT_STATUSES:
        raise ValueError(f"汇算状态「{status_text}」不合法，可选：{'、'.join(models.SETTLEMENT_STATUSES)}")
    current = _find_by(SETTLEMENT_TABLE, "year", text)
    if current is None:
        created = table(SETTLEMENT_TABLE).append(
            {"year": text, "data_json": get_annual_inputs(text), "status": status_text, "note": ""}
        )
        log_user_action("年度汇算状态变更", f"{text} 年度：草稿 → {status_text}")
        return _input_view(created)
    updated = table(SETTLEMENT_TABLE).update(str(current.get("id")), {"status": status_text})
    log_user_action("年度汇算状态变更", f"{text} 年度：{current.get('status') or '草稿'} → {status_text}")
    return _input_view(updated)


# =============================================================================
# 纳税调整清单
# =============================================================================
def list_adjustments(year: Any) -> list[dict[str, Any]]:
    """列出某年度的纳税调整记录（按编号排序）。"""
    text = normalize_year(year)
    rows = [row for row in table(ADJUSTMENT_TABLE).read() if str(row.get("year") or "") == text]
    return sorted(rows, key=lambda row: str(row.get("id") or ""))


def _normalize_adjustment(payload: dict[str, Any], *, year: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("纳税调整数据必须是 dict")
    item_type = str(payload.get("item_type") or "").strip()
    if not item_type:
        raise ValueError("调整项目（item_type）不能为空")
    direction = str(payload.get("direction") or "调增").strip()
    if direction not in models.ADJUSTMENT_DIRECTIONS:
        raise ValueError(f"纳税调整方向「{direction}」不合法，应为 调增 或 调减")
    adjustment_amount = round2(payload.get("adjustment_amount"))
    if adjustment_amount < 0:
        raise ValueError("调整金额不能为负数（方向由「调增/调减」表示）")
    accounting_amount = round2(payload.get("accounting_amount"))
    tax_amount = round2(payload.get("tax_amount"))
    if accounting_amount < 0 or tax_amount < 0:
        raise ValueError("会计金额与税法金额不能为负数")
    return {
        "year": year,
        "item_type": item_type,
        "direction": direction,
        "accounting_amount": accounting_amount,
        "tax_amount": tax_amount,
        "adjustment_amount": adjustment_amount,
        "basis": str(payload.get("basis") or "").strip(),
        "source": str(payload.get("source") or "").strip(),
        "note": str(payload.get("note") or "").strip(),
    }


def add_adjustment(
    year: Any,
    item_type: str,
    direction: str,
    adjustment_amount: float,
    *,
    accounting_amount: float = 0.0,
    tax_amount: float = 0.0,
    basis: str = "",
    source: str = "",
    note: str = "",
) -> dict[str, Any]:
    """新增一条纳税调整记录（调增/调减），返回保存后的记录。"""
    text = normalize_year(year)
    record = _normalize_adjustment(
        {
            "item_type": item_type,
            "direction": direction,
            "adjustment_amount": adjustment_amount,
            "accounting_amount": accounting_amount,
            "tax_amount": tax_amount,
            "basis": basis,
            "source": source,
            "note": note,
        },
        year=text,
    )
    added = table(ADJUSTMENT_TABLE).append(record)
    log_user_action(
        "新增纳税调整",
        f"{text} 年度：{record['item_type']} {record['direction']} {record['adjustment_amount']:.2f} 元",
    )
    return added


def update_adjustment(adjustment_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """修改纳税调整记录（允许调整项目/方向/金额/依据等），返回更新后的记录。"""
    if not isinstance(patch, dict):
        raise ValueError("更新内容必须是 dict")
    current = table(ADJUSTMENT_TABLE).get(adjustment_id)
    if current is None:
        raise KeyError(f"纳税调整清单中不存在编号 {adjustment_id}")
    merged = {**current}
    for key, value in patch.items():
        if key in ("id", "created_at", "year"):
            continue
        merged[key] = value
    record = _normalize_adjustment(merged, year=str(current.get("year") or normalize_year(current.get("year"))))
    updated = table(ADJUSTMENT_TABLE).update(adjustment_id, record)
    log_user_action(
        "修改纳税调整",
        f"编号 {adjustment_id}：{updated.get('item_type')} {updated.get('direction')} "
        f"{round2(updated.get('adjustment_amount')):.2f} 元",
    )
    return updated


def delete_adjustment(adjustment_id: str) -> bool:
    """删除纳税调整记录，返回是否删除成功。"""
    deleted = table(ADJUSTMENT_TABLE).delete(adjustment_id)
    if deleted:
        log_user_action("删除纳税调整", f"删除编号 {adjustment_id}")
    return deleted


# =============================================================================
# 亏损弥补台账
# =============================================================================
def list_losses(year: Any = None) -> list[dict[str, Any]]:
    """列出亏损台账（可按亏损年度过滤），按亏损年度升序。"""
    rows = table(LOSS_TABLE).read()
    if year not in (None, ""):
        text = normalize_year(year)
        rows = [row for row in rows if str(row.get("year") or "") == text]
    return sorted(rows, key=lambda row: (str(row.get("year") or ""), str(row.get("id") or "")))


def _loss_expire_year(year: str) -> str:
    from engine.cit_settlement import LOSS_CARRYFORWARD_YEARS

    return str(int(year) + int(LOSS_CARRYFORWARD_YEARS))


def add_loss(year: Any, loss_amount: float, *, used_amount: float = 0.0, note: str = "") -> dict[str, Any]:
    """新增一条亏损结转记录（到期年度自动 = 亏损年度 + 5 年）。"""
    text = normalize_year(year)
    amount = round2(loss_amount)
    if amount <= 0:
        raise ValueError("可弥补亏损额必须大于 0")
    used = round2(used_amount)
    if used < 0:
        raise ValueError("已弥补金额不能为负数")
    if used > amount:
        raise ValueError("已弥补金额不能超过可弥补亏损额")
    record = {
        "year": text,
        "loss_amount": amount,
        "used_amount": used,
        "remaining": round2(amount - used),
        "expire_year": _loss_expire_year(text),
        "note": str(note or "").strip(),
    }
    added = table(LOSS_TABLE).append(record)
    log_user_action("新增亏损结转", f"{text} 年度亏损 {amount:.2f} 元，到期 {record['expire_year']} 年")
    return added


def update_loss(loss_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """更新亏损台账的已弥补金额/备注，并重算剩余可弥补额。"""
    if not isinstance(patch, dict):
        raise ValueError("更新内容必须是 dict")
    current = table(LOSS_TABLE).get(loss_id)
    if current is None:
        raise KeyError(f"亏损弥补台账中不存在编号 {loss_id}")
    loss_amount = round2(current.get("loss_amount"))
    used = round2(patch.get("used_amount", current.get("used_amount")))
    if used < 0:
        raise ValueError("已弥补金额不能为负数")
    if used > loss_amount:
        raise ValueError("已弥补金额不能超过可弥补亏损额")
    payload = {
        "used_amount": used,
        "remaining": round2(loss_amount - used),
        "note": str(patch.get("note", current.get("note") or "")).strip(),
    }
    if "expire_year" in patch and str(patch.get("expire_year") or "").strip():
        expire = str(patch.get("expire_year")).strip()
        if not expire.isdigit() or len(expire) != 4:
            raise ValueError(f"到期年度格式不正确：{expire}，请使用 2029 形式")
        payload["expire_year"] = expire
    updated = table(LOSS_TABLE).update(loss_id, payload)
    log_user_action(
        "修改亏损结转",
        f"编号 {loss_id}：已弥补 {used:.2f} 元，剩余 {payload['remaining']:.2f} 元",
    )
    return updated


def delete_loss(loss_id: str) -> bool:
    """删除亏损结转记录，返回是否删除成功。"""
    deleted = table(LOSS_TABLE).delete(loss_id)
    if deleted:
        log_user_action("删除亏损结转", f"删除编号 {loss_id}")
    return deleted


# =============================================================================
# 汇算测算
# =============================================================================
def compute_settlement(year: Any) -> dict[str, Any]:
    """汇总台账数据 + 年度输入 + 纳税调整 + 亏损台账，调用 engine 计算汇算结果。"""
    text = normalize_year(year)
    annual = annual_ledger_data(text)
    inputs = get_annual_inputs(text)
    adjustments = list_adjustments(text)
    all_losses = list_losses()
    losses = [row for row in all_losses if int(str(row.get("year") or "0")) < int(text)]

    accounting_profit = inputs.get("accounting_profit")
    if accounting_profit is None:
        accounting_profit_value = None
    else:
        accounting_profit_value = round2(accounting_profit)
    rd_deduction = inputs.get("rd_deduction")
    if rd_deduction is None:
        rd_deduction_value = annual["rd_deduction"]
    else:
        rd_deduction_value = round2(rd_deduction)

    input_prepaid = inputs.get("prepaid_tax")
    if input_prepaid not in (None, 0, 0.0):
        prepaid_tax = round2(input_prepaid)
        prepaid_source = "inputs"
    else:
        prepaid_tax = annual["prepaid_tax"]
        prepaid_source = annual["prepaid_source"]

    engine_result = calc_settlement(
        year=text,
        revenue_exclusive=annual["revenue_exclusive"],
        cost_total=annual["expense_total"],
        accounting_profit=accounting_profit_value,
        salary_total=annual["salary_total"],
        entertainment_actual=round2(inputs.get("entertainment_actual")),
        advertising_actual=round2(inputs.get("advertising_actual")),
        welfare_actual=round2(inputs.get("welfare_actual")),
        education_actual=round2(inputs.get("education_actual")),
        union_actual=round2(inputs.get("union_actual")),
        rd_deduction=rd_deduction_value,
        extra_adjustments=adjustments,
        losses=losses,
        prepaid_tax=prepaid_tax,
        employees=inputs.get("employees"),
        total_assets=inputs.get("total_assets") or None,
        is_restricted_industry=models.parse_bool(inputs.get("is_restricted_industry")),
    )
    result = dict(engine_result)
    result.update(
        {
            "annual": annual,
            "inputs": inputs,
            "adjustments": adjustments,
            "losses": losses,
            "all_losses": all_losses,
            "prepaid_tax": round2(prepaid_tax),
            "prepaid_source": prepaid_source,
        }
    )
    log_calculation(
        "企业所得税汇算",
        text,
        {
            "revenue_exclusive": annual["revenue_exclusive"],
            "expense_total": annual["expense_total"],
            "prepaid_tax": prepaid_tax,
            "adjustment_count": len(adjustments),
            "loss_count": len(losses),
        },
        {
            "accounting_profit": result["accounting_profit"],
            "taxable_income": result["taxable_income"],
            "corporate_tax": result["corporate_tax"],
            "payable": result["payable"],
            "refund": result["refund"],
        },
    )
    return result


# =============================================================================
# 汇算检查清单
# =============================================================================
def _checklist_item(item: str, status: str, message: str, action: str = "") -> dict[str, Any]:
    if status not in ("PASS", "WARNING", "ERROR"):
        status = "WARNING"
    return {"item": item, "status": status, "message": message, "action": action}


def settlement_checklist(year: Any, settlement: dict[str, Any]) -> list[dict[str, Any]]:
    """生成汇算检查清单（收入一致、费用凭证、研发、亏损、预缴、小微、资料、期限、调整遗漏）。"""
    text = normalize_year(year)
    settlement = settlement if isinstance(settlement, dict) else {}
    annual = settlement.get("annual") if isinstance(settlement.get("annual"), dict) else annual_ledger_data(text)
    inputs = settlement.get("inputs") if isinstance(settlement.get("inputs"), dict) else get_annual_inputs(text)
    items: list[dict[str, Any]] = []

    # 1) 收入与台账一致
    if abs(round2(settlement.get("revenue_exclusive")) - round2(annual.get("revenue_exclusive"))) <= 0.01:
        items.append(
            _checklist_item(
                "收入与台账一致",
                "PASS",
                f"申报使用收入 {round2(settlement.get('revenue_exclusive')):.2f} 元，与台账全年收入一致。",
            )
        )
    else:
        items.append(
            _checklist_item(
                "收入与台账一致",
                "ERROR",
                f"申报收入 {round2(settlement.get('revenue_exclusive')):.2f} 元与台账收入 "
                f"{round2(annual.get('revenue_exclusive')):.2f} 元不一致，请核对。",
                "检查是否存在未入账收入或跨期收入。",
            )
        )

    # 2) 费用凭证
    if annual.get("counts", {}).get("expense", 0) > 0 and annual.get("counts", {}).get("journal_entries", 0) > 0:
        items.append(
            _checklist_item(
                "费用凭证完整",
                "PASS",
                f"全年费用 {annual['counts']['expense']} 笔、凭证 {annual['counts']['journal_entries']} 张，可支撑成本费用扣除。",
            )
        )
    elif annual.get("expense_total", 0) > 0:
        items.append(
            _checklist_item(
                "费用凭证完整",
                "WARNING",
                "存在费用台账但凭证数量不足，请补充成本费用凭证。",
                "在「台账管理 → 凭证」补充或核对。",
            )
        )
    else:
        items.append(
            _checklist_item(
                "费用凭证完整",
                "WARNING",
                "全年无费用记录，请确认是否漏记成本费用。",
                "如确无成本费用，可继续；否则补录台账。",
            )
        )

    # 3) 研发辅助账
    rd_detail = annual.get("rd_detail") or {}
    if round2(settlement.get("research_deduction")) > 0:
        if rd_detail.get("manual_count", 0) > 0 or (rd_detail.get("from_expenses") or {}).get("source_count", 0) > 0:
            items.append(
                _checklist_item(
                    "研发辅助账",
                    "PASS",
                    f"研发费用加计扣除 {round2(settlement.get('research_deduction')):.2f} 元，研发辅助账已归集。",
                    "留存研发项目立项、研发辅助账与费用分摊依据备查。",
                )
            )
        else:
            items.append(
                _checklist_item(
                    "研发辅助账",
                    "WARNING",
                    "存在研发加计扣除但未识别到研发辅助账明细，请补充立项与归集资料。",
                    "在「台账管理 → 研发」补录研发辅助账。",
                )
            )
    else:
        items.append(
            _checklist_item(
                "研发辅助账",
                "PASS",
                "本年未享受研发费用加计扣除（如有研发支出请补充归集）。",
            )
        )

    # 4) 亏损弥补台账
    loss_lines = (settlement.get("loss_carryforward") or {}).get("lines") or []
    expired = [line for line in loss_lines if str(line.get("status")) == "已过期"]
    current_profit = round2(settlement.get("accounting_profit"))
    if expired:
        items.append(
            _checklist_item(
                "亏损弥补台账",
                "WARNING",
                f"存在 {len(expired)} 条已超过 5 年弥补期限的亏损，不得再弥补。",
                "在「亏损弥补」页核销过期亏损。",
            )
        )
    elif loss_lines:
        items.append(
            _checklist_item(
                "亏损弥补台账",
                "PASS",
                f"本年弥补亏损 {round2(settlement.get('loss_used')):.2f} 元，亏损台账勾稽正确。",
            )
        )
    elif current_profit < 0:
        items.append(
            _checklist_item(
                "亏损弥补台账",
                "WARNING",
                f"本年会计利润为负（{current_profit:.2f} 元），请登记亏损结转台账以便以后年度弥补。",
                "在「亏损弥补」页登记本年度亏损额。",
            )
        )
    else:
        items.append(_checklist_item("亏损弥补台账", "PASS", "本年无亏损弥补事项。"))

    # 5) 预缴核对
    prepaid = round2(settlement.get("prepaid_tax"))
    if prepaid > 0:
        items.append(
            _checklist_item(
                "预缴税额核对",
                "PASS",
                f"已读取{prepaid:.2f} 元季度预缴税额（来源：{'季度申报记录' if settlement.get('prepaid_source') == 'filings' else '年度输入'}）。",
                "与电子税务局「已缴税额」核对后申报。",
            )
        )
    else:
        items.append(
            _checklist_item(
                "预缴税额核对",
                "WARNING",
                "未读取到本年季度预缴税额，请核对是否已按季预缴。",
                "在「年度数据」录入预缴税额或在「申报填报」保存季度预缴申报记录。",
            )
        )

    # 6) 小型微利企业条件
    if settlement.get("qualifies_small_low_profit"):
        items.append(
            _checklist_item(
                "小型微利企业条件",
                "PASS",
                "符合小型微利企业条件（应纳税所得额≤300 万、从业人数≤300、资产总额≤5000 万），实际税负 5%。",
            )
        )
    else:
        items.append(
            _checklist_item(
                "小型微利企业条件",
                "WARNING",
                f"不符合小型微利企业条件，按法定税率 25% 计算。"
                + ("原因：" + "；".join((settlement.get("small_low_profit_check") or {}).get("reasons") or []) if (settlement.get("small_low_profit_check") or {}).get("reasons") else ""),
                "请核对从业人数、资产总额与行业限制。",
            )
        )

    # 7) 资料留存
    items.append(
        _checklist_item(
            "资料留存",
            "PASS",
            "请留存年度财务报表、纳税调整依据、研发辅助账、亏损弥补资料、预缴凭证与合同等备查资料。",
            "保存期限一般不少于 10 年。",
        )
    )

    # 8) 申报期限（次年 5 月 31 日）
    from datetime import date

    deadline = date(int(text) + 1, 5, 31)
    today = config.today()
    days_left = (deadline - today).days
    if days_left < 0:
        items.append(
            _checklist_item(
                "申报期限",
                "WARNING",
                f"{text} 年度汇算清缴截止日 {deadline.isoformat()} 已逾期 {-days_left} 天，请尽快更正申报。",
                "联系主管税务机关或通过电子税务局更正申报。",
            )
        )
    elif days_left <= 30:
        items.append(
            _checklist_item(
                "申报期限",
                "WARNING",
                f"距 {text} 年度汇算清缴截止日 {deadline.isoformat()} 仅剩 {days_left} 天，请尽快申报。",
                "在电子税务局完成 A 类年度申报并缴款。",
            )
        )
    else:
        items.append(
            _checklist_item(
                "申报期限",
                "PASS",
                f"汇算清缴截止日 {deadline.isoformat()}（次年 5 月 31 日），距今天还有 {days_left} 天。",
            )
        )

    # 9) 纳税调整项遗漏
    limit_keys = {
        "entertainment_actual": ("business_entertainment", "业务招待费"),
        "advertising_actual": ("advertising", "广告费和业务宣传费"),
        "welfare_actual": ("welfare", "职工福利费"),
        "education_actual": ("education", "职工教育经费"),
        "union_actual": ("union", "工会经费"),
    }
    limit_item_keys = {str(item.get("key")) for item in (settlement.get("limit_adjustment_items") or [])}
    missing: list[str] = []
    for input_key, (item_key, label) in limit_keys.items():
        if round2(inputs.get(input_key)) > 0 and item_key not in limit_item_keys:
            missing.append(label)
    if missing:
        items.append(
            _checklist_item(
                "纳税调整项完整性",
                "WARNING",
                f"以下项目有实际发生额但未生成纳税调整项：{'、'.join(missing)}。",
                "重新计算汇算结果，或在「纳税调整」页补充手工调整。",
            )
        )
    else:
        items.append(
            _checklist_item(
                "纳税调整项完整性",
                "PASS",
                f"已处理 {len(settlement.get('limit_adjustment_items') or []) + len(settlement.get('manual_adjustment_items') or [])} 项纳税调整（限额类 + 手工）。",
            )
        )
    return items


__all__ = [
    "INPUT_KEYS",
    "normalize_year",
    "annual_ledger_data",
    "get_settlement_record",
    "get_annual_inputs",
    "save_annual_inputs",
    "get_settlement_record",
    "mark_settlement_status",
    "list_adjustments",
    "add_adjustment",
    "update_adjustment",
    "delete_adjustment",
    "list_losses",
    "add_loss",
    "update_loss",
    "delete_loss",
    "compute_settlement",
    "settlement_checklist",
]
