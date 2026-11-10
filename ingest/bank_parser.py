"""银行流水解析：CSV（多编码）与 XLSX（表头探测）→ ``bank_transactions`` 表记录。

方向判定三种来源（按优先级）
----------------------------
1. **收入/支出分列**：非空列为方向；两列都空的行跳过；两列都有值时取绝对值较大者并在 note 说明；
2. **单金额 + 方向列**：方向文本含「收入/收/贷/进/入账」→ 收入，含「支出/付/借/出/出账」→ 支出；
3. **单金额无方向列**：按金额正负判断（负 → 支出），金额统一存正数。

CSV 编码自动尝试 ``utf-8-sig`` / ``gbk`` / ``gb18030``；表头允许出现在前 5 行内（银行导出常见说明行），
列名按 :data:`BANK_COLUMN_ALIASES` 自动识别，也支持调用方传入自定义 ``mapping`` 覆盖。
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from ledger import models
from ledger.store import table

from ingest._common import clean_text, content_hash, parse_date, parse_number

TABLE_NAME = "bank_transactions"
TABLE_LABEL = "银行流水池"

#: 银行流水列名别名（canonical_key → 常见表头）
BANK_COLUMN_ALIASES: dict[str, list[str]] = {
    "date": ["交易日期", "日期", "记账日期", "交易时间"],
    "amount": ["交易金额", "金额", "发生额", "收支金额"],
    "income": ["收入", "收入金额", "贷方发生额", "收入金额(元)", "贷方金额"],
    "expense": ["支出", "支出金额", "借方发生额", "支出金额(元)", "借方金额"],
    "direction": ["收支方向", "方向", "借贷标志", "收付标志", "交易类型"],
    "counterparty": ["对方户名", "对方账户名", "交易对方", "对方单位", "收/付款方"],
    "summary": ["摘要", "交易摘要", "用途", "备注", "交易说明"],
    "transaction_no": ["交易流水号", "流水号", "交易序号", "凭证号"],
    "balance": ["余额", "账户余额", "交易后余额"],
}

#: 方向精确值（借贷标志/收付标志的常见单字值）
_DIRECTION_EXACT: dict[str, str] = {
    "借": "支出",
    "支": "支出",
    "出": "支出",
    "付": "支出",
    "借方": "支出",
    "贷": "收入",
    "收": "收入",
    "进": "收入",
    "贷方": "收入",
}

#: 方向文本关键词（先判断支出，避免「支出」被「收」误判）
_EXPENSE_KEYWORDS = ("支出", "付出", "付款", "借方", "借", "出账", "转出")
_INCOME_KEYWORDS = ("收入", "收款", "贷方", "进账", "入账", "转入", "贷")

_HEADER_SCAN_ROWS = 5


# =============================================================================
# 列识别与表头探测
# =============================================================================
def _normalize_header(value: Any) -> str:
    return clean_text(value).replace("\ufeff", "").replace(" ", "")


def detect_columns(headers: list[str]) -> dict[str, str]:
    """从表头中识别标准列，返回 ``{canonical_key: 原始列名}``（未识别到的为空字符串）。"""
    normalized = [_normalize_header(header) for header in headers]
    result: dict[str, str] = {key: "" for key in BANK_COLUMN_ALIASES}
    used: set[int] = set()
    # 第一轮：完全匹配
    for key, aliases in BANK_COLUMN_ALIASES.items():
        for alias in aliases:
            alias_norm = _normalize_header(alias)
            for index, header in enumerate(normalized):
                if index in used or not header:
                    continue
                if header == alias_norm:
                    result[key] = headers[index]
                    used.add(index)
                    break
            if result[key]:
                break
    # 第二轮：包含匹配（如「收入金额(元)」「交易日期时间」）
    for key, aliases in BANK_COLUMN_ALIASES.items():
        if result[key]:
            continue
        for alias in aliases:
            alias_norm = _normalize_header(alias)
            for index, header in enumerate(normalized):
                if index in used or not header:
                    continue
                if alias_norm and (alias_norm in header or header in alias_norm):
                    result[key] = headers[index]
                    used.add(index)
                    break
            if result[key]:
                break
    return result


def _find_header(rows: list[list[Any]]) -> tuple[int, list[str], dict[str, str]]:
    """在数据前 5 行内寻找表头行，返回 ``(行索引, 表头, 列映射)``；找不到返回 -1。"""
    for index, row in enumerate(rows[:_HEADER_SCAN_ROWS]):
        headers = [clean_text(cell) for cell in row]
        if not any(headers):
            continue
        columns = detect_columns(headers)
        if columns.get("date") and (columns.get("amount") or columns.get("income") or columns.get("expense")):
            return index, headers, columns
    # 兜底：用第一个非空行当表头
    for index, row in enumerate(rows):
        headers = [clean_text(cell) for cell in row]
        if any(headers):
            return index, headers, detect_columns(headers)
    return -1, [], {}


def _merge_mapping(detected: dict[str, str], mapping: dict[str, str] | None) -> dict[str, str]:
    """用调用方提供的映射覆盖自动识别结果（空值不覆盖）。"""
    merged = dict(detected)
    if mapping:
        for key, value in mapping.items():
            if key in merged and clean_text(value):
                merged[key] = clean_text(value)
    return merged


# =============================================================================
# 单行流水规范化
# =============================================================================
def _direction_from_text(text: str) -> str | None:
    """把方向/借贷标志文本转换为 ``收入`` / ``支出``，无法识别返回 ``None``。"""
    value = clean_text(text)
    if not value:
        return None
    if value in _DIRECTION_EXACT:
        return _DIRECTION_EXACT[value]
    if any(keyword in value for keyword in _EXPENSE_KEYWORDS):
        return "支出"
    if any(keyword in value for keyword in _INCOME_KEYWORDS):
        return "收入"
    return None


def _value(raw: dict[str, Any], mapping: dict[str, str], key: str) -> Any:
    """按映射取原始值；映射缺失时回退到 canonical key。"""
    header = clean_text(mapping.get(key))
    if header and header in raw:
        return raw[header]
    if header:
        normalized_header = _normalize_header(header)
        for raw_key, raw_value in raw.items():
            if _normalize_header(raw_key) == normalized_header:
                return raw_value
    return raw.get(key)


def normalize_transaction(
    raw: dict[str, Any],
    mapping: dict[str, str],
    *,
    account_no: str = "",
) -> dict[str, Any]:
    """把一行银行流水统一为 ``bank_transactions`` 表记录。

    缺日期、缺金额、无法判断方向、金额为 0 时抛 :class:`ValueError`（中文说明），
    由解析层捕获并按行记录到 ``errors``。
    """
    if not isinstance(raw, dict):
        raise ValueError("流水原始数据必须是 dict")
    mapping = mapping or {}

    income = parse_number(_value(raw, mapping, "income"), absolute=True)
    expense = parse_number(_value(raw, mapping, "expense"), absolute=True)
    income = income if (income is not None and income > 0) else None
    expense = expense if (expense is not None and expense > 0) else None
    note = ""
    has_split_columns = bool(mapping.get("income") or mapping.get("expense"))

    if has_split_columns:
        if income is None and expense is None:
            raise ValueError("收入与支出列均为空，已跳过")
        if income is not None and expense is not None:
            if income >= expense:
                direction, amount = "收入", income
            else:
                direction, amount = "支出", expense
            note = "收入列与支出列同时有值，按绝对值较大者判定收支方向，请核对。"
        elif income is not None:
            direction, amount = "收入", income
        else:
            direction, amount = "支出", expense
    else:
        signed = parse_number(_value(raw, mapping, "amount"))
        if signed is None or signed == 0:
            raise ValueError("金额为空或为 0，已跳过")
        direction_text = clean_text(_value(raw, mapping, "direction"))
        if direction_text:
            direction = _direction_from_text(direction_text)
            if direction is None:
                raise ValueError(f"无法识别收支方向「{direction_text}」，已跳过")
        else:
            direction = "支出" if signed < 0 else "收入"
        amount = abs(signed)

    transaction_date = parse_date(_value(raw, mapping, "date"))
    if not transaction_date:
        raise ValueError("缺少或无法识别交易日期，已跳过")

    balance = parse_number(_value(raw, mapping, "balance"))
    return {
        "transaction_date": transaction_date,
        "direction": direction,
        "amount": round(float(amount), 2),
        "counterparty": clean_text(_value(raw, mapping, "counterparty")),
        "account_no": clean_text(account_no),
        "summary": clean_text(_value(raw, mapping, "summary")),
        "transaction_no": clean_text(_value(raw, mapping, "transaction_no")),
        "balance": balance if balance is not None else 0.0,
        "status": "待匹配",
        "note": note,
    }


# =============================================================================
# CSV / XLSX 解析
# =============================================================================
def _decode_csv(data: bytes) -> tuple[str, str]:
    """按 utf-8-sig → gbk → gb18030 顺序尝试解码，返回 ``(文本, 编码)``。"""
    for encoding in ("utf-8-sig", "gbk", "gb18030"):
        try:
            return data.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace"), "utf-8(replace)"


def _rows_to_transactions(
    rows: list[list[Any]],
    *,
    mapping: dict[str, str] | None,
    account_no: str,
    source_format: str,
) -> tuple[list[dict[str, Any]], dict[str, str], list[str]]:
    """把二维表转换为流水记录，返回 ``(transactions, columns, errors)``。"""
    header_index, headers, detected = _find_header(rows)
    if header_index < 0:
        return [], {}, ["未能在前 5 行内识别到表头（需要包含日期与金额/收入/支出列）。"]
    columns = _merge_mapping(detected, mapping)
    transactions: list[dict[str, Any]] = []
    errors: list[str] = []
    for offset, row in enumerate(rows[header_index + 1 :]):
        line_number = header_index + offset + 2  # 面向用户的文件行号（1 起）
        values = [str(cell) if cell is not None else "" for cell in row]
        if not any(value.strip() for value in values):
            continue
        raw: dict[str, Any] = {}
        for index, header in enumerate(headers):
            if not header:
                continue
            raw[header] = row[index] if index < len(row) else ""
        try:
            transaction = normalize_transaction(raw, columns, account_no=account_no)
        except ValueError as exc:
            errors.append(f"第 {line_number} 行：{exc}")
            continue
        transaction["source_format"] = source_format
        if transaction.get("note"):
            errors.append(f"第 {line_number} 行提示：{transaction['note']}")
        transactions.append(transaction)
    if not transactions and not errors:
        errors.append("未解析到任何流水记录，请检查表头与数据行。")
    return transactions, columns, errors


def parse_bank_csv(data: bytes, mapping: dict[str, str] | None = None) -> dict[str, Any]:
    """解析 CSV 流水（编码自动尝试），返回 parse_bank_file 约定的结构。"""
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("parse_bank_csv 的 data 必须是 bytes")
    text, encoding = _decode_csv(bytes(data))
    try:
        rows = [row for row in csv.reader(io.StringIO(text))]
    except csv.Error as exc:
        return {
            "ok": False,
            "format": "CSV",
            "transactions": [],
            "columns": {},
            "text": text[:4000],
            "errors": [f"CSV 解析失败（{encoding}）：{exc}"],
        }
    transactions, columns, errors = _rows_to_transactions(
        rows, mapping=mapping, account_no="", source_format="CSV"
    )
    return {
        "ok": bool(transactions),
        "format": "CSV",
        "transactions": transactions,
        "columns": columns,
        "text": text[:4000],
        "errors": errors,
        "encoding": encoding,
    }


def parse_bank_excel(data: bytes, mapping: dict[str, str] | None = None) -> dict[str, Any]:
    """解析 XLSX 流水（自动探测前 5 行内的表头），返回 parse_bank_file 约定的结构。"""
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("parse_bank_excel 的 data 必须是 bytes")
    try:
        workbook = load_workbook(io.BytesIO(bytes(data)), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001 - openpyxl 对损坏文件抛多种异常
        return {
            "ok": False,
            "format": "XLSX",
            "transactions": [],
            "columns": {},
            "text": "",
            "errors": [f"XLSX 解析失败：{exc}"],
        }
    try:
        rows: list[list[Any]] = []
        for sheet in workbook.worksheets:
            sheet_rows = [list(row) for row in sheet.iter_rows(values_only=True)]
            if any(any(clean_text(cell) for cell in row) for row in sheet_rows):
                rows = sheet_rows
                break
    finally:
        workbook.close()
    if not rows:
        return {"ok": False, "format": "XLSX", "transactions": [], "columns": {}, "text": "", "errors": ["XLSX 中没有任何工作表数据。"]}
    transactions, columns, errors = _rows_to_transactions(
        rows, mapping=mapping, account_no="", source_format="XLSX"
    )
    text = "\n".join("\t".join(clean_text(cell) for cell in row) for row in rows[:30])
    return {
        "ok": bool(transactions),
        "format": "XLSX",
        "transactions": transactions,
        "columns": columns,
        "text": text[:4000],
        "errors": errors,
    }


# =============================================================================
# 文件入口与入库
# =============================================================================
def _read_source(source: Any, filename: str | None) -> tuple[bytes, str]:
    """把路径 / bytes / 文件对象统一读成 ``(bytes, filename)``。"""
    if isinstance(source, (bytes, bytearray)):
        if not filename:
            raise ValueError("传入 bytes 时必须提供 filename 以判断文件格式")
        return bytes(source), str(filename)
    if isinstance(source, (str, Path)) or hasattr(source, "__fspath__"):
        path = Path(source)
        name = str(filename or path.name)
        try:
            return path.read_bytes(), name
        except OSError as exc:
            raise ValueError(f"读取文件失败：{path}（{exc}）") from exc
    if hasattr(source, "getvalue"):
        data = source.getvalue()
        name = str(filename or getattr(source, "name", "") or "上传文件")
        if isinstance(data, str):
            data = data.encode("utf-8")
        return bytes(data), name
    if hasattr(source, "read"):
        data = source.read()
        name = str(filename or getattr(source, "name", "") or "上传文件")
        if isinstance(data, str):
            data = data.encode("utf-8")
        return bytes(data), name
    raise ValueError("不支持的文件来源：请传入文件路径、bytes 或文件对象")


def _sniff_format(data: bytes) -> str:
    head = data[:512].lstrip()
    if head.startswith(b"PK\x03\x04"):
        return "XLSX"
    lowered = head.lower()
    if head.startswith(b"<?xml") or b"<worksheet" in lowered:
        return "XLSX"
    return "CSV"


def parse_bank_file(
    source: Any,
    filename: str | None = None,
    *,
    mapping: dict[str, str] | None = None,
    account_no: str = "",
) -> dict[str, Any]:
    """解析银行流水文件（CSV / XLSX），返回 ``{"ok","format","transactions","columns","text","errors"}``。"""
    try:
        data, name = _read_source(source, filename)
    except ValueError as exc:
        return {"ok": False, "format": "UNKNOWN", "transactions": [], "columns": {}, "text": "", "errors": [str(exc)]}
    suffix = Path(name).suffix.lower().lstrip(".")
    fmt = {"csv": "CSV", "txt": "CSV", "xlsx": "XLSX", "xls": "XLSX"}.get(suffix)
    if fmt is None:
        fmt = _sniff_format(data)
    if fmt == "XLSX":
        result = parse_bank_excel(data, mapping)
    else:
        result = parse_bank_csv(data, mapping)
    if account_no:
        for transaction in result["transactions"]:
            transaction["account_no"] = clean_text(account_no)
    for transaction in result["transactions"]:
        if not transaction.get("source_file"):
            transaction["source_file"] = name
    return result


def _transaction_dedup_key(record: dict[str, Any]) -> str:
    """流水去重键：优先流水号；无流水号时用日期+方向+金额+对方+摘要哈希。"""
    transaction_no = clean_text(record.get("transaction_no"))
    if transaction_no:
        return f"NO|{transaction_no}"
    return "HASH|" + content_hash(
        record.get("transaction_date"),
        record.get("direction"),
        record.get("amount"),
        record.get("counterparty"),
        record.get("summary"),
    )


def save_transactions(
    transactions: list[dict[str, Any]],
    source_file: str = "",
) -> dict[str, Any]:
    """流水去重入库，返回 ``{"saved": [...], "skipped": [{"transaction","reason"}]}``。"""
    if not isinstance(transactions, list):
        raise ValueError("transactions 必须是 list")
    bank_table = table(TABLE_NAME)
    existing_keys = {_transaction_dedup_key(row) for row in bank_table.read()}
    saved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for transaction in transactions:
        if not isinstance(transaction, dict):
            skipped.append({"transaction": transaction, "reason": "格式错误：不是 dict"})
            continue
        payload = dict(transaction)
        payload.setdefault("status", "待匹配")
        payload.setdefault("source_format", "CSV")
        if payload.get("status") not in models.INGEST_STATUSES:
            payload["status"] = "待匹配"
        if source_file and not clean_text(payload.get("source_file")):
            payload["source_file"] = source_file
        key = _transaction_dedup_key(payload)
        if key in existing_keys:
            skipped.append({"transaction": payload, "reason": "重复"})
            continue
        existing_keys.add(key)
        saved.append(bank_table.append(payload))
    return {"saved": saved, "skipped": skipped}


__all__ = [
    "BANK_COLUMN_ALIASES",
    "detect_columns",
    "parse_bank_file",
    "parse_bank_csv",
    "parse_bank_excel",
    "normalize_transaction",
    "save_transactions",
]
