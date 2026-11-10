"""P0-3 后端访问封装：期间结账（``closing.*``）、申报包归档与备份恢复（``archiving.*``）。

设计约定
--------
* 页面只依赖本模块签名；后端由队友并行开发，缺失时用 :mod:`ui.components.runtime`
  容错降级，按钮禁用 + 中文提示，绝不白屏；
* 期间状态与锁始终可用（``ledger.locks`` 是基础层），因此即使 ``closing`` 未就绪，
  首页结账卡片与申报页结账状态徽标仍能正确展示；
* 备份 / 归档列表在后端缺失时直接读 ``ledger`` 的 ``backups`` / ``archives`` 表兜底，
  文件是否存在、哈希摘要由 UI 本地计算（只读，不改数据）。
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import config
import ledger
from engine import period_label, period_range
from ledger import locks
from ui.components import runtime

# =============================================================================
# 模块加载与就绪判断
# =============================================================================
def _load(dotted: str):
    return runtime.load(dotted)


def closing_checks_ready() -> bool:
    """预结账检查模块是否可用。"""
    module = _load("closing.checks")
    return module is not None and hasattr(module, "pre_close_checks")


def closing_ready() -> bool:
    """结账 / 反结账是否可用。"""
    module = _load("closing.close")
    return module is not None and hasattr(module, "close_period") and hasattr(module, "reopen_period")


def closing_periods_ready() -> bool:
    module = _load("closing.periods")
    return module is not None and hasattr(module, "list_periods")


def archiving_ready() -> bool:
    """申报包归档是否可用。"""
    module = _load("archiving.filing_archive")
    return module is not None and hasattr(module, "archive_filing_package")


def archive_verify_ready() -> bool:
    module = _load("archiving.filing_archive")
    return module is not None and hasattr(module, "verify_archive")


def backup_ready() -> bool:
    """创建备份是否可用。"""
    module = _load("archiving.backup")
    return module is not None and hasattr(module, "create_backup")


def backup_manage_ready() -> bool:
    """备份校验 / 删除是否可用。"""
    module = _load("archiving.backup")
    return module is not None and hasattr(module, "verify_backup") and hasattr(module, "delete_backup")


def restore_ready() -> bool:
    """备份预览 / 恢复是否可用。"""
    module = _load("archiving.restore")
    return module is not None and hasattr(module, "inspect_backup") and hasattr(module, "restore_backup")


def backend_available() -> bool:
    """P0-3 任一后端能力是否就绪。"""
    return any(
        (
            closing_checks_ready(),
            closing_ready(),
            archiving_ready(),
            backup_ready(),
            restore_ready(),
        )
    )


def missing_detail() -> str:
    """后端缺失原因（页面提示用）。"""
    return (
        runtime.error_of("closing")
        or runtime.error_of("closing.checks")
        or runtime.error_of("archiving")
        or runtime.error_of("archiving.backup")
        or ""
    )


def _require(dotted: str, feature: str):
    module = _load(dotted)
    if module is None:
        raise ValueError(f"{feature}暂不可用：{dotted} 模块尚未就绪（后端由队友并行开发中）。")
    return module


# =============================================================================
# 期间工具
# =============================================================================
def _read_table(name: str) -> list[dict]:
    try:
        return list(ledger.table(name).read())
    except Exception:  # noqa: BLE001
        return []


def current_month(today: date | str | None = None) -> str:
    """当前月份键（与 ``closing.periods.current_period`` 一致）。"""
    if isinstance(today, str) and today:
        return locks.month_key(today)
    day = today if isinstance(today, date) else date.today()
    return f"{day.year}-{day.month:02d}"


def prev_month(month: str) -> str:
    """上一个月键。"""
    try:
        start, _ = period_range(locks.month_key(month))
    except Exception:  # noqa: BLE001
        return ""
    previous = start - timedelta(days=1)
    return f"{previous.year}-{previous.month:02d}"


def month_label(month: str) -> str:
    """``2026-01`` → ``2026年1月``。"""
    text = str(month or "").strip()
    if len(text) >= 7 and text[4] == "-":
        try:
            return f"{int(text[:4])}年{int(text[5:7])}月"
        except ValueError:
            return text
    return period_label(text) if text else "—"


def status_map() -> dict[str, str]:
    try:
        return dict(locks.status_map())
    except Exception:  # noqa: BLE001
        return {}


def period_status(period: str) -> str:
    """月份 / 季度状态（未建记录时返回「未结账」）。"""
    try:
        if "Q" in str(period).upper():
            statuses = status_map()
            quarter = str(period).upper()
            if quarter in statuses:
                return statuses[quarter]
            return "未结账"
        return locks.status_of(period)
    except Exception:  # noqa: BLE001
        return "未结账"


def is_locked(period: str) -> bool:
    try:
        return bool(locks.is_locked(period))
    except Exception:  # noqa: BLE001
        return False


def latest_closed_month() -> str:
    try:
        return str(locks.latest_closed_month() or "")
    except Exception:  # noqa: BLE001
        return ""


def lock_summary() -> dict:
    try:
        return dict(locks.lock_summary())
    except Exception:  # noqa: BLE001
        return {
            "latest_closed_month": "",
            "current_month": current_month(),
            "current_locked": False,
            "locked_periods": [],
            "disclaimer": "期间锁概览暂不可用。",
        }


def quarter_closure(period: str) -> dict:
    """季度内 3 个月的结账状态（供申报页徽标 / 归档前提示）。"""
    quarter = str(period or "").strip().upper()
    try:
        start, end = period_range(quarter)
    except Exception:  # noqa: BLE001
        return {
            "period": quarter,
            "months": [],
            "statuses": {},
            "closed_count": 0,
            "total": 0,
            "all_closed": False,
            "quarter_status": "未结账",
            "label": quarter,
        }
    months: list[str] = []
    cursor = start
    while cursor <= end:
        months.append(f"{cursor.year}-{cursor.month:02d}")
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    statuses = {month: period_status(month) for month in months}
    closed_count = sum(1 for status in statuses.values() if status != "未结账")
    quarter_status = period_status(quarter)
    return {
        "period": quarter,
        "months": months,
        "statuses": statuses,
        "closed_count": closed_count,
        "total": len(months),
        "all_closed": bool(months) and closed_count == len(months),
        "quarter_status": quarter_status,
        "label": period_label(quarter),
    }


def list_periods(limit: int = 24) -> list[dict]:
    """期间列表（优先后端 periods 派生数据，缺失时读 periods 表兜底）。"""
    module = _load("closing.periods")
    if module is not None and hasattr(module, "list_periods"):
        try:
            return list(module.list_periods(limit=limit))
        except Exception:  # noqa: BLE001
            pass
    rows = _read_table("periods")
    result = []
    for row in rows:
        period = str(row.get("period") or "")
        if not period:
            continue
        result.append(
            {
                "period": period,
                "period_label": period_label(period) if "Q" in period.upper() else month_label(period),
                "period_type": row.get("period_type") or ("季" if "Q" in period.upper() else "月"),
                "status": row.get("status") or "未结账",
                "closed_at": row.get("closed_at") or "",
                "closed_by": row.get("closed_by") or "",
                "reopened_at": row.get("reopened_at") or "",
                "snapshot_hash": row.get("snapshot_hash") or "",
                "note": row.get("note") or "",
            }
        )
    result.sort(key=lambda item: str(item.get("period") or ""), reverse=True)
    return result[:limit]


def period_overview(period: str) -> dict:
    """单期间概览（后端可用时含金额/笔数；否则返回基础状态）。"""
    module = _load("closing.periods")
    if module is not None and hasattr(module, "period_overview"):
        try:
            return dict(module.period_overview(period))
        except Exception:  # noqa: BLE001
            pass
    status = period_status(period)
    return {
        "period": period,
        "period_label": period_label(period) if "Q" in str(period).upper() else month_label(period),
        "period_type": "季" if "Q" in str(period).upper() else "月",
        "status": status,
        "locked": status != "未结账",
    }


# =============================================================================
# 结账 / 反结账
# =============================================================================
def pre_close_checks(period: str) -> dict:
    """预结账检查清单。"""
    module = _require("closing.checks", "预结账检查")
    if not hasattr(module, "pre_close_checks"):
        raise ValueError("预结账检查暂不可用：closing.checks.pre_close_checks 未实现。")
    return dict(module.pre_close_checks(period))


def close_period(period: str, *, force: bool = False, note: str = "") -> dict:
    """执行期间结账。"""
    module = _require("closing.close", "期间结账")
    if not hasattr(module, "close_period"):
        raise ValueError("期间结账暂不可用：closing.close.close_period 未实现。")
    return dict(module.close_period(period, force=force, note=note))


def reopen_period(period: str, *, reason: str = "") -> dict:
    """执行反结账。"""
    module = _require("closing.close", "反结账")
    if not hasattr(module, "reopen_period"):
        raise ValueError("反结账暂不可用：closing.close.reopen_period 未实现。")
    return dict(module.reopen_period(period, reason=reason))


def is_closed(period: str) -> bool:
    module = _load("closing.close")
    if module is not None and hasattr(module, "is_closed"):
        try:
            return bool(module.is_closed(period))
        except Exception:  # noqa: BLE001
            pass
    return period_status(period) != "未结账"


# =============================================================================
# 归档
# =============================================================================
def archive_dir(period: str = "") -> Path:
    """归档目录（后端可用时取后端定义，否则 ``data/archive/{period}``）。"""
    module = _load("archiving.filing_archive")
    if module is not None and hasattr(module, "archive_dir"):
        try:
            return Path(module.archive_dir(period))
        except Exception:  # noqa: BLE001
            pass
    base = Path(config.DATA_DIR) / "archive"
    return base / str(period) if period else base


def archive_filing_package(period: str, *, note: str = "", include_pdf: bool = True) -> dict:
    """归档本期申报包（Excel + PDF + manifest）。"""
    module = _require("archiving.filing_archive", "申报包归档")
    if not hasattr(module, "archive_filing_package"):
        raise ValueError("申报包归档暂不可用：archiving.filing_archive.archive_filing_package 未实现。")
    return dict(
        module.archive_filing_package(period, include_pdf=bool(include_pdf), note=note)
    )


def list_archives(period: str | None = None) -> list[dict]:
    """归档记录列表（后端优先，缺失时读 archives 表 + 本地文件检查）。"""
    module = _load("archiving.filing_archive")
    if module is not None and hasattr(module, "list_archives"):
        try:
            rows = list(module.list_archives(period=period))
        except TypeError:
            try:
                rows = list(module.list_archives())
            except Exception:  # noqa: BLE001
                rows = []
        except Exception:  # noqa: BLE001
            rows = []
        if period:
            rows = [row for row in rows if str(row.get("period") or "") == str(period)]
        return [_enrich_archive(row) for row in rows]
    rows = _read_table("archives")
    if period:
        rows = [row for row in rows if str(row.get("period") or "") == str(period)]
    rows = [_enrich_archive(row) for row in rows]
    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return rows


def _enrich_archive(row: dict) -> dict:
    """补全归档记录的文件名 / 存在性 / 总大小 / 哈希摘要。

    后端 ``archives.path`` 指向 manifest.json，真正打包文件在 ``files`` 里；
    这里统一把 ``file_name`` 显示为「申报包_xxx.xlsx / 申报表_xxx.pdf」，并把大小补成各文件之和。
    """
    record = dict(row)
    path = Path(str(record.get("path") or ""))
    record["file_hash_short"] = short_hash(record.get("file_hash"))
    files = record.get("files") or []
    if isinstance(files, list) and files:
        names: list[str] = []
        total_size = 0
        all_exist = True
        for item in files:
            if not isinstance(item, dict):
                continue
            item_path = Path(str(item.get("path") or ""))
            name = str(item.get("name") or item_path.name)
            if name:
                names.append(name)
            try:
                total_size += int(item.get("size") or 0)
            except (TypeError, ValueError):
                pass
            if item_path and not item_path.exists():
                all_exist = False
        record["file_name"] = " · ".join(names) if names else path.name
        record["files_count"] = len(names)
        if total_size:
            record["size_bytes"] = total_size
        record["exists"] = bool(all_exist) and bool(path.exists() or names)
    else:
        exists = path.exists() if str(record.get("path") or "") else False
        record["exists"] = exists
        record["file_name"] = path.name if record.get("path") else ""
        record["files_count"] = 1 if exists else 0
        if exists and not record.get("size_bytes"):
            try:
                record["size_bytes"] = path.stat().st_size
            except OSError:
                pass
    record["size_text"] = fmt_size(record.get("size_bytes"))
    return record


def verify_archive(archive_id_or_path: str) -> dict:
    """校验归档包哈希 / 完整性。"""
    module = _require("archiving.filing_archive", "归档校验")
    if not hasattr(module, "verify_archive"):
        raise ValueError("归档校验暂不可用：archiving.filing_archive.verify_archive 未实现。")
    return dict(module.verify_archive(archive_id_or_path))


def has_archive(period: str) -> bool:
    """期间是否已有归档记录。"""
    return any(str(row.get("period") or "") == str(period) for row in list_archives(period))


# =============================================================================
# 备份
# =============================================================================
def backup_dir() -> Path:
    module = _load("archiving.backup")
    if module is not None and hasattr(module, "backup_dir"):
        try:
            return Path(module.backup_dir())
        except Exception:  # noqa: BLE001
            pass
    return Path(config.DATA_DIR) / "backups"


def create_backup(tag: str = "", *, include_logs: bool = True, note: str = "") -> dict:
    """创建 zip 备份（含 manifest，不含 API Key）。"""
    module = _require("archiving.backup", "创建备份")
    if not hasattr(module, "create_backup"):
        raise ValueError("创建备份暂不可用：archiving.backup.create_backup 未实现。")
    return dict(module.create_backup(tag=tag, include_logs=bool(include_logs), note=note))


def list_backups() -> list[dict]:
    """备份列表（后端优先，缺失时读 backups 表兜底）。"""
    module = _load("archiving.backup")
    if module is not None and hasattr(module, "list_backups"):
        try:
            rows = list(module.list_backups())
            return [_enrich_backup(row) for row in rows]
        except Exception:  # noqa: BLE001
            pass
    rows = [_enrich_backup(row) for row in _read_table("backups")]
    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return rows


def _enrich_backup(row: dict) -> dict:
    record = dict(row)
    path = Path(str(record.get("path") or ""))
    exists = path.exists() if record.get("path") else False
    record["exists"] = exists
    record["file_name"] = path.name if record.get("path") else ""
    record["file_hash_short"] = short_hash(record.get("file_hash"))
    if exists:
        try:
            record["size_bytes"] = record.get("size_bytes") or path.stat().st_size
        except OSError:
            pass
    record["size_text"] = fmt_size(record.get("size_bytes"))
    manifest = record.get("manifest_json") or {}
    record["file_count"] = len(manifest.get("files") or []) if isinstance(manifest, dict) else 0
    record["tables"] = manifest.get("tables") if isinstance(manifest, dict) else {}
    return record


def verify_backup(backup_id_or_path: str) -> dict:
    module = _require("archiving.backup", "备份校验")
    if not hasattr(module, "verify_backup"):
        raise ValueError("备份校验暂不可用：archiving.backup.verify_backup 未实现。")
    return dict(module.verify_backup(backup_id_or_path))


def delete_backup(backup_id_or_path: str) -> bool:
    module = _require("archiving.backup", "删除备份")
    if not hasattr(module, "delete_backup"):
        raise ValueError("删除备份暂不可用：archiving.backup.delete_backup 未实现。")
    return bool(module.delete_backup(backup_id_or_path))


# =============================================================================
# 恢复
# =============================================================================
def inspect_backup(backup_id_or_path: str) -> dict:
    """预览备份内容（表 / 条数 / 校验结果），不写盘。"""
    module = _require("archiving.restore", "备份预览")
    if not hasattr(module, "inspect_backup"):
        raise ValueError("备份预览暂不可用：archiving.restore.inspect_backup 未实现。")
    return dict(module.inspect_backup(backup_id_or_path))


def restore_backup(
    backup_id_or_path: str,
    *,
    dry_run: bool = False,
    safety_backup: bool = True,
) -> dict:
    """恢复备份（内部自动安全备份），返回恢复结果与安全备份路径。"""
    module = _require("archiving.restore", "备份恢复")
    if not hasattr(module, "restore_backup"):
        raise ValueError("备份恢复暂不可用：archiving.restore.restore_backup 未实现。")
    return dict(
        module.restore_backup(
            backup_id_or_path,
            dry_run=bool(dry_run),
            safety_backup=bool(safety_backup),
        )
    )


def stage_upload(data: bytes, filename: str) -> Path:
    """把上传的备份 zip 暂存到**当前公司**的 uploads 目录，返回路径。

    注意：必须在函数内动态读取 ``config.UPLOAD_DIR``，公司切换后该路径会变，
    不能使用模块级缓存。
    """
    upload_dir = Path(config.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(str(filename or "backup.zip")).name
    target = upload_dir / f"restore_{datetime.now():%Y%m%d_%H%M%S}_{safe_name}"
    target.write_bytes(data)
    return target


# =============================================================================
# 工具
# =============================================================================
def fmt_size(size_bytes: Any) -> str:
    """字节数 → 人类可读大小。"""
    try:
        size = float(size_bytes or 0)
    except (TypeError, ValueError):
        return "—"
    if size <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def short_hash(value: Any, length: int = 12) -> str:
    """哈希摘要短展示。"""
    text = str(value or "").strip()
    if not text:
        return "—"
    return text[:length] + "…" if len(text) > length else text


def sha256_file(path: str | Path) -> str:
    """本地计算文件 SHA-256（用于展示，不写库）。"""
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def copy_file_for_download(path: str | Path) -> Path | None:
    """确保文件可下载（保留原路径并返回）。"""
    target = Path(str(path or ""))
    return target if target.exists() else None


__all__ = [
    "closing_checks_ready",
    "closing_ready",
    "closing_periods_ready",
    "archiving_ready",
    "archive_verify_ready",
    "backup_ready",
    "backup_manage_ready",
    "restore_ready",
    "backend_available",
    "missing_detail",
    "current_month",
    "prev_month",
    "month_label",
    "status_map",
    "period_status",
    "is_locked",
    "latest_closed_month",
    "lock_summary",
    "quarter_closure",
    "list_periods",
    "period_overview",
    "pre_close_checks",
    "close_period",
    "reopen_period",
    "is_closed",
    "archive_dir",
    "archive_filing_package",
    "list_archives",
    "verify_archive",
    "has_archive",
    "backup_dir",
    "create_backup",
    "list_backups",
    "verify_backup",
    "delete_backup",
    "inspect_backup",
    "restore_backup",
    "stage_upload",
    "fmt_size",
    "short_hash",
    "sha256_file",
]
