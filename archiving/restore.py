"""备份预览与安全恢复。

安全规则
--------
1. :func:`inspect_backup` 与 ``restore_backup(dry_run=True)`` 只读校验，不写盘；
2. 恢复前先做整体校验：zip 可读、manifest 存在且结构正确、逐文件 SHA-256 一致、
   zip 条目名安全（拒绝 ``../``、绝对路径、反斜杠等 zip-slip）；
3. 实际恢复仅允许 manifest 白名单中的 ``data/*.csv``（按 basename 写回
   ``config.DATA_DIR``），拒绝未知文件、``user_settings.json`` 等敏感文件；
4. 默认先自动 ``create_backup(tag="restore_safety")`` 安全备份；
5. 逐文件先写临时文件、校验后 ``os.replace`` 原子替换，最后
   ``ledger.store.reset_cache()`` + ``ledger.init_storage()`` 并写审计日志「数据恢复」。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import config

from archiving import backup as backup_module
from archiving import hash as hash_tools
from ledger import audit

#: 恢复白名单：只允许 data/ 下的 CSV
RESTORE_PREFIX = "data/"


# =============================================================================
# 预览
# =============================================================================
def _manifest_preview(manifest: dict[str, Any]) -> dict[str, Any]:
    """从 manifest 提取预览信息（文件、表条数、备注等）。"""
    files = [
        {
            "name": str(item.get("name") or ""),
            "size": int(item.get("size") or 0),
            "sha256": str(item.get("sha256") or ""),
            "role": str(item.get("role") or ""),
        }
        for item in (manifest.get("files") or [])
        if isinstance(item, dict)
    ]
    return {
        "created_at": str(manifest.get("created_at") or manifest.get("generated_at") or ""),
        "app_version": str(manifest.get("app_version") or ""),
        "files": files,
        "tables": manifest.get("tables") or {},
        "note": str(manifest.get("note") or ""),
        "operator": str(manifest.get("operator") or ""),
        "include_logs": bool(manifest.get("include_logs")),
    }


def inspect_backup(backup_id_or_path: Any) -> dict[str, Any]:
    """预览备份内容（manifest / 各表条数 / 完整性校验），**不写盘**。"""
    record, path = backup_module._resolve_backup_ref(backup_id_or_path)  # noqa: SLF001
    result: dict[str, Any] = {
        "ok": False,
        "id": str((record or {}).get("id") or ""),
        "path": str(path),
        "created_at": "",
        "app_version": "",
        "files": [],
        "tables": {},
        "verify": {},
        "warnings": [],
        "disclaimer": config.DISCLAIMER,
    }
    if not path.is_file():
        message = f"备份文件不存在：{path}（可能已被移动或删除）"
        result.update({"message": message, "warnings": [message], "verify": {"ok": False}})
        return result
    verify = backup_module.verify_backup(path)
    result["verify"] = verify
    try:
        info = backup_module._read_backup_zip(path)  # noqa: SLF001
    except ValueError as exc:
        message = f"备份无法预览：{exc}"
        result.update({"message": message, "warnings": [message], "verify": verify})
        return result
    manifest = info["manifest"]
    result.update(_manifest_preview(manifest))
    result["manifest"] = manifest
    result["files_count"] = len(result["files"])
    result["ok"] = bool(verify.get("ok"))
    if verify.get("ok"):
        result["message"] = (
            f"备份校验通过，共 {len(result['files'])} 个文件、{len(result['tables'])} 张表；"
            "实际恢复前将自动创建安全备份。"
        )
    else:
        warnings = list(result["warnings"])
        if verify.get("tampered"):
            warnings.append(f"文件哈希不一致：{'、'.join(verify['tampered'])}")
        if verify.get("missing"):
            warnings.append(f"缺少文件：{'、'.join(verify['missing'])}")
        if verify.get("error"):
            warnings.append(str(verify["error"]))
        result["warnings"] = warnings
        result["message"] = "备份完整性校验未通过，禁止恢复：" + "；".join(warnings)
    return result


# =============================================================================
# 恢复
# =============================================================================
def _resolve_restore_members(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """把 manifest 文件清单拆分为「可恢复的 data/*.csv」与日志跳过提示。"""
    restore_items: list[dict[str, Any]] = []
    skipped_logs: list[str] = []
    for entry in entries:
        name = backup_module._safe_member_name(entry.get("name"))  # noqa: SLF001
        if name.startswith(RESTORE_PREFIX):
            basename = name[len(RESTORE_PREFIX):]
            if "/" in basename or "\\" in basename or not basename:
                raise ValueError(f"备份包含非法台账路径：{name}，已拒绝恢复")
            if basename in backup_module.PROTECTED_NAMES:
                raise ValueError(f"备份包含禁止恢复的敏感文件：{name}，已拒绝恢复")
            if not basename.endswith(".csv"):
                raise ValueError(f"备份清单只允许恢复 CSV 台账，包含：{name}，已拒绝恢复")
            restore_items.append(
                {
                    "member": name,
                    "basename": basename,
                    "entry": entry,
                }
            )
        elif name.startswith("logs/"):
            skipped_logs.append(name)
        else:
            raise ValueError(f"备份清单包含未知文件：{name}，已拒绝恢复（安全白名单）")
    return restore_items, skipped_logs


def _check_whole_file_hash(record: dict[str, Any] | None, path: Path) -> None:
    """若备份有记录，校验整体 zip 哈希未被篡改。"""
    if not record:
        return
    expected = str(record.get("file_hash") or "")
    if not expected:
        return
    actual = hash_tools.sha256_file(path)
    if actual != expected:
        raise ValueError(
            f"备份文件哈希与备份记录不一致，文件可能已被篡改：{path.name}，已拒绝恢复"
        )


def _verify_before_restore(path: Path) -> dict[str, Any]:
    """恢复前的完整校验：结构 + 逐文件哈希；不通过抛中文 ``ValueError``。"""
    info = backup_module._read_backup_zip(path)  # noqa: SLF001
    verify = backup_module._verify_manifest_entries(path, info["manifest"])  # noqa: SLF001
    if not verify["ok"]:
        problems: list[str] = []
        if verify["tampered"]:
            problems.append(f"哈希不一致文件：{'、'.join(verify['tampered'])}")
        if verify["missing"]:
            problems.append(f"缺失文件：{'、'.join(verify['missing'])}")
        raise ValueError(
            "备份完整性校验未通过，已拒绝恢复：" + "；".join(problems or ["未知原因"])
        )
    return verify


def restore_backup(
    backup_id_or_path: Any,
    *,
    dry_run: bool = True,
    safety_backup: bool = True,
    operator: str = "system",
) -> dict[str, Any]:
    """恢复备份：默认仅演练校验（``dry_run=True``），不写盘。

    参数
    ----
    backup_id_or_path : 备份编号（如 ``BAK000001``）或 zip 路径；
    dry_run : ``True`` 时只校验并返回预览，不写任何文件、不创建安全备份；
    safety_backup : 实际恢复前是否自动创建安全备份（默认 ``True``，强烈建议保持）；
    operator : 操作人（写入审计日志）。
    """
    record, path = backup_module._resolve_backup_ref(backup_id_or_path)  # noqa: SLF001
    if not path.is_file():
        raise ValueError(f"备份文件不存在：{path}（可能已被移动或删除）")
    _check_whole_file_hash(record, path)
    verify = _verify_before_restore(path)
    info = backup_module._read_backup_zip(path)  # noqa: SLF001
    manifest = info["manifest"]
    restore_items, skipped_logs = _resolve_restore_members(info["entries"])

    data_dir = Path(config.DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    resolved_data_dir = data_dir.resolve()
    targets: list[dict[str, Any]] = []
    for item in restore_items:
        destination = (data_dir / item["basename"]).resolve()
        if destination.parent != resolved_data_dir:
            raise ValueError(
                f"备份文件将被写入数据目录之外：{item['basename']}，已拒绝恢复（防 zip-slip）"
            )
        targets.append({**item, "destination": destination})

    preview = {
        "path": str(path),
        "id": str((record or {}).get("id") or ""),
        "files": [
            {
                "name": item["member"],
                "basename": item["basename"],
                "table": backup_module._table_label_for_file(item["basename"]),  # noqa: SLF001
                "size": int((item.get("entry") or {}).get("size") or 0),
                "sha256": str((item.get("entry") or {}).get("sha256") or ""),
            }
            for item in restore_items
        ],
        "tables": manifest.get("tables") or {},
        "verify": verify,
    }
    warnings: list[str] = []
    if skipped_logs:
        warnings.append(
            f"备份含 {len(skipped_logs)} 个日志文件，恢复时不会写回（避免审计日志重复），请按需人工留存。"
        )
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "restored": [],
            "safety_backup": "",
            "warnings": warnings + ["演练模式：仅校验与预览，未写入任何数据。"],
            "message": (
                f"备份校验通过，可恢复 {len(targets)} 个台账文件；"
                "确认后以 dry_run=False 执行（将自动创建安全备份）。"
            ),
            **preview,
            "disclaimer": config.DISCLAIMER,
        }

    # 实际恢复：先安全备份，再逐文件原子替换
    safety_path = ""
    if safety_backup:
        safety = backup_module.create_backup(  # noqa: SLF001
            tag="restore_safety",
            note=f"恢复前安全备份：{path.name}",
            operator=operator,
        )
        safety_path = str(safety.get("path") or "")
    else:
        warnings.append(
            "未执行恢复前安全备份（safety_backup=False），如恢复结果不符合预期将无法一键回退。"
        )

    staged: list[tuple[Path, Path]] = []
    try:
        for item in targets:
            data = backup_module._read_member(path, item["member"])  # noqa: SLF001
            expected = str((item.get("entry") or {}).get("sha256") or "")
            actual = hash_tools.sha256_bytes(data)
            if expected and actual != expected:
                raise ValueError(f"备份文件哈希不一致：{item['member']}，已中止恢复")
            temp = item["destination"].with_name(item["destination"].name + ".restore.tmp")
            temp.write_bytes(data)
            staged.append((temp, item["destination"]))
        for temp, destination in staged:
            os.replace(temp, destination)
        staged.clear()
    finally:
        for temp, _ in staged:
            try:
                temp.unlink()
            except OSError:
                pass

    # 刷新表缓存并重载存储（使恢复后的 CSV 立即生效）
    from ledger import store as ledger_store  # noqa: PLC0415 - 延迟导入避免循环

    ledger_store.reset_cache()
    try:
        import ledger  # noqa: PLC0415 - 延迟导入避免循环

        ledger.init_storage()
    except Exception as exc:  # noqa: BLE001 - 初始化失败不掩盖恢复结果
        warnings.append(f"恢复后初始化存储失败：{exc}，请重启应用确认数据。")

    restored: list[dict[str, Any]] = []
    for item in targets:
        basename = item["basename"]
        destination = item["destination"]
        table_key = backup_module._table_key_for_file(basename)  # noqa: SLF001
        restored.append(
            {
                "table": backup_module._table_label_for_file(basename),  # noqa: SLF001
                "table_key": table_key,
                "rows": backup_module._csv_row_count(destination),  # noqa: SLF001
                "path": str(destination),
            }
        )
    audit.log_user_action(
        "数据恢复",
        f"备份文件={path}；恢复表={','.join(item['basename'] for item in targets)}；"
        f"安全备份={safety_path or '无'}；操作人={operator}",
    )
    return {
        "ok": True,
        "dry_run": False,
        "restored": restored,
        "safety_backup": safety_path,
        "warnings": warnings,
        "message": f"已从备份恢复 {len(restored)} 个台账文件："
        + "、".join(f"{item['table']} {item['rows']} 条" for item in restored),
        **preview,
        "disclaimer": config.DISCLAIMER,
    }


__all__ = ["inspect_backup", "restore_backup"]
