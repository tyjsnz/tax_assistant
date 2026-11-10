"""数据备份：把全部台账 CSV 打包为含 SHA-256 清单的 zip。

备份内容（``data/backups/backup_YYYYmmdd_HHMMSS[_tag].zip``）
----------------------------------------------------------
* ``data/*.csv``：全部台账与状态表（按文件名排序）；
* ``manifest.json``：结构版本、生成时间、应用版本、逐文件名称/大小/SHA-256、各表条数；
* ``README.txt``：人类可读的备份说明；
* ``logs/*.csv``（``include_logs=True`` 时）：审计日志。

**绝不包含**：``data/user_settings.json``（DeepSeek API Key）、``data/uploads/``、
``data/backups/``、``data/archive/``、``*.tmp`` 等。备份只读，不改变台账。
"""

from __future__ import annotations

import csv
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import config

from archiving import hash as hash_tools
from archiving import manifest as manifest_tools
from ledger import audit, models
from ledger.store import table

#: 备份文件前缀与扩展名
BACKUP_PREFIX = "backup"
#: 备份 zip 中允许出现的非清单文件
ALLOWED_EXTRA_MEMBERS = ("manifest.json", "README.txt")
#: 永不进入备份的文件名（API Key 等本地设置）
EXCLUDED_NAMES = ("user_settings.json",)
#: 永不进入备份的目录（相对 ``config.DATA_DIR``）
EXCLUDED_DIRS = ("uploads", "backups", "archive")
#: 恢复时允许写入的台账文件（data/*.csv 的 basename）
PROTECTED_NAMES = ("user_settings.json",)


# =============================================================================
# 路径与命名
# =============================================================================
def backup_dir() -> Path:
    """备份目录：``config.DATA_DIR / "backups"``（动态读取配置，便于测试隔离）。"""
    return Path(config.DATA_DIR) / "backups"


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _safe_tag(tag: Any) -> str:
    """把 tag 清洗为安全文件名片段（仅保留中文/字母/数字/下划线/短横线）。"""
    text = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fa5]", "_", str(tag or "").strip())
    return text.strip("_")[:40]


def _unique_target(target: Path) -> Path:
    """同名备份已存在时追加序号，避免覆盖历史备份。"""
    if not target.exists():
        return target
    stem = target.stem
    for index in range(1, 1000):
        candidate = target.with_name(f"{stem}_{index}.zip")
        if not candidate.exists():
            return candidate
    raise ValueError(f"备份目录中同名文件过多，无法创建：{target}")


# =============================================================================
# 台账读取工具（restore 复用）
# =============================================================================
def _data_csv_files() -> list[Path]:
    """``data/`` 根目录下的全部 CSV（按文件名排序，不含子目录）。"""
    data_dir = Path(config.DATA_DIR)
    if not data_dir.is_dir():
        return []
    return sorted(
        (item for item in data_dir.glob("*.csv") if item.is_file()),
        key=lambda item: item.name,
    )


def _log_csv_files() -> list[Path]:
    """``logs/`` 下的全部 CSV（按文件名排序）。"""
    logs_dir = Path(config.LOGS_DIR)
    if not logs_dir.is_dir():
        return []
    return sorted(
        (item for item in logs_dir.glob("*.csv") if item.is_file()),
        key=lambda item: item.name,
    )


def _csv_row_count(path: Any) -> int:
    """统计 CSV 数据行数（不含表头），文件缺失/损坏时返回 0。"""
    try:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            next(reader, None)
            return sum(1 for _ in reader)
    except (OSError, csv.Error, UnicodeDecodeError):
        return 0


def _table_key_for_file(file_name: str) -> str:
    """由台账文件名反查 ``ledger.models.TABLE_DEFS`` 的表名。"""
    target = str(file_name or "")
    for name, definition in models.TABLE_DEFS.items():
        if str(definition.get("file") or "") == target:
            return name
    return ""


def _table_label_for_file(file_name: str) -> str:
    """由台账文件名取中文表名（未知时用文件名）。"""
    key = _table_key_for_file(file_name)
    if key:
        return str(models.TABLE_DEFS[key].get("label") or key)
    return Path(str(file_name)).stem


def _table_summary(files: list[Path]) -> dict[str, int]:
    """各表条数：{中文表名: 行数}（manifest 对用户可读，按文件名排序）。"""
    summary: dict[str, int] = {}
    for path in files:
        summary[_table_label_for_file(path.name)] = _csv_row_count(path)
    return summary


