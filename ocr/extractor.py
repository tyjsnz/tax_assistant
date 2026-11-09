"""申报表截图文字提取（本地 OCR）。

**重要**：DeepSeek 官方 API **没有视觉能力**，截图里的文字不可能直接交给大模型识别。
本模块因此采用「本地 OCR 提文字 → DeepSeek 理解文字」的分工：

1. 首选 **macOS Vision**（系统自带 Vision 框架，支持简体中文 ``zh-Hans``）：
   把内置的 Swift 源码编译成 ``ocr/bin/macos_ocr`` 小工具并缓存，之后直接调用；
2. 其次 **pytesseract**（需本机安装 tesseract；本机仅有 ``eng`` 语言包，能识别数字即可）；
3. 都不可用时不崩溃，返回中文提示，由 UI 引导用户使用「手动粘贴文本」入口。

编译注意（macOS 沙箱）：``swiftc`` 默认把 module cache 写到系统临时目录可能被沙箱拒绝，
因此编译时把 ``TMPDIR`` 与 ``-module-cache-path`` 都指向项目内 ``.cache/`` 目录。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import config

#: OCR 包目录与产物路径
OCR_DIR = Path(__file__).resolve().parent
BIN_DIR = OCR_DIR / "bin"
BINARY_NAME = "macos_ocr"
BINARY_PATH = BIN_DIR / BINARY_NAME
SWIFT_SOURCE = OCR_DIR / "vision_ocr.swift"
BUILD_TIMEOUT = 180
OCR_TIMEOUT = 60

#: 最近一次使用的后端（供 extract_text 返回）
_LAST_BACKEND = "manual"
#: 最近一次编译错误（便于 UI 展示原因）
_LAST_BUILD_ERROR = ""

#: 内置 Swift 源码（vision_ocr.swift 缺失时的兜底）
SWIFT_SOURCE_CODE = r"""// macOS Vision 本地 OCR 小工具（由 ocr/extractor.py 编译并调用）
import Foundation
import Vision
import AppKit

let args = CommandLine.arguments
guard args.count > 1, let img = NSImage(contentsOfFile: args[1]),
      let tiff = img.tiffRepresentation,
      let bmp = NSBitmapImageRep(data: tiff),
      let cg = bmp.cgImage else { print("ERR_IMAGE"); exit(1) }
