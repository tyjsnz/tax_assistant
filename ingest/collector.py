"""发票自动归集（v1.25）：本地文件夹 + 专用邮箱 IMAP。

* 本地文件夹：扫描企业从电子税务局导出的 PDF/OFD/XML 发票；
* 邮箱：IMAP 只读拉取附件（PDF/OFD/XML），不删除邮件、不标记已读；
* 自动按文件哈希去重，解析后写入发票池，并可自动生成匹配建议；
* 邮箱授权码只保存在本机数据目录（600 权限），不上传。
"""

from __future__ import annotations

import email
import hashlib
import imaplib
import json
import os
import re
import socket
from email.header import decode_header
from pathlib import Path
from typing import Any

import config
from ledger import store
from ledger.audit import log_user_action

SETTINGS_PATH = Path(config.DATA_ROOT) / "invoice_collector.json"
LOG_TABLE = "collection_log"
ALLOWED_EXTENSIONS = {".pdf", ".ofd", ".xml"}
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
INBOX_DIR = lambda: Path(config.UPLOAD_DIR) / "invoice_inbox"


def default_settings() -> dict[str, Any]:
    return {
        "folder_path": "",
        "email_host": "",
        "email_port": 993,
        "email_user": "",
        "email_password": "",
        "email_mailbox": "INBOX",
        "email_days": 30,
        "auto_match": True,
    }


def load_settings() -> dict[str, Any]:
    data = default_settings()
    try:
        if SETTINGS_PATH.exists():
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update({key: raw[key] for key in data if key in raw})
    except Exception:  # noqa: BLE001
        pass
    return data


def save_settings(settings: dict[str, Any]) -> dict[str, Any]:
    data = default_settings()
    for key in data:
        if key in settings:
            data[key] = settings[key]
    try:
        data["email_port"] = int(data.get("email_port") or 993)
        data["email_days"] = max(int(data.get("email_days") or 30), 1)
    except (TypeError, ValueError):
        data["email_port"] = 993
        data["email_days"] = 30
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(SETTINGS_PATH, 0o600)
    except OSError:
        pass
    return data


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _already_collected(file_hash: str) -> bool:
    return any(str(row.get("file_hash") or "") == file_hash for row in store.table(LOG_TABLE).read())


def _log(kind: str, source: str, file_name: str, file_hash: str, imported: int, skipped: int, status: str, message: str = "") -> dict[str, Any]:
    return store.table(LOG_TABLE).append(
        {
            "kind": kind, "source": source, "file_name": file_name, "file_hash": file_hash,
            "imported": int(imported), "skipped": int(skipped), "status": status, "message": message,
        }
    )


def collect_folder(folder: str | None = None, *, auto_match: bool = True) -> dict[str, Any]:
    """扫描本地文件夹中的 PDF/OFD/XML 发票并导入。"""
    from ingest.importer import import_invoice_files

    root = Path(str(folder or load_settings().get("folder_path") or "")).expanduser()
    if not str(root) or not root.exists() or not root.is_dir():
        raise ValueError("请先在设置中填写有效的发票文件夹路径。")
    result = {"scanned": 0, "imported": 0, "skipped": 0, "errors": [], "files": []}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        result["scanned"] += 1
        try:
            data = path.read_bytes()
        except OSError as exc:
            result["errors"].append(f"{path.name}：{exc}")
            continue
        digest = _hash_bytes(data)
        if _already_collected(digest):
            result["skipped"] += 1
            continue
        try:
            imported = import_invoice_files([path], auto_match=auto_match)
            saved = int(imported.get("saved") or 0)
            result["imported"] += saved
            if saved == 0:
                result["skipped"] += 1
            _log("folder", str(root), path.name, digest, saved, int(imported.get("skipped") or 0),
                 "成功" if saved else "跳过", "；".join(str(item.get("error")) for item in imported.get("errors") or [])[:300])
            result["files"].append({"name": path.name, "saved": saved})
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"{path.name}：{exc}")
            _log("folder", str(root), path.name, digest, 0, 0, "失败", str(exc)[:300])
    log_user_action("发票归集（本地文件夹）", f"扫描 {result['scanned']}，导入 {result['imported']}")
    return result


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    parts = []
    for text, charset in decode_header(value):
        if isinstance(text, bytes):
            parts.append(text.decode(charset or "utf-8", "replace"))
        else:
            parts.append(str(text))
    return "".join(parts)


def _safe_filename(name: str) -> str:
    base = Path(str(name or "")).name
    base = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", base)
    return base[:120] or "attachment.pdf"


