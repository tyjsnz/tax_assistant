"""二维码生成（v1.32）：用于手机扫码导入申报日历。"""

from __future__ import annotations

import io


def qr_png(data: str, *, size: int = 320) -> bytes:
    """把文本生成 PNG 二维码；opencv 缺失时抛 RuntimeError。"""
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("未安装 opencv，无法生成二维码。") from exc
    text = str(data or "").strip()
    if not text:
        raise ValueError("二维码内容不能为空")
    encoder = cv2.QRCodeEncoder.create()
    qr = encoder.encode(text)
    # 加白边（quiet zone），手机更容易识别
    qr = cv2.copyMakeBorder(qr, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)
    scale = max(1, size // max(qr.shape[:2]))
    image = cv2.resize(qr, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError("二维码编码失败")
    return encoded.tobytes()


__all__ = ["qr_png"]
