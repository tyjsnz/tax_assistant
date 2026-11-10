"""多公司（多账套）工作区：公司注册表、数据目录切换、旧版数据迁移、schema 版本。

目录约定
--------
```
DATA_ROOT/
├── companies.json                # 公司注册表（当前激活公司）
├── schema.json                   # 数据 schema 版本
├── user_settings.json            # 全局设置（API Key / 模型，不随公司切换）
├── logs/                         # 全局审计日志
└── companies/
    └── {company_id}/             # 每家公司独立账套
        ├── income_ledger.csv
        ├── ...（全部台账表）
        ├── archive/              # 申报包归档
        └── backups/              # 数据备份
```

设计要点
--------
1. **单例切换**：``activate(company_id)`` 只改 ``config.DATA_DIR`` / ``BACKUP_DIR`` / ``UPLOAD_DIR``
   三个模块级变量，其余模块在运行时读取它们，因此在同进程内切换公司后无需重新 import。
2. **兼容旧版**：首次升级时 ``ensure_schema()`` 会把根目录下的旧台账 CSV 迁移到
   ``companies/main/``，并把旧版 ``user_settings.json`` 中的企业信息写入首家公司，数据不丢。
3. **安全**：删除公司默认软删除（移动到 ``companies/_deleted/``），且不允许删除唯一/当前公司。
"""

from __future__ import annotations

import json
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import config

#: 数据布局 schema 版本：1=单公司扁平目录；2=多公司目录
SCHEMA_VERSION = 2
#: 公司注册表版本
REGISTRY_VERSION = 1
#: 默认公司 ID（旧数据迁移目标）
DEFAULT_COMPANY_ID = "main"

_REGISTRY_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "short_name",
    "taxpayer_type",
    "industry",
    "unified_social_credit_code",
    "tax_authority",
    "province",
    "city",
    "county",
    "region_id",
    "city_maintenance_tax_rate",
    "accounting_standard",
    "vat_levy_method",
    "note",
)

_guard = threading.RLock()


# =============================================================================
# 路径
# =============================================================================
def data_root() -> Path:
    return Path(config.DATA_ROOT)


def companies_root() -> Path:
    return data_root() / "companies"


def registry_path() -> Path:
    return Path(config.COMPANIES_FILE)


def schema_path() -> Path:
    return Path(config.SCHEMA_FILE)


def company_dir(company_id: str) -> Path:
    return companies_root() / str(company_id)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# =============================================================================
# 注册表读写
# =============================================================================
def _empty_registry() -> dict[str, Any]:
    return {"version": REGISTRY_VERSION, "active": "", "companies": [], "updated_at": _now()}


def load_registry() -> dict[str, Any]:
    """读取公司注册表；不存在/损坏时返回空注册表。"""
    path = registry_path()
    if not path.exists():
        return _empty_registry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return _empty_registry()
    if not isinstance(data, dict) or not isinstance(data.get("companies"), list):
        return _empty_registry()
    data.setdefault("version", REGISTRY_VERSION)
    data.setdefault("active", "")
    return data


def save_registry(registry: dict[str, Any]) -> dict[str, Any]:
    """原子写入公司注册表。"""
    config.ensure_dirs()
    registry["version"] = REGISTRY_VERSION
    registry["updated_at"] = _now()
    path = registry_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return registry


#: 新建公司时的通用默认值（不含信用代码/税务机关等主体标识，避免复制其他公司的身份信息）
_NEW_COMPANY_DEFAULTS: dict[str, Any] = {
    "short_name": "",
    "taxpayer_type": "小规模纳税人",
    "industry": "",
    "unified_social_credit_code": "",
    "tax_authority": "",
    "province": "",
    "city": "",
    "county": "",
    "accounting_standard": "小企业会计准则",
    "vat_levy_method": "按季申报",
    "note": "",
}


