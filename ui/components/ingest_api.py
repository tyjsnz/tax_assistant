"""票据与流水导入的后端访问封装（``ingest.*``）。

设计目的
--------
1. 页面只依赖本模块的稳定签名，后端 ``ingest/*`` 由队友并行开发，暂缺时用
   :mod:`ui.components.runtime` 容错降级，页面不白屏；
2. 池表（``invoices`` / ``bank_transactions`` / ``matches``）按契约第 1 节直接通过
   ``ledger.store.table`` 读写（后端也可能提供汇总接口，优先使用）；
3. 银行流水的「表头探测 + 列映射」在 UI 侧先做预览，便于用户纠正列映射后再导入。
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, Iterable, Sequence

import ledger
from engine.money import round2
from ledger import models
from ui.components import runtime

#: 支持的扩展名
INVOICE_EXTENSIONS: tuple[str, ...] = (".xml", ".ofd", ".pdf")
BANK_EXTENSIONS: tuple[str, ...] = (".csv", ".xlsx", ".xls")

#: 池表名映射（UI 语义 → ledger 表名）
POOL_TABLES = {"invoices": "invoices", "transactions": "bank_transactions"}

#: 枚举（来自 ledger.models，只读引用）
INGEST_STATUSES = models.INGEST_STATUSES
INVOICE_DIRECTIONS = models.INVOICE_DIRECTIONS
INVOICE_KINDS = models.INVOICE_KINDS
MATCH_STATUSES = models.MATCH_STATUSES

#: 银行流水标准列（canonical key → 中文标签）
BANK_COLUMN_LABELS: dict[str, str] = {
    "date": "交易日期",
    "amount": "交易金额",
    "income": "收入列",
    "expense": "支出列",
    "direction": "收支方向",
    "counterparty": "对方户名",
    "summary": "摘要",
    "transaction_no": "交易流水号",
    "balance": "余额",
}

#: 后端 ``detect_columns`` 缺失时的兜底别名（与契约第 3 节一致）
_FALLBACK_BANK_ALIASES: dict[str, list[str]] = {
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

#: 自动匹配默认参数
DEFAULT_DATE_WINDOW = 15
DEFAULT_MIN_SCORE = 60.0


# =============================================================================
# 模块加载
# =============================================================================
def _load(name: str):
    return runtime.load(f"ingest.{name}")


def _has(module_name: str, *attrs: str) -> bool:
    """模块存在且至少提供一个所需属性。"""
    module = _load(module_name)
    if module is None:
        return False
    return any(hasattr(module, attr) for attr in attrs)


def demo_ready() -> bool:
    """一键加载示例是否可用（``ingest.demo.seed_demo_pool``）。"""
    return _has("demo", "seed_demo_pool")


def invoice_import_ready() -> bool:
    """发票导入是否可用。"""
    return _has("importer", "import_invoice_files", "import_files") or _has(
        "invoice_parser", "parse_invoice_file"
    )


def bank_import_ready() -> bool:
    """银行流水导入是否可用。"""
    return _has("importer", "import_bank_files", "import_files") or _has("bank_parser", "parse_bank_file")


def manual_invoice_ready() -> bool:
    """手动录入发票是否可用。"""
    return _has("importer", "add_manual_invoice") or _has("invoice_parser", "normalize_invoice")


def match_ready() -> bool:
    """自动匹配 / 确认入账是否可用。"""
    return _has("matcher", "suggest_matches", "confirm_match")


def available() -> bool:
    """后端 ingest 包是否可用（核心导入或匹配能力就绪）。"""
    return any(
        (
            demo_ready(),
            invoice_import_ready(),
            bank_import_ready(),
            match_ready(),
        )
    )


def missing_detail() -> str:
    """后端缺失原因（用于页面提示）。"""
    return (
        runtime.error_of("ingest")
        or runtime.error_of("ingest.importer")
        or runtime.error_of("ingest.matcher")
        or runtime.error_of("ingest.invoice_parser")
        or ""
    )


def _require(name: str, feature: str):
    module = _load(name)
    if module is None:
        raise ValueError(f"{feature}暂不可用：ingest.{name} 模块尚未就绪（后端由队友并行开发中）。")
    return module


# =============================================================================
# 演示数据 / 池操作
# =============================================================================
def seed_demo_pool(reset: bool = False) -> dict:
    """一键导入 ``examples/`` 示例发票与流水并自动匹配。"""
    module = _require("demo", "一键加载示例票据/流水")
    if not hasattr(module, "seed_demo_pool"):
        raise ValueError("一键加载示例暂不可用：ingest.demo.seed_demo_pool 未实现。")
    return dict(module.seed_demo_pool(reset=reset))


def reset_pool() -> dict:
    """清空票据池、流水池与匹配建议。"""
    importer = _load("importer")
    if importer is not None and hasattr(importer, "reset_pool"):
        return dict(importer.reset_pool())
    counts = {}
    for _, table_name in POOL_TABLES.items():
        table = ledger.table(table_name)
        counts[table_name] = table.count()
        table.replace_all([])
    matches = ledger.table("matches")
    counts["matches"] = matches.count()
    matches.replace_all([])
    return {"ok": True, "cleared": counts}


def pool_summary() -> dict:
    """票据池 / 流水池汇总（优先后端，缺失时本地统计）。"""
    importer = _load("importer")
    if importer is not None and hasattr(importer, "pool_summary"):
        try:
            return dict(importer.pool_summary())
        except Exception:  # noqa: BLE001 - 汇总失败不影响页面
            pass
    return _local_pool_summary()


def match_summary() -> dict:
    """匹配建议汇总（优先后端，缺失时本地统计）。"""
    matcher = _load("matcher")
    if matcher is not None and hasattr(matcher, "match_summary"):
        try:
            return dict(matcher.match_summary())
        except Exception:  # noqa: BLE001
            pass
    return _local_match_summary()


def overview() -> dict:
    """页面指标卡数据：合并池汇总与匹配汇总，并补默认值。"""
    pool = pool_summary()
    matches = match_summary()
    invoices = pool.get("invoices") or {}
    transactions = pool.get("transactions") or {}
    by_status = invoices.get("by_status") or {}
    tx_by_status = transactions.get("by_status") or {}
    # 后端 match_summary 中 matched_income / matched_expense 是「笔数」，
    # 金额在 matched_income_amount / matched_expense_amount；本地兜底实现则直接是金额。
    matched_income = matches.get("matched_income_amount")
    if matched_income is None:
        matched_income = matches.get("matched_income") or 0
    matched_expense = matches.get("matched_expense_amount")
    if matched_expense is None:
        matched_expense = matches.get("matched_expense") or 0
    result = {
        "invoices_total": int(invoices.get("count") or 0),
        "invoices_pending": int(matches.get("invoices_pending", by_status.get("待匹配", 0)) or 0),
        "invoices_matched": int(matches.get("invoices_matched", by_status.get("已匹配", 0)) or 0),
        "invoices_booked": int(by_status.get("已入账", 0) or 0),
        "transactions_total": int(transactions.get("count") or 0),
        "transactions_pending": int(matches.get("transactions_pending", tx_by_status.get("待匹配", 0)) or 0),
        "transactions_booked": int(tx_by_status.get("已入账", 0) or 0),
        "suggestions": int(matches.get("suggestions") or 0),
        "confirmed": int(matches.get("confirmed") or 0),
        "ignored": int(matches.get("ignored") or 0),
        "matched_income": round2(matched_income),
        "matched_expense": round2(matched_expense),
        "invoice_amount": round2(invoices.get("amount") or 0),
        "transaction_amount": round2(transactions.get("amount") or 0),
        "last_import_at": str(pool.get("last_import_at") or ""),
    }
    result["booked_total"] = round2(result["matched_income"] + result["matched_expense"])
    return result


def _local_pool_summary() -> dict:
    """本地池汇总（后端缺失时兜底）。"""
    invoices = _read_table("invoices")
    transactions = _read_table("bank_transactions")
    matches = _read_table("matches")

    def _aggregate(rows: list[dict], status_key: str = "status", amount_key: str = "total_inclusive") -> dict:
        by_status: dict[str, int] = {}
        by_direction: dict[str, int] = {}
        by_type: dict[str, int] = {}
        amount = 0.0
        for row in rows:
            status = str(row.get(status_key) or "待匹配")
            by_status[status] = by_status.get(status, 0) + 1
            direction = str(row.get("direction") or "")
            if direction:
                by_direction[direction] = by_direction.get(direction, 0) + 1
            kind = str(row.get("invoice_type") or "")
            if kind:
                by_type[kind] = by_type.get(kind, 0) + 1
            try:
                amount = round2(amount + float(row.get(amount_key) or 0))
            except (TypeError, ValueError):
                continue
        return {
            "count": len(rows),
            "by_status": by_status,
            "by_direction": by_direction,
            "by_type": by_type,
            "amount": amount,
        }

    timestamps = [str(row.get("created_at") or "") for row in invoices + transactions + matches]
    return {
        "invoices": _aggregate(invoices),
        "transactions": _aggregate(transactions, amount_key="amount"),
        "matches": {
            "count": len(matches),
            "by_status": {
                status: sum(1 for row in matches if str(row.get("status")) == status)
                for status in MATCH_STATUSES
            },
        },
        "last_import_at": max(timestamps) if timestamps else "",
    }


def _local_match_summary() -> dict:
    """本地匹配汇总（后端缺失时兜底）。"""
    matches = _read_table("matches")
    invoices = {str(row.get("id")): row for row in _read_table("invoices")}
    transactions = {str(row.get("id")): row for row in _read_table("bank_transactions")}
    matched_income = 0.0
    matched_expense = 0.0
    for match in matches:
        if str(match.get("status")) not in {"已确认", "建议"}:
            continue
        invoice = invoices.get(str(match.get("invoice_id"))) or {}
        direction = str(invoice.get("direction") or "")
        try:
            amount = float(invoice.get("total_inclusive") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        if direction == "销项":
            matched_income = round2(matched_income + amount)
        elif direction == "进项":
            matched_expense = round2(matched_expense + amount)
    return {
        "invoices_pending": sum(1 for row in invoices.values() if str(row.get("status")) == "待匹配"),
        "invoices_matched": sum(1 for row in invoices.values() if str(row.get("status")) == "已匹配"),
        "transactions_pending": sum(
            1 for row in transactions.values() if str(row.get("status")) == "待匹配"
        ),
        "suggestions": sum(1 for row in matches if str(row.get("status")) == "建议"),
        "confirmed": sum(1 for row in matches if str(row.get("status")) == "已确认"),
        "ignored": sum(1 for row in matches if str(row.get("status")) == "已忽略"),
        "matched_income": matched_income,
        "matched_expense": matched_expense,
    }


def _read_table(name: str) -> list[dict]:
    try:
        return list(ledger.table(name).read())
    except Exception:  # noqa: BLE001
        return []


# =============================================================================
# 发票
# =============================================================================
def import_invoice_files(
    files: Iterable[tuple[str, bytes]] | Iterable[Any],
    *,
    auto_match: bool = False,
) -> dict:
    """解析并入库发票文件（xml / ofd / pdf）。

    ``files`` 支持 ``(filename, bytes)`` 元组或 Streamlit 上传对象（带 ``.name``/``.getvalue``）。
    """
    payload = normalize_files(files)
    importer = _require("importer", "发票导入")
    if hasattr(importer, "import_invoice_files"):
        return dict(importer.import_invoice_files(payload, auto_match=auto_match))
    if hasattr(importer, "import_files"):
        return dict(importer.import_files(payload, auto_match=auto_match))
    raise ValueError("发票导入暂不可用：ingest.importer 未提供 import_invoice_files。")


def add_manual_invoice(payload: dict) -> dict:
    """手动录入发票。"""
    importer = _load("importer")
    if importer is not None and hasattr(importer, "add_manual_invoice"):
        return dict(importer.add_manual_invoice(payload))
    parser = _load("invoice_parser")
    if parser is not None and hasattr(parser, "normalize_invoice"):
        normalized = dict(parser.normalize_invoice(payload, source_format="手动"))
        normalized["status"] = "待匹配"
        if hasattr(parser, "save_invoices"):
            saved = parser.save_invoices([normalized], source_file="手动录入")
            records = saved.get("saved") or []
            if records:
                return dict(records[0])
        return dict(ledger.table("invoices").append(normalized))
    raise ValueError("手动录入发票暂不可用：ingest.importer / ingest.invoice_parser 尚未就绪。")


def invoice_rows(status: str | None = None) -> list[dict]:
    """发票池记录（按开票日期倒序）。"""
    rows = _read_table("invoices")
    if status and status not in ("全部", ""):
        rows = [row for row in rows if str(row.get("status")) == status]
    return sorted(rows, key=lambda row: str(row.get("invoice_date") or ""), reverse=True)


# =============================================================================
# 银行流水
# =============================================================================
def import_bank_files(
    files: Iterable[tuple[str, bytes]] | Iterable[Any],
    *,
    mapping: dict[str, str] | None = None,
    account_no: str = "",
    auto_match: bool = False,
) -> dict:
    """解析并入库银行流水文件（csv / xlsx）。"""
    payload = normalize_files(files)
    importer = _require("importer", "银行流水导入")
    if hasattr(importer, "import_bank_files"):
        return dict(
            importer.import_bank_files(
                payload,
                mapping=mapping,
                account_no=account_no,
                auto_match=auto_match,
            )
        )
    if hasattr(importer, "import_files"):
        return dict(importer.import_files(payload, auto_match=auto_match))
    raise ValueError("银行流水导入暂不可用：ingest.importer 未提供 import_bank_files。")


def transaction_rows(status: str | None = None) -> list[dict]:
    """银行流水池记录（按交易日期倒序）。"""
    rows = _read_table("bank_transactions")
    if status and status not in ("全部", ""):
        rows = [row for row in rows if str(row.get("status")) == status]
    return sorted(rows, key=lambda row: str(row.get("transaction_date") or ""), reverse=True)


def detect_columns(headers: Sequence[str]) -> dict[str, str]:
    """列别名自动识别（优先后端，缺失时本地别名表）。"""
    parser = _load("bank_parser")
    if parser is not None and hasattr(parser, "detect_columns"):
        try:
            return dict(parser.detect_columns(list(headers)))
        except Exception:  # noqa: BLE001
            pass
    normalized = {str(header).strip(): header for header in headers if str(header).strip()}
    result: dict[str, str] = {}
    for key, aliases in _FALLBACK_BANK_ALIASES.items():
        for alias in aliases:
            for clean, original in normalized.items():
                if alias == clean or (alias in clean and len(clean) <= len(alias) + 6):
                    result[key] = original
                    break
            if key in result:
                break
    return result


def read_bank_headers(data: bytes, filename: str) -> list[str]:
    """从银行流水文件前几行探测表头（CSV / XLSX）。"""
    suffix = Path(str(filename or "")).suffix.lower()
    if suffix in (".xlsx", ".xls"):
        return _xlsx_headers(data)
    return _csv_headers(data)


def _csv_headers(data: bytes) -> list[str]:
    for encoding in ("utf-8-sig", "gb18030", "gbk", "utf-8"):
        try:
            text = data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        reader = csv.reader(io.StringIO(text))
        rows = []
        for index, row in enumerate(reader):
            if index >= 6:
                break
            rows.append([str(cell).strip() for cell in row])
        header = _pick_header_row(rows)
        if header:
            return header
        return rows[0] if rows else []
    return []


def _xlsx_headers(data: bytes) -> list[str]:
    try:
        from openpyxl import load_workbook
    except ImportError:
        return []
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001
        return []
    try:
        sheet = workbook.active
        rows: list[list[str]] = []
        for index, row in enumerate(sheet.iter_rows(values_only=True)):
            if index >= 6:
                break
            rows.append(["" if cell is None else str(cell).strip() for cell in row])
        header = _pick_header_row(rows)
        return header or (rows[0] if rows else [])
    finally:
        try:
            workbook.close()
        except Exception:  # noqa: BLE001
            pass


def _pick_header_row(rows: Sequence[Sequence[str]]) -> list[str]:
    """在前几行中挑包含「日期/金额/摘要」等关键词最多的一行作为表头。"""
    keywords = ("日期", "金额", "摘要", "对方", "余额", "收入", "支出", "流水", "date", "amount")
    best: list[str] = []
    best_score = 0
    for row in rows:
        cells = [str(cell).strip() for cell in row if str(cell).strip()]
        if len(cells) < 2:
            continue
        score = sum(1 for cell in cells if any(keyword.lower() in cell.lower() for keyword in keywords))
        if score > best_score:
            best, best_score = cells, score
    return best if best_score >= 2 else (list(rows[0]) if rows else [])


def bank_preview(data: bytes, filename: str) -> dict:
    """银行流水导入预览：表头、自动列映射、错误。"""
    try:
        headers = read_bank_headers(data, filename)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "headers": [], "columns": {}, "errors": [f"读取文件失败：{exc}"]}
    if not headers:
        return {
            "ok": False,
            "headers": [],
            "columns": {},
            "errors": ["未识别到表头行，请确认文件是 CSV / XLSX 银行流水。"],
        }
    columns = detect_columns(headers)
    errors: list[str] = []
    if not columns.get("date"):
        errors.append("未自动识别「交易日期」列，可在高级列映射中手动指定。")
    if not (columns.get("amount") or columns.get("income") or columns.get("expense")):
        errors.append("未自动识别金额列（交易金额 / 收入 / 支出），可在高级列映射中手动指定。")
    return {"ok": True, "headers": headers, "columns": columns, "errors": errors}


# =============================================================================
# 自动匹配
# =============================================================================
def suggest_matches(
    *,
    date_window_days: int = DEFAULT_DATE_WINDOW,
    min_score: float = DEFAULT_MIN_SCORE,
) -> dict:
    """生成匹配建议。"""
    matcher = _require("matcher", "自动匹配")
    if not hasattr(matcher, "suggest_matches"):
        raise ValueError("自动匹配暂不可用：ingest.matcher.suggest_matches 未实现。")
    return dict(
        matcher.suggest_matches(date_window_days=int(date_window_days), min_score=float(min_score))
    )


def list_matches(status: str | None = None) -> list[dict]:
    """匹配建议列表（含发票 / 流水摘要，优先后端）。"""
    matcher = _load("matcher")
    if matcher is not None and hasattr(matcher, "list_matches"):
        try:
            try:
                return list(matcher.list_matches(status=status))
            except TypeError:
                rows = list(matcher.list_matches())
        except Exception:  # noqa: BLE001
            return _enrich_matches(_read_table("matches"), status)
        if status:
            rows = [row for row in rows if str(row.get("status")) == status]
        return rows
    return _enrich_matches(_read_table("matches"), status)


def _enrich_matches(matches: Sequence[dict], status: str | None = None) -> list[dict]:
    """本地补全匹配建议的发票 / 流水摘要字段。"""
    invoices = {str(row.get("id")): row for row in _read_table("invoices")}
    transactions = {str(row.get("id")): row for row in _read_table("bank_transactions")}
    rows: list[dict] = []
    for match in matches:
        if status and str(match.get("status")) != status:
            continue
        invoice = invoices.get(str(match.get("invoice_id"))) or {}
        transaction = transactions.get(str(match.get("transaction_id"))) or {}
        enriched = dict(match)
        enriched.update(
            {
                "invoice_no": invoice.get("invoice_no") or match.get("invoice_no") or "",
                "invoice_direction": invoice.get("direction") or "",
                "invoice_date": invoice.get("invoice_date") or "",
                "invoice_type": invoice.get("invoice_type") or "",
                "invoice_counterparty": invoice.get("buyer")
                if str(invoice.get("direction")) == "销项"
                else invoice.get("seller") or "",
                "invoice_total": invoice.get("total_inclusive") or 0.0,
                "transaction_date": transaction.get("transaction_date") or "",
                "transaction_direction": transaction.get("direction") or "",
                "transaction_amount": transaction.get("amount") or 0.0,
                "counterparty": transaction.get("counterparty") or "",
                "summary": transaction.get("summary") or "",
            }
        )
        rows.append(enriched)
    return sorted(rows, key=lambda row: float(row.get("score") or 0), reverse=True)


def confirm_match(match_id: str, *, post_journal: bool = True) -> dict:
    """确认单条匹配并入账。"""
    matcher = _require("matcher", "匹配确认入账")
    if not hasattr(matcher, "confirm_match"):
        raise ValueError("匹配确认暂不可用：ingest.matcher.confirm_match 未实现。")
    return dict(matcher.confirm_match(str(match_id), post_journal=post_journal))


def ignore_match(match_id: str) -> dict:
    """忽略单条匹配建议。"""
    matcher = _require("matcher", "忽略匹配建议")
    if not hasattr(matcher, "ignore_match"):
        raise ValueError("忽略匹配暂不可用：ingest.matcher.ignore_match 未实现。")
    return dict(matcher.ignore_match(str(match_id)))


# =============================================================================
# 池记录编辑 / 删除（直接走 ledger.store，与契约第 1 节一致）
# =============================================================================
def update_pool_row(kind: str, row_id: str, patch: dict) -> dict:
    """更新池记录的状态 / 备注。"""
    table_name = POOL_TABLES.get(kind)
    if table_name is None:
        raise ValueError(f"未知池类型：{kind}")
    allowed = {key: value for key, value in patch.items() if key in {"status", "note"}}
    if not allowed:
        return dict(ledger.table(table_name).get(row_id) or {})
    return dict(ledger.table(table_name).update(row_id, allowed))


def delete_pool_rows(kind: str, row_ids: Iterable[str]) -> dict:
    """删除池记录，返回删除数量与失败明细。"""
    table_name = POOL_TABLES.get(kind)
    if table_name is None:
        raise ValueError(f"未知池类型：{kind}")
    table = ledger.table(table_name)
    deleted = 0
    errors: list[str] = []
    for row_id in row_ids:
        try:
            if table.delete(str(row_id)):
                deleted += 1
            else:
                errors.append(f"编号 {row_id} 不存在")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"删除 {row_id} 失败：{exc}")
    return {"deleted": deleted, "errors": errors}


# =============================================================================
# 工具
# =============================================================================
def normalize_files(files: Iterable[tuple[str, bytes]] | Iterable[Any]) -> list[tuple[str, bytes]]:
    """把上传对象统一转换为 ``[(filename, bytes), ...]``。"""
    payload: list[tuple[str, bytes]] = []
    for item in files or []:
        if isinstance(item, tuple) and len(item) == 2:
            filename, data = item
            payload.append((str(filename), bytes(data)))
            continue
        name = getattr(item, "name", None)
        if name is None:
            continue
        if hasattr(item, "getvalue"):
            data = item.getvalue()
        elif hasattr(item, "read"):
            data = item.read()
        else:
            continue
        payload.append((str(name), bytes(data)))
    return payload


__all__ = [
    "INVOICE_EXTENSIONS",
    "BANK_EXTENSIONS",
    "INGEST_STATUSES",
    "INVOICE_DIRECTIONS",
    "INVOICE_KINDS",
    "MATCH_STATUSES",
    "BANK_COLUMN_LABELS",
    "available",
    "missing_detail",
    "demo_ready",
    "invoice_import_ready",
    "bank_import_ready",
    "manual_invoice_ready",
    "match_ready",
    "seed_demo_pool",
    "reset_pool",
    "pool_summary",
    "match_summary",
    "overview",
    "import_invoice_files",
    "add_manual_invoice",
    "invoice_rows",
    "import_bank_files",
    "transaction_rows",
    "detect_columns",
    "read_bank_headers",
    "bank_preview",
    "suggest_matches",
    "list_matches",
    "confirm_match",
    "ignore_match",
    "update_pool_row",
    "delete_pool_rows",
    "normalize_files",
]
