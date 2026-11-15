"""启动时检查官网最新版本并提示下载（v1.36）。

原则：
* 只请求官网静态 version.json，不发送账目/公司信息；
* 3 秒超时，失败静默，不影响启动；
* 不自动下载/替换程序，只提示并给出官方下载链接；
* 可在设置/环境变量关闭：TAX_DISABLE_UPDATE_CHECK=1。
"""

from __future__ import annotations

import os
import platform
import sys
from typing import Any

import requests
import streamlit as st

import config

VERSION_URL = "https://tax.flacn.net/version.json"
SITE = "https://tax.flacn.net"
STATE_KEY = "_update_notice_dismissed"
FETCHED_KEY = "_update_notice_fetched"


def parse_version(text: str) -> tuple[int, ...]:
    parts = []
    for piece in str(text or "").strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(latest: str, current: str | None = None) -> bool:
    return parse_version(latest) > parse_version(current or config.APP_VERSION)


def _download_url(info: dict[str, Any]) -> str:
    downloads = info.get("downloads") or {}
    system = platform.system().lower()
    if system == "darwin":
        return str(downloads.get("macos") or SITE)
    if system == "windows":
        return str(downloads.get("windows") or SITE)
    return str(downloads.get("source") or downloads.get("home") or SITE)


def fetch_latest(timeout: float = 3.0) -> dict[str, Any] | None:
    if os.environ.get("TAX_DISABLE_UPDATE_CHECK") == "1":
        return None
    try:
        response = requests.get(VERSION_URL, timeout=timeout, headers={"User-Agent": "zhixiaoshui-update-check"})
        if response.status_code != 200:
            return None
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - 网络失败静默
        return None


def render() -> None:
    """在页面顶部提示新版本；每个会话只提示一次。"""
    # 测试环境不发外部请求，避免 AppTest 网络抖动
    if "pytest" in sys.modules:
        return
    if st.session_state.get(STATE_KEY):
        return
    if not st.session_state.get(FETCHED_KEY):
        st.session_state[FETCHED_KEY] = True
        info = fetch_latest()
        if not info:
            return
        st.session_state["_update_notice"] = info
    info = st.session_state.get("_update_notice")
    if not isinstance(info, dict):
        return
    latest = str(info.get("version") or "")
    if not latest or not is_newer(latest):
        return
    notes = str(info.get("notes") or "").strip()
    with st.container(border=True):
        st.warning(
            f"发现新版本 **v{latest}**（当前 v{config.APP_VERSION}）。"
            "请到官网下载新版覆盖安装；账套数据保存在用户目录，不会被覆盖。",
            icon="⬆️",
        )
        if notes:
            st.caption(notes)
        cols = st.columns([2, 2, 5])
        with cols[0]:
            st.link_button(f"下载 v{latest}", _download_url(info), width="stretch")
        with cols[1]:
            st.link_button("查看更新说明", f"{SITE}/#download", width="stretch")
        with cols[2]:
            if st.button("稍后提醒", key="update_notice_dismiss"):
                st.session_state[STATE_KEY] = True
                st.rerun()


__all__ = ["parse_version", "is_newer", "fetch_latest", "render", "VERSION_URL"]