def _normalize_record(
    payload: dict[str, Any],
    *,
    company_id: str | None = None,
    use_defaults: bool = True,
) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key in _REGISTRY_FIELDS:
        if key in payload and payload.get(key) is not None:
            record[key] = payload.get(key)
    record["id"] = str(company_id or payload.get("id") or _next_company_id()).strip()
    defaults = dict(config.COMPANY) if use_defaults else dict(_NEW_COMPANY_DEFAULTS)
    for key, value in defaults.items():
        record.setdefault(key, value)
    record["id"] = str(record["id"])
    # 地区：region_id 优先，其次按 city_maintenance_tax_rate 推断
    preset = next((item for item in config.REGION_PRESETS if item.get("id") == record.get("region_id")), None)
    if preset:
        record["city_maintenance_tax_rate"] = preset["city_maintenance_tax_rate"]
        for key in ("province", "city", "county"):
            if preset.get(key) and not record.get(key):
                record[key] = preset[key]
    try:
        rate = float(record.get("city_maintenance_tax_rate", 0.07))
    except (TypeError, ValueError):
        rate = 0.07
    if rate not in (0.07, 0.05, 0.01):
        rate = 0.07
    record["city_maintenance_tax_rate"] = rate
    if not record.get("region_id"):
        record["region_id"] = _infer_region_id(record.get("province"), rate)
    record.setdefault("created_at", _now())
    record["updated_at"] = _now()
    return record


def _infer_region_id(province: str | None, rate: float) -> str:
    prefix = "yunnan" if str(province or "").startswith("云南") else "generic"
    suffix = {0.07: "urban", 0.05: "county-town", 0.01: "other"}.get(rate, "urban")
    return f"{prefix}-{suffix}"


def _next_company_id() -> str:
    registry = load_registry()
    used = {str(item.get("id") or "") for item in registry["companies"]}
    if not used:
        return DEFAULT_COMPANY_ID
    index = 2
    while f"co{index}" in used:
        index += 1
    return f"co{index}"


def _sanitize_company_id(value: str | None) -> str:
    text = re.sub(r"[^a-z0-9_-]", "-", str(value or "").strip().lower()).strip("-")
    return text[:32]


# =============================================================================
# 查询
# =============================================================================
def list_companies() -> list[dict[str, Any]]:
    """公司列表（附带 is_active / data_dir / has_data）。"""
    registry = load_registry()
    active = str(registry.get("active") or "")
    results: list[dict[str, Any]] = []
    for record in registry["companies"]:
        item = dict(record)
        item["is_active"] = str(record.get("id")) == active
        item["data_dir"] = str(company_dir(str(record.get("id"))))
        directory = company_dir(str(record.get("id")))
        item["has_data"] = directory.exists() and any(
            path.is_file() and path.stat().st_size > 0 for path in directory.glob("*.csv")
        )
        results.append(item)
    return results


def get_company(company_id: str | None = None) -> dict[str, Any] | None:
    registry = load_registry()
    target = str(company_id or registry.get("active") or "")
    for record in registry["companies"]:
        if str(record.get("id")) == target:
            return dict(record)
    return None


def active_company_id() -> str:
    registry = load_registry()
    return str(registry.get("active") or "")


def active_company() -> dict[str, Any]:
    """当前激活公司；注册表为空时回退到默认企业信息（不创建文件）。"""
    record = get_company()
    if record:
        return record
    fallback = dict(config.COMPANY)
    fallback.setdefault("id", DEFAULT_COMPANY_ID)
    fallback.setdefault("region_id", "yunnan-kunming-urban")
    try:
        fallback.update(config.load_user_settings().get("company") or {})
    except Exception:  # noqa: BLE001
        pass
    return fallback


# =============================================================================
# 激活与目录切换
# =============================================================================
def _apply_paths(company_id: str) -> dict[str, Any]:
    directory = company_dir(company_id)
    config.DATA_DIR = directory
    config.BACKUP_DIR = directory / "backups"
    config.UPLOAD_DIR = directory / "uploads"
    config.ensure_dirs()
    from ledger import store as ledger_store

    ledger_store.reset_cache()
    return {
        "company_id": company_id,
        "data_dir": str(directory),
        "backup_dir": str(config.BACKUP_DIR),
        "upload_dir": str(config.UPLOAD_DIR),
    }


def activate(company_id: str, *, persist: bool = True) -> dict[str, Any]:
    """切换当前公司并返回其记录。"""
    record = get_company(company_id)
    if record is None:
        raise KeyError(f"公司不存在：{company_id}")
    with _guard:
        _apply_paths(str(record["id"]))
        if persist:
            registry = load_registry()
            registry["active"] = str(record["id"])
            save_registry(registry)
    return record


