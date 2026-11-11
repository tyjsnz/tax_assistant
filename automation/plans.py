"""AI 代办：把确定性操作封装成「预览 → 确认 → 一键执行」（v1.22）。

边界
----
* 只自动执行**本地确定性操作**：生成工资表、匹配入账、生成/归档申报包、导出更正操作单；
* **不自动登录电子税务局、不自动提交申报、不自动缴款**（无公开 API，合规风险高）；
* 执行写操作前自动创建本地备份；结果均可在对应页面复核。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import config
from engine import current_quarter, period_label


def quarter_period(today: date | None = None) -> str:
    return current_quarter(today)


def month_period(today: date | None = None) -> str:
    return (today or config.today()).strftime("%Y-%m")


def preview_payroll(period: str) -> dict[str, Any]:
    from ledger import employees, payroll

    month = payroll.normalize_month(period)
    active = employees.active_employees_for_month(month)
    existing = payroll.list_payroll(month)
    existing_ids = {str(row.get("employee_id")) for row in existing}
    pending = [row for row in active if str(row.get("id")) not in existing_ids]
    return {
        "action": "payroll",
        "period": month,
        "period_label": period_label(month),
        "active_count": len(active),
        "existing_count": len(existing),
        "pending_count": len(pending),
        "pending_names": [str(row.get("name") or row.get("id")) for row in pending[:10]],
        "warnings": [] if active else ["当前没有在职员工，无法生成工资表。"],
    }


def execute_payroll(period: str, *, operator: str = "AI 代办") -> dict[str, Any]:
    from archiving.backup import create_backup
    from ledger import payroll

    month = payroll.normalize_month(period)
    backup = create_backup("before_auto_payroll", note=f"AI 代办生成工资表 {month}", operator=operator)
    result = payroll.build_payroll(month)
    return {"backup": backup, "result": result, "period": month}


def preview_filing(period: str) -> dict[str, Any]:
    from filing import registry

    quarter = str(period or quarter_period())
    bundles = registry.build_all(quarter)
    forms = []
    total = 0.0
    for bundle in bundles:
        amount = float((bundle.get("totals") or {}).get("total_tax") or 0)
        total += amount
        forms.append(
            {
                "form_name": bundle.get("form_name"),
                "form_id": bundle.get("form_id"),
                "total_tax": amount,
                "summary": (bundle.get("summary") or {}).get("text") or "",
            }
        )
    return {
        "action": "filing",
        "period": quarter,
        "period_label": period_label(quarter),
        "form_count": len(bundles),
        "forms": forms,
        "total_tax": round(total, 2),
        "warnings": [] if bundles else ["该期间没有生成任何申报表。"],
    }


def execute_filing(period: str, *, operator: str = "AI 代办") -> dict[str, Any]:
    from archiving.backup import create_backup
    from archiving.filing_archive import archive_filing_package

    quarter = str(period or quarter_period())
    backup = create_backup("before_auto_filing", note=f"AI 代办生成申报包 {quarter}", operator=operator)
    result = archive_filing_package(quarter, note="AI 代办生成申报包", operator=operator)
    return {"backup": backup, "result": result, "period": quarter}


def preview_ingest() -> dict[str, Any]:
    from ingest import matcher

    summary = matcher.match_summary()
    return {
        "action": "ingest",
        "summary": summary,
        "warnings": [] if summary.get("invoices_total") else ["尚未导入发票；请先到「票据与流水」导入。"],
    }


def execute_ingest(*, min_score: float = 80.0, operator: str = "AI 代办") -> dict[str, Any]:
    """生成匹配建议并确认高置信匹配入账（≥80 分），返回处理明细。"""
    from archiving.backup import create_backup
    from ingest import matcher

    backup = create_backup("before_auto_ingest", note="AI 代办票据匹配入账", operator=operator)
    suggestion = matcher.suggest_matches(min_score=min_score)
    confirmed = []
    skipped = []
    for row in matcher.list_matches("建议"):
        try:
            if float(row.get("score") or 0) >= min_score:
                matcher.confirm_match(str(row.get("id")))
                confirmed.append(str(row.get("id")))
            else:
                skipped.append(str(row.get("id")))
        except Exception as exc:  # noqa: BLE001 - 单条失败不阻塞其他
            skipped.append(f"{row.get('id')}: {exc}")
    return {
        "backup": backup,
        "suggestion": suggestion,
        "confirmed": confirmed,
        "skipped": skipped,
        "confirmed_count": len(confirmed),
    }


def preview_correction() -> dict[str, Any]:
    from ledger import corrections

    scan = corrections.scan_departed_overpayments()
    summary = scan.get("summary") or {}
    return {
        "action": "correction",
        "summary": summary,
        "items": scan.get("items") or [],
        "warnings": [] if summary.get("employees") else ["未发现离职后仍申报的记录。"],
    }


def execute_correction(*, operator: str = "AI 代办") -> dict[str, Any]:
    """扫描并为每位异常员工导出《更正操作单》PDF（不代替扣缴端更正申报）。"""
    from archiving.backup import create_backup
    from ledger import corrections

    backup = create_backup("before_auto_correction", note="AI 代办离职更正操作单", operator=operator)
    scan = corrections.scan_departed_overpayments()
    exported = []
    for item in scan.get("items") or []:
        employee_id = item.get("employee_id")
        if not employee_id:
            continue
        try:
            plan = corrections.build_correction_plan(str(employee_id))
            path = corrections.export_correction_plan_pdf(plan)
            exported.append({"employee": item.get("employee_name"), "path": str(path)})
        except Exception as exc:  # noqa: BLE001
            exported.append({"employee": item.get("employee_name"), "error": str(exc)})
    return {"backup": backup, "exported": exported, "summary": scan.get("summary") or {}}


__all__ = [
    "quarter_period", "month_period",
    "preview_payroll", "execute_payroll",
    "preview_filing", "execute_filing",
    "preview_ingest", "execute_ingest",
    "preview_correction", "execute_correction",
]
