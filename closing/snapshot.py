"""期间快照与校验：可复现的 SHA-256 快照，用于结账存证与篡改检测。

可复现要求
----------
* 数据取数口径固定：收入/费用取台账期间汇总，税额取确定性引擎，科目净额取期间内凭证分录；
* 按科目名排序、金额统一 2 位小数、JSON ``sort_keys=True, ensure_ascii=False, separators=(",",":")``；
* 哈希只覆盖业务数据（``period/period_type/start/end/totals/counts/balances``），
  不包含 ``generated_at`` 等易变字段，因此同一账务状态多次生成哈希一致。

:func:`verify_snapshot` 用当前账务重新生成快照并与结账时保存的哈希比对，可发现结账后台账被改动。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import config
from engine import calc_vat
from engine.money import round2
from ledger.expense_ledger import period_totals as expense_period_totals
from ledger.income_ledger import period_totals as income_period_totals
from ledger.rd_auxiliary import summarize_rd
from ledger.store import table

from closing import periods

_TOTAL_LABELS: dict[str, str] = {
    "income_inclusive": "收入含税合计",
    "income_exclusive": "收入不含税合计",
    "vat": "应纳增值税",
    "expense": "费用合计",
    "profit": "利润",
    "rd_cost": "研发费用可加计扣除额",
    "rd_deduction": "研发加计扣除额",
    "surtax": "附加税费",
}

_COUNT_LABELS: dict[str, str] = {
    "income": "收入笔数",
    "expense": "费用笔数",
    "rd": "研发归集条数",
    "contracts": "合同份数",
    "journal_entries": "凭证张数",
    "invoices": "发票张数",
    "transactions": "流水笔数",
}


# =============================================================================
# 内部取数
# =============================================================================
def canonical_json(payload: Any) -> str:
    """生成规范化 JSON（键排序、无多余空白），保证哈希可复现。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _rd_metrics(period: str) -> tuple[float, float]:
    """研发费用可归集额与加计扣除额（取数失败时按 0 处理）。"""
    try:
        summary = summarize_rd(period)
        return round2(summary.get("total_collectible")), round2(summary.get("deduction_amount"))
    except (ValueError, KeyError):
        return 0.0, 0.0


def _counts(period: str, start: str, end: str) -> dict[str, int]:
    """期间内各类数据笔数（用于快照与差异定位）。"""
    income = income_period_totals(period)
    expense = expense_period_totals(period)
    rd_count = sum(
        1
        for row in table("rd").read()
        if str(row.get("period") or "") == period
    )
    return {
        "income": int(income.get("count") or 0),
        "expense": int(expense.get("count") or 0),
        "rd": rd_count,
        "contracts": len(table("contracts").filter(start=start, end=end)),
        "journal_entries": len(table("journal_entries").filter(start=start, end=end)),
        "invoices": len(table("invoices").filter(start=start, end=end)),
        "transactions": len(table("bank_transactions").filter(start=start, end=end)),
    }


def _balances(start: str, end: str) -> dict[str, float]:
    """期间内各会计科目净额（借 - 贷），金额 2 位、按科目名排序。"""
    accounts: dict[str, float] = {}
    for line in table("journal_lines").filter(start=start, end=end):
        account = str(line.get("account") or "（未填科目）")
        amount = round2(line.get("amount"))
        if str(line.get("direction")) == "借":
            accounts[account] = round2(accounts.get(account, 0.0) + amount)
        else:
            accounts[account] = round2(accounts.get(account, 0.0) - amount)
    return {account: accounts[account] for account in sorted(accounts)}


