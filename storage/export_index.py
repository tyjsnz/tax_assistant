"""导出资料索引：扫描标准导出目录，生成/更新 index.json。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import config
from storage.export_paths import active_company_id, relative_to_exports

INDEX_NAME = "index.json"


def _company_root(company_id: str | None = None) -> Path:
    return Path(config.EXPORT_DIR) / (company_id or active_company_id())


def index_path(company_id: str | None = None) -> Path:
    return _company_root(company_id) / INDEX_NAME


def load_index(company_id: str | None = None) -> list[dict[str, Any]]:
    try:
        path = index_path(company_id)
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record(path: str | Path, *, period: str = "", category: str = "", company_id: str | None = None, note: str = "") -> dict[str, Any]:
    file_path = Path(path)
    entries = load_index(company_id)
    key = relative_to_exports(file_path)
    for entry in entries:
        if entry.get("relative_path") == key:
            return entry
    entry = {
        "relative_path": key,
        "file_name": file_path.name,
        "period": period,
        "category": category or (file_path.parent.name if file_path.parent else ""),
        "size": file_path.stat().st_size if file_path.exists() else 0,
        "sha256": _sha256(file_path) if file_path.exists() else "",
        "exported_at": datetime.fromtimestamp(file_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S") if file_path.exists() else "",
        "note": note,
    }
    entries.append(entry)
    try:
        path_obj = index_path(company_id)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        path_obj.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return entry


def scan(company_id: str | None = None) -> list[dict[str, Any]]:
    """扫描公司导出目录，把尚未索引的文件补入 index.json。"""
    root = _company_root(company_id)
    entries = load_index(company_id)
    known = {entry.get("relative_path") for entry in entries}
    changed = False
    if root.exists():
        for path in root.rglob("*"):
            if not path.is_file() or path.name == INDEX_NAME:
                continue
            rel = relative_to_exports(path)
            if rel in known or "_legacy" in path.parts:
                continue
            parts = path.parts
            period = ""
            try:
                idx = parts.index(str(root.name))
                if len(parts) > idx + 3:
                    year, slot = parts[idx + 1], parts[idx + 2]
                    period = f"{year}Q{slot[1:]}" if slot.startswith("Q") else (f"{year}-{slot}" if slot.isdigit() else year)
            except ValueError:
                pass
            entries.append({
                "relative_path": rel,
                "file_name": path.name,
                "period": period,
                "category": path.parent.name,
                "size": path.stat().st_size,
                "sha256": _sha256(path),
                "exported_at": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "note": "",
            })
            changed = True
    if changed:
        try:
            index_path(company_id).write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    return sorted(entries, key=lambda entry: str(entry.get("exported_at")), reverse=True)


def find(company_id: str | None = None, *, year: str = "", month: str = "", category: str = "", keyword: str = "") -> list[dict[str, Any]]:
    entries = scan(company_id)
    root = Path(config.EXPORT_DIR)
    result = []
    for entry in entries:
        rel = str(entry.get("relative_path") or "")
        if year and f"/{year}/" not in f"/{rel}":
            continue
        if month and f"/{month}/" not in f"/{rel}" and not (month == "年度" and f"/{year}/年度/" in f"/{rel}"):
            # 季度目录按 Qn 匹配；月份筛选不强行套季度
            if not (month.startswith("Q") and f"/{month}/" in f"/{rel}"):
                continue
        if category and category not in str(entry.get("category") or ""):
            continue
        if keyword and keyword not in str(entry.get("file_name") or "") + str(entry.get("period") or ""):
            continue
        entry = dict(entry)
        entry["path"] = str(root / rel)
        result.append(entry)
    return result


__all__ = ["index_path", "load_index", "record", "scan", "find"]
