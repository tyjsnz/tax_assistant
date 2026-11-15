#!/usr/bin/env python3
"""生成官网静态申报日历 .ics（v1.32），供手机扫码/订阅导入。

用法：.venv/bin/python scripts/build_calendar_ics.py
输出：site/download/tax-calendar-2026.ics、tax-calendar-2027.ics
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.calendar_export import build_ics  # noqa: E402
from engine.tax_calendar import filing_deadlines  # noqa: E402


def main() -> int:
    target_dir = ROOT / "site" / "download"
    target_dir.mkdir(parents=True, exist_ok=True)
    for year in (2026, 2027):
        items = filing_deadlines(year)
        data = build_ics(items, calendar_name=f"智小税 申报日历 {year}")
        path = target_dir / f"tax-calendar-{year}.ics"
        path.write_text(data, encoding="utf-8")
        print(f"{path}：{len(items)} 个事项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
