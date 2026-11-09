"""申报进度管理：申报记录 upsert、状态流转（草稿 → 已确认 → 已申报）与季度完成度。

* 申报记录（``filings`` 表）以 ``period + form_id`` 唯一，重复保存只更新不新增。
* 保存内容为 bundle 的精简快照（合计、校验结论、生成时间），便于列表展示与审计追溯。
* 每次保存、状态变更、标记已申报都会写 ``filing_log`` 审计日志。
"""

from __future__ import annotations

from typing import Any

import config
from engine import completion_checklist, period_label
from ledger import models
from ledger.audit import log_filing, log_user_action
from ledger.store import table

TABLE_NAME = "filings"
TABLE_LABEL = "申报记录"

_STATUS_ORDER = {"草稿": 0, "已确认": 1, "已申报": 2}
_MAX_CHECKS_IN_SNAPSHOT = 30


# =============================================================================
# 内部工具
# =============================================================================
def _now() -> str:
    return config.now_text()


def _validate_period(period: Any) -> str:
    text = models.normalize_period(period, default="")
    if not text:
        raise ValueError("请提供申报所属期间，如 2026Q1")
    return text


def _validate_status(status: Any) -> str:
    text = str(status or "草稿").strip()
    if text not in models.FILING_STATUSES:
        raise ValueError(f"申报状态「{text}」不合法，可选：{'、'.join(models.FILING_STATUSES)}")
    return text


def _checklist_form_names(period: str) -> dict[str, str]:
    """申报事项清单中的 form_id → 名称映射（含未生成申报表的事项，如个税）。"""
    return {entry["form_id"]: entry["item"] for entry in completion_checklist(period)}


def _snapshot(bundle: dict[str, Any], status: str) -> dict[str, Any]:
    """生成申报表快照（只保留列表展示与审计所需字段）。"""
    totals = bundle.get("totals") or {}
    checks = []
    for check in (bundle.get("checks") or [])[:_MAX_CHECKS_IN_SNAPSHOT]:
        checks.append(
            {
                "field": check.get("field"),
                "status": check.get("status"),
                "message": check.get("message"),
            }
        )
    return {
        "form_id": str(bundle.get("form_id") or ""),
        "form_name": str(bundle.get("form_name") or ""),
        "period": str(bundle.get("period") or ""),
        "period_label": str(bundle.get("period_label") or ""),
        "category": str(bundle.get("category") or ""),
        "generated_at": str(bundle.get("generated_at") or ""),
        "status": status,
        "totals": {key: value for key, value in totals.items() if isinstance(value, (int, float, bool, str))},
        "summary": bundle.get("summary") or {},
        "checks": checks,
        "sections_count": len(bundle.get("sections") or []),
        "disclaimer": str(bundle.get("disclaimer") or config.DISCLAIMER),
    }


def _find(period: str, form_id: str) -> dict[str, Any] | None:
    for row in table(TABLE_NAME).read():
        if str(row.get("period") or "") == period and str(row.get("form_id") or "") == form_id:
            return row
    return None


def _as_record(row: dict[str, Any]) -> dict[str, Any]:
    """把 filings 表中的一行转换为对外记录（data_json 保证为 dict）。"""
    record = dict(row)
    if not isinstance(record.get("data_json"), dict):
        record["data_json"] = {}
    return record


# =============================================================================
# 保存 / 查询 / 状态流转
# =============================================================================
def save_filing(bundle: dict[str, Any], status: str = "草稿") -> dict[str, Any]:
    """保存（或覆盖）一张申报表记录，返回记录详情。

    以 ``period + form_id`` 唯一：已存在时更新状态与数据快照，不存在时新建。
    """
    if not isinstance(bundle, dict):
        raise ValueError("申报表数据必须是 dict")
    period = _validate_period(bundle.get("period"))
    form_id = str(bundle.get("form_id") or "").strip()
    if not form_id:
        raise ValueError("申报表缺少 form_id，无法保存")
    form_name = str(bundle.get("form_name") or form_id).strip()
    status_text = _validate_status(status)
    snapshot = _snapshot(bundle, status_text)
    normalized = models.normalize_filing(
        {
            "period": period,
            "form_id": form_id,
            "form_name": form_name,
            "status": status_text,
            "data_json": snapshot,
            "note": str(bundle.get("note") or ""),
        }
    )
    existing = _find(period, form_id)
    if existing is not None:
        updated = table(TABLE_NAME).update(
            existing["id"],
            {
                "form_name": form_name,
                "status": status_text,
                "data_json": snapshot,
                "note": normalized["note"],
            },
        )
        log_filing(period, form_id, form_name, "保存申报表（覆盖更新）", status_text)
        log_user_action("保存申报表", f"{period} {form_name}，状态：{status_text}")
        return _as_record(updated)
    record = table(TABLE_NAME).append({**normalized, "created_at": _now(), "updated_at": _now()})
    log_filing(period, form_id, form_name, "保存申报表（新建）", status_text)
    log_user_action("保存申报表", f"{period} {form_name}，状态：{status_text}")
    return _as_record(record)