def _table_keys(files: list[Path]) -> dict[str, int]:
    """各表条数：{表名: 行数}（机器可读别名，键取 ``TABLE_DEFS``，未知时用文件名）。"""
    summary: dict[str, int] = {}
    for path in files:
        key = _table_key_for_file(path.name) or Path(path.name).stem
        summary[key] = _csv_row_count(path)
    return summary


def _table_labels(files: list[Path]) -> dict[str, str]:
    """各表中文名称：{表名: 中文表名}（供程序按表名展示中文）。"""
    labels: dict[str, str] = {}
    for path in files:
        key = _table_key_for_file(path.name) or Path(path.name).stem
        labels[key] = _table_label_for_file(path.name)
    return labels


# =============================================================================
# 创建备份
# =============================================================================
def _readme_text(manifest: dict[str, Any]) -> str:
    """生成备份包内的 README.txt（中文说明，便于脱离系统阅读）。"""
    company = config.get_company()
    lines = [
        f"{config.APP_NAME}——数据备份说明",
        "=" * 56,
        f"企业名称：{company.get('name') or ''}",
        f"纳税人识别号：{company.get('unified_social_credit_code') or ''}",
        f"应用版本：{config.APP_VERSION}",
        f"创建时间：{manifest.get('created_at') or ''}",
        f"备份编号：{manifest.get('backup_id') or '（创建时生成）'}",
        f"备注：{manifest.get('note') or '无'}",
        f"操作人：{manifest.get('operator') or 'system'}",
        "",
        "一、备份内容",
        "  1. data/*.csv：全部台账与状态表（收入、费用、研发辅助账、凭证、",
        "     合同、发票池、银行流水池、匹配建议、期间、归档/备份索引、申报记录）；",
        "  2. manifest.json：逐文件 SHA-256 与各表条数，用于完整性校验；",
        f"  3. logs/*.csv：审计日志（{'已包含' if manifest.get('include_logs') else '未包含'}）。",
        "",
        "二、安全说明",
        "  * 本备份不包含 data/user_settings.json（DeepSeek API Key）；",
        "  * 不包含 uploads/（上传文件）、backups/（历史备份）、archive/（申报归档）；",
        "  * 备份文件本身为普通 zip，可离线保存；恢复请使用系统「设置 → 数据管理 → 恢复」。",
        "",
        "三、各表条数",
    ]
    for label, count in (manifest.get("tables") or {}).items():
        lines.append(f"  - {label}：{count} 条")
    lines.extend(
        [
            "",
            "四、文件与 SHA-256",
        ]
    )
    for entry in manifest.get("files") or []:
        lines.append(f"  - {entry.get('name')}  {entry.get('size')} B  {entry.get('sha256')}")
    lines.extend(
        [
            "",
            config.DISCLAIMER,
            "",
        ]
    )
    return "\n".join(lines)


