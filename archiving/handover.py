"""公司账套交接/迁移包（v1.28）。

与"数据备份"的区别：
* 备份：给本系统恢复用，包含内部表结构，适合自己换电脑；
* 交接包：给公司/新代账迁移用，含原始 CSV + 人类可读 Excel + 归档文件 + 清单哈希 + 说明，
  不包含 API Key/全局设置，便于移交、审计与导入其他系统。

内容：
1. README.md：公司与包内容说明、迁移建议；
2. company.json：公司基本信息；
3. data/：当前公司全部 CSV（原始数据，排除密钥/设置）；
4. 账套汇总.xlsx：每张表一个 sheet，另加交接摘要；
5. archives/：申报归档文件（可选）；
6. uploads/：原始发票/截图等附件（可选，可能较大）；
7. manifest.json：文件清单、SHA-256、行数、版本、生成时间。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

import config
import workspace
from ledger import models


def _safe_suffix(name: str) -> str:
    return "".join(char for char in str(name) if char.isalnum() or char in "-_")[:40] or "company"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _csv_row_count(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return max(sum(1 for _ in handle) - 1, 0)
    except OSError:
        return 0


def _company_info() -> dict[str, Any]:
    try:
        company = workspace.active_company()
    except Exception:  # noqa: BLE001
        company = config.get_company()
    return {
        "id": company.get("id"),
        "name": company.get("name"),
        "short_name": company.get("short_name"),
        "unified_social_credit_code": company.get("unified_social_credit_code"),
        "tax_authority": company.get("tax_authority"),
        "province": company.get("province"),
        "city": company.get("city"),
        "region_id": company.get("region_id"),
        "accounting_standard": company.get("accounting_standard"),
    }


def _excluded_csv(name: str) -> bool:
    lowered = name.lower()
    return any(word in lowered for word in ("user_settings", "secret", "api_key", "apikey"))


def _table_label(file_name: str) -> str:
    for defn in models.TABLE_DEFS.values():
        if str(defn.get("file")) == file_name:
            return str(defn.get("label") or file_name)
    return file_name


def _write_excel(data_dir: Path, csv_files: list[Path], target: Path, summary: dict[str, Any]) -> None:
    with pd.ExcelWriter(target, engine="openpyxl") as writer:
        pd.DataFrame([
            {"项目": "公司名称", "值": summary["company"].get("name")},
            {"项目": "统一社会信用代码", "值": summary["company"].get("unified_social_credit_code")},
            {"项目": "应用版本", "值": config.APP_VERSION},
            {"项目": "生成时间", "值": summary["generated_at"]},
            {"项目": "数据表数量", "值": len(csv_files)},
            {"项目": "记录总数", "值": sum(summary["tables"].values())},
            {"项目": "说明", "值": "本表为交接用可读导出；原始 CSV 在同包 data/ 下。"},
        ]).to_excel(writer, sheet_name="交接摘要", index=False)
        for csv_path in csv_files:
            try:
                frame = pd.read_csv(csv_path, dtype=str).fillna("")
            except Exception:  # noqa: BLE001
                continue
            if frame.empty:
                continue
            label = _table_label(csv_path.name)
            sheet = "".join(char for char in label if char not in "[]:*?/\\")[:31] or csv_path.stem[:31]
            if sheet in writer.sheets:
                sheet = f"{sheet[:28]}_{len(writer.sheets)}"
            frame.to_excel(writer, sheet_name=sheet, index=False)


def build_handover_package(
    *,
    include_archives: bool = True,
    include_uploads: bool = False,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """生成当前公司的交接/迁移 zip，返回路径、大小、SHA-256、清单。"""
    company = _company_info()
    data_dir = Path(config.DATA_DIR)
    if not data_dir.is_dir():
        raise ValueError("当前公司数据目录不存在，无法生成交接包。")
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    csv_files = sorted(
        item for item in data_dir.glob("*.csv")
        if item.is_file() and not _excluded_csv(item.name)
    )
    table_counts = {item.name: _csv_row_count(item) for item in csv_files}
    summary = {
        "company": company,
        "generated_at": generated_at,
        "version": config.APP_VERSION,
        "tables": table_counts,
        "include_archives": include_archives,
        "include_uploads": include_uploads,
    }

    target = Path(path) if path else (
        Path(config.EXPORT_DIR) / f"公司账套交接_{_safe_suffix(str(company.get('name') or company.get('id') or 'company'))}_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"
    )
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="tax_handover_") as temp_root:
        root = Path(temp_root)
        (root / "data").mkdir(parents=True, exist_ok=True)
        (root / "company.json").write_text(json.dumps(company, ensure_ascii=False, indent=2), encoding="utf-8")

        for csv_path in csv_files:
            shutil.copy2(csv_path, root / "data" / csv_path.name)
        _write_excel(data_dir, csv_files, root / "账套汇总.xlsx", summary)

        if include_archives:
            archive_src = data_dir / "archive"
            if archive_src.is_dir():
                shutil.copytree(archive_src, root / "archives", dirs_exist_ok=True)
        if include_uploads:
            upload_src = data_dir / "uploads"
            if upload_src.is_dir():
                shutil.copytree(upload_src, root / "uploads", dirs_exist_ok=True)

        readme = f"""# 公司账套交接包

- 公司：{company.get('name') or '—'}
- 统一社会信用代码：{company.get('unified_social_credit_code') or '—'}
- 主管税务机关：{company.get('tax_authority') or '—'}
- 生成时间：{generated_at}
- 应用：{config.APP_NAME} v{config.APP_VERSION}
- 数据表：{len(csv_files)} 张，记录总数：{sum(table_counts.values())}

## 包内容

- `data/`：原始 CSV 台账（可直接导入 Excel/其他系统）；
- `账套汇总.xlsx`：每张表的可读 Excel 汇总；
- `archives/`：申报归档文件（若勾选）；
- `uploads/`：原始附件（若勾选，可能含敏感信息）；
- `company.json`：公司基本信息；
- `manifest.json`：文件清单与 SHA-256。

## 迁移建议

1. 先解压并核对 `账套汇总.xlsx` 与 `manifest.json` 中的记录数；
2. 把 `data/` 交给新代账/新系统，或保留作为公司档案；
3. 交接双方在清单上签字/留存哈希，确认数据完整；
4. 本包不含 API Key、全局设置与审计日志，避免密钥泄露。

> 本包由 {config.APP_NAME} 生成，仅为数据交接用途，不构成税务鉴证。
"""
        (root / "README.md").write_text(readme, encoding="utf-8")

        files = []
        for item in sorted(root.rglob("*")):
            if item.is_file() and item.name != "manifest.json":
                files.append({
                    "path": str(item.relative_to(root)).replace("\\", "/"),
                    "size": item.stat().st_size,
                    "sha256": _sha256(item),
                })
        manifest = {
            "company": company,
            "generated_at": generated_at,
            "app": config.APP_NAME,
            "version": config.APP_VERSION,
            "tables": table_counts,
            "include_archives": include_archives,
            "include_uploads": include_uploads,
            "files": files,
            "notice": "交接包不含 API Key/全局设置/审计日志；请妥善保管。",
        }
        (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in sorted(root.rglob("*")):
                if item.is_file():
                    archive.write(item, arcname=str(item.relative_to(root)).replace("\\", "/"))

    return {
        "path": Path(target),
        "size": Path(target).stat().st_size,
        "sha256": _sha256(Path(target)),
        "company": company,
        "tables": table_counts,
        "files": len(files),
        "include_archives": include_archives,
        "include_uploads": include_uploads,
    }


__all__ = ["build_handover_package"]