let request = VNRecognizeTextRequest()
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
let handler = VNImageRequestHandler(cgImage: cg, options: [:])
do {
    try handler.perform([request])
    for obs in request.results ?? [] {
        if let c = obs.topCandidates(1).first { print(c.string) }
    }
} catch { print("ERR_OCR: \(error)"); exit(2) }
"""


class OCRError(RuntimeError):
    """OCR 执行失败（图片损坏、二进制异常等），消息为中文。"""


class OCRAvailableError(OCRError):
    """所有本地 OCR 后端均不可用，需要用户手动粘贴文本。"""


# =============================================================================
# 环境检测
# =============================================================================
def _swiftc_path() -> str | None:
    return shutil.which("swiftc") or ("/usr/bin/swiftc" if Path("/usr/bin/swiftc").exists() else None)


def _binary_ready() -> bool:
    return BINARY_PATH.exists() and os.access(BINARY_PATH, os.X_OK)


def _tesseract_available() -> bool:
    if not shutil.which("tesseract"):
        return False
    try:
        import importlib.util

        return importlib.util.find_spec("pytesseract") is not None
    except Exception:  # pragma: no cover
        return False


def is_available() -> dict[str, bool]:
    """返回可用 OCR 后端：``{"macos_vision": bool, "tesseract": bool, "manual": True}``。"""
    macos_vision = _binary_ready() or (sys.platform == "darwin" and bool(_swiftc_path()))
    return {
        "macos_vision": bool(macos_vision),
        "tesseract": _tesseract_available(),
        "manual": True,
    }


def availability_text() -> str:
    """供 UI 展示的中文后端状态说明。"""
    status = is_available()
    parts = []
    parts.append("✅ macOS Vision（推荐，支持中文）" if status["macos_vision"] else "❌ macOS Vision 不可用")
    parts.append("✅ pytesseract（数字识别）" if status["tesseract"] else "❌ pytesseract 不可用（未安装或缺少语言包）")
    parts.append("✅ 手动粘贴文本（始终可用）")
    return "；".join(parts)


def _cache_dirs() -> tuple[Path, Path]:
    base = Path(getattr(config, "BASE_DIR", OCR_DIR.parent)) / ".cache"
    swift_cache = base / "swift"
    tmp_dir = base / "tmp"
    swift_cache.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return swift_cache, tmp_dir


# =============================================================================
# Swift 二进制编译
# =============================================================================
def build_vision_binary(force: bool = False) -> Path | None:
    """编译（或复用缓存的）macOS Vision OCR 小工具，返回二进制路径；失败返回 None。

    - 已编译且源文件未更新时直接复用；
    - 编译命令：``TMPDIR=<项目>/.cache/tmp swiftc -module-cache-path <项目>/.cache/swift
      -O ocr/vision_ocr.swift -o ocr/bin/macos_ocr``；
    - 非 macOS / 没有 swiftc / 编译失败时返回 None，**不抛异常**，错误原因记录在
      :func:`last_build_error`。
    """
    global _LAST_BUILD_ERROR
    _LAST_BUILD_ERROR = ""
    if sys.platform != "darwin":
        _LAST_BUILD_ERROR = "当前系统不是 macOS，无法使用 Vision OCR。"
        return None
    swiftc = _swiftc_path()
    if not swiftc:
        _LAST_BUILD_ERROR = "未找到 swiftc（需安装 Xcode Command Line Tools）。"
        return None

    source = SWIFT_SOURCE
    try:
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        if not source.exists():
            source = BIN_DIR / "vision_ocr.swift"
            source.write_text(SWIFT_SOURCE_CODE, encoding="utf-8")
    except OSError as exc:
        _LAST_BUILD_ERROR = f"写入 Swift 源码失败：{exc}"
        return None

    if not force and _binary_ready():
        try:
            if BINARY_PATH.stat().st_mtime >= source.stat().st_mtime:
                return BINARY_PATH
        except OSError:
            pass

    swift_cache, tmp_dir = _cache_dirs()
    env = os.environ.copy()
    env["TMPDIR"] = str(tmp_dir)
    command = [
        swiftc,
        "-module-cache-path",
        str(swift_cache),
        "-O",
        str(source),
        "-o",
        str(BINARY_PATH),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT,
            env=env,
            cwd=str(Path(getattr(config, "BASE_DIR", OCR_DIR.parent))),
        )
    except subprocess.TimeoutExpired:
        _LAST_BUILD_ERROR = f"编译 Vision OCR 超时（>{BUILD_TIMEOUT} 秒）。"
        return None
    except OSError as exc:
        _LAST_BUILD_ERROR = f"调用 swiftc 失败：{exc}"
        return None
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        _LAST_BUILD_ERROR = "编译 Vision OCR 失败：" + ("；".join(detail[-3:]) if detail else f"exit {completed.returncode}")
        return None
    if not _binary_ready():
        _LAST_BUILD_ERROR = "编译完成但未生成可执行文件。"
        return None
    try:
        os.chmod(BINARY_PATH, 0o755)
    except OSError:
        pass
    return BINARY_PATH


def last_build_error() -> str:
    """最近一次编译失败原因（空串表示无错误）。"""
    return _LAST_BUILD_ERROR


# =============================================================================
# 各后端识别
# =============================================================================
def _run_vision(image_path: Path) -> str:
    binary = build_vision_binary()
    if binary is None:
        raise OCRAvailableError(_LAST_BUILD_ERROR or "macOS Vision OCR 不可用。")
    try:
        completed = subprocess.run(
            [str(binary), str(image_path)],
            capture_output=True,
            text=True,
            timeout=OCR_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise OCRError(f"macOS Vision 识别超时（>{OCR_TIMEOUT} 秒），请压缩图片后重试。") from exc
    except OSError as exc:
        raise OCRError(f"调用 macOS Vision OCR 失败：{exc}") from exc
    stdout = (completed.stdout or "").strip()
    if completed.returncode != 0:
        detail = (completed.stderr or stdout or "").strip()
        if stdout.startswith("ERR_IMAGE"):
            raise OCRError("图片无法读取，请确认文件是有效的 PNG / JPG 图片。")
        raise OCRError(f"macOS Vision 识别失败（退出码 {completed.returncode}）：{detail[:200]}")
    if stdout.startswith("ERR_IMAGE"):
        raise OCRError("图片无法读取，请确认文件是有效的 PNG / JPG 图片。")
    if not stdout:
        raise OCRError("macOS Vision 未识别到任何文字，请确认图片清晰、方向正确。")
    return stdout


def _run_tesseract(image_path: Path) -> str:
    if not _tesseract_available():
        raise OCRAvailableError("pytesseract 不可用：请安装 pytesseract 与 tesseract，并安装 chi_sim 中文语言包。")
    try:
        import pytesseract
        from PIL import Image
    except Exception as exc:  # noqa: BLE001
        raise OCRAvailableError(f"pytesseract / Pillow 未安装：{exc}") from exc
    try:
        with Image.open(image_path) as image:
            try:
                languages = set(pytesseract.get_languages(config=""))
            except Exception:  # noqa: BLE001 - 老版本无该方法
                languages = {"eng"}
            lang = "chi_sim+eng" if "chi_sim" in languages else "eng"
            text = pytesseract.image_to_string(image, lang=lang)
    except Exception as exc:  # noqa: BLE001
        raise OCRError(f"pytesseract 识别失败：{exc}") from exc
    text = (text or "").strip()
    if not text:
        raise OCRError("pytesseract 未识别到任何文字（本机仅英文语言包，中文识别效果有限）。")
    return text


def _ocr_with_backend(image_path: Path) -> tuple[str, str]:
    """依次尝试 macOS Vision → pytesseract，返回 ``(text, backend)``。"""
    errors: list[str] = []
    try:
        return _run_vision(image_path), "macos_vision"
    except OCRAvailableError as exc:
        errors.append(str(exc))
    except OCRError as exc:
        errors.append(str(exc))
    try:
        return _run_tesseract(image_path), "tesseract"
    except OCRError as exc:
        errors.append(str(exc))
    raise OCRAvailableError(
        "本地 OCR 均不可用：" + "；".join(errors) + "。请改用「手动粘贴文本」入口录入申报表栏次。"
    )


def ocr_image(image_path: str | Path) -> str:
    """识别图片文字：优先 macOS Vision，其次 pytesseract；都不可用抛 :class:`OCRAvailableError`。"""
    global _LAST_BACKEND
    path = Path(str(image_path))
    if not path.exists() or not path.is_file():
        raise OCRError(f"图片文件不存在：{path}")
    text, backend = _ocr_with_backend(path)
    _LAST_BACKEND = backend
    return text


def extract_text(image_path: str | Path) -> dict[str, Any]:
    """识别申报表截图，返回 ``{"text", "backend", "ok", "message"}``，**永不抛异常**。

    backend 取值：``macos_vision`` / ``tesseract`` / ``manual``（失败时的兜底提示）。
    """
    global _LAST_BACKEND
    path = Path(str(image_path))
    if not path.exists() or not path.is_file():
        return {
            "text": "",
            "backend": "manual",
            "ok": False,
            "message": f"找不到图片文件：{path.name}。请重新上传，或改用「手动粘贴文本」入口。",
        }
    try:
        text, backend = _ocr_with_backend(path)
        _LAST_BACKEND = backend
        if not text.strip():
            return {
                "text": "",
                "backend": backend,
                "ok": False,
                "message": "OCR 未识别到文字，请确认图片清晰、方向正确，或改用「手动粘贴文本」入口。",
            }
        backend_label = "macOS Vision" if backend == "macos_vision" else "pytesseract"
        return {
            "text": text,
            "backend": backend,
            "ok": True,
            "message": f"已通过 {backend_label} 识别到 {len(text.splitlines())} 行文字，请核对后提交校验。",
        }
    except OCRAvailableError as exc:
        _LAST_BACKEND = "manual"
        return {
            "text": "",
            "backend": "manual",
            "ok": False,
            "message": str(exc),
        }
    except OCRError as exc:
        return {
            "text": "",
            "backend": _LAST_BACKEND,
            "ok": False,
            "message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 - OCR 失败不允许影响 UI
        return {
            "text": "",
            "backend": "manual",
            "ok": False,
            "message": f"OCR 识别异常：{exc}。请改用「手动粘贴文本」入口。",
        }
