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
import io
import json
import os
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

ENCRYPT_MAGIC = b"TAXHANDOVER1\n"


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


def build_handover_receipt(result: dict[str, Any], path: str | Path | None = None) -> Path:
    """生成交接签收单 PDF（双方签收、哈希确认）。"""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font = "STSong-Light"
    try:
        pdfmetrics.registerFont(UnicodeCIDFont(font))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"中文字体加载失败：{exc}") from exc
    target = Path(path) if path else (
        Path(config.EXPORT_DIR) / f"交接签收单_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    sample = getSampleStyleSheet()
    title = ParagraphStyle("t", parent=sample["Title"], fontName=font, fontSize=18, leading=24, alignment=1)
    body = ParagraphStyle("b", parent=sample["Normal"], fontName=font, fontSize=10.5, leading=17)
    small = ParagraphStyle("s", parent=sample["Normal"], fontName=font, fontSize=9, leading=13, textColor=colors.HexColor("#555555"))
    company = result.get("company") or {}
    tables = result.get("tables") or {}
    story = [
        Paragraph("公司账套交接签收单", title),
        Spacer(1, 8),
        Paragraph(f"公司名称：{company.get('name') or '—'}", body),
        Paragraph(f"统一社会信用代码：{company.get('unified_social_credit_code') or '—'}", body),
        Paragraph(f"交接包文件：{Path(result.get('path')).name if result.get('path') else '—'}", body),
        Paragraph(f"SHA-256：{result.get('sha256') or '—'}", body),
        Paragraph(f"文件大小：{result.get('size', 0)} 字节 · 数据表：{len(tables)} 张 · 记录总数：{sum(tables.values())}", body),
        Paragraph(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}", body),
        Spacer(1, 10),
        Paragraph("数据表记录数", body),
    ]
    rows = [["数据表", "记录数"]] + [[name, str(count)] for name, count in tables.items()]
    table = Table(rows, colWidths=[100 * mm, 50 * mm])
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6fffb")),
    ]))
    story.extend([
        table,
        Spacer(1, 14),
        Paragraph("交接确认", body),
        Paragraph("移交方（签字/盖章）：____________________    日期：________________", body),
        Spacer(1, 12),
        Paragraph("接收方（签字/盖章）：____________________    日期：________________", body),
        Spacer(1, 12),
        Paragraph("说明：接收方已核对 manifest.json 中的数据表记录数与 SHA-256；本签收单仅确认数据交接，不构成税务鉴证。", small),
        Paragraph("官方发行版 · tax.flacn.net · 开发方 snz · 274043505@qq.com", small),
    ])
    SimpleDocTemplate(str(target), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                      topMargin=18 * mm, bottomMargin=18 * mm).build(story)
    return target


def _derive_key(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    import base64

    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000)
    return base64.urlsafe_b64encode(kdf.derive(str(password).encode("utf-8")))


def encrypt_package(zip_path: str | Path, password: str, target: str | Path | None = None) -> Path:
    """用密码加密交接包（PBKDF2 + Fernet），输出 .zip.enc。"""
    from cryptography.fernet import Fernet

    source = Path(zip_path)
    if not source.exists():
        raise FileNotFoundError(f"交接包不存在：{source}")
    if not str(password or ""):
        raise ValueError("加密密码不能为空")
    target_path = Path(target) if target else source.with_suffix(".zip.enc")
    salt = os.urandom(16)
    token = Fernet(_derive_key(password, salt)).encrypt(source.read_bytes())
    target_path.write_bytes(ENCRYPT_MAGIC + salt + token)
    return target_path


def decrypt_package(enc_path: str | Path, password: str, target: str | Path | None = None) -> Path:
    """解密 .zip.enc，返回临时 zip 路径。"""
    from cryptography.fernet import Fernet, InvalidToken

    source = Path(enc_path)
    raw = source.read_bytes()
    if not raw.startswith(ENCRYPT_MAGIC):
        raise ValueError("不是有效的加密交接包")
    salt = raw[len(ENCRYPT_MAGIC): len(ENCRYPT_MAGIC) + 16]
    token = raw[len(ENCRYPT_MAGIC) + 16:]
    try:
        data = Fernet(_derive_key(password, salt)).decrypt(token)
    except InvalidToken as exc:
        raise ValueError("密码不正确或交接包已损坏") from exc
    target_path = Path(target) if target else Path(tempfile.gettempdir()) / f"handover_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    target_path.write_bytes(data)
    return target_path


def _zip_from_source(data: bytes, filename: str = "", password: str = "") -> tuple[zipfile.ZipFile, Path | None]:
    """把上传的 zip/加密包转为 ZipFile；加密包先解密到临时文件。"""
    suffix = Path(str(filename or "")).suffix.lower()
    raw = bytes(data)
    temp_path = None
    if suffix == ".enc" or raw.startswith(ENCRYPT_MAGIC):
        if not password:
            raise ValueError("这是加密交接包，请填写密码。")
        temp_path = decrypt_package_bytes(raw, password)
        raw = temp_path.read_bytes()
    try:
        return zipfile.ZipFile(io.BytesIO(raw)), temp_path
    except zipfile.BadZipFile as exc:
        raise ValueError("文件不是有效的 zip 交接包") from exc


