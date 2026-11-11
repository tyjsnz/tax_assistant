"""全局配置：企业信息、政策参数、目录、DeepSeek 模型配置与本地用户设置。

安全约定
--------
1. 所有财务数据仅保存在本项目本地目录（data/、exports/、logs/），不上传任何第三方云服务。
2. DeepSeek API Key 只保存在本地 ``data/user_settings.json``（权限 600），
   不会写入代码、不会提交版本库。
3. 除「政策知识库问答」「申报表文字解析」「AI 助手对话」会向 DeepSeek 官方 API 发送必要
   的文本片段外，算税、台账、申报表生成均由本地确定性代码完成，不经过任何模型。
"""

from __future__ import annotations

import json
import os
import stat
from datetime import date, datetime
from pathlib import Path
from typing import Any

# =============================================================================
# 项目路径
# =============================================================================
# 多公司（多账套）目录约定：
#   DATA_ROOT/companies.json          公司注册表（当前激活公司）
#   DATA_ROOT/companies/{id}/         每家公司独立的台账 CSV、archive/、backups/、uploads/
#   DATA_ROOT/user_settings.json      全局设置（API Key、模型等，不随公司切换）
#   DATA_ROOT/schema.json             数据 schema 版本（用于升级迁移）
#   LOGS_DIR                          全局审计日志（不随公司切换，便于统一追溯）
#
# workspace.activate(company_id) 会切换 DATA_DIR/BACKUP_DIR/UPLOAD_DIR；
# 其余模块在运行时读取这些模块级变量，因此切换后无需重新 import。
BASE_DIR = Path(__file__).resolve().parent

#: 数据根目录（测试可用 TAX_DATA_DIR 指向临时目录实现隔离）
DATA_ROOT = Path(os.environ.get("TAX_DATA_DIR") or (BASE_DIR / "data"))

#: 当前激活公司的数据目录（未启用多公司前默认等于 DATA_ROOT，由 workspace 切换）
DATA_DIR = DATA_ROOT
BACKUP_DIR = DATA_ROOT / "backups"
UPLOAD_DIR = DATA_ROOT / "uploads"
EXPORT_DIR = Path(os.environ.get("TAX_EXPORT_DIR") or (BASE_DIR / "exports"))
LOGS_DIR = Path(os.environ.get("TAX_LOG_DIR") or (DATA_ROOT / "logs"))
POLICY_DIR = BASE_DIR / "knowledge" / "policy_files"
SETTINGS_FILE = DATA_ROOT / "user_settings.json"
COMPANIES_FILE = DATA_ROOT / "companies.json"
SCHEMA_FILE = DATA_ROOT / "schema.json"
ENV_FILE = BASE_DIR / ".env"

APP_NAME = "税小智 · 小规模纳税人自主税务填报系统"
APP_SHORT_NAME = "税小智"
APP_VERSION = "1.22.0"

DISCLAIMER = "⚠️ 仅供参考，以主管税务机关核定为准"

# =============================================================================
# 企业信息（可在「设置」页面覆盖，覆盖值保存在 data/user_settings.json）
# =============================================================================
COMPANY: dict[str, Any] = {
    "id": "main",
    "name": "某科技有限公司",
    "short_name": "某科技",
    "taxpayer_type": "小规模纳税人",
    "industry": "网络科技（技术服务、软件开发）",
    "unified_social_credit_code": "91530000MA0000000X",
    "tax_authority": "国家税务总局某市税务局",
    "province": "云南省",
    "city": "昆明市",
    "county": "禄劝彝族苗族自治县",
    "region_id": "yunnan-kunming-urban",
    "city_maintenance_tax_rate": 0.07,  # 市区 7%，县城/镇 5%，其他 1%
    "accounting_standard": "小企业会计准则",
    "vat_levy_method": "按季申报",
    "note": "",
}

#: 城建税地区预设（城建税税率：市区 7%、县城/镇 5%、其他 1%；教育费附加 3%、地方教育附加 2% 全国统一）
REGION_PRESETS: list[dict[str, Any]] = [
    {
        "id": "yunnan-kunming-urban",
        "label": "云南省 · 昆明市（市区）",
        "province": "云南省",
        "city": "昆明市",
        "county": "",
        "city_maintenance_tax_rate": 0.07,
    },
    {
        "id": "yunnan-county-town",
        "label": "云南省 · 县城 / 镇",
        "province": "云南省",
        "city": "",
        "county": "",
        "city_maintenance_tax_rate": 0.05,
    },
    {
        "id": "yunnan-other",
        "label": "云南省 · 其他（乡 / 村）",
        "province": "云南省",
        "city": "",
        "county": "",
        "city_maintenance_tax_rate": 0.01,
    },
    {
        "id": "generic-urban",
        "label": "通用 · 市区",
        "province": "",
        "city": "",
        "county": "",
        "city_maintenance_tax_rate": 0.07,
    },
    {
        "id": "generic-county-town",
        "label": "通用 · 县城 / 镇",
        "province": "",
        "city": "",
        "county": "",
        "city_maintenance_tax_rate": 0.05,
    },
    {
        "id": "generic-other",
        "label": "通用 · 其他",
        "province": "",
        "city": "",
        "county": "",
        "city_maintenance_tax_rate": 0.01,
    },
]

