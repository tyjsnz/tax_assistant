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


def _modules_from_package(package: str) -> list[str]:
    """直接从源码目录枚举所有 .py，避免 collect_submodules 在 spec 环境下导入失败而静默漏包。"""
    package_dir = ROOT / package
    modules: list[str] = []
    if not package_dir.is_dir():
        return modules
    for path in package_dir.rglob("*.py"):
        rel = path.relative_to(ROOT).with_suffix("")
        parts = list(rel.parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            modules.append(".".join(parts))
    return modules


for package in PACKAGES:
    hiddenimports.append(package)
    hiddenimports.extend(_modules_from_package(package))

# 根目录核心模块
hiddenimports += ["config", "workspace"]

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
