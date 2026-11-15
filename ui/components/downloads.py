"""导出文件统一下载/保存（v1.52）。

桌面版原生窗口对浏览器下载支持不稳定，因此打包后统一改为：
* 网页版：st.download_button；
* 桌面版：写入导出目录，提供「保存到桌面」和「打开文件夹」。

所有页面生成导出文件时统一走这里，避免某页能下载、某页没反应。
"""

from __future__ import annotations

import hashlib
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

import streamlit as st

import config


def _safe_name(file_name: str, key: str) -> str:
    name = Path(str(file_name or "")).name
    if not name:
        suffix = hashlib.sha1(str(key).encode()).hexdigest()[:8]
        name = f"导出文件_{suffix}.bin"
    return name


def _open_location(path: Path) -> None:
    try:
        system = platform.system().lower()
        if system == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        elif system == "windows":
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
    except Exception as exc:  # noqa: BLE001
        st.warning(f"无法自动打开文件夹，请手动到：{path}")


def download_or_save(
    label: str,
    *,
    data: bytes | None = None,
    file_path: str | Path | None = None,
    file_name: str = "",
    mime: str = "application/octet-stream",
    key: str = "",
    width: str = "stretch",
    help: str | None = None,
    disabled: bool = False,
) -> None:
    """网页下载；桌面版保存到桌面。调用方式与原 st.download_button 基本一致。"""
    name = _safe_name(file_name or (Path(file_path).name if file_path else ""), key or label)
    if not config.FROZEN:
        if data is None and file_path is not None:
            data = Path(file_path).read_bytes()
        st.download_button(
            label,
            data=data or b"",
            file_name=name,
            mime=mime,
            key=key or None,
            width=width,
            help=help,
            disabled=disabled,
        )
        return

    # 桌面版：确保文件在本地导出目录
    if file_path is not None and Path(file_path).exists():
        path = Path(file_path)
    else:
        path = Path(config.EXPORT_DIR) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if data is not None:
                path.write_bytes(data)
        except Exception as exc:  # noqa: BLE001
            st.error(f"保存导出文件失败：{exc}")
            return
    st.caption(f"导出文件：{path}")
    cols = st.columns(2)
    with cols[0]:
        if st.button(
            "💾 保存到桌面",
            key=f"{key or label}_save_desktop",
            width="stretch",
            help=help,
            disabled=disabled,
        ):
            try:
                desktop = Path.home() / "Desktop"
                desktop.mkdir(parents=True, exist_ok=True)
                target = desktop / path.name
                shutil.copy2(path, target)
                st.success(f"已保存到：{target}")
            except Exception as exc:  # noqa: BLE001
                st.error(f"保存失败：{exc}")
    with cols[1]:
        if st.button(
            "📂 打开文件夹",
            key=f"{key or label}_open_folder",
            width="stretch",
            disabled=disabled,
        ):
            _open_location(path)


__all__ = ["download_or_save"]