#: 附加税费中教育费附加 / 地方教育附加税率（全国统一）
EDUCATION_SURTAX_RATE = 0.03
LOCAL_EDUCATION_SURTAX_RATE = 0.02

# =============================================================================
# 政策参数（基础参数；**按期间的适用版本与政策依据**见 engine/policy_params.py）
# =============================================================================
POLICY: dict[str, Any] = {
    # 增值税 —— 小规模纳税人
    "vat_levy_rate": 0.01,                  # 3% 征收率减按 1%
    "vat_standard_rate": 0.03,              # 法定征收率 3%
    "vat_reduction_rate": 0.02,             # 3% → 1% 的减征幅度
    "vat_exemption_limit_quarter": 300_000.0,  # 季度免税额度（不含税销售额）
    "vat_exemption_limit_month": 100_000.0,    # 月度免税额度（不含税销售额）
    "vat_threshold_per_transaction": 1_000.0,  # 按次（日）纳税起征点（2026 年第 10 号公告）
    "vat_reduction_exclusions": ["销售不动产", "出租不动产", "转让土地使用权"],
    "vat_reduction_scope": (
        "小规模纳税人发生除销售、出租不动产或者转让土地使用权之外的应税交易，"
        "依 3% 征收率减按 1% 征收（财政部 税务总局公告2026年第10号 第三条第三项第 6 目）"
    ),
    # 附加税费
    "surtax_rate": 0.06,                    # 附加税费综合率（12% × 六税两费减半 50%）
    "surtax_rate_full": 0.12,               # 城建 7% + 教育 3% + 地方教育 2%
    "liutax_liangfei_discount": 0.5,        # 六税两费减半
    # 企业所得税 —— 小型微利企业
    "small_low_profit_rate": 0.05,          # 实际税负 5%（减按 25% 计入 × 20% 税率）
    "small_low_profit_threshold": 3_000_000.0,  # 年应纳税所得额上限
    "cit_standard_rate": 0.25,              # 法定税率
    "employee_threshold": 300,              # 从业人数上限
    "asset_threshold": 50_000_000.0,        # 资产总额上限
    # 研发费用加计扣除
    "rd_deduction_ratio": 1.0,              # 未形成无形资产：100% 加计扣除
    "rd_amortization_ratio": 2.0,           # 形成无形资产：按 200% 税前摊销
    "rd_other_cost_ratio": 0.10,            # 其他相关费用限额比例（按五项费用之和计算）
    "rd_intangible_amortization_years": 10, # 无形资产最低摊销年限
    # 印花税（六税两费减半后对小规模纳税人再减半）
    "stamp_tax_rates": {
        "买卖合同": 0.0003,
        "技术合同": 0.0003,
        "建设工程合同": 0.0003,
        "运输合同": 0.0003,
        "借款合同": 0.00005,
        "租赁合同": 0.001,
        "产权转移书据": 0.0005,
        "营业账簿": 0.00025,
    },
    # 政策依据按「期间 → 版本」维护在 engine/policy_params.py（勿在本文件写死文号）：
    #   2023-01-01 ~ 2025-12-31：财政部 税务总局公告2023年第19号（增值税暂行条例时期）
    #   2026-01-01 ~ 2027-12-31：增值税法 + 财政部 税务总局公告2026年第10号（衔接公告）
    # 2023 年第 19 号公告自 2026-01-01 起停止执行（2026 年第 10 号公告第六条）。
    "last_verified": "2026-11-10",
}