# =============================================================================
# schema / 旧版迁移
# =============================================================================
def schema_version() -> int:
    path = schema_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get("version") or 0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0


def _write_schema(version: int, *, migrated: bool = False) -> None:
    config.ensure_dirs()
    payload = {
        "version": version,
        "app_version": config.APP_VERSION,
        "migrated_at": _now() if migrated else "",
        "updated_at": _now(),
    }
    schema_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _legacy_flat_files() -> list[Path]:
    """根目录下旧版单公司布局的台账/子目录。"""
    root = data_root()
    if not root.exists():
        return []
    skip = {
        "companies",
        "logs",
        "user_settings.json",
        "companies.json",
        "schema.json",
        "backups",
        "uploads",
        "archive",
    }
    items: list[Path] = []
    for path in sorted(root.iterdir()):
        if path.name in skip:
            continue
        if path.is_file() and path.suffix.lower() == ".csv":
            items.append(path)
    return items


def migrate_legacy_layout() -> dict[str, Any]:
    """把旧版单公司扁平数据迁移到 ``companies/main/``（幂等）。"""
    root = data_root()
    target = company_dir(DEFAULT_COMPANY_ID)
    target.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    moved_dirs: list[str] = []
    for path in _legacy_flat_files():
        destination = target / path.name
        if destination.exists():
            continue
        try:
            shutil.move(str(path), str(destination))
            moved.append(path.name)
        except OSError:
            continue
    for name in ("backups", "uploads", "archive"):
        source = root / name
        if source.is_dir() and not (target / name).exists():
            try:
                shutil.move(str(source), str(target / name))
                moved_dirs.append(name)
            except OSError:
                continue
    return {"target": str(target), "moved_files": moved, "moved_dirs": moved_dirs}


def ensure_schema() -> dict[str, Any]:
    """确保目录布局为当前 schema 版本；旧版自动迁移。"""
    config.ensure_dirs()
    current = schema_version()
    if current >= SCHEMA_VERSION:
        return {"version": current, "migrated": False}
    # 已有公司注册表说明布局已是新版，无需迁移
    migration: dict[str, Any] = {"moved_files": [], "moved_dirs": []}
    if not registry_path().exists() and _legacy_flat_files():
        migration = migrate_legacy_layout()
    _write_schema(SCHEMA_VERSION, migrated=bool(migration["moved_files"] or migration["moved_dirs"]))
    return {"version": SCHEMA_VERSION, "migrated": bool(migration["moved_files"] or migration["moved_dirs"]), **migration}


def ensure_default() -> dict[str, Any]:
    """确保至少存在一家公司并激活；首次升级时迁移旧数据、承接旧企业信息。"""
    config.ensure_dirs()
    ensure_schema()
    registry = load_registry()
    if not registry["companies"]:
        legacy_company = {}
        try:
            legacy_company = dict(config.load_user_settings().get("company") or {})
        except Exception:  # noqa: BLE001
            legacy_company = {}
        payload = dict(config.COMPANY)
        payload.update(legacy_company)
        record = _normalize_record(payload, company_id=DEFAULT_COMPANY_ID)
        company_dir(DEFAULT_COMPANY_ID).mkdir(parents=True, exist_ok=True)
        registry = {
            "version": REGISTRY_VERSION,
            "active": DEFAULT_COMPANY_ID,
            "companies": [record],
            "updated_at": _now(),
        }
        save_registry(registry)
        _apply_paths(DEFAULT_COMPANY_ID)
        # 迁移成功后清理旧版企业信息覆盖，避免两份数据不一致
        if legacy_company:
            try:
                config.save_user_settings({"company": {}})
            except OSError:
                pass
    active = str(registry.get("active") or "")
    if not any(str(item.get("id")) == active for item in registry["companies"]):
        active = str(registry["companies"][0].get("id"))
        registry["active"] = active
        save_registry(registry)
    return activate(active)