def create_backup(
    tag: str = "",
    *,
    include_logs: bool = True,
    note: str = "",
    operator: str = "system",
) -> dict[str, Any]:
    """创建数据备份 zip（含 manifest.json 与 README.txt），返回备份信息。

    参数
    ----
    tag : 文件名标签（如 ``before_close``），清洗后追加到文件名；
    include_logs : 是否把 ``logs/*.csv`` 一并打包；
    note / operator : 备注与操作人（写入 manifest 与审计日志）。
    """
    target_dir = backup_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_tag = _safe_tag(tag)
    stem = f"{BACKUP_PREFIX}_{_timestamp()}"
    if safe_tag:
        stem = f"{stem}_{safe_tag}"
    target = _unique_target(target_dir / f"{stem}.zip")

    data_files = _data_csv_files()
    log_files = _log_csv_files() if include_logs else []
    files: list[dict[str, Any]] = []
    for path in data_files:
        if path.name in EXCLUDED_NAMES:
            continue
        files.append(
            {
                "name": f"data/{path.name}",
                "role": "data",
                "size": path.stat().st_size,
                "sha256": hash_tools.sha256_file(path),
            }
        )
    for path in log_files:
        files.append(
            {
                "name": f"logs/{path.name}",
                "role": "log",
                "size": path.stat().st_size,
                "sha256": hash_tools.sha256_file(path),
            }
        )
    manifest = manifest_tools.build_manifest(
        kind="数据备份",
        files=files,
        tables=_table_summary(data_files),
        note=note,
        operator=operator,
        extra={
            "include_logs": bool(include_logs),
            "excluded": list(EXCLUDED_NAMES) + [f"{name}/" for name in EXCLUDED_DIRS],
            "base_dir": "data",
            "table_keys": _table_keys(data_files),
            "table_labels": _table_labels(data_files),
        },
    )
    manifest["backup_id"] = target.stem
    readme = _readme_text(manifest)
    try:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in data_files:
                if path.name in EXCLUDED_NAMES:
                    continue
                archive.write(path, arcname=f"data/{path.name}")
            for path in log_files:
                archive.write(path, arcname=f"logs/{path.name}")
            archive.writestr("manifest.json", manifest_tools.manifest_bytes(manifest))
            archive.writestr("README.txt", readme.encode("utf-8"))
    except OSError as exc:
        raise ValueError(f"创建备份失败（写入 {target}）：{exc}") from exc

    file_hash = hash_tools.sha256_file(target)
    size_bytes = target.stat().st_size
    row = table("backups").append(
        {
            "path": str(target),
            "file_hash": file_hash,
            "size_bytes": size_bytes,
            "manifest_json": manifest,
            "note": note or f"操作人：{operator}",
        }
    )
    audit.log_user_action(
        "数据备份",
        f"备份文件={target}；大小={size_bytes} B；文件数={len(files)}；"
        f"表数={len(manifest.get('tables') or {})}；含日志={'是' if include_logs else '否'}；"
        f"操作人={operator}；备注={note or '无'}",
    )
    return {
        "ok": True,
        "id": str(row.get("id") or ""),
        "path": str(target),
        "name": target.name,
        "file_hash": file_hash,
        "size_bytes": size_bytes,
        "files": files,
        "file_count": len(files),
        "tables": manifest.get("tables") or {},
        "created_at": str(manifest.get("created_at") or ""),
        "app_version": config.APP_VERSION,
        "include_logs": bool(include_logs),
        "manifest": manifest,
        "manifest_hash": hash_tools.hash_json(manifest),
        "note": note,
        "operator": operator,
        "warnings": [],
        "disclaimer": config.DISCLAIMER,
    }


# =============================================================================
# 备份引用解析 / 读取 / 校验（restore 复用）
# =============================================================================
def _resolve_backup_ref(backup_id_or_path: Any) -> tuple[dict[str, Any] | None, Path]:
    """把备份编号 / 路径解析为 ``(backups 行, zip 路径)``。"""
    text = str(backup_id_or_path or "").strip()
    if not text:
        raise ValueError("请提供备份编号（如 BAK000001）或备份 zip 路径")
    row = table("backups").get(text)
    if row is not None:
        return row, Path(str(row.get("path") or ""))
    candidate = Path(text).expanduser()
    if not candidate.exists():
        alternative = backup_dir() / text
        if alternative.exists():
            candidate = alternative
    if not candidate.exists():
        raise ValueError(
            f"未找到备份：{text}（可用 list_backups() 查看现有备份，或使用 data/backups 下的 zip 路径）"
        )
    resolved = candidate.resolve()
    for record in table("backups").read():
        try:
            if Path(str(record.get("path") or "")).resolve() == resolved:
                return record, candidate
        except OSError:
            continue
    return None, candidate


def _safe_member_name(name: Any) -> str:
    """校验 zip 条目名安全（拒绝绝对路径、``..``、反斜杠、空名）。

    任何不安全条目都抛中文 ``ValueError``，调用方应整体拒绝该备份。
    """
    text = str(name or "").strip()
    if not text:
        raise ValueError("备份 zip 中存在空文件名条目，已拒绝")
    if "\x00" in text:
        raise ValueError("备份 zip 中存在非法文件名（含空字符），已拒绝")
    if text.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", text):
        raise ValueError(f"备份 zip 包含绝对路径条目：{text}，已拒绝（防 zip-slip）")
    if "\\" in text:
        raise ValueError(f"备份 zip 包含反斜杠路径条目：{text}，已拒绝（防 zip-slip）")
    parts = Path(text).parts
    if any(part in ("..", "") for part in parts) or text in (".", ".."):
        raise ValueError(f"备份 zip 包含越界路径条目：{text}，已拒绝（防 zip-slip）")
    return text