# =============================================================================
# 申报日历（金额单位：元；日期为税法规定的截止日，遇节假日顺延，以税局公告为准）
# =============================================================================
FILING_CALENDAR: dict[str, Any] = {
    "vat_quarterly": {
        "name": "增值税及附加税费申报（小规模纳税人）",
        "months": [1, 4, 7, 10],
        "day": 15,
        "tax_period": "上一季度",
        "remind_days": [7, 3, 1],
        "category": "增值税",
    },
    "corp_tax_quarterly": {
        "name": "企业所得税季度预缴申报",
        "months": [1, 4, 7, 10],
        "day": 15,
        "tax_period": "上一季度",
        "remind_days": [7, 3, 1],
        "category": "企业所得税",
    },
    "financial_report_quarterly": {
        "name": "财务报表报送（资产负债表、利润表）",
        "months": [1, 4, 7, 10],
        "day": 15,
        "tax_period": "上一季度",
        "remind_days": [3],
        "category": "财务报表",
    },
    "personal_tax_monthly": {
        "name": "个人所得税扣缴申报（自然人电子税务局扣缴端）",
        "months": list(range(1, 13)),
        "day": 15,
        "tax_period": "上一月",
        "remind_days": [3],
        "category": "个人所得税",
    },
    "stamp_tax_quarterly": {
        "name": "印花税申报（按季）",
        "months": [1, 4, 7, 10],
        "day": 15,
        "tax_period": "上一季度",
        "remind_days": [3],
        "category": "印花税",
    },
    "annual_settlement": {
        "name": "企业所得税年度汇算清缴",
        "months": [5],
        "day": 31,
        "tax_period": "上一纳税年度",
        "remind_days": [30, 14, 3],
        "category": "企业所得税",
    },
    "annual_report": {
        "name": "工商年报公示（国家企业信用信息公示系统）",
        "months": [6],
        "day": 30,
        "tax_period": "上一纳税年度",
        "remind_days": [30],
        "category": "工商年报",
    },
    "social_insurance_monthly": {
        "name": "社会保险费申报缴纳（单位部分）",
        "months": list(range(1, 13)),
        "day": 25,
        "tax_period": "上一月",
        "remind_days": [5, 2],
        "category": "社保费",
        "note": "各地截止日不同（常见每月 15 日 / 25 日），以当地税务/社保公告为准",
    },
    "disability_fund_annual": {
        "name": "残疾人就业保障金申报",
        "months": [9],
        "day": 30,
        "tax_period": "上一纳税年度",
        "remind_days": [30, 15, 3],
        "category": "残保金",
        "note": "多数地区按年申报（6–9 月），以当地残联/税务公告为准",
    },
}

# =============================================================================
# DeepSeek 模型配置（全部 AI 能力仅使用 DeepSeek）
# =============================================================================
LLM_CONFIG: dict[str, Any] = {
    "provider": "deepseek",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "reasoner_model": "deepseek-reasoner",
    "model_options": [
        "deepseek-chat",
        "deepseek-reasoner",
    ],
    "temperature": 0.3,
    "max_tokens": 2000,
    "timeout": 90,
    "api_key_env": "DEEPSEEK_API_KEY",
    "base_url_env": "DEEPSEEK_BASE_URL",
    "model_env": "DEEPSEEK_MODEL",
}

# =============================================================================
# 用户设置（API Key 等本地配置）
# =============================================================================
DEFAULT_SETTINGS: dict[str, Any] = {
    "api_key": "",
    "model": LLM_CONFIG["model"],
    "base_url": LLM_CONFIG["base_url"],
    "temperature": LLM_CONFIG["temperature"],
    "max_tokens": LLM_CONFIG["max_tokens"],
    "company": {},
    "privacy_mode": False,  # True 时台账客户名自动脱敏（客户A、客户B…）
}

_dotenv_loaded = False


# =============================================================================
# 目录
# =============================================================================
def ensure_dirs() -> None:
    """确保数据根目录、当前公司数据目录、备份、上传、导出、日志目录存在。"""
    for path in (DATA_ROOT, DATA_ROOT / "companies", DATA_DIR, BACKUP_DIR, UPLOAD_DIR, EXPORT_DIR, LOGS_DIR, POLICY_DIR):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


# =============================================================================
# .env 加载（不依赖 python-dotenv 也可工作）
# =============================================================================
def _load_dotenv(path: Path | None = None) -> None:
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    env_path = path or ENV_FILE
    if not env_path.exists():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


# =============================================================================
# 用户设置读写
# =============================================================================
def load_user_settings() -> dict[str, Any]:
    """读取本地用户设置（不存在时返回默认值）。"""
    ensure_dirs()
    settings: dict[str, Any] = json.loads(json.dumps(DEFAULT_SETTINGS, ensure_ascii=False))
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key, value in data.items():
                    if value is not None:
                        settings[key] = value
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass
    if not isinstance(settings.get("company"), dict):
        settings["company"] = {}
    return settings


