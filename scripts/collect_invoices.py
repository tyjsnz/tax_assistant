#!/usr/bin/env python3
"""命令行发票归集（v1.26）：供 cron / launchd / Windows 任务计划调用。

用法：
  .venv/bin/python scripts/collect_invoices.py --auto
  .venv/bin/python scripts/collect_invoices.py --folder /path/to/invoices
  .venv/bin/python scripts/collect_invoices.py --email
  .venv/bin/python scripts/collect_invoices.py --auto --json

--auto 使用「📬 发票归集」页保存的设置；输出 JSON 摘要，不输出邮箱授权码。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingest import collector  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="发票归集：文件夹扫描 / 邮箱 IMAP")
    parser.add_argument("--auto", action="store_true", help="使用已保存的设置自动扫描")
    parser.add_argument("--folder", type=str, default="", help="扫描指定文件夹")
    parser.add_argument("--email", action="store_true", help="拉取邮箱附件")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    settings = collector.load_settings()
    outputs: dict[str, object] = {}
    try:
        if args.auto:
            if settings.get("folder_path"):
                outputs["folder"] = collector.collect_folder(settings["folder_path"])
            if settings.get("email_host"):
                outputs["email"] = collector.collect_email(settings)
            settings["last_scan_at"] = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            collector.save_settings(settings)
        else:
            if args.folder:
                outputs["folder"] = collector.collect_folder(args.folder)
            if args.email:
                outputs["email"] = collector.collect_email(settings)
    except Exception as exc:  # noqa: BLE001
        outputs["error"] = str(exc)
        if args.json:
            print(json.dumps(outputs, ensure_ascii=False, indent=2))
        else:
            print(f"发票归集失败：{exc}")
        return 1
    if args.json:
        print(json.dumps(outputs, ensure_ascii=False, indent=2))
    else:
        for key, result in outputs.items():
            if isinstance(result, dict):
                print(f"{key}: {result.get('imported', 0)} 张导入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
