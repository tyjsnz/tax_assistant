"""CSV 台账存储层：表注册、原子读写、ID 生成、备份与审计日志。

设计要点
--------
* 表结构全部来自 :mod:`ledger.models` 的 ``TABLE_DEFS``，本模块不重复定义字段。
* 写入采用「临时文件 + ``os.replace``」原子替换，避免中途崩溃导致台账损坏。
* 每个表一把线程锁，适配 Streamlit 的单进程多线程模型。
* 目录路径在**调用时**从 :mod:`config` 读取（而非导入时固化），便于测试隔离。
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import config
from engine.money import round2
from ledger import models

# =============================================================================
# 审计日志定义
# =============================================================================
LOG_DEFS: dict[str, list[str]] = {
    "calculation_log": ["timestamp", "calc_type", "period", "inputs", "result", "operator"],
    "journal_log": ["timestamp", "entry_no", "description", "debit", "credit", "amount", "source"],
    "policy_check_log": ["timestamp", "checked_total", "stale_count", "latest_verified", "detail"],
    "user_action_log": ["timestamp", "action", "detail"],
    "filing_log": ["timestamp", "period", "form_id", "form_name", "action", "status"],
}

_LOG_LABELS = {
    "calculation_log": "算税记录",
    "journal_log": "分录生成记录",
    "policy_check_log": "政策更新检查记录",
    "user_action_log": "用户操作日志",
    "filing_log": "申报填报记录",
}

#: 每张表一把**可重入锁**：``append()`` 内部会调用 ``read()``，而 ``read()`` 在
#: 文件缺失时会经 ``ensure()`` 再次取同一把锁；若用普通 Lock 会自锁死（RLock 可避免）。
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
_tables: dict[str, "CSVTable"] = {}


def _lock_for(path: Path) -> threading.RLock:
    key = str(path)
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.RLock()
        return _locks[key]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_csv_value(kind: str, value: Any) -> str:
    """把 Python 值转换为 CSV 单元格字符串。"""
    if value is None:
        return ""
    if kind == "money":
        return f"{round2(value):.2f}"
    if kind == "bool":
        return models.bool_text(value)
    if kind == "json":
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if kind == "int":
        try:
            return str(int(value))
        except (TypeError, ValueError):
            return "0"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _from_csv_value(kind: str, value: str) -> Any:
    """把 CSV 单元格字符串转换为 Python 值。"""
    text = (value or "").strip()
    if kind == "money":
        try:
            return round2(float(text.replace(",", "")))
        except (TypeError, ValueError):
            return 0.0
    if kind == "bool":
        return models.parse_bool(text)
    if kind == "json":
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}
    if kind == "int":
        try:
            return int(float(text))
        except (TypeError, ValueError):
            return 0
    return text


class CSVTable:
    """单张 CSV 表的读写封装。"""

    def __init__(self, name: str) -> None:
        if name not in models.TABLE_DEFS:
            raise KeyError(f"未定义的数据表：{name}")
        self.name = name
        self.defn = models.TABLE_DEFS[name]
        self.file_name = self.defn["file"]
        self.label = self.defn.get("label", name)
        self.prefix = self.defn.get("prefix", name.upper())
        self.columns: list[dict[str, str]] = self.defn["columns"]
        self.column_names: list[str] = [column["name"] for column in self.columns]
        self.date_column = self._find_date_column()
        self._type_map = {column["name"]: column["type"] for column in self.columns}
        self._label_map = {column["name"]: column["label"] for column in self.columns}

    # ---------------------------------------------------------------- 路径
    @property
    def path(self) -> Path:
        """数据文件路径（调用时读取 config.DATA_DIR，便于测试隔离）。"""
        return Path(config.DATA_DIR) / self.file_name

    @property
    def lock(self) -> threading.RLock:
        return _lock_for(self.path)

    def _find_date_column(self) -> str | None:
        for column in self.columns:
            if column["type"] == "date":
                return column["name"]
        return None

    # ---------------------------------------------------------------- 基础 IO
    def ensure(self) -> Path:
        """确保文件与表头存在。"""
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.stat().st_size == 0:
            with self.lock:
                if not path.exists() or path.stat().st_size == 0:
                    self._write_rows([])
        return path

    def _write_rows(self, rows: list[dict[str, Any]]) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        try:
            with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=self.column_names, extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow({name: _to_csv_value(self._type_map[name], row.get(name)) for name in self.column_names})
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def read(self) -> list[dict[str, Any]]:
        """读取全部记录（类型已转换）。文件损坏或缺失时返回空列表。"""
        self.ensure()
        rows: list[dict[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
                for raw in csv.DictReader(handle):
                    row = {}
                    for name in self.column_names:
                        row[name] = _from_csv_value(self._type_map[name], raw.get(name, ""))
                    rows.append(row)
        except (OSError, csv.Error, UnicodeDecodeError):
            return []
        return rows

    def write(self, rows: Iterable[dict[str, Any]]) -> None:
        """整体覆盖写入。"""
        with self.lock:
            self._write_rows(list(rows))

    # ---------------------------------------------------------------- ID
    def _next_sequence(self, rows: list[dict[str, Any]]) -> int:
        max_seq = 0
        for row in rows:
            raw = str(row.get("id") or "")
            tail = raw[len(self.prefix):] if raw.startswith(self.prefix) else ""
            if tail.isdigit():
                max_seq = max(max_seq, int(tail))
        return max_seq + 1

    def next_id(self) -> str:
        """生成下一个业务编号，如 ``INC000007``。"""
        with self.lock:
            return f"{self.prefix}{self._next_sequence(self.read()):06d}"

    # ---------------------------------------------------------------- 单条操作
    def append(self, row: dict[str, Any]) -> dict[str, Any]:
        """追加一条记录，自动生成 id / created_at，返回完整记录。"""
        with self.lock:
            rows = self.read()
            record = {name: row.get(name) for name in self.column_names}
            if not record.get("id"):
                record["id"] = f"{self.prefix}{self._next_sequence(rows):06d}"
            if "created_at" in self.column_names and not record.get("created_at"):
                record["created_at"] = _now()
            rows.append(record)
            self._write_rows(rows)
            return {name: _from_csv_value(self._type_map[name], _to_csv_value(self._type_map[name], record.get(name))) for name in self.column_names}

    def append_many(self, records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """批量追加，只写一次磁盘。"""
        with self.lock:
            rows = self.read()
            seq = self._next_sequence(rows)
            added: list[dict[str, Any]] = []
            for row in records:
                record = {name: row.get(name) for name in self.column_names}
                if not record.get("id"):
                    record["id"] = f"{self.prefix}{seq:06d}"
                    seq += 1
                if "created_at" in self.column_names and not record.get("created_at"):
                    record["created_at"] = _now()
                rows.append(record)
                added.append(record)
            self._write_rows(rows)
            return added

    def get(self, row_id: str) -> dict[str, Any] | None:
        for row in self.read():
            if str(row.get("id")) == str(row_id):
                return row
        return None

    def update(self, row_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """按 id 更新字段，返回更新后的记录；不存在时抛 KeyError。"""
        with self.lock:
            rows = self.read()
            for index, row in enumerate(rows):
                if str(row.get("id")) == str(row_id):
                    for key, value in patch.items():
                        if key in self._type_map:
                            row[key] = _from_csv_value(self._type_map[key], _to_csv_value(self._type_map[key], value))
                    if "updated_at" in self.column_names:
                        row["updated_at"] = _now()
                    rows[index] = row
                    self._write_rows(rows)
                    return row
            raise KeyError(f"{self.label}中不存在编号 {row_id}")

    def delete(self, row_id: str) -> bool:
        with self.lock:
            rows = self.read()
            keep = [row for row in rows if str(row.get("id")) != str(row_id)]
            if len(keep) == len(rows):
                return False
            self._write_rows(keep)
            return True

    def replace_all(self, rows: Iterable[dict[str, Any]]) -> int:
        """整体替换（供表格编辑器保存使用），返回记录数。"""
        cleaned: list[dict[str, Any]] = []
        with self.lock:
            seq = 0
            for row in rows:
                record = {name: row.get(name) for name in self.column_names}
                if not record.get("id"):
                    seq = seq or self._next_sequence(cleaned)
                    record["id"] = f"{self.prefix}{seq:06d}"
                    seq += 1
                if "created_at" in self.column_names and not record.get("created_at"):
                    record["created_at"] = _now()
                cleaned.append(record)
            self._write_rows(cleaned)
        return len(cleaned)

    # ---------------------------------------------------------------- 查询
    def all(self) -> list[dict[str, Any]]:
        return self.read()

    def filter(
        self,
        start: str | None = None,
        end: str | None = None,
        **equals: Any,
    ) -> list[dict[str, Any]]:
        """按日期区间与等值条件过滤。"""
        start_date = models.normalize_date(start, default="") if start else ""
        end_date = models.normalize_date(end, default="") if end else ""
        results = []
        for row in self.read():
            if start_date or end_date:
                column = self.date_column or "date"
                row_date = str(row.get(column) or "")
                if start_date and row_date < start_date:
                    continue
                if end_date and row_date > end_date:
                    continue
            matched = True
            for key, value in equals.items():
                if value is None or value == "":
                    continue
                if str(row.get(key)) != str(value):
                    matched = False
                    break
            if matched:
                results.append(row)
        return results

    def sorted_by_date(self, descending: bool = True) -> list[dict[str, Any]]:
        column = self.date_column or "date"
        return sorted(self.read(), key=lambda row: str(row.get(column) or ""), reverse=descending)

    def count(self) -> int:
        return len(self.read())

    # ---------------------------------------------------------------- 备份
    def backup(self, tag: str = "") -> Path:
        """备份到 data/backups/，返回备份文件路径。"""
        self.ensure()
        backup_dir = Path(config.BACKUP_DIR)
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = f"_{tag}" if tag else ""
        target = backup_dir / f"{Path(self.file_name).stem}_{stamp}{suffix}.csv"
        shutil.copy2(self.path, target)
        return target


# =============================================================================
# 表注册表
# =============================================================================
def table(name: str) -> CSVTable:
    """获取（并缓存）一张表。"""
    if name not in _tables:
        _tables[name] = CSVTable(name)
    return _tables[name]


def reset_cache() -> None:
    """清空表缓存（测试隔离用）。"""
    _tables.clear()
    _locks.clear()


def init_storage() -> None:
    """初始化所有数据表与日志文件。"""
    config.ensure_dirs()
    for name in models.TABLE_NAMES:
        table(name).ensure()
    for name, columns in LOG_DEFS.items():
        _ensure_log(name, columns)


def backup_all(tag: str = "") -> list[Path]:
    """备份全部台账，返回备份文件列表。"""
    backups = []
    for name in models.TABLE_NAMES:
        try:
            backups.append(table(name).backup(tag=tag))
        except OSError:
            continue
    return backups


def storage_stats() -> dict[str, int]:
    """各表记录数（供驾驶舱展示）。"""
    stats = {}
    for name in models.TABLE_NAMES:
        try:
            stats[name] = table(name).count()
        except OSError:
            stats[name] = 0
    return stats


# =============================================================================
# 审计日志
# =============================================================================
def _log_path(name: str) -> Path:
    return Path(config.LOGS_DIR) / f"{name}.csv"


def _ensure_log(name: str, columns: list[str] | None = None) -> Path:
    columns = columns or LOG_DEFS.get(name) or ["timestamp", "action", "detail"]
    path = _log_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        with _lock_for(path):
            if not path.exists() or path.stat().st_size == 0:
                with path.open("w", encoding="utf-8-sig", newline="") as handle:
                    csv.DictWriter(handle, fieldnames=columns).writeheader()
    return path


def append_log(name: str, row: dict[str, Any]) -> None:
    """追加一条审计日志。写入失败不影响主流程（仅告警）。"""
    try:
        columns = LOG_DEFS.get(name) or list(row.keys())
        path = _ensure_log(name, columns)
        with _lock_for(path):
            with path.open("a", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
                payload = {column: row.get(column, "") for column in columns}
                payload.setdefault("timestamp", payload.get("timestamp") or _now())
                if "timestamp" in columns:
                    payload["timestamp"] = payload.get("timestamp") or _now()
                for key, value in list(payload.items()):
                    if isinstance(value, (dict, list)):
                        payload[key] = json.dumps(value, ensure_ascii=False)
                writer.writerow(payload)
    except OSError:
        pass


def read_log(name: str, limit: int | None = None) -> list[dict[str, Any]]:
    """读取审计日志（新的在前）。"""
    path = _log_path(name)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                rows.append(dict(row))
    except (OSError, csv.Error):
        return []
    rows.reverse()
    return rows[:limit] if limit else rows


def log_label(name: str) -> str:
    return _LOG_LABELS.get(name, name)
