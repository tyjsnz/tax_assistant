#!/usr/bin/env python3
"""桌面版启动器（v1.35）：双击运行，启动本地 Streamlit + 原生窗口。

* 不要求用户安装 Python / 使用命令行；
* 数据默认保存在用户目录（macOS Application Support / Windows APPDATA）；
* 原生窗口使用 pywebview；若系统 WebView 不可用，自动退回默认浏览器。
* 环境变量 TAX_DESKTOP_TEST=1 时只启动服务并打印 URL，便于打包验证。
"""

from __future__ import annotations

import multiprocessing
import os
import socket
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_health(port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/_stcore/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    return False


def _log(message: str) -> None:
    try:
        from config import DATA_ROOT

        log_dir = Path(DATA_ROOT) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "desktop.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except Exception:  # noqa: BLE001
        pass


def _server_process(port: int) -> None:
    """子进程中运行 Streamlit（bootstrap 需要在主线程）。"""
    from streamlit.web import bootstrap

    app_script = ROOT / "ui" / "streamlit_app.py"
    flags = {
        "server.headless": True,
        "server.port": port,
        "server.address": "127.0.0.1",
        "server.fileWatcherType": "none",
        "browser.gatherUsageStats": False,
        "global.developmentMode": False,
    }
    # Streamlit 的 bootstrap.run 不会自动应用 flag_options 到初始配置，先显式 load
    bootstrap.load_config_options(flags)
    bootstrap.run(
        str(app_script),
        False,
        [],
        flags,
    )


def main() -> int:
    app_script = ROOT / "ui" / "streamlit_app.py"
    if not app_script.exists():
        _log(f"找不到应用入口：{app_script}")
        return 1
    os.environ.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"

    # Streamlit 的信号处理要求在主线程，因此放到独立子进程中运行；
    # 父进程主线程保留给 pywebview 原生窗口。
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    server = ctx.Process(target=_server_process, args=(port,), daemon=True)
    server.start()
    if not _wait_health(port):
        _log(f"服务启动失败：{url}")
        return 1
    if os.environ.get("TAX_DESKTOP_TEST") == "1":
        print(f"READY {url}", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0
    try:
        import webview

        webview.create_window("税小智 · 小规模纳税人自主税务填报系统", url, width=1440, height=960, min_size=(1024, 720))
        webview.start()
        return 0
    except Exception as exc:  # noqa: BLE001 - WebView 不可用时退回浏览器
        _log(f"原生窗口失败，改用浏览器：{exc}")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0
    finally:
        try:
            server.terminate()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