def _open_backup_zip(path: Any) -> zipfile.ZipFile:
    """打开备份 zip，损坏时抛中文 ``ValueError``。"""
    target = Path(path)
    if not target.is_file():
        raise ValueError(f"备份文件不存在：{target}")
    try:
        return zipfile.ZipFile(target)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError(f"备份 zip 已损坏或无法读取：{target.name}（{exc}）") from exc


def _decode_manifest(data: bytes, path: Any) -> dict[str, Any]:
    """解析 manifest.json 并做结构校验，失败时抛中文 ``ValueError``。"""
    try:
        raw = data.decode("utf-8")
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"备份 manifest.json 无法解析：{Path(path).name}（{exc}）") from exc
    if not isinstance(manifest, dict):
        raise ValueError(f"备份 manifest.json 格式不正确：{Path(path).name}（应为 JSON 对象）")
    check = manifest_tools.validate_manifest(manifest, required=manifest_tools.BACKUP_FIELDS)
    if not check["ok"]:
        problems = "、".join(list(check.get("missing") or []) + list(check.get("errors") or []))
        raise ValueError(f"备份 manifest.json 结构不完整：{problems}，已拒绝")
    return manifest


def _read_backup_zip(path: Any) -> dict[str, Any]:
    """读取并校验备份 zip 的结构（不写盘）。

    校验：zip 可读、条目名安全（防 zip-slip）、manifest.json 存在且结构正确、
    清单内文件均存在、无清单外文件。返回 ``{"path","manifest","entries","names"}``。
    """
    target = Path(path)
    with _open_backup_zip(target) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError(f"备份 zip 存在重复文件条目，已拒绝：{target.name}")
        checked_names: list[str] = []
        for name in names:
            checked_names.append(_safe_member_name(name))
        if "manifest.json" not in checked_names:
            raise ValueError(f"备份缺少 manifest.json，无法校验，已拒绝：{target.name}")
        manifest = _decode_manifest(archive.read("manifest.json"), target)
        entries = [item for item in manifest.get("files") or [] if isinstance(item, dict)]
        entry_names: list[str] = []
        for entry in entries:
            name = _safe_member_name(entry.get("name"))
            if name not in checked_names:
                raise ValueError(f"备份缺少清单中的文件：{name}，已拒绝")
            entry_names.append(name)
        allowed = set(entry_names) | set(ALLOWED_EXTRA_MEMBERS)
        unknown = [name for name in checked_names if name not in allowed]
        if unknown:
            raise ValueError(
                f"备份包含未在清单中的文件（安全白名单拒绝）：{'、'.join(unknown[:5])}"
            )
        return {
            "path": target,
            "manifest": manifest,
            "entries": entries,
            "names": checked_names,
        }


