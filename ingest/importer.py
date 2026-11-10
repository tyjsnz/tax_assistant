"""导入编排：多文件批量导入、手动录入发票、票据/流水池汇总与清空。

文件来源统一支持 ``[(filename, bytes)]`` 与 Streamlit 上传对象（``.name`` + ``.getvalue()``），
按扩展名分发：``xml/ofd/pdf`` → 发票解析；``csv/xlsx`` → 银行流水解析。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Iterator

import config
from ledger import models
from ledger.audit import log_user_action
from ledger.store import table

from ingest.bank_parser import parse_bank_file, save_transactions
from ingest.invoice_parser import normalize_invoice, parse_invoice_bytes, save_invoices
from ingest.matcher import match_summary, suggest_matches

INVOICE_TABLE = "invoices"
TRANSACTION_TABLE = "bank_transactions"
MATCH_TABLE = "matches"

_INVOICE_EXTENSIONS = {"xml", "ofd", "pdf"}
_BANK_EXTENSIONS = {"csv", "txt", "xlsx", "xls"}


# =============================================================================
# 文件迭代
# =============================================================================
def _iter_files(files: Any) -> Iterator[tuple[str, bytes]]:
    """把多种传入形式统一迭代为 ``(filename, bytes)``。"""
    if files is None:
        return
    if isinstance(files, (str, Path)):
        files = [files]
    elif isinstance(files, tuple) and len(files) == 2 and isinstance(files[1], (bytes, bytearray)):
        files = [files]
    for item in files:
        if isinstance(item, (str, Path)):
            path = Path(item)
            yield path.name, path.read_bytes()
            continue
        if isinstance(item, tuple) and len(item) == 2:
            name, data = item
            if isinstance(data, str):
                data = data.encode("utf-8")
            yield str(name), bytes(data)
            continue
        name = str(getattr(item, "name", None) or getattr(item, "filename", None) or "未命名文件")
        if hasattr(item, "getvalue"):
            data = item.getvalue()
        elif hasattr(item, "read"):
            data = item.read()
        else:  # pragma: no cover - 传入不可识别的对象
            raise ValueError(f"无法读取上传文件：{item!r}，请传入 (filename, bytes) 或文件对象。")
        if isinstance(data, str):
            data = data.encode("utf-8")
        yield name, bytes(data)


def _extension(filename: str) -> str:
    return Path(str(filename or "")).suffix.lower().lstrip(".")


def _empty_result() -> dict[str, Any]:
    return {
        "parsed": 0,
        "saved": 0,
        "skipped": 0,
        "errors": [],
        "by_format": {},
        "saved_records": [],
        "skipped_details": [],
        "match": None,
    }


# =============================================================================
# 批量导入
# =============================================================================
def import_invoice_files(files: Any, *, auto_match: bool = False) -> dict[str, Any]:
    """批量导入发票文件，返回解析/入库统计（``parsed/saved/skipped/errors/by_format``）。"""
    result = _empty_result()
    for name, data in _iter_files(files):
        try:
            parsed = parse_invoice_bytes(data, name)
        except ValueError as exc:
            result["errors"].append({"file": name, "error": str(exc)})
            continue
        if not parsed.get("ok"):
            message = "；".join(parsed.get("errors") or []) or "发票解析失败"
            result["errors"].append({"file": name, "error": message})
        else:
            fmt = str(parsed.get("format") or "UNKNOWN")
            result["by_format"][fmt] = result["by_format"].get(fmt, 0) + 1
        invoices = list(parsed.get("invoices") or [])
        result["parsed"] += len(invoices)
        if not invoices:
            continue
        try:
            saved = save_invoices(invoices, source_file=name)
        except (ValueError, KeyError) as exc:
            result["errors"].append({"file": name, "error": f"入库失败：{exc}"})
            continue
        result["saved_records"].extend(saved["saved"])
        result["saved"] += len(saved["saved"])
        result["skipped"] += len(saved["skipped"])
        result.setdefault("skipped_details", []).extend(saved["skipped"])
    result["invoices"] = result["saved"]
    result["transactions"] = 0
    if auto_match:
        result["match"] = suggest_matches()
    return result


def import_bank_files(
    files: Any,
    *,
    mapping: dict[str, str] | None = None,
    account_no: str = "",
    auto_match: bool = False,
) -> dict[str, Any]:
    """批量导入银行流水文件（CSV/XLSX），返回解析/入库统计。"""
    result = _empty_result()
    for name, data in _iter_files(files):
        try:
            parsed = parse_bank_file(data, name, mapping=mapping, account_no=account_no)
        except ValueError as exc:
            result["errors"].append({"file": name, "error": str(exc)})
            continue
        if not parsed.get("ok"):
            message = "；".join(parsed.get("errors") or []) or "流水解析失败"
            result["errors"].append({"file": name, "error": message})
        else:
            fmt = str(parsed.get("format") or "UNKNOWN")
            result["by_format"][fmt] = result["by_format"].get(fmt, 0) + 1
        transactions = list(parsed.get("transactions") or [])
        result["parsed"] += len(transactions)
        if not transactions:
            continue
        try:
            saved = save_transactions(transactions, source_file=name)
        except (ValueError, KeyError) as exc:
            result["errors"].append({"file": name, "error": f"入库失败：{exc}"})
            continue
        result["saved_records"].extend(saved["saved"])
        result["saved"] += len(saved["saved"])
        result["skipped"] += len(saved["skipped"])
        result.setdefault("skipped_details", []).extend(saved["skipped"])
    result["invoices"] = 0
    result["transactions"] = result["saved"]
    if auto_match:
        result["match"] = suggest_matches()
    return result


def import_files(files: Any, *, auto_match: bool = True) -> dict[str, Any]:
    """按扩展名自动分发导入（发票 XML/OFD/PDF、流水 CSV/XLSX），可自动生成匹配建议。"""
    entries = list(_iter_files(files))
    invoice_files: list[tuple[str, bytes]] = []
    bank_files: list[tuple[str, bytes]] = []
    errors: list[dict[str, str]] = []
    for name, data in entries:
        extension = _extension(name)
        if extension in _INVOICE_EXTENSIONS:
            invoice_files.append((name, data))
        elif extension in _BANK_EXTENSIONS:
            bank_files.append((name, data))
        else:
            errors.append({"file": name, "error": f"不支持的文件类型：{name}（仅支持 xml/ofd/pdf/csv/xlsx）"})

    invoice_result = import_invoice_files(invoice_files) if invoice_files else _empty_result()
    bank_result = import_bank_files(bank_files) if bank_files else _empty_result()
    by_format: dict[str, int] = {}
    for source in (invoice_result["by_format"], bank_result["by_format"]):
        for key, value in source.items():
            by_format[key] = by_format.get(key, 0) + int(value)
    skipped_details = list(invoice_result.get("skipped_details") or []) + list(
        bank_result.get("skipped_details") or []
    )
    return {
        "parsed": invoice_result["parsed"] + bank_result["parsed"],
        "saved": invoice_result["saved"] + bank_result["saved"],
        "skipped": skipped_details,
        "skipped_count": len(skipped_details),
        "errors": errors + invoice_result["errors"] + bank_result["errors"],
        "by_format": by_format,
        "saved_records": invoice_result["saved_records"] + bank_result["saved_records"],
        "invoices": invoice_result["saved"],
        "transactions": bank_result["saved"],
        "files": len(entries),
        "file_names": [name for name, _ in entries],
        "match": suggest_matches() if auto_match else None,
    }


# =============================================================================
# 手动录入
# =============================================================================
def add_manual_invoice(payload: dict[str, Any]) -> dict[str, Any]:
    """手动录入一张发票并入库，返回保存后的记录；重复录入抛 ``ValueError``。"""
    if not isinstance(payload, dict):
        raise ValueError("手动录入数据必须是 dict")
    invoice_no = str(payload.get("invoice_no") or "").strip()
    total_inclusive = payload.get("total_inclusive")
    if not invoice_no and not total_inclusive:
        raise ValueError("手动录入发票至少需要「发票号码」或「价税合计」。")
    raw = {
        "invoice_no": invoice_no,
        "invoice_code": payload.get("invoice_code"),
        "invoice_date": payload.get("invoice_date"),
        "invoice_type": payload.get("invoice_type"),
        "direction": payload.get("direction"),
        "seller": payload.get("seller"),
        "buyer": payload.get("buyer"),
        "amount_exclusive": payload.get("amount_exclusive"),
        "tax_amount": payload.get("tax_amount"),
        "total_inclusive": total_inclusive,
        "note": payload.get("note") or "手动录入",
        "raw_json": {"manual": True},
    }
    record = normalize_invoice(raw, source_format="手动")
    if not record.get("invoice_date"):
        record["invoice_date"] = models.normalize_date(None)
    saved = save_invoices([record], source_file="手动录入")
    if not saved["saved"]:
        raise ValueError(f"发票 {invoice_no or '（无号码）'} 已存在，请勿重复录入。")
    log_user_action(
        "手动录入发票",
        f"发票 {record.get('invoice_no') or '—'}，价税合计 {record.get('total_inclusive'):.2f} 元，"
        f"方向 {record.get('direction')}",
    )
    return saved["saved"][0]


# =============================================================================
# 池管理
# =============================================================================
def list_pool_records(kind: str, status: str | None = None) -> list[dict[str, Any]]:
    """列出票据池（``invoices``）或流水池（``bank_transactions``）记录，可按状态过滤。"""
    table_name = {"invoices": INVOICE_TABLE, "transactions": TRANSACTION_TABLE}.get(str(kind))
    if not table_name:
        raise ValueError(f"未知的池类型：{kind}，应为 invoices 或 transactions")
    rows = table(table_name).read()
    if status:
        rows = [row for row in rows if str(row.get("status")) == str(status)]
    return rows


def delete_pool_record(kind: str, record_id: str) -> bool:
    """删除票据/流水记录（同时删除关联的匹配建议），返回是否删除成功。"""
    table_name = {"invoices": INVOICE_TABLE, "transactions": TRANSACTION_TABLE}.get(str(kind))
    if not table_name:
        raise ValueError(f"未知的池类型：{kind}，应为 invoices 或 transactions")
    deleted = table(table_name).delete(record_id)
    if not deleted:
        return False
    key = "invoice_id" if kind == "invoices" else "transaction_id"
    match_table = table(MATCH_TABLE)
    for match in match_table.read():
        if str(match.get(key)) == str(record_id):
            match_table.delete(match["id"])
    log_user_action("删除票据池记录", f"{table_name} 删除编号 {record_id}")
    return True


def update_pool_status(kind: str, record_id: str, status: str) -> dict[str, Any]:
    """修改票据/流水状态（UI 表格编辑用），返回更新后的记录。"""
    table_name = {"invoices": INVOICE_TABLE, "transactions": TRANSACTION_TABLE}.get(str(kind))
    if not table_name:
        raise ValueError(f"未知的池类型：{kind}，应为 invoices 或 transactions")
    status_text = str(status or "").strip()
    if status_text not in models.INGEST_STATUSES:
        raise ValueError(f"票据状态「{status_text}」不合法，可选：{'、'.join(models.INGEST_STATUSES)}")
    return table(table_name).update(record_id, {"status": status_text})


def pool_summary() -> dict[str, Any]:
    """票据池 / 流水池 / 匹配池汇总（数量、状态、方向、类型、金额与最近导入时间）。"""
    invoices = table(INVOICE_TABLE).read()
    transactions = table(TRANSACTION_TABLE).read()
    matches = table(MATCH_TABLE).read()

    def _distribution(rows: list[dict[str, Any]], key: str, values: Iterable[str]) -> dict[str, int]:
        counts = {value: 0 for value in values}
        for row in rows:
            value = str(row.get(key) or "")
            if value in counts:
                counts[value] += 1
        return counts

    invoice_status = _distribution(invoices, "status", models.INGEST_STATUSES)
    invoice_direction = _distribution(invoices, "direction", models.INVOICE_DIRECTIONS)
    invoice_type = _distribution(invoices, "invoice_type", models.INVOICE_KINDS)
    transaction_status = _distribution(transactions, "status", models.INGEST_STATUSES)
    transaction_direction = _distribution(transactions, "direction", ("收入", "支出"))
    match_status = _distribution(matches, "status", models.MATCH_STATUSES)
    created_at_values = [str(row.get("created_at") or "") for row in invoices + transactions]
    last_import_at = max(created_at_values) if created_at_values else ""
    return {
        "invoices": {
            "count": len(invoices),
            "by_status": invoice_status,
            "by_direction": invoice_direction,
            "by_type": invoice_type,
            "amount": round(float(sum(float(row.get("total_inclusive") or 0) for row in invoices)), 2),
        },
        "transactions": {
            "count": len(transactions),
            "by_status": transaction_status,
            "by_direction": transaction_direction,
            "amount": round(float(sum(float(row.get("amount") or 0) for row in transactions)), 2),
        },
        "matches": {
            "count": len(matches),
            "by_status": match_status,
        },
        "last_import_at": last_import_at,
        "summary": match_summary(),
        "disclaimer": config.DISCLAIMER,
    }


def reset_pool() -> dict[str, Any]:
    """清空票据池、流水池与匹配建议（保留台账与日志），返回清空条数。"""
    cleared = {
        "invoices": table(INVOICE_TABLE).count(),
        "transactions": table(TRANSACTION_TABLE).count(),
        "matches": table(MATCH_TABLE).count(),
    }
    for table_name in (INVOICE_TABLE, TRANSACTION_TABLE, MATCH_TABLE):
        table(table_name).replace_all([])
    log_user_action("清空票据流水池", f"发票 {cleared['invoices']} 张、流水 {cleared['transactions']} 笔、匹配 {cleared['matches']} 条")
    return {"ok": True, "cleared": cleared}


__all__ = [
    "import_invoice_files",
    "import_bank_files",
    "import_files",
    "add_manual_invoice",
    "list_pool_records",
    "delete_pool_record",
    "update_pool_status",
    "pool_summary",
    "reset_pool",
]
