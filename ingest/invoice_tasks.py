"""发票待办任务状态（v1.41）。

任务本身由发票池实时计算；这里只持久化人工处理结果：
* 忽略：冲突/暂不处理的发票，跨天保留在「已忽略」；
* 恢复：重新放回待办。

已完成不需要单独存：生成凭证后发票状态变为「已入账」，任务会自然消失。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import config

STATE_FILE = lambda: Path(config.DATA_DIR) / "invoice_task_states.json"


def _load() -> dict[str, Any]:
    try:
        path = STATE_FILE()
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(data: dict[str, Any]) -> None:
    try:
        path = STATE_FILE()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def ignored_keys() -> set[str]:
    return {key for key, value in _load().items() if isinstance(value, dict) and value.get("status") == "ignored"}


def ignore(identity: str, *, note: str = "") -> None:
    data = _load()
    data[str(identity)] = {"status": "ignored", "note": str(note or "")[:300], "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    _save(data)


def restore(identity: str) -> None:
    data = _load()
    data.pop(str(identity), None)
    _save(data)


def ignored_rows() -> list[dict[str, Any]]:
    rows = []
    for key, value in _load().items():
        if isinstance(value, dict) and value.get("status") == "ignored":
            rows.append({"identity": key, "note": value.get("note", ""), "updated_at": value.get("updated_at", "")})
    return sorted(rows, key=lambda row: str(row.get("updated_at")), reverse=True)


__all__ = ["ignored_keys", "ignore", "restore", "ignored_rows"]