def decrypt_package_bytes(raw: bytes, password: str) -> Path:
    from cryptography.fernet import Fernet, InvalidToken

    if not raw.startswith(ENCRYPT_MAGIC):
        raise ValueError("不是有效的加密交接包")
    salt = raw[len(ENCRYPT_MAGIC): len(ENCRYPT_MAGIC) + 16]
    token = raw[len(ENCRYPT_MAGIC) + 16:]
    try:
        data = Fernet(_derive_key(password, salt)).decrypt(token)
    except InvalidToken as exc:
        raise ValueError("密码不正确或交接包已损坏") from exc
    target = Path(tempfile.gettempdir()) / f"handover_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    target.write_bytes(data)
    return target


def inspect_handover_package(data: bytes, filename: str = "", password: str = "") -> dict[str, Any]:
    """检查交接包：公司信息、表行数、哈希校验结果，不写入账套。"""
    archive, temp = _zip_from_source(data, filename, password)
    try:
        names = set(archive.namelist())
        if "manifest.json" not in names or "company.json" not in names:
            raise ValueError("交接包缺少 manifest.json/company.json，可能不是有效交接包。")
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        company = json.loads(archive.read("company.json").decode("utf-8"))
        errors = []
        for item in manifest.get("files") or []:
            name = str(item.get("path") or "")
            try:
                payload = archive.read(name)
            except KeyError:
                errors.append(f"缺少文件：{name}")
                continue
            actual = hashlib.sha256(payload).hexdigest()
            if actual != str(item.get("sha256") or ""):
                errors.append(f"哈希不匹配：{name}")
        return {"ok": not errors, "manifest": manifest, "company": company, "errors": errors,
                "tables": manifest.get("tables") or {}, "generated_at": manifest.get("generated_at")}
    finally:
        archive.close()
        if temp and Path(temp).exists():
            try:
                Path(temp).unlink()
            except OSError:
                pass


def import_handover_package(
    data: bytes,
    filename: str = "",
    password: str = "",
    *,
    activate: bool = True,
) -> dict[str, Any]:
    """把交接包导入为一家新公司（不覆盖现有公司），返回新公司信息。"""
    info = inspect_handover_package(data, filename, password)
    if not info.get("ok"):
        raise ValueError("交接包校验失败：" + "；".join(info.get("errors") or []))
    archive, temp = _zip_from_source(data, filename, password)
    try:
        company = info["company"]
        name = str(company.get("name") or "交接导入公司").strip()
        existing_names = {str(item.get("name")) for item in workspace.list_companies()}
        final_name = name if name not in existing_names else f"{name}（交接导入 {datetime.now().strftime('%m%d%H%M')}）"
        payload = {key: company.get(key) for key in ("name", "short_name", "unified_social_credit_code",
                                                     "tax_authority", "province", "city", "county",
                                                     "region_id", "industry", "accounting_standard", "note")}
        payload["name"] = final_name
        record = workspace.add_company(payload, activate_new=False)
        company_id = str(record.get("id"))
        workspace.activate(company_id, persist=True)
        data_dir = Path(config.DATA_DIR)
        data_dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        for name in archive.namelist():
            pure = Path(name)
            if pure.is_absolute() or ".." in pure.parts:
                continue
            if name.startswith("data/") and name.endswith(".csv"):
                target = data_dir / pure.name
                target.write_bytes(archive.read(name))
                copied += 1
            elif name.startswith("archives/"):
                target = data_dir / "archive" / Path(*pure.parts[1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
            elif name.startswith("uploads/"):
                target = data_dir / "uploads" / Path(*pure.parts[1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
        if not activate:
            # 已激活；调用方如需留在原公司可自行切回，这里保持新公司便于查看
            pass
        return {"company": record, "company_id": company_id, "copied_csv": copied,
                "tables": info.get("tables") or {}, "name": final_name}
    finally:
        archive.close()
        if temp and Path(temp).exists():
            try:
                Path(temp).unlink()
            except OSError:
                pass


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

    result = {
        "path": Path(target),
        "size": Path(target).stat().st_size,
        "sha256": _sha256(Path(target)),
        "company": company,
        "tables": table_counts,
        "files": len(files),
        "include_archives": include_archives,
        "include_uploads": include_uploads,
    }
    try:
        result["receipt"] = build_handover_receipt(result)
    except Exception:  # noqa: BLE001 - 签收单失败不影响交接包
        result["receipt"] = None
    return result


__all__ = ["build_handover_package"]
