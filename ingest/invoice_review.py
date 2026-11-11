"""发票查验辅助与复核状态（v1.27）。

* 二维码解码：PNG/JPG 图片、PDF 内嵌图片（需要 opencv；缺失时降级提示）；
* 官方查验入口：只信任 chinatax.gov.cn 域名，其他域名明确警告；
* 复核状态：待复核 / 正常 / 红冲 / 作废 / 需人工确认；
* 查验状态：未查验 / 已查验-正常 / 查验异常；
* 不做自动登录、不自动判定真伪。
"""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ledger import store
from ledger.audit import log_user_action

INVOICE_TABLE = "invoices"
REVIEW_STATUSES = ("待复核", "正常", "红冲", "作废", "需人工确认")
VERIFY_STATUSES = ("未查验", "已查验-正常", "查验异常")
OFFICIAL_HOSTS = ("chinatax.gov.cn", "inv-veri.chinatax.gov.cn")
REVIEW_DISCLAIMER = "⚠️ 扫码/查验结果仅作辅助，不替代官方查验结论；不要向非官方页面输入账号密码。"


def _is_official(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
    except Exception:  # noqa: BLE001
        return False
    return any(host == item or host.endswith("." + item) for item in OFFICIAL_HOSTS)


def decode_qr_image(data: bytes, *, filename: str = "image.png") -> dict[str, Any]:
    """从图片 bytes 解码二维码（使用 OpenCV；缺失时返回 ok=False）。"""
    try:
        import cv2
        import numpy as np
        from PIL import Image
    except ImportError:
        return {"ok": False, "errors": ["未安装 opencv/Pillow，无法解码二维码；可手动到官方平台查验。"]}
    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
        array = np.array(image)
        # 放大有助于识别小二维码
        if max(array.shape[:2]) < 400:
            array = cv2.resize(array, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
        detector = cv2.QRCodeDetector()
        payload, points, _ = detector.detectAndDecode(array)
        if not payload:
            return {"ok": False, "errors": ["未识别到二维码；可能是矢量二维码或图片不清晰，请手动查验。"]}
        return {"ok": True, "payload": payload, "url": payload if payload.startswith(("http://", "https://")) else ""}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "errors": [f"二维码解码失败：{exc}"]}


def decode_qr_file(data: bytes, filename: str) -> dict[str, Any]:
    """根据文件类型解码二维码（图片或 PDF 内嵌图片）。"""
    suffix = Path(str(filename or "")).suffix.lower()
    if suffix in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        result = decode_qr_image(data, filename=filename)
        result["kind"] = "image"
        return result
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            errors = []
            for page in reader.pages:
                try:
                    for image_file in page.images:
                        result = decode_qr_image(image_file.data, filename="pdf-image")
                        if result.get("ok"):
                            result["kind"] = "pdf"
                            return result
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))
            return {"ok": False, "kind": "pdf", "errors": ["PDF 中未识别到二维码（可能是矢量二维码），请手动查验。"]}
        except ImportError:
            return {"ok": False, "kind": "pdf", "errors": ["未安装 pypdf，无法解析 PDF。"]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "kind": "pdf", "errors": [f"PDF 解析失败：{exc}"]}
    return {"ok": False, "errors": ["仅支持上传图片（PNG/JPG）或 PDF 进行二维码识别。"]}


def inspect_payload(payload: str) -> dict[str, Any]:
    payload = str(payload or "").strip()
    url = payload if payload.startswith(("http://", "https://")) else ""
    if url:
        official = _is_official(url)
        warning = "" if official else "⚠️ 二维码打开的是非官方域名，请勿输入账号/密码/验证码；请到官方平台手工查验。"
        return {"payload": payload, "url": url, "is_official": official, "warning": warning}
    return {
        "payload": payload,
        "url": "",
        "is_official": False,
        "warning": "二维码内容不是网址（可能是发票字段/第三方标识）；请到官方平台手工查验，勿轻信扫码结果。",
    }


def list_invoices(limit: int = 500) -> list[dict[str, Any]]:
    rows = store.table(INVOICE_TABLE).read()
    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return rows[: int(limit)]


def update_review(
    invoice_id: str,
    *,
    review_status: str | None = None,
    verify_status: str | None = None,
    review_note: str | None = None,
) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    if review_status is not None:
        if review_status not in REVIEW_STATUSES:
            raise ValueError(f"复核状态不合法，可选：{'、'.join(REVIEW_STATUSES)}")
        patch["review_status"] = review_status
    if verify_status is not None:
        if verify_status not in VERIFY_STATUSES:
            raise ValueError(f"查验状态不合法，可选：{'、'.join(VERIFY_STATUSES)}")
        patch["verify_status"] = verify_status
        if verify_status != "未查验":
            patch["verified_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if review_note is not None:
        patch["review_note"] = str(review_note)[:500]
    if not patch:
        return store.table(INVOICE_TABLE).get(str(invoice_id)) or {}
    record = store.table(INVOICE_TABLE).update(str(invoice_id), patch)
    log_user_action("发票查验/复核", f"{invoice_id}：{patch}")
    return record


def summary() -> dict[str, Any]:
    rows = store.table(INVOICE_TABLE).read()
    review_counts: dict[str, int] = {}
    verify_counts: dict[str, int] = {}
    for row in rows:
        review = str(row.get("review_status") or "待复核")
        verify = str(row.get("verify_status") or "未查验")
        review_counts[review] = review_counts.get(review, 0) + 1
        verify_counts[verify] = verify_counts.get(verify, 0) + 1
    return {"total": len(rows), "review": review_counts, "verify": verify_counts}


__all__ = [
    "REVIEW_STATUSES", "VERIFY_STATUSES", "OFFICIAL_HOSTS", "REVIEW_DISCLAIMER",
    "decode_qr_image", "decode_qr_file", "inspect_payload",
    "list_invoices", "update_review", "summary",
]