def list_filings(period: Any = None) -> list[dict[str, Any]]:
    """查询申报记录（可按期间过滤），按期间、报表编码排序。"""
    rows = table(TABLE_NAME).read()
    result = []
    for row in rows:
        if period not in (None, ""):
            text = models.normalize_period(period, default="")
            if str(row.get("period") or "") != text:
                continue
        result.append(_as_record(row))
    return sorted(result, key=lambda row: (str(row.get("period") or ""), str(row.get("form_id") or "")))


def get_filing(filing_id: str) -> dict[str, Any] | None:
    """按记录编号读取一条申报记录。"""
    row = table(TABLE_NAME).get(filing_id)
    return _as_record(row) if row else None


def update_status(filing_id: str, status: str) -> dict[str, Any]:
    """推进申报状态（只允许 草稿 → 已确认 → 已申报 依次前进），返回更新后的记录。"""
    current = get_filing(filing_id)
    if current is None:
        raise KeyError(f"{TABLE_LABEL}中不存在编号 {filing_id}")
    new_status = _validate_status(status)
    old_status = _validate_status(current.get("status"))
    if _STATUS_ORDER[new_status] < _STATUS_ORDER[old_status]:
        raise ValueError(f"申报状态不能由「{old_status}」回退为「{new_status}」，请按草稿→已确认→已申报顺序推进")
    updated = table(TABLE_NAME).update(filing_id, {"status": new_status})
    log_filing(
        str(updated.get("period") or ""),
        str(updated.get("form_id") or ""),
        str(updated.get("form_name") or ""),
        f"状态变更：{old_status}→{new_status}",
        new_status,
    )
    log_user_action(
        "申报状态变更",
        f"编号 {filing_id}：{old_status}→{new_status}",
    )
    return _as_record(updated)


# =============================================================================
# 进度与标记
# =============================================================================
def progress(period: Any) -> dict[str, Any]:
    """返回某期间的申报完成度：``total`` / ``done`` / ``percent`` / ``items``。"""
    text = _validate_period(period)
    checklist = completion_checklist(text)
    filings = {str(row.get("form_id") or ""): row for row in list_filings(text)}
    items: list[dict[str, Any]] = []
    done = 0
    submitted = 0
    for entry in checklist:
        form_id = str(entry.get("form_id") or "")
        record = filings.get(form_id)
        external = bool(entry.get("external"))
        if record:
            status = str(record.get("status") or "")
        else:
            status = "扣缴端申报" if external else "未生成"
        if status in ("已确认", "已申报"):
            done += 1
        if status == "已申报":
            submitted += 1
        items.append(
            {
                "form_id": form_id,
                "form_name": str((record or {}).get("form_name") or entry.get("item") or form_id),
                "status": status,
                "required": bool(entry.get("required")),
                "external": external,
                "note": str(entry.get("note") or ""),
                "updated_at": str((record or {}).get("updated_at") or ""),
            }
        )
    total = len(checklist)
    percent = round(done / total * 100, 1) if total else 0.0
    return {
        "period": text,
        "period_label": period_label(text),
        "total": total,
        "done": done,
        "submitted": submitted,
        "percent": percent,
        "items": items,
        "disclaimer": config.DISCLAIMER,
    }


def mark_submitted(period: Any, form_id: str) -> dict[str, Any]:
    """把某张申报表标记为「已申报」（不存在记录时按台账生成后保存）。"""
    text = _validate_period(period)
    form_key = str(form_id or "").strip()
    if not form_key:
        raise ValueError("请提供报表编码 form_id")
    existing = _find(text, form_key)
    if existing is not None:
        return update_status(existing["id"], "已申报")

    bundle: dict[str, Any] | None = None
    try:
        from filing.registry import build_form

        bundle = build_form(form_key, text)
    except KeyError:
        names = _checklist_form_names(text)
        bundle = {
            "form_id": form_key,
            "form_name": names.get(form_key) or form_key,
            "category": "其他",
            "period": text,
            "period_label": period_label(text),
            "frequency": "monthly",
            "generated_at": config.now_text(),
            "status": "已申报",
            "sections": [],
            "totals": {},
            "calc": {},
            "checks": [],
            "summary": {
                "level": "PASS",
                "text": "该申报事项在电子税务局（自然人扣缴端）办理，本系统仅记录申报状态。",
            },
            "guide": ["请登录对应申报系统完成申报，并留存申报表与完税凭证备查。", config.DISCLAIMER],
            "data": {"source": "manual"},
            "disclaimer": config.DISCLAIMER,
        }
    return save_filing(bundle, status="已申报")


__all__ = [
    "save_filing",
    "list_filings",
    "get_filing",
    "update_status",
    "progress",
    "mark_submitted",
]
