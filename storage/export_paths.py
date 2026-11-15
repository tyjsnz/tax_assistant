"""统一导出目录规范（v1.54）。

规范：
    EXPORT_DIR/{company_id}/{YYYY}/{MM或Qn或年度}/{类别}/文件名

* 按**税款所属期**归档，而不是操作当天；
* 账套原始数据不分月，仍存放在 companies/{id}/ 下；
* 类别固定，便于「资料中心」按年月/类别检索。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

import config

CATEGORIES: dict[str, str] = {
    "申报表": "01_申报表",
    "工资个税": "02_工资个税",
    "发票": "03_发票",
    "报表台账": "04_报表台账",
    "社保": "05_社保",
    "交接备份": "06_交接备份",
    "其他": "99_其他",
}


def _safe(value: str, fallback: str) -> str:
    text = re.sub(r"[\\/:*?\"<>|\s]+", "_", str(value or "")).strip("._")
    return text or fallback


def active_company_id() -> str:
    try:
        import workspace

        return _safe(workspace.active_company_id(), "main")
    except Exception:  # noqa: BLE001
        return "main"


def period_parts(period: Any = None) -> tuple[str, str, str]:
    """返回 (年, 月/季, 所属期文本)。"""
    text = str(period or "").strip().upper()
    match = re.fullmatch(r"(\d{4})[-/]?(\d{2})", text)
    if match:
        year, month = match.groups()
        return year, month, f"{year}-{month}"
    match = re.fullmatch(r"(\d{4})Q([1-4])", text)
    if match:
        year, quarter = match.groups()
        return year, f"Q{quarter}", f"{year}Q{quarter}"
    match = re.fullmatch(r"(\d{4})", text)
    if match:
        return match.group(1), "年度", f"{match.group(1)}年"
    now = datetime.now()
    return str(now.year), f"{now.month:02d}", f"{now.year}-{now.month:02d}"


def category_dir(category: str) -> str:
    return CATEGORIES.get(str(category or "其他"), _safe(str(category), CATEGORIES["其他"]))


def export_dir(period: Any = None, category: str = "其他", company_id: str | None = None) -> Path:
    year, slot, _ = period_parts(period)
    company = _safe(company_id or active_company_id(), "main")
    path = Path(config.EXPORT_DIR) / company / year / slot / category_dir(category)
    path.mkdir(parents=True, exist_ok=True)
    return path


def export_file(
    prefix: str,
    extension: str,
    *,
    period: Any = None,
    category: str = "其他",
    company_id: str | None = None,
    stamp: bool = True,
) -> Path:
    stamp_text = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = _safe(prefix, "导出文件")
    if stamp:
        name = f"{name}_{stamp_text}"
    return export_dir(period, category, company_id) / f"{name}.{str(extension or 'bin').lstrip('.')}"


def relative_to_exports(path: str | Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(config.EXPORT_DIR).resolve()))
    except Exception:  # noqa: BLE001
        return str(path)


__all__ = ["CATEGORIES", "active_company_id", "period_parts", "category_dir", "export_dir", "export_file", "relative_to_exports"]