def _business_payload(period: str) -> dict[str, Any]:
    """快照的业务数据部分（不含哈希与生成时间）。"""
    text, start, end = periods.period_bounds(period)
    income = income_period_totals(text)
    expense = expense_period_totals(text)
    calc = calc_vat(income["total_inclusive"], income["special_inclusive"], period=text)
    rd_cost, rd_deduction = _rd_metrics(text)
    return {
        "period": text,
        "period_type": periods.period_type_of(text),
        "start": start,
        "end": end,
        "totals": {
            "income_inclusive": round2(income["total_inclusive"]),
            "income_exclusive": round2(income["total_exclusive"]),
            "vat": round2(calc["vat_payable"]),
            "expense": round2(expense["total"]),
            "profit": round2(income["total_exclusive"] - expense["total"]),
            "rd_cost": rd_cost,
            "rd_deduction": rd_deduction,
            "surtax": round2(calc["surtax"]),
        },
        "counts": _counts(text, start, end),
        "balances": _balances(start, end),
    }


# =============================================================================
# 对外接口
# =============================================================================
def period_snapshot(period: Any) -> dict[str, Any]:
    """生成期间快照（业务数据 + 可复现 SHA-256 哈希 + 生成时间与免责声明）。"""
    payload = _business_payload(periods.normalize_period(period))
    snapshot = dict(payload)
    snapshot["generated_at"] = config.now_text()
    snapshot["hash"] = _payload_hash(payload)
    snapshot["disclaimer"] = config.DISCLAIMER
    return snapshot


def verify_snapshot(period: Any) -> dict[str, Any]:
    """校验期间快照：用当前账务重算哈希并与结账时保存的哈希比对。

    返回 ``{"ok","period","expected_hash","current_hash","changes","disclaimer"}``；
    未结账或未生成快照时 ``ok=False`` 并在 ``changes`` 中说明原因。
    """
    text = periods.normalize_period(period)
    row = periods.find_period_row(text) or {}
    expected_hash = str(row.get("snapshot_hash") or "")
    current = period_snapshot(text)
    current_hash = str(current["hash"])
    changes: list[str] = []

    if not expected_hash:
        return {
            "ok": False,
            "period": text,
            "expected_hash": "",
            "current_hash": current_hash,
            "changes": [f"期间 {text} 尚未结账或未生成快照，无法校验。"],
            "disclaimer": config.DISCLAIMER,
        }
    if expected_hash == current_hash:
        return {
            "ok": True,
            "period": text,
            "expected_hash": expected_hash,
            "current_hash": current_hash,
            "changes": [],
            "disclaimer": config.DISCLAIMER,
        }

    stored = row.get("snapshot_json")
    stored = stored if isinstance(stored, dict) else {}
    if not stored:
        changes.append("结账快照数据缺失或损坏，无法定位具体差异，仅能确认哈希不一致。")
    else:
        if "totals" in stored or "counts" in stored:
            for section, labels, title in (
                ("totals", _TOTAL_LABELS, "合计"),
                ("counts", _COUNT_LABELS, "笔数"),
            ):
                old_section = stored.get(section) or {}
                new_section = current.get(section) or {}
                for key in sorted(set(old_section) | set(new_section)):
                    old_value = old_section.get(key)
                    new_value = new_section.get(key)
                    if old_value == new_value:
                        continue
                    label = labels.get(key, key)
                    changes.append(f"{title}「{label}」：结账时 {old_value} → 当前 {new_value}")
        old_balances = stored.get("balances") or {}
        new_balances = current.get("balances") or {}
        for account in sorted(set(old_balances) | set(new_balances)):
            old_value = old_balances.get(account)
            new_value = new_balances.get(account)
            if old_value == new_value:
                continue
            changes.append(f"科目「{account}」净额：结账时 {old_value} → 当前 {new_value}")
        if not changes:
            changes.append("账务数据与结账快照不一致（哈希不同，但未定位到具体字段差异）。")
    if len(changes) > 20:
        changes = changes[:20] + [f"……另有 {len(changes) - 20} 项差异未列出。"]
    return {
        "ok": False,
        "period": text,
        "expected_hash": expected_hash,
        "current_hash": current_hash,
        "changes": changes,
        "disclaimer": config.DISCLAIMER,
    }


__all__ = [
    "canonical_json",
    "period_snapshot",
    "verify_snapshot",
]
