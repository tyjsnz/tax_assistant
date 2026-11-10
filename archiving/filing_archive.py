"""申报包归档：Excel + PDF + SHA-256 清单（manifest），可随时校验未被篡改。

归档流程
--------
``filing.registry.build_all(period)`` → 逐表 ``filing.validator.validate_bundle`` →
``filing.export.export_package_excel`` 生成 Excel → :mod:`archiving.pdf` 生成 PDF →
复制到 ``data/archive/{period}/``（``申报包_{period}.xlsx``、``申报表_{period}.pdf``）→
逐文件 SHA-256 → 写 ``manifest.json``（UTF-8 indent=2）→ 写 ``archives`` 表 + 审计日志。

设计要点
--------
* 归档是**只读快照**：不改变台账与期间状态；期间未结账时返回 WARNING 提示
  「建议先结账再归档」；
* 重复归档同期间允许覆盖同名文件，旧 ``manifest.json`` 备份为
  ``manifest_YYYYmmdd_HHMMSS.json``，``archives`` 表新增记录、不删除历史；
* reportlab 不可用时自动降级为「仅 Excel + manifest」，并在 ``warnings`` 说明。
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import config

from archiving import hash as hash_tools
from archiving import manifest as manifest_tools
from archiving import pdf as pdf_module
from filing.export import export_package_excel
from filing.registry import build_all
from filing.validator import validate_bundle
from ledger import audit
from ledger.store import table

#: 归档子目录名（位于 ``config.DATA_DIR`` 下）
ARCHIVE_SUBDIR = "archive"

#: 归档包类型（与 ``ledger.models.ARCHIVE_KINDS`` 对齐）
ARCHIVE_KIND = "申报包"


# =============================================================================
# 路径与期间工具
# =============================================================================
def _normalize_period(period: Any) -> str:
    """规范化期间（``2026Q1`` / ``2026-03``），非法时抛中文 ``ValueError``。"""
    from ledger.reports import period_bounds  # noqa: PLC0415 - 延迟导入避免重依赖

    text, _, _ = period_bounds(period)
    return text


def archive_dir(period: Any = "") -> Path:
    """某期间的归档目录：``config.DATA_DIR / "archive" / {period}``。"""
    base = Path(config.DATA_DIR) / ARCHIVE_SUBDIR
    text = str(period or "").strip()
    if not text:
        return base
    return base / _normalize_period(text)


def _copy_atomic(source: Path, target: Path) -> Path:
    """把文件原子复制到目标位置（同目录临时文件 + ``os.replace``）。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    try:
        shutil.copyfile(source, tmp)
        tmp.replace(target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return target


def _period_warnings(period: str) -> list[str]:
    """期间未结账时给出归档提示（不阻断归档）。"""
    try:
        from ledger import locks  # noqa: PLC0415 - 基础层模块，缺失时忽略

        status = str(locks.status_of(period) or "未结账")
    except Exception:  # noqa: BLE001 - 期间锁不可用时按未结账提示
        status = "未结账"
    if status == "未结账":
        return [
            f"期间 {period} 尚未结账（当前状态：{status}），建议先结账再归档；"
            "归档包为只读快照，本次操作不会改变期间状态。"
        ]
    return []


# =============================================================================
# 归档主流程
# =============================================================================
def _validate_bundles(bundles: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """逐表校验并把校验结论回填到 bundle（供 Excel/PDF 展示），返回报告与计数。"""
    reports: list[dict[str, Any]] = []
    counts = {"PASS": 0, "WARNING": 0, "ERROR": 0}
    for bundle in bundles:
        report = validate_bundle(bundle)
        status = str(report.get("status") or "PASS").upper()
        if status not in counts:
            status = "PASS"
        counts[status] += 1
        details = list(report.get("details") or [])
        bundle["checks"] = details
        bundle["summary"] = {"level": status, "text": report.get("summary") or ""}
        bundle["validate_status"] = status
        reports.append(
            {
                "form_id": str(bundle.get("form_id") or ""),
                "form_name": str(bundle.get("form_name") or ""),
                "status": status,
                "summary": str(report.get("summary") or ""),
                "details": details,
            }
        )
    return reports, counts


def _backup_old_manifest(target_dir: Path) -> str:
    """归档前保留旧 manifest（``manifest_YYYYmmdd_HHMMSS.json``），返回历史文件名。"""
    manifest_path = target_dir / "manifest.json"
    if not manifest_path.is_file():
        return ""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    history = target_dir / f"manifest_{stamp}.json"
    index = 1
    while history.exists():
        history = target_dir / f"manifest_{stamp}_{index}.json"
        index += 1
    try:
        shutil.copy2(manifest_path, history)
    except OSError:
        return ""
    return history.name


def archive_filing_package(
    period: Any,
    *,
    include_pdf: bool = True,
    note: str = "",
    operator: str = "system",
) -> dict[str, Any]:
    """生成并归档某期间申报包（Excel + PDF + SHA-256 清单）。

    返回
    ----
    dict：``ok`` / ``period`` / ``dir`` / ``files`` / ``manifest_path`` /
    ``manifest_hash`` / ``pdf`` / ``forms`` / ``checks_summary`` / ``warnings`` /
    ``disclaimer`` 等。
    """
    text = _normalize_period(period)
    warnings = _period_warnings(text)

    bundles = build_all(text)
    if not bundles:
        raise ValueError(f"期间 {text} 未生成任何申报表，无法归档")
    reports, counts = _validate_bundles(bundles)

    target_dir = archive_dir(text)
    target_dir.mkdir(parents=True, exist_ok=True)
    excel_name = f"申报包_{text}.xlsx"
    pdf_name = f"申报表_{text}.pdf"

    file_entries: list[dict[str, Any]] = []
    pdf_generated = False
    with tempfile.TemporaryDirectory(prefix="tax_archive_") as temp_root:
        temp_dir = Path(temp_root)
        temp_excel = export_package_excel(text, bundles, temp_dir / excel_name)
        produced: list[tuple[Path, str, str]] = [(Path(temp_excel), excel_name, "excel")]
        if include_pdf:
            try:
                temp_pdf = pdf_module.build_filing_pdf(
                    text, bundles, checks=reports, path=temp_dir / pdf_name
                )
                produced.append((Path(temp_pdf), pdf_name, "pdf"))
                pdf_generated = True
            except pdf_module.PdfUnavailableError as exc:
                warnings.append(f"PDF 生成不可用，已降级为「Excel + manifest」：{exc}")
            except Exception as exc:  # noqa: BLE001 - PDF 意外失败不影响归档
                warnings.append(f"PDF 生成失败，已降级为「Excel + manifest」：{exc}")
        for source, name, role in produced:
            destination = _copy_atomic(source, target_dir / name)
            file_entries.append(
                {
                    "name": name,
                    "role": role,
                    "sha256": hash_tools.sha256_file(destination),
                    "size": destination.stat().st_size,
                }
            )

    history_manifest = _backup_old_manifest(target_dir)
    manifest_path = target_dir / "manifest.json"
    policy_version = next(
        (str(bundle.get("policy_version") or "") for bundle in bundles if bundle.get("policy_version")),
        "",
    )
    policy_basis_text = next(
        (
            str(bundle.get("policy_basis_text") or "")
            for bundle in bundles
            if bundle.get("policy_basis_text")
        ),
        "",
    )
    forms = [
        {
            "form_id": str(bundle.get("form_id") or ""),
            "form_name": str(bundle.get("form_name") or ""),
            "status": str(bundle.get("status") or "草稿"),
            "period_label": str(bundle.get("period_label") or ""),
            "check_status": str(bundle.get("validate_status") or "PASS"),
        }
        for bundle in bundles
    ]
    manifest = manifest_tools.build_manifest(
        kind=ARCHIVE_KIND,
        files=file_entries,
        period=text,
        forms=forms,
        checks={
            "summary": counts,
            "forms": [
                {
                    "form_id": report["form_id"],
                    "form_name": report["form_name"],
                    "status": report["status"],
                    "summary": report["summary"],
                }
                for report in reports
            ],
        },
        policy_version=policy_version,
        policy_basis_text=policy_basis_text,
        note=note,
        operator=operator,
        extra={"include_pdf": pdf_generated, "history_manifest": history_manifest},
    )
    manifest_tools.write_manifest(manifest_path, manifest)
    manifest_hash = hash_tools.sha256_file(manifest_path)
    total_size = sum(int(item.get("size") or 0) for item in file_entries) + manifest_path.stat().st_size

    package_files = [
        {
            "name": item["name"],
            "path": str(target_dir / item["name"]),
            "sha256": item["sha256"],
            "size": item["size"],
            "role": item.get("role") or "",
        }
        for item in file_entries
    ]
    # 返回的文件清单包含 manifest.json 本身，便于调用方一并展示/下载
    files = package_files + [
        {
            "name": "manifest.json",
            "path": str(manifest_path),
            "sha256": manifest_hash,
            "size": manifest_path.stat().st_size,
            "role": "manifest",
        }
    ]
    row = table("archives").append(
        {
            "period": text,
            "kind": ARCHIVE_KIND,
            "path": str(manifest_path),
            "file_hash": manifest_hash,
            "size_bytes": total_size,
            "manifest_json": manifest,
            "note": note or f"操作人：{operator}",
        }
    )
    audit.log_user_action(
        "申报包归档",
        f"期间={text}；目录={target_dir}；文件={','.join(item['name'] for item in files)}；"
        f"清单哈希={manifest_hash[:16]}…；PDF={'是' if pdf_generated else '否'}；"
        f"操作人={operator}；备注={note or '无'}",
    )

    return {
        "ok": True,
        "id": str(row.get("id") or ""),
        "period": text,
        "dir": str(target_dir),
        "archive_dir": str(target_dir),
        "files": files,
        "manifest_path": str(manifest_path),
        "manifest_hash": manifest_hash,
        "size_bytes": total_size,
        "pdf": pdf_generated,
        "forms": [str(bundle.get("form_id") or "") for bundle in bundles],
        "checks": [
            {
                "form_id": report["form_id"],
                "form_name": report["form_name"],
                "status": report["status"],
                "summary": report["summary"],
            }
            for report in reports
        ],
        "checks_summary": counts,
        "generated_at": str(manifest.get("generated_at") or ""),
        "app_version": config.APP_VERSION,
        "policy_version": policy_version,
        "policy_basis_text": policy_basis_text,
        "history_manifest": history_manifest,
        "not_closed_warning": any("建议先结账再归档" in item for item in warnings),
        "warnings": warnings,
        "note": note,
        "disclaimer": config.DISCLAIMER,
    }


# =============================================================================
# 查询与校验
# =============================================================================
def _manifest_for_row(row: dict[str, Any] | None, manifest_path: Path) -> dict[str, Any] | None:
    """优先读取磁盘 manifest，失败时回退到 archives 表中保存的 manifest_json。"""
    if manifest_path.is_file():
        try:
            return manifest_tools.read_manifest(manifest_path)
        except ValueError:
            return None
    stored = (row or {}).get("manifest_json")
    return stored if isinstance(stored, dict) else None


def list_archives(period: Any = None) -> list[dict[str, Any]]:
    """列出归档记录（含文件存在性、哈希校验概要），``period`` 为空时返回全部。"""
    rows = table("archives").read()
    target_period = ""
    if period not in (None, ""):
        try:
            target_period = _normalize_period(period)
        except ValueError:
            target_period = str(period).strip()
    results: list[dict[str, Any]] = []
    for row in rows:
        row_period = str(row.get("period") or "")
        if target_period and row_period != target_period:
            continue
        results.append(_archive_detail(row))
    results.sort(
        key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")),
        reverse=True,
    )
    return results


def _archive_detail(row: dict[str, Any]) -> dict[str, Any]:
    """把 archives 表记录补全为含文件校验概要的展示字典。"""
    record = dict(row)
    raw_path = str(row.get("path") or "")
    path = Path(raw_path) if raw_path else None
    if path is not None and path.name == "manifest.json":
        target_dir = path.parent
    elif path is not None and path.is_dir():
        target_dir = path
    elif path is not None:
        target_dir = path.parent
    else:
        target_dir = archive_dir(str(row.get("period") or ""))
    manifest_path = target_dir / "manifest.json" if target_dir else None
    manifest = _manifest_for_row(row, manifest_path) if manifest_path else None
    files: list[dict[str, Any]] = []
    tampered: list[str] = []
    missing: list[str] = []
    if manifest:
        for entry in manifest.get("files") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            if not name:
                continue
            file_path = (target_dir / name) if target_dir else Path(name)
            exists = file_path.is_file()
            actual = ""
            ok = False
            if exists:
                try:
                    actual = hash_tools.sha256_file(file_path)
                except OSError:
                    actual = ""
                ok = actual == str(entry.get("sha256") or "")
            if not exists:
                missing.append(name)
            elif not ok:
                tampered.append(name)
            files.append(
                {
                    "name": name,
                    "path": str(file_path),
                    "sha256": str(entry.get("sha256") or ""),
                    "actual": actual,
                    "size": int(entry.get("size") or 0),
                    "ok": ok,
                }
            )
    manifest_ok = manifest is not None
    if not manifest_ok:
        missing.append("manifest.json")
    record.update(
        {
            "dir": str(target_dir or ""),
            "archive_dir": str(target_dir or ""),
            "manifest_path": str(manifest_path or ""),
            "manifest_hash": str(row.get("file_hash") or ""),
            "files": files,
            "pdf": any(item.get("name", "").endswith(".pdf") for item in files),
            "forms": [
                str(item.get("form_id") or "")
                for item in ((manifest or {}).get("forms") or [])
                if isinstance(item, dict)
            ],
            "checks_summary": (manifest or {}).get("checks", {}).get("summary", {}),
            "generated_at": str((manifest or {}).get("generated_at") or row.get("created_at") or ""),
            "verified": bool(manifest_ok and not tampered and not missing),
            "exists": all(item.get("ok") for item in files) if files else False,
            "tampered": tampered,
            "missing": missing,
        }
    )
    return record


def _resolve_archive_ref(archive_id_or_path: Any) -> tuple[dict[str, Any] | None, Path]:
    """把归档编号 / 目录 / manifest 路径解析为 ``(archives 行, 归档目录)``。"""
    text = str(archive_id_or_path or "").strip()
    if not text:
        raise ValueError("请提供归档编号（如 ARC000001）或归档目录/清单路径")
    row = table("archives").get(text)
    if row is not None:
        raw_path = str(row.get("path") or "")
        manifest_path = Path(raw_path)
        target_dir = manifest_path.parent if manifest_path.name == "manifest.json" else manifest_path
        return row, (target_dir if target_dir else archive_dir(str(row.get("period") or "")))
    candidate = Path(text)
    if candidate.is_file():
        return None, candidate.parent
    if candidate.is_dir():
        return None, candidate
    raise ValueError(f"未找到归档记录或归档路径：{text}（可用 list_archives() 查看现有归档）")


def verify_archive(archive_id_or_path: Any) -> dict[str, Any]:
    """校验归档包：逐文件比对 manifest 中的 SHA-256，检测篡改与缺失。"""
    row, target_dir = _resolve_archive_ref(archive_id_or_path)
    manifest_path = target_dir / "manifest.json" if target_dir else None
    stored = (row or {}).get("manifest_json")
    expected_manifest_hash = str((row or {}).get("file_hash") or "")
    manifest: dict[str, Any] | None = None
    if manifest_path is not None and manifest_path.is_file():
        try:
            manifest = manifest_tools.read_manifest(manifest_path)
        except ValueError as exc:
            return {
                "ok": False,
                "period": str((row or {}).get("period") or ""),
                "manifest_path": str(manifest_path),
                "manifest_hash": expected_manifest_hash,
                "files": [],
                "tampered": ["manifest.json"],
                "missing": [],
                "error": str(exc),
                "checked_at": config.now_text(),
                "disclaimer": config.DISCLAIMER,
            }
    elif isinstance(stored, dict):
        manifest = stored
    if manifest is None:
        return {
            "ok": False,
            "period": str((row or {}).get("period") or ""),
            "manifest_path": str(manifest_path or ""),
            "manifest_hash": expected_manifest_hash,
            "files": [],
            "tampered": [],
            "missing": ["manifest.json"],
            "error": "归档目录缺少 manifest.json，且 archives 表中无清单快照",
            "checked_at": config.now_text(),
            "disclaimer": config.DISCLAIMER,
        }

    files: list[dict[str, Any]] = []
    tampered: list[str] = []
    missing: list[str] = []
    for entry in manifest.get("files") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        if Path(name).name != name or name in (".", ".."):
            tampered.append(name)
            continue
        file_path = target_dir / name
        expected = str(entry.get("sha256") or "").lower()
        if not file_path.is_file():
            missing.append(name)
            files.append(
                {"name": name, "expected": expected, "actual": "", "ok": False}
            )
            continue
        try:
            actual = hash_tools.sha256_file(file_path)
        except OSError:
            actual = ""
        ok = bool(expected) and actual == expected
        if not ok:
            tampered.append(name)
        files.append({"name": name, "expected": expected, "actual": actual, "ok": ok})

    manifest_ok = True
    actual_manifest_hash = ""
    if expected_manifest_hash:
        if manifest_path is not None and manifest_path.is_file():
            actual_manifest_hash = hash_tools.sha256_file(manifest_path)
            if actual_manifest_hash != expected_manifest_hash:
                manifest_ok = False
                tampered.append("manifest.json")
        else:
            manifest_ok = False
            missing.append("manifest.json")
    return {
        "ok": bool(manifest_ok and not tampered and not missing),
        "period": str(manifest.get("period") or (row or {}).get("period") or ""),
        "manifest_path": str(manifest_path or ""),
        "manifest_hash": actual_manifest_hash or expected_manifest_hash,
        "files": files,
        "tampered": tampered,
        "missing": missing,
        "checked_at": config.now_text(),
        "disclaimer": config.DISCLAIMER,
    }


__all__ = [
    "ARCHIVE_KIND",
    "archive_dir",
    "archive_filing_package",
    "list_archives",
    "verify_archive",
]
