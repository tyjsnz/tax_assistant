"""新手引导进度（v1.18）：按公司保存 5 步完成情况。

存储位置：``DATA_ROOT/onboarding.json``（不随公司账套删除，仅记录引导进度）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import config

TOTAL_STEPS = 5
STEP_LABELS = [
    "公司信息",
    "税务基础",
    "第一笔账",
    "第一次申报",
    "完成",
]


def _path() -> Path:
    return Path(config.DATA_ROOT) / "onboarding.json"


def _read() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write(data: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def get_state(company_id: str) -> dict[str, Any]:
    key = str(company_id or "main")
    data = _read().get(key) or {}
    steps = data.get("steps") if isinstance(data.get("steps"), list) else []
    steps = [bool(item) for item in steps] + [False] * (TOTAL_STEPS - len(steps))
    return {
        "steps": steps[:TOTAL_STEPS],
        "data": data.get("data") if isinstance(data.get("data"), dict) else {},
        "updated_at": data.get("updated_at") or "",
    }


def save_state(company_id: str, state: dict[str, Any]) -> None:
    key = str(company_id or "main")
    data = _read()
    steps = [bool(item) for item in (state.get("steps") or [])][:TOTAL_STEPS]
    data[key] = {
        "steps": steps + [False] * (TOTAL_STEPS - len(steps)),
        "data": state.get("data") or {},
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _write(data)


def mark_step(company_id: str, index: int, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    state = get_state(company_id)
    steps = state["steps"]
    if 0 <= index < TOTAL_STEPS:
        steps[index] = True
    if extra:
        state["data"].update(extra)
    save_state(company_id, {"steps": steps, "data": state["data"]})
    return get_state(company_id)


def reset(company_id: str) -> None:
    state = get_state(company_id)
    save_state(company_id, {"steps": [False] * TOTAL_STEPS, "data": state.get("data") or {}})


def is_complete(company_id: str) -> bool:
    return all(get_state(company_id)["steps"])


def completed_count(company_id: str) -> int:
    return sum(1 for item in get_state(company_id)["steps"] if item)


__all__ = [
    "TOTAL_STEPS", "STEP_LABELS", "get_state", "save_state", "mark_step", "reset",
    "is_complete", "completed_count",
]
