"""多公司（多账套）工作区访问封装。

职责
----
* 统一包装 ``workspace.py`` 与 ``config.py`` 的多公司 / 地区接口，页面只依赖本模块；
* 工作区模块缺失或读取异常时降级为「单公司」视图（使用 ``config.get_company()``），
  并在写操作时给出中文 ``ValueError``，保证 UI 不白屏；
* 公司切换后负责清理 Streamlit 数据缓存，避免旧公司数据残留。

注意：所有路径必须**在调用时**读取 ``config.DATA_DIR`` / ``config.BACKUP_DIR`` /
``config.UPLOAD_DIR``，不得在模块导入时缓存（切换公司后缓存会指向旧账套）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import config
from ui.components import cache, runtime

DEFAULT_COMPANY_ID = "main"


# =============================================================================
# 模块加载与就绪判断
# =============================================================================
def _workspace():
    return runtime.load("workspace")


def workspace_ready() -> bool:
    """多公司工作区模块是否可用。"""
    module = _workspace()
    return module is not None and hasattr(module, "list_companies")


def missing_detail() -> str:
    return runtime.error_of("workspace")


def _require(feature: str):
    module = _workspace()
    if module is None:
        raise ValueError(f"{feature}暂不可用：workspace 模块未就绪（{missing_detail() or '导入失败'}）。")
    return module


# =============================================================================
# 地区
# =============================================================================
def region() -> dict:
    """当前公司地区参数（城建税 / 教育费附加 / 综合附加税率）。"""
    try:
        return dict(config.get_region())
    except Exception:  # noqa: BLE001
        return {
            "region_id": "",
            "label": "未指定地区",
            "province": "",
            "city": "",
            "county": "",
            "city_maintenance_tax_rate": 0.07,
            "education_surtax_rate": 0.03,
            "local_education_surtax_rate": 0.02,
            "surtax_rate_full": 0.12,
            "surtax_rate_discounted": 0.06,
        }


def region_presets() -> list[dict]:
    """城建税地区预设列表。"""
    try:
        presets = list(config.get_region_presets())
    except Exception:  # noqa: BLE001
        presets = []
    return presets or [
        {
            "id": "yunnan-kunming-urban",
            "label": "云南省 · 昆明市（市区）",
            "province": "云南省",
            "city": "昆明市",
            "county": "",
            "city_maintenance_tax_rate": 0.07,
        }
    ]


def region_label(preset: dict) -> str:
    """地区预设展示文案：地区名 + 城建税税率。"""
    rate = _rate(preset.get("city_maintenance_tax_rate"))
    return f"{preset.get('label') or preset.get('id') or '未指定'}（城建税 {rate * 100:.0f}%）"


def preset_by_id(region_id: str) -> dict:
    for preset in region_presets():
        if str(preset.get("id")) == str(region_id):
            return dict(preset)
    return {}


def infer_region_id(province: str | None, rate: Any = 0.07) -> str:
    """按省份与税率推断地区预设 id（与 workspace 推断规则一致）。"""
    prefix = "yunnan" if str(province or "").startswith("云南") else "generic"
    suffix = {0.07: "urban", 0.05: "county-town", 0.01: "other"}.get(_rate(rate), "urban")
    return f"{prefix}-{suffix}"


def _rate(value: Any) -> float:
    try:
        rate = float(value or 0.07)
    except (TypeError, ValueError):
        rate = 0.07
    return rate if rate in (0.07, 0.05, 0.01) else 0.07


def region_text(company: dict | None = None) -> str:
    """公司地区摘要（用于列表 / 名片）。"""
    company = company or {}
    region_id = str(company.get("region_id") or "")
    preset = preset_by_id(region_id) if region_id else {}
    rate = _rate(company.get("city_maintenance_tax_rate"))
    label = preset.get("label") or "、".join(
        part for part in (company.get("province"), company.get("city"), company.get("county")) if part
    ) or region().get("label") or "未指定地区"
    return f"{label} · 城建税 {rate * 100:.0f}%"


# =============================================================================
# 公司查询 / 切换
# =============================================================================
def _fallback_company() -> dict:
    """工作区不可用时的单公司降级视图。"""
    try:
        company = dict(config.get_company())
    except Exception:  # noqa: BLE001
        company = dict(config.COMPANY)
    company.setdefault("id", DEFAULT_COMPANY_ID)
    company.setdefault("is_active", True)
    company.setdefault("has_data", False)
    company.setdefault("data_dir", str(config.DATA_DIR))
    return company


def list_companies() -> list[dict]:
    """公司列表（含 is_active / data_dir / has_data）；异常时降级为单公司。"""
    module = _workspace()
    if module is not None and hasattr(module, "list_companies"):
        try:
            companies = [dict(item) for item in module.list_companies()]
            if companies:
                return companies
        except Exception:  # noqa: BLE001
            pass
    return [_fallback_company()]


def active_company_id() -> str:
    module = _workspace()
    if module is not None and hasattr(module, "active_company_id"):
        try:
            return str(module.active_company_id() or DEFAULT_COMPANY_ID)
        except Exception:  # noqa: BLE001
            pass
    return str(_fallback_company().get("id") or DEFAULT_COMPANY_ID)


def active_company() -> dict:
    module = _workspace()
    if module is not None and hasattr(module, "active_company"):
        try:
            return dict(module.active_company())
        except Exception:  # noqa: BLE001
            pass
    return _fallback_company()


def get_company(company_id: str) -> dict | None:
    module = _workspace()
    if module is not None and hasattr(module, "get_company"):
        try:
            record = module.get_company(company_id)
            return dict(record) if record else None
        except Exception:  # noqa: BLE001
            return None
    company = _fallback_company()
    return company if str(company.get("id")) == str(company_id) else None


def company_label(company: dict) -> str:
    """公司选择器展示文案：名称（编号）。"""
    name = str(company.get("short_name") or company.get("name") or "未命名公司")
    company_id = str(company.get("id") or "")
    return f"{name}（{company_id}）" if company_id else name


def switch_company(company_id: str) -> dict:
    """切换到指定公司：激活账套 → 清理 Streamlit 缓存。

    调用方负责在成功（或需要刷新）后执行 ``st.rerun()``。
    """
    module = _require("切换公司")
    if not hasattr(module, "activate"):
        raise ValueError("切换公司暂不可用：workspace.activate 未实现。")
    record = dict(module.activate(str(company_id)))
    cache.clear()
    return record


# =============================================================================
# 公司 CRUD
# =============================================================================
def add_company(payload: dict, *, activate_new: bool = False) -> dict:
    """新建公司。"""
    module = _require("新建公司")
    if not hasattr(module, "add_company"):
        raise ValueError("新建公司暂不可用：workspace.add_company 未实现。")
    record = dict(module.add_company(payload, activate_new=bool(activate_new)))
    if activate_new:
        cache.clear()
    return record


def update_company(company_id: str, patch: dict) -> dict:
    """更新公司信息（含地区预设，城建税税率由后端按预设同步）。"""
    module = _require("更新公司信息")
    if not hasattr(module, "update_company"):
        raise ValueError("更新公司信息暂不可用：workspace.update_company 未实现。")
    record = dict(module.update_company(str(company_id), patch))
    cache.clear()
    return record


def delete_company(company_id: str, *, hard: bool = False) -> dict:
    """删除公司（默认软删除；后端会拒绝删除当前 / 唯一公司并抛 ValueError）。"""
    module = _require("删除公司")
    if not hasattr(module, "delete_company"):
        raise ValueError("删除公司暂不可用：workspace.delete_company 未实现。")
    result = dict(module.delete_company(str(company_id), hard=bool(hard)))
    cache.clear()
    return result


# =============================================================================
# 工作区信息
# =============================================================================
def schema_version() -> int:
    module = _workspace()
    if module is not None and hasattr(module, "schema_version"):
        try:
            return int(module.schema_version())
        except Exception:  # noqa: BLE001
            pass
    return 0


def data_root() -> Path:
    """数据根目录（不随公司切换）。"""
    module = _workspace()
    if module is not None and hasattr(module, "data_root"):
        try:
            return Path(module.data_root())
        except Exception:  # noqa: BLE001
            pass
    return Path(config.DATA_ROOT)


def workspace_summary() -> dict:
    """工作区概览（数据根目录 / schema 版本 / 当前公司数据目录等）。"""
    module = _workspace()
    if module is not None and hasattr(module, "workspace_summary"):
        try:
            return dict(module.workspace_summary())
        except Exception:  # noqa: BLE001
            pass
    return {
        "data_root": str(data_root()),
        "schema_version": schema_version(),
        "active_company_id": active_company_id(),
        "companies": list_companies(),
        "active_data_dir": str(config.DATA_DIR),
        "logs_dir": str(config.LOGS_DIR),
    }


def current_data_dir() -> Path:
    """当前公司数据目录（动态读取，切换公司后立即生效）。"""
    return Path(config.DATA_DIR)


def current_backup_dir() -> Path:
    return Path(config.BACKUP_DIR)


def current_upload_dir() -> Path:
    return Path(config.UPLOAD_DIR)


__all__ = [
    "DEFAULT_COMPANY_ID",
    "workspace_ready",
    "missing_detail",
    "region",
    "region_presets",
    "region_label",
    "preset_by_id",
    "infer_region_id",
    "region_text",
    "list_companies",
    "active_company_id",
    "active_company",
    "get_company",
    "company_label",
    "switch_company",
    "add_company",
    "update_company",
    "delete_company",
    "schema_version",
    "data_root",
    "workspace_summary",
    "current_data_dir",
    "current_backup_dir",
    "current_upload_dir",
]