def _verify_manifest_entries(
    path: Any,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """逐文件比对 manifest 中的 SHA-256 与大小，返回校验明细。"""
    files: list[dict[str, Any]] = []
    missing: list[str] = []
    tampered: list[str] = []
    with _open_backup_zip(path) as archive:
        available = set(archive.namelist())
        for entry in manifest.get("files") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            expected = str(entry.get("sha256") or "")
            expected_size = entry.get("size")
            if name not in available:
                missing.append(name)
                files.append(
                    {"name": name, "expected": expected, "actual": "", "ok": False}
                )
                continue
            data = archive.read(name)
            actual = hash_tools.sha256_bytes(data)
            ok = actual == expected
            if expected_size is not None:
                try:
                    ok = ok and len(data) == int(expected_size)
                except (TypeError, ValueError):
                    ok = False
            if not ok:
                tampered.append(name)
            files.append(
                {
                    "name": name,
                    "expected": expected,
                    "actual": actual,
                    "size": len(data),
                    "expected_size": expected_size,
                    "ok": ok,
                }
            )
    return {
        "ok": not missing and not tampered,
        "files": files,
        "missing": missing,
        "tampered": tampered,
    }


def _read_member(path: Any, name: str) -> bytes:
    """读取备份 zip 中一个条目的字节内容。"""
    with _open_backup_zip(path) as archive:
        try:
            return archive.read(name)
        except KeyError as exc:
            raise ValueError(f"备份中缺少文件：{name}") from exc


# =============================================================================
# 列表 / 校验 / 删除
# =============================================================================
def list_backups() -> list[dict[str, Any]]:
    """列出全部备份记录（文件存在性、大小、创建时间、manifest 概要）。"""
    rows = table("backups").read()
    results: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        path = Path(str(record.get("path") or "")) if record.get("path") else None
        exists = bool(path and path.is_file())
        manifest = record.get("manifest_json")
        manifest = manifest if isinstance(manifest, dict) else {}
        size_bytes = record.get("size_bytes") or 0
        if exists and not size_bytes and path is not None:
            try:
                size_bytes = path.stat().st_size
            except OSError:
                size_bytes = 0
        files = [
            {
                "name": str(item.get("name") or ""),
                "sha256": str(item.get("sha256") or ""),
                "size": int(item.get("size") or 0),
            }
            for item in (manifest.get("files") or [])
            if isinstance(item, dict)
        ]
        record.update(
            {
                "name": path.name if path else "",
                "exists": exists,
                "size_bytes": int(size_bytes or 0),
                "file_count": len(files),
                "files": files,
                "tables": manifest.get("tables") or {},
                "created_at": str(record.get("created_at") or manifest.get("created_at") or ""),
                "manifest_created_at": str(manifest.get("created_at") or ""),
                "app_version": str(manifest.get("app_version") or ""),
                "include_logs": bool(manifest.get("include_logs")),
                "note": str(record.get("note") or manifest.get("note") or ""),
                "disclaimer": config.DISCLAIMER,
            }
        )
        results.append(record)
    results.sort(
        key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")),
        reverse=True,
    )
    return results


def verify_backup(backup_id_or_path: Any) -> dict[str, Any]:
    """校验备份：item 哈希、清单内文件哈希、文件存在性与整体 zip 哈希。"""
    record, path = _resolve_backup_ref(backup_id_or_path)
    result: dict[str, Any] = {
        "ok": False,
        "path": str(path),
        "id": str((record or {}).get("id") or ""),
        "files": [],
        "missing": [],
        "tampered": [],
        "file_hash": "",
        "expected_file_hash": str((record or {}).get("file_hash") or ""),
        "file_hash_ok": True,
        "checked_at": config.now_text(),
        "disclaimer": config.DISCLAIMER,
    }
    if not path.is_file():
        result.update(
            {
                "missing": [path.name],
                "error": f"备份文件不存在：{path}（可能已被移动或删除）",
            }
        )
        return result
    actual_zip_hash = hash_tools.sha256_file(path)
    result["file_hash"] = actual_zip_hash
    if result["expected_file_hash"]:
        result["file_hash_ok"] = actual_zip_hash == result["expected_file_hash"]
    try:
        info = _read_backup_zip(path)
    except ValueError as exc:
        result["error"] = str(exc)
        if "manifest" in str(exc):
            result["missing"] = ["manifest.json"]
        return result
    verify = _verify_manifest_entries(path, info["manifest"])
    result.update(
        {
            "files": verify["files"],
            "missing": verify["missing"],
            "tampered": verify["tampered"],
            "manifest": info["manifest"],
            "tables": info["manifest"].get("tables") or {},
        }
    )
    result["ok"] = bool(
        result["file_hash_ok"] and not verify["missing"] and not verify["tampered"]
    )
    return result


def delete_backup(backup_id_or_path: Any) -> bool:
    """删除备份文件 + ``backups`` 表记录 + 审计日志；无对应备份时返回 ``False``。"""
    record, path = _resolve_backup_ref(backup_id_or_path)
    deleted_file = False
    if path.is_file():
        try:
            path.unlink()
            deleted_file = True
        except OSError as exc:
            raise ValueError(f"删除备份文件失败：{path}（{exc}）") from exc
    deleted_row = False
    if record is not None:
        deleted_row = table("backups").delete(str(record.get("id") or ""))
    if not deleted_file and not deleted_row:
        return False
    audit.log_user_action(
        "数据备份删除",
        f"备份编号={(record or {}).get('id') or '（无记录）'}；文件={path}；"
        f"删除文件={'是' if deleted_file else '否'}；删除记录={'是' if deleted_row else '否'}",
    )
    return True


__all__ = [
    "BACKUP_PREFIX",
    "backup_dir",
    "create_backup",
    "list_backups",
    "verify_backup",
    "delete_backup",
]
