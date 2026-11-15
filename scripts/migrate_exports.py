"""把 exports/ 根目录下旧的平铺文件迁移到 exports/{company}/_legacy/日期/。

默认只预览；确认后加 --apply 执行移动。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import workspace  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移旧导出文件")
    parser.add_argument("--apply", action="store_true", help="实际移动文件（默认只预览）")
    parser.add_argument("--company", default="", help="目标公司 ID，默认当前公司")
    args = parser.parse_args()
    root = Path(config.EXPORT_DIR)
    company = args.company.strip() or workspace.active_company_id()
    target = root / company / "_legacy" / datetime.now().strftime("%Y-%m-%d")
    files = [path for path in root.iterdir() if path.is_file() and path.suffix.lower() != ".json"] if root.exists() else []
    if not files:
        print(f"没有待迁移的旧导出文件：{root}")
        return 0
    print(f"找到 {len(files)} 个旧文件 → {target}")
    manifest = []
    for path in files:
        item = {"file": path.name, "size": path.stat().st_size, "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat()}
        manifest.append(item)
        print(("移动 " if args.apply else "预览 ") + path.name)
        if args.apply:
            target.mkdir(parents=True, exist_ok=True)
            destination = target / path.name
            if destination.exists():
                destination = target / f"{path.stem}_{datetime.now():%H%M%S}{path.suffix}"
            shutil.move(str(path), str(destination))
            item["moved_to"] = str(destination)
    manifest_path = target / "legacy_index.json"
    if args.apply:
        target.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"迁移完成；索引：{manifest_path}")
    else:
        print("预览完成；加 --apply 执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
