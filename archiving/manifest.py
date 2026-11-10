"""归档/备份清单（manifest）的构造、写入、读取与字段校验。

manifest 是归档包与备份包的「自证文件」：记录生成时间、版本、政策依据、
文件清单（名称 + SHA-256 + 大小）、各表条数与免责声明。归档包与备份 zip
内均放置 ``manifest.json``（UTF-8、indent=2），便于脱离系统人工核查。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import config

from archiving import hash as hash_tools

#: manifest 结构版本（字段变化时递增）
FORMAT_VERSION = "1.0"

#: 所有 manifest 必须具备的字段
REQUIRED_FIELDS: tuple[str, ...] = (
    "format_version",
    "kind",
    "files",
    "disclaimer",
)

#: 归档包 manifest 额外必备字段
ARCHIVE_FIELDS: tuple[str, ...] = (
    "period",
    "generated_at",
    "app_version",
    "preview",
)

#: 备份包 manifest 额外必备字段
BACKUP_FIELDS: tuple[str, ...] = (
    "created_at",
    "app_version",
    "tables",
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def build_manifest(
    *,
    kind: str,
    files: Iterable[dict[str, Any]] | None = None,
    period: str = "",
    forms: Iterable[dict[str, Any]] | None = None,
    checks: dict[str, Any] | None = None,
    tables: dict[str, int] | None = None,
    policy_version: str = "",
    policy_basis_text: str = "",
    note: str = "",
    operator: str = "system",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 manifest 字典（同时兼容归档包与备份包）。

    参数
    ----
    kind : 「申报包」/「数据备份」等类型标签；
    files : 文件清单，每项至少含 ``name`` / ``sha256`` / ``size``；
    period/forms/checks : 归档包专用（期间、报表元数据、校验汇总）；
    tables : 备份包专用（各表条数）；
    policy_version/policy_basis_text : 生成时适用的政策版本与依据；
    extra : 附加字段（如备份的 include_logs、排除目录说明）。
    """
    created_at = _now()
    manifest: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "kind": str(kind or "其他"),
        "generated_at": created_at,
        "created_at": created_at,
        "app_version": str(config.APP_VERSION or ""),
        "policy_version": str(policy_version or ""),
        "policy_basis_text": str(policy_basis_text or ""),
        "files": [dict(item) for item in (files or [])],
        "note": str(note or ""),
        "operator": str(operator or "system"),
        "disclaimer": config.DISCLAIMER,
    }
    if period:
        manifest["period"] = str(period)
    if forms is not None:
        manifest["forms"] = [dict(item) for item in forms]
    else:
        manifest["forms"] = []
    if checks is not None:
        manifest["checks"] = dict(checks)
    else:
        manifest["checks"] = {"summary": {}, "forms": []}
    if tables is not None:
        manifest["tables"] = dict(tables)
    else:
        manifest["tables"] = {}
    # 「preview」字段让 manifest 自描述（供 UI/人工快速了解包内容）
    manifest["preview"] = {
        "files": len(manifest["files"]),
        "forms": len(manifest.get("forms") or []),
        "tables": len(manifest.get("tables") or {}),
    }
    if extra:
        for key, value in extra.items():
            if key not in manifest:
                manifest[key] = value
    return manifest


def validate_manifest(
    manifest: Any,
    *,
    required: Iterable[str] | None = None,
) -> dict[str, Any]:
    """校验 manifest 结构，返回 ``{"ok", "missing", "errors"}``。

    只做结构校验（字段是否存在、类型是否合理），不校验文件哈希——文件哈希
    由归档/备份的 ``verify_*`` 函数读取实际文件后比对。
    """
    required_fields = set(REQUIRED_FIELDS)
    if required:
        required_fields.update(required)
    errors: list[str] = []
    missing: list[str] = []
    if not isinstance(manifest, dict):
        return {"ok": False, "missing": sorted(required_fields), "errors": ["manifest 不是 JSON 对象"]}
    for field in sorted(required_fields):
        if field not in manifest or manifest.get(field) in (None, ""):
            if field in ("files", "tables"):
                if manifest.get(field) in (None, ""):
                    missing.append(field)
            else:
                missing.append(field)
    files = manifest.get("files")
    if files is not None and not isinstance(files, list):
        errors.append("manifest.files 必须是列表")
    elif isinstance(files, list):
        for index, item in enumerate(files):
            if not isinstance(item, dict):
                errors.append(f"manifest.files[{index}] 不是对象")
                continue
            for key in ("name", "sha256"):
                if not str(item.get(key) or "").strip():
                    errors.append(f"manifest.files[{index}] 缺少 {key}")
    if "tables" in manifest and not isinstance(manifest["tables"], dict):
        errors.append("manifest.tables 必须是对象")
    if manifest.get("format_version") and str(manifest["format_version"]) != FORMAT_VERSION:
        errors.append(
            f"manifest 结构版本不受支持：{manifest.get('format_version')}（当前支持 {FORMAT_VERSION}）"
        )
    return {"ok": not missing and not errors, "missing": missing, "errors": errors}


def manifest_bytes(manifest: dict[str, Any]) -> bytes:
    """序列化 manifest（UTF-8、indent=2、中文不转义）。"""
    return json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")


def write_manifest(path: Any, manifest: dict[str, Any]) -> Path:
    """把 manifest 原子写入目标路径（UTF-8 indent=2）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_bytes(manifest_bytes(manifest))
        tmp.replace(target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return target


def read_manifest(path: Any) -> dict[str, Any]:
    """读取并解析 manifest.json；缺失/损坏/非对象时抛 ``ValueError``（中文提示）。"""
    target = Path(path)
    if not target.is_file():
        raise ValueError(f"未找到清单文件：{target}（归档/备份可能不完整）")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"清单文件无法解析：{target}（{exc}）") from exc
    if not isinstance(data, dict):
        raise ValueError(f"清单文件格式不正确：{target}（应为 JSON 对象）")
    return data


def file_entry(path: Any, *, name: str | None = None, role: str = "") -> dict[str, Any]:
    """构造文件清单项：名称 + 角色 + 大小 + SHA-256。"""
    target = Path(path)
    entry: dict[str, Any] = {
        "name": str(name or target.name),
        "sha256": hash_tools.sha256_file(target),
        "size": target.stat().st_size,
    }
    if role:
        entry["role"] = str(role)
    return entry


__all__ = [
    "FORMAT_VERSION",
    "REQUIRED_FIELDS",
    "ARCHIVE_FIELDS",
    "BACKUP_FIELDS",
    "build_manifest",
    "validate_manifest",
    "manifest_bytes",
    "write_manifest",
    "read_manifest",
    "file_entry",
]