def save_user_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """合并并持久化用户设置（原子写入，权限 600）。"""
    ensure_dirs()
    settings = load_user_settings()
    for key, value in patch.items():
        settings[key] = value
    tmp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".tmp")
    try:
        tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, SETTINGS_FILE)
        try:
            os.chmod(SETTINGS_FILE, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return settings


def update_settings(**patch: Any) -> dict[str, Any]:
    return save_user_settings(patch)


# =============================================================================
# API Key / LLM
# =============================================================================
def get_api_key() -> str:
    """API Key 优先级：设置页保存 > 环境变量/DEEPSEEK_API_KEY > .env。"""
    _load_dotenv()
    key = str(load_user_settings().get("api_key") or "").strip()
    if key:
        return key
    return str(os.environ.get(LLM_CONFIG["api_key_env"], "") or "").strip()


def set_api_key(api_key: str) -> dict[str, Any]:
    """保存 API Key（仅本地），空字符串表示清除。"""
    return save_user_settings({"api_key": (api_key or "").strip()})


def mask_key(api_key: str | None = None) -> str:
    """脱敏展示 API Key。"""
    key = (api_key if api_key is not None else get_api_key()) or ""
    if not key:
        return "（未配置）"
    if len(key) <= 10:
        return key[:2] + "*" * max(len(key) - 2, 1)
    return f"{key[:6]}****{key[-4:]}"


def is_ai_configured() -> bool:
    return bool(get_api_key())


def get_llm_config() -> dict[str, Any]:
    """合并默认 LLM 配置与本地用户设置/环境变量。"""
    _load_dotenv()
    settings = load_user_settings()
    config = dict(LLM_CONFIG)
    config["api_key"] = get_api_key()
    config["model"] = (
        str(settings.get("model") or "").strip()
        or os.environ.get(LLM_CONFIG["model_env"], "")
        or LLM_CONFIG["model"]
    )
    config["base_url"] = (
        str(settings.get("base_url") or "").strip().rstrip("/")
        or os.environ.get(LLM_CONFIG["base_url_env"], "").strip().rstrip("/")
        or LLM_CONFIG["base_url"]
    )
    try:
        config["temperature"] = float(settings.get("temperature", LLM_CONFIG["temperature"]))
    except (TypeError, ValueError):
        config["temperature"] = LLM_CONFIG["temperature"]
    try:
        config["max_tokens"] = int(settings.get("max_tokens", LLM_CONFIG["max_tokens"]))
    except (TypeError, ValueError):
        config["max_tokens"] = LLM_CONFIG["max_tokens"]
    return config


def get_company() -> dict[str, Any]:
    """当前激活公司的企业信息（多公司时由 workspace 提供，未启用时回退默认值）。"""
    company = dict(COMPANY)
    try:
        from workspace import active_company

        company.update(active_company())
    except Exception:  # noqa: BLE001 - 工作区尚未初始化或循环导入时回退默认企业
        company.update(load_user_settings().get("company") or {})
    return company


def get_region() -> dict[str, Any]:
    """当前公司的地区参数（城建税税率、省市区县、地区预设标签）。"""
    company = get_company()
    rate = company.get("city_maintenance_tax_rate")
    try:
        rate = float(rate) if rate is not None else 0.07
    except (TypeError, ValueError):
        rate = 0.07
    if rate not in (0.07, 0.05, 0.01):
        rate = 0.07
    preset = next((item for item in REGION_PRESETS if item.get("id") == company.get("region_id")), None)
    return {
        "region_id": company.get("region_id") or "",
        "label": (preset or {}).get("label") or "、".join(
            part for part in (company.get("province"), company.get("city"), company.get("county")) if part
        ) or "未指定地区",
        "province": company.get("province") or "",
        "city": company.get("city") or "",
        "county": company.get("county") or "",
        "city_maintenance_tax_rate": rate,
        "education_surtax_rate": EDUCATION_SURTAX_RATE,
        "local_education_surtax_rate": LOCAL_EDUCATION_SURTAX_RATE,
        "surtax_rate_full": rate + EDUCATION_SURTAX_RATE + LOCAL_EDUCATION_SURTAX_RATE,
        "surtax_rate_discounted": (rate + EDUCATION_SURTAX_RATE + LOCAL_EDUCATION_SURTAX_RATE) * 0.5,
    }


def get_region_presets() -> list[dict[str, Any]]:
    return [dict(item) for item in REGION_PRESETS]


def get_policy() -> dict[str, Any]:
    return dict(POLICY)


def today() -> date:
    return date.today()


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def is_privacy_mode() -> bool:
    return bool(load_user_settings().get("privacy_mode"))


def settings_snapshot() -> dict[str, Any]:
    """供 UI 展示的设置快照（API Key 已脱敏）。"""
    settings = load_user_settings()
    return {
        "api_key_masked": mask_key(),
        "api_key_configured": is_ai_configured(),
        "model": settings.get("model"),
        "base_url": settings.get("base_url"),
        "temperature": settings.get("temperature"),
        "max_tokens": settings.get("max_tokens"),
        "privacy_mode": bool(settings.get("privacy_mode")),
        "company": get_company(),
        "region": get_region(),
        "version": APP_VERSION,
    }
