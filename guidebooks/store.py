"""操作指引「我的截图备注」本地存储（v1.17）。

* 截图只写入当前公司账套的 ``uploads/guide_shots/``；
* 不上传、不进入安装包、不跨公司共享；
* 元数据存 ``guide_shots`` 表，文件按随机名保存，避免路径穿越与重名。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import config
from ledger import store
from ledger.audit import log_user_action

TABLE = "guide_shots"
SUBDIR = "guide_shots"
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_BYTES = 8 * 1024 * 1024  # 8 MB


def _target_dir() -> Path:
    directory = Path(config.UPLOAD_DIR) / SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _safe_suffix(filename: str) -> str:
    suffix = Path(str(filename or "")).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError("只支持 PNG / JPG / JPEG / WebP 图片")
    return suffix


def resolve_path(record: dict[str, Any]) -> Path:
    """返回截图绝对路径，并确保仍在当前公司 uploads 目录内。"""
    base = (Path(config.UPLOAD_DIR) / SUBDIR).resolve()
    path = (base / str(record.get("stored_name") or "")).resolve()
    if base not in path.parents:
        raise ValueError("截图路径不合法")
    return path


def save_shot(
    workflow_id: str,
    step_index: int,
    data: bytes,
    original_name: str,
    note: str = "",
) -> dict[str, Any]:
    """保存一张本地截图与备注，返回元数据记录。"""
    if not data:
        raise ValueError("截图内容为空")
    if len(data) > MAX_BYTES:
        raise ValueError("截图不能超过 8 MB")
    suffix = _safe_suffix(original_name)
    stored_name = f"{uuid.uuid4().hex}{suffix}"
    target = _target_dir() / stored_name
    target.write_bytes(data)
    record = store.table(TABLE).append(
        {
            "workflow_id": str(workflow_id or ""),
            "step_index": int(step_index or 0),
            "file_name": str(original_name or "")[:200],
            "stored_name": stored_name,
            "note": str(note or "").strip()[:500],
        }
    )
    log_user_action("操作指引截图备注", f"{workflow_id} 第 {step_index} 步：{record.get('id')}")
    return record


def list_shots(workflow_id: str | None = None, step_index: int | None = None) -> list[dict[str, Any]]:
    rows = store.table(TABLE).read()
    if workflow_id:
        rows = [row for row in rows if str(row.get("workflow_id")) == str(workflow_id)]
    if step_index is not None:
        rows = [row for row in rows if int(row.get("step_index") or 0) == int(step_index)]
    return sorted(rows, key=lambda row: str(row.get("created_at") or ""), reverse=True)


def delete_shot(shot_id: str) -> None:
    record = store.table(TABLE).get(str(shot_id))
    if not record:
        raise KeyError(f"截图记录不存在：{shot_id}")
    try:
        resolve_path(record).unlink(missing_ok=True)
    finally:
        store.table(TABLE).delete(str(shot_id))
        log_user_action("删除操作指引截图", str(shot_id))


__all__ = ["save_shot", "list_shots", "delete_shot", "resolve_path", "ALLOWED_EXTENSIONS", "MAX_BYTES"]
