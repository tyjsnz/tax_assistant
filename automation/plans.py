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
    try:
        from closing.close import is_closed

        closed = bool(is_closed(month))
    except Exception:  # noqa: BLE001
        closed = False
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
        "closed": closed,
        "warnings": (["该期间已结账：流水线会跳过工资生成，避免改动已锁定数据。"] if closed else [])
        + ([] if active else ["当前没有在职员工，无法生成工资表。"]),
    }


def _run_payroll(period: str) -> dict[str, Any]:
    from ledger import payroll

    month = payroll.normalize_month(period)
    return {"result": payroll.build_payroll(month), "period": month}


def execute_payroll(period: str, *, operator: str = "AI 代办") -> dict[str, Any]:
    from archiving.backup import create_backup

    month = month_period() if not period else period
    backup = create_backup("before_auto_payroll", note=f"AI 代办生成工资表 {month}", operator=operator)
    result = _run_payroll(month)
    result["backup"] = backup
    return result


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


def _run_filing(period: str, *, operator: str = "AI 代办") -> dict[str, Any]:
    from archiving.filing_archive import archive_filing_package

    quarter = str(period or quarter_period())
    return {"result": archive_filing_package(quarter, note="AI 代办生成申报包", operator=operator), "period": quarter}


def execute_filing(period: str, *, operator: str = "AI 代办") -> dict[str, Any]:
    from archiving.backup import create_backup

    quarter = str(period or quarter_period())
    backup = create_backup("before_auto_filing", note=f"AI 代办生成申报包 {quarter}", operator=operator)
    result = _run_filing(quarter, operator=operator)
    result["backup"] = backup
    return result


def preview_ingest() -> dict[str, Any]:
    from ingest import matcher

    summary = matcher.match_summary()
    return {
        "action": "ingest",
        "summary": summary,
        "warnings": [] if summary.get("invoices_total") else ["尚未导入发票；请先到「票据与流水」导入。"],
    }


def _run_ingest(*, min_score: float = 80.0) -> dict[str, Any]:
    from ingest import matcher

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
    return {"suggestion": suggestion, "confirmed": confirmed, "skipped": skipped, "confirmed_count": len(confirmed)}


def execute_ingest(*, min_score: float = 80.0, operator: str = "AI 代办") -> dict[str, Any]:
    """生成匹配建议并确认高置信匹配入账（≥80 分），返回处理明细。"""
    from archiving.backup import create_backup

    backup = create_backup("before_auto_ingest", note="AI 代办票据匹配入账", operator=operator)
    result = _run_ingest(min_score=min_score)
    result["backup"] = backup
    return result


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


def _run_correction() -> dict[str, Any]:
    from ledger import corrections

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
    return {"exported": exported, "summary": scan.get("summary") or {}}


def execute_correction(*, operator: str = "AI 代办") -> dict[str, Any]:
    """扫描并为每位异常员工导出《更正操作单》PDF（不代替扣缴端更正申报）。"""
    from archiving.backup import create_backup

    backup = create_backup("before_auto_correction", note="AI 代办离职更正操作单", operator=operator)
    result = _run_correction()
    result["backup"] = backup
    return result


def quarter_months(quarter: str) -> list[str]:
    """把 2026Q4 转成 ['2026-10','2026-11','2026-12']。"""
    text = str(quarter or "").strip().upper()
    try:
        year_text, q_text = text.split("Q")
        year = int(year_text)
        q = int(q_text)
    except (ValueError, AttributeError):
        now = config.today()
        year = now.year
        q = (now.month - 1) // 3 + 1
    start = (q - 1) * 3 + 1
    return [f"{year}-{month:02d}" for month in range(start, start + 3)]


def preview_pipeline(quarter: str | None = None) -> dict[str, Any]:
    """一键季度流水线预览：票据 → 工资 → 离职异常 → 申报包。"""
    period = str(quarter or quarter_period())
    months = quarter_months(period)
    ingest = preview_ingest()
    payrolls = [preview_payroll(month) for month in months]
    correction = preview_correction()
    filing = preview_filing(period)
    steps = [
        {"key": "ingest", "title": "票据匹配入账", "summary": f"待匹配发票 {ingest['summary'].get('invoices_pending', 0)} 张"},
        {"key": "payroll", "title": "生成工资表", "summary": f"待生成 {sum(item.get('pending_count', 0) for item in payrolls)} 人"},
        {"key": "correction", "title": "扫描离职异常", "summary": f"异常员工 {correction['summary'].get('employees', 0)} 人"},
        {"key": "filing", "title": "生成并归档申报包", "summary": f"{filing.get('form_count', 0)} 张表 / ¥{filing.get('total_tax', 0):,.2f}"},
    ]
    warnings = []
    warnings.extend(ingest.get("warnings") or [])
    for item in payrolls:
        warnings.extend(item.get("warnings") or [])
    warnings.extend(filing.get("warnings") or [])
    return {
        "action": "pipeline",
        "period": period,
        "months": months,
        "ingest": ingest,
        "payrolls": payrolls,
        "correction": correction,
        "filing": filing,
        "steps": steps,
        "warnings": warnings,
    }


def execute_pipeline(
    quarter: str | None = None,
    *,
    min_score: float = 80.0,
    include_ingest: bool = True,
    include_payroll: bool = True,
    include_correction: bool = True,
    include_filing: bool = True,
    operator: str = "AI 代办",
) -> dict[str, Any]:
    """按顺序执行季度流水线：票据 → 工资 → 离职异常 → 申报包；只备份一次。"""
    from archiving.backup import create_backup

    period = str(quarter or quarter_period())
    backup = create_backup("before_auto_pipeline", note=f"AI 代办季度流水线 {period}", operator=operator)
    steps: list[dict[str, Any]] = []

    if include_ingest:
        try:
            steps.append({"key": "ingest", "ok": True, "result": _run_ingest(min_score=min_score)})
        except Exception as exc:  # noqa: BLE001
            steps.append({"key": "ingest", "ok": False, "error": str(exc)})

    if include_payroll:
        for month in quarter_months(period):
            try:
                from closing.close import is_closed

                if is_closed(month):
                    steps.append({"key": "payroll", "month": month, "ok": True, "skipped": True,
                                  "note": "已结账，跳过工资生成"})
                    continue
            except Exception:  # noqa: BLE001
                pass
            try:
                steps.append({"key": "payroll", "month": month, "ok": True, "result": _run_payroll(month)})
            except Exception as exc:  # noqa: BLE001
                steps.append({"key": "payroll", "month": month, "ok": False, "error": str(exc)})

    if include_correction:
        try:
            steps.append({"key": "correction", "ok": True, "result": _run_correction()})
        except Exception as exc:  # noqa: BLE001
            steps.append({"key": "correction", "ok": False, "error": str(exc)})

    if include_filing:
        try:
            steps.append({"key": "filing", "ok": True, "result": _run_filing(period, operator=operator)})
        except Exception as exc:  # noqa: BLE001
            steps.append({"key": "filing", "ok": False, "error": str(exc)})

    return {"period": period, "backup": backup, "steps": steps, "ok": all(item.get("ok") for item in steps)}


__all__ = [
    "quarter_period", "month_period",
    "preview_payroll", "execute_payroll",
    "preview_filing", "execute_filing",
    "preview_ingest", "execute_ingest",
    "preview_correction", "execute_correction",
    "preview_pipeline", "execute_pipeline", "quarter_months",
]