# =============================================================================
# 公司 CRUD
# =============================================================================
def add_company(payload: dict[str, Any], *, activate_new: bool = False, company_id: str | None = None) -> dict[str, Any]:
    """新建公司（创建独立账套目录并初始化表结构）。"""
    registry = load_registry()
    new_id = _sanitize_company_id(company_id or payload.get("id")) or _sanitize_company_id(_next_company_id())
    if not new_id:
        new_id = _next_company_id()
    if any(str(item.get("id")) == new_id for item in registry["companies"]):
        raise ValueError(f"公司编号已存在：{new_id}")
    if not str(payload.get("name") or "").strip():
        raise ValueError("公司名称不能为空")
    record = _normalize_record(payload, company_id=new_id, use_defaults=False)
    company_dir(new_id).mkdir(parents=True, exist_ok=True)
    registry["companies"].append(record)
    save_registry(registry)
    if activate_new:
        activate(new_id)
    return record


def update_company(company_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """更新公司信息；修改 region_id 时同步地区与城建税税率。"""
    registry = load_registry()
    for index, item in enumerate(registry["companies"]):
        if str(item.get("id")) != str(company_id):
            continue
        merged = dict(item)
        merged.update({key: value for key, value in patch.items() if key in _REGISTRY_FIELDS and key != "id"})
        preset = next((entry for entry in config.REGION_PRESETS if entry.get("id") == merged.get("region_id")), None)
        if preset:
            merged["city_maintenance_tax_rate"] = preset["city_maintenance_tax_rate"]
            if preset.get("province"):
                merged["province"] = preset["province"]
            if preset.get("city"):
                merged["city"] = preset["city"]
            if preset.get("county"):
                merged["county"] = preset["county"]
        merged = _normalize_record(merged, company_id=str(item.get("id")))
        merged["created_at"] = str(item.get("created_at") or merged.get("created_at"))
        registry["companies"][index] = merged
        save_registry(registry)
        return merged
    raise KeyError(f"公司不存在：{company_id}")


def delete_company(company_id: str, *, hard: bool = False) -> dict[str, Any]:
    """删除公司（默认软删除：目录移动到 ``companies/_deleted/``）。"""
    registry = load_registry()
    target = str(company_id)
    if not any(str(item.get("id")) == target for item in registry["companies"]):
        raise KeyError(f"公司不存在：{company_id}")
    if len(registry["companies"]) <= 1:
        raise ValueError("至少需要保留一家公司，无法删除。")
    if str(registry.get("active")) == target:
        raise ValueError("不能删除当前正在使用的公司，请先切换到其他公司。")
    directory = company_dir(target)
    action = "removed" if hard else "archived"
    if directory.exists():
        if hard:
            shutil.rmtree(directory)
        else:
            deleted_root = companies_root() / "_deleted"
            deleted_root.mkdir(parents=True, exist_ok=True)
            destination = deleted_root / f"{target}_{datetime.now():%Y%m%d_%H%M%S}"
            shutil.move(str(directory), str(destination))
    registry["companies"] = [item for item in registry["companies"] if str(item.get("id")) != target]
    save_registry(registry)
    return {"id": target, "hard": hard, "action": action, "company_dir": str(directory)}


# =============================================================================
# 测试/重置
# =============================================================================
def _assert_safe_root() -> None:
    """重置数据目录的安全校验：只允许项目内 data/ 或环境变量指定的临时目录。"""
    root = data_root().resolve()
    base = config.BASE_DIR.resolve()
    allowed = str(root).startswith(str(base)) or bool(__import__("os").environ.get("TAX_DATA_DIR"))
    if not allowed:
        raise RuntimeError(f"拒绝重置非项目数据目录：{root}")


def reset_all(*, delete_data: bool = True) -> dict[str, Any]:
    """重置为单公司默认状态（供测试与「清空全部数据」使用）。"""
    _assert_safe_root()
    root = data_root()
    if delete_data and root.exists():
        shutil.rmtree(root)
    config.DATA_DIR = root
    config.BACKUP_DIR = root / "backups"
    config.UPLOAD_DIR = root / "uploads"
    from ledger import store as ledger_store

    ledger_store.reset_cache()
    config.ensure_dirs()
    record = ensure_default()
    return {"company_id": record.get("id"), "data_dir": str(config.DATA_DIR)}


def workspace_summary() -> dict[str, Any]:
    """供界面/诊断使用的工作区概览。"""
    registry = load_registry()
    return {
        "data_root": str(data_root()),
        "schema_version": schema_version(),
        "registry_version": registry.get("version"),
        "active_company_id": registry.get("active"),
        "companies": list_companies(),
        "active_data_dir": str(config.DATA_DIR),
        "logs_dir": str(config.LOGS_DIR),
    }