def _connect(settings: dict[str, Any]) -> imaplib.IMAP4_SSL:
    host = str(settings.get("email_host") or "").strip()
    user = str(settings.get("email_user") or "").strip()
    password = str(settings.get("email_password") or "")
    if not host or not user or not password:
        raise ValueError("请先填写邮箱服务器、账号和授权码。")
    try:
        connection = imaplib.IMAP4_SSL(host, int(settings.get("email_port") or 993), timeout=20)
    except (socket.error, OSError) as exc:
        raise ValueError(f"无法连接邮箱服务器：{exc}") from exc
    try:
        connection.login(user, password)
    except imaplib.IMAP4.error as exc:
        raise ValueError(f"邮箱登录失败：{exc}（请使用 IMAP 授权码，不是网页登录密码）") from exc
    return connection


def test_email(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    data = settings or load_settings()
    connection = _connect(data)
    try:
        status, folders = connection.list()
        return {"ok": True, "folders": len(folders or []), "message": "邮箱连接成功"}
    finally:
        try:
            connection.logout()
        except Exception:  # noqa: BLE001
            pass


def collect_email(settings: dict[str, Any] | None = None, *, limit: int = 50) -> dict[str, Any]:
    """拉取邮箱最近 N 天附件（只读），导入发票池。"""
    from ingest.importer import import_invoice_files

    data = settings or load_settings()
    days = max(int(data.get("email_days") or 30), 1)
    import datetime as _dt

    since = (_dt.date.today() - _dt.timedelta(days=days)).strftime("%d-%b-%Y")
    connection = _connect(data)
    result = {"messages": 0, "attachments": 0, "imported": 0, "skipped": 0, "errors": []}
    inbox = INBOX_DIR()
    inbox.mkdir(parents=True, exist_ok=True)
    try:
        connection.select(str(data.get("email_mailbox") or "INBOX"), readonly=True)
        status, ids = connection.search(None, f'(SINCE {since})')
        if status != "OK":
            raise ValueError("邮箱搜索失败，请检查邮箱目录是否正确。")
        message_ids = list((ids[0] or b"").split())[-limit:]
        result["messages"] = len(message_ids)
        for message_id in message_ids:
            status, payload = connection.fetch(message_id, "(BODY.PEEK[])")
            if status != "OK" or not payload:
                continue
            raw = b""
            for part in payload:
                if isinstance(part, tuple) and len(part) >= 2:
                    raw = part[1]
                    break
            if not raw:
                continue
            message = email.message_from_bytes(raw)
            sender = _decode_header(message.get("From"))
            for part in message.walk():
                filename = _decode_header(part.get_filename())
                if not filename or Path(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
                    continue
                try:
                    content = part.get_payload(decode=True) or b""
                except Exception:  # noqa: BLE001
                    continue
                if not content or len(content) > MAX_ATTACHMENT_BYTES:
                    continue
                result["attachments"] += 1
                digest = _hash_bytes(content)
                if _already_collected(digest):
                    result["skipped"] += 1
                    continue
                safe = f"{digest[:10]}_{_safe_filename(filename)}"
                target = inbox / safe
                target.write_bytes(content)
                try:
                    imported = import_invoice_files([target], auto_match=bool(data.get("auto_match")) if "auto_match" in data else True)
                    saved = int(imported.get("saved") or 0)
                    result["imported"] += saved
                    _log("email", sender, filename, digest, saved, int(imported.get("skipped") or 0),
                         "成功" if saved else "跳过", "；".join(str(item.get("error")) for item in imported.get("errors") or [])[:300])
                except Exception as exc:  # noqa: BLE001
                    result["errors"].append(f"{filename}：{exc}")
                    _log("email", sender, filename, digest, 0, 0, "失败", str(exc)[:300])
    finally:
        try:
            connection.logout()
        except Exception:  # noqa: BLE001
            pass
    log_user_action("发票归集（邮箱）", f"邮件 {result['messages']}，附件 {result['attachments']}，导入 {result['imported']}")
    return result


def recent_logs(limit: int = 50) -> list[dict[str, Any]]:
    rows = sorted(store.table(LOG_TABLE).read(), key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return rows[: int(limit)]


__all__ = [
    "default_settings", "load_settings", "save_settings",
    "collect_folder", "collect_email", "test_email", "recent_logs",
    "ALLOWED_EXTENSIONS", "INBOX_DIR", "SETTINGS_PATH",
]
