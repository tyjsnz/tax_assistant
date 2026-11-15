# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 桌面打包配置（v1.35）。

用法（在项目根目录）：
  .venv/bin/pyinstaller packaging/tax.spec --noconfirm --clean
产物：dist/税小智.app（macOS）或 dist/税小智/（Windows）
"""

from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).resolve().parent if "SPECPATH" in globals() else Path.cwd()
ROOT = ROOT.parent if ROOT.name == "packaging" else ROOT

datas = [
    (str(ROOT / "knowledge"), "knowledge"),
    (str(ROOT / "examples"), "examples"),
    (str(ROOT / "ui"), "ui"),
]
for optional in (".streamlit",):
    path = ROOT / optional
    if path.exists():
        datas.append((str(path), optional))

binaries = []
hiddenimports = []

def _collect(package):
    global binaries, hiddenimports
    try:
        d, b, h = collect_all(package)
        datas.extend(d)
        binaries.extend(b)
        hiddenimports.extend(h)
    except Exception:
        pass

for package in ("streamlit", "webview"):
    _collect(package)

PACKAGES = ("ui", "agent", "engine", "ledger", "filing", "closing", "archiving", "ingest", "knowledge", "onboarding", "integrations", "guidebooks", "automation", "ocr")
for package in PACKAGES:
    # 顶层包自身必须显式加入，否则 __init__.py 可能不会被打包；
    # 动态页面（ui.views.*）依赖这个入口，缺了会出现 ImportError。
    hiddenimports.append(package)
    try:
        hiddenimports.extend(collect_submodules(package))
    except Exception:
        pass

# 这些是页面动态导入链上的关键子模块，显式兜底
hiddenimports += [
    "ledger.locks",
    "ledger.store",
    "ledger.models",
    "ui.streamlit_app",
    "ui.views.dashboard",
]

hiddenimports += [
    "webview.platforms.cocoa",
    "webview.platforms.edgechromium",
    "webview.platforms.gtk",
    "webview.platforms.qt",
]

a = Analysis(
    [str(ROOT / "desktop_launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="税小智",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="税小智",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="税小智.app",
        icon=None,
        bundle_identifier="net.flacn.zhixiaoshui",
        info_plist={
            "CFBundleName": "税小智",
            "CFBundleDisplayName": "税小智",
            "CFBundleShortVersionString": "1.35.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
