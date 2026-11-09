"""可选模块的容错加载。

队友模块（ledger 子模块 / filing / agent / knowledge / ocr）与 UI 并行开发，
某些时刻可能尚未落盘。UI 不允许因此白屏：统一通过本模块 ``load()`` 取得模块，
缺失时页面展示友好提示并降级为空态。
"""

from __future__ import annotations

import importlib
from types import ModuleType

#: 最近一次加载失败的原因（供页面提示，仅保留内存态）
_errors: dict[str, str] = {}


def load(dotted: str) -> ModuleType | None:
    """导入模块；失败返回 ``None`` 并记录原因（不抛出异常）。"""
    try:
        module = importlib.import_module(dotted)
    except Exception as exc:  # noqa: BLE001 - 任何导入失败都要降级，不能整页崩溃
        _errors[dotted] = f"{type(exc).__name__}: {exc}"
        return None
    _errors.pop(dotted, None)
    return module


def require(dotted: str, attr: str | None = None):
    """导入模块（或模块属性）；失败返回 ``None``。"""
    module = load(dotted)
    if module is None:
        return None
    if attr is None:
        return module
    return getattr(module, attr, None)


def error_of(dotted: str) -> str:
    """返回某模块最近一次导入失败的信息（成功或未尝试时为空字符串）。"""
    return _errors.get(dotted, "")


def available(dotted: str) -> bool:
    """模块是否可用（会真实尝试导入）。"""
    return load(dotted) is not None
