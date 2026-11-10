"""政策参数版本化：按所属期间匹配适用税率、限额与**政策依据**。

设计原则
--------
1. **一个期间只对应一个政策版本**：版本以 ``effective_from`` / ``effective_to`` 划定区间，
   算税函数必须通过 :func:`get_params` 取参数，不得直接读 ``config.POLICY``。
2. **依据可追溯**：每个版本携带政策依据列表（标题、文号、生效日、失效日、原文链接、
   适用税种、备注），算税结果与 Agent 回答统一引用当前期间版本的依据。
3. **停用文件显式记录**：旧依据在新版本中以 ``note`` / ``superseded_documents`` 标注
   “自某日起停止执行”，避免继续引用已失效文件。

2026 年重要变化（已核对原文）
---------------------------
* 《中华人民共和国增值税法》自 2026-01-01 施行。
* 财政部 税务总局公告 **2026 年第 10 号**《关于增值税法施行后增值税优惠政策衔接事项的公告》
  （2026-01-30 发布，2026-01-01 实施）承接增值税优惠：
  - 第一条：小规模纳税人起征点：按月 10 万元 / 按季 30 万元 / 按次（日）1,000 元，
    执行至 2027-12-31；
  - 第三条第三项第 6 目：小规模纳税人发生**除销售、出租不动产或者转让土地使用权之外**的
    应税交易，依 3% 征收率减按 1% 征收；预缴项目符合条件减按 1% 预征率预缴，执行至 2027-12-31；
  - 第二条第二项第 2 目：技术转让、技术开发及与之相关的技术咨询、技术服务免征增值税
    （需省级科技/工信主管部门认定并报税务机关备查），执行至 2027-12-31；
  - 第六条：除本公告、增值税法及实施条例、2025 年第 17 号公告外，2025-12-31 前制发的
    国内环节增值税优惠政策文件自 2026-01-01 起停止执行。
* 因此 **财政部 税务总局公告 2023 年第 19 号自 2026-01-01 起停止执行**，
  2026—2027 年期间不得再作为增值税优惠依据引用。
* 「六税两费」减半（2023 年第 12 号）、小型微利企业所得税 5%（2023 年第 12 号）、
  研发费用 100% 加计扣除（2023 年第 7 号）不属于增值税优惠文件，继续有效。
"""

from __future__ import annotations

import copy
import json
import re
from datetime import date
from typing import Any

import config

#: 已验证的政策参数区间（超出区间时会在结果中给出 warnings）
VERIFIED_FROM = date(2023, 1, 1)
VERIFIED_TO = date(2027, 12, 31)

#: 增值税法施行日（2026-01-01），此日后适用 2026 年第 10 号公告
VAT_LAW_EFFECTIVE = date(2026, 1, 1)

#: 官方原文链接（用于知识库与 Agent 引用）
URL_MOF_2026_10 = "https://szs.mof.gov.cn/zhengcefabu/202601/t20260131_3983036.htm"
URL_GOV_2023_19 = "https://www.gov.cn/zhengce/zhengceku/202308/content_6896287.htm"


def _source(
    title: str,
    source: str,
    effective_date: str,
    expiry_date: str,
    *,
    applies_to: str = "vat",
    url: str = "",
    note: str = "",
) -> dict[str, str]:
    """构造一条政策依据（供知识库、申报表、Agent 回答统一引用）。"""
    return {
        "title": title,
        "source": source,
        "effective_date": effective_date,
        "expiry_date": expiry_date,
        "applies_to": applies_to,
        "url": url,
        "note": note,
    }


# =============================================================================
# 政策版本库（按时间顺序排列；新增政策时在末尾追加新版本，不要修改历史版本）
# =============================================================================
POLICY_VERSIONS: list[dict[str, Any]] = [
    {
        "id": "2023-2025",
        "label": "2023—2025 年小规模纳税人 / 小型微利企业优惠（增值税暂行条例时期）",
        "effective_from": "2023-01-01",
        "effective_to": "2025-12-31",
        "region": "全国",
        "params": {
            # 2023 年第 19 号公告未单独规定按次起征点，按次纳税沿用 500 元口径
            "vat_threshold_per_transaction": 500.0,
            "vat_reduction_exclusions": ["销售不动产", "出租不动产", "转让土地使用权"],
            "vat_reduction_scope": "适用 3% 征收率的应税销售收入（销售不动产、出租不动产、转让土地使用权等适用 5% 征收率的项目除外）",
        },
        "sources": [
            _source(
                "增值税起征点：月销售额 10 万元 / 季度销售额 30 万元以下免征增值税",
                "财政部 税务总局公告2023年第19号",
                "2023-01-01",
                "2027-12-31",
                url=URL_GOV_2023_19,
                note="自 2026-01-01 起停止执行（财政部 税务总局公告2026年第10号第六条）",
            ),
            _source(
                "小规模纳税人 3% 征收率减按 1% 征收增值税",
                "财政部 税务总局公告2023年第19号",
                "2023-01-01",
                "2027-12-31",
                url=URL_GOV_2023_19,
                note="自 2026-01-01 起停止执行（财政部 税务总局公告2026年第10号第六条）",
            ),
            _source(
                "技术转让、技术开发及相关技术咨询、技术服务免征增值税",
                "财税〔2016〕36号 附件3《营业税改征增值税试点过渡政策的规定》第一条第（二十六）项",
                "2016-05-01",
                "长期",
                applies_to="vat",
            ),
            _source(
                "增值税小规模纳税人、小型微利企业和个体工商户减半征收“六税两费”",
                "财政部 税务总局公告2023年第12号",
                "2023-01-01",
                "2027-12-31",
                applies_to="surtax",
            ),
            _source(
                "小型微利企业减按 25% 计算应纳税所得额、按 20% 税率缴纳企业所得税",
                "财政部 税务总局公告2023年第12号",
                "2023-01-01",
                "2027-12-31",
                applies_to="cit",
            ),
            _source(
                "研发费用未形成无形资产按 100% 加计扣除、形成无形资产按 200% 摊销",
                "财政部 税务总局公告2023年第7号",
                "2023-01-01",
                "长期",
                applies_to="rd",
            ),
        ],
        "notes": [
            "2023—2025 年为《增值税暂行条例》时期，增值税优惠依据为财政部 税务总局公告2023年第19号。",
        ],
        "superseded_by": "2026-2027",
        "superseded_documents": [],
    },
    {
        "id": "2026-2027",
        "label": "2026—2027 年（《增值税法》施行后，依据财政部 税务总局公告2026年第10号）",
        "effective_from": "2026-01-01",
        "effective_to": "2027-12-31",
        "region": "全国",
        "params": {
            "vat_threshold_per_transaction": 1000.0,
            "vat_reduction_exclusions": ["销售不动产", "出租不动产", "转让土地使用权"],
            "vat_reduction_scope": (
                "小规模纳税人发生除销售、出租不动产或者转让土地使用权之外的应税交易，"
                "依 3% 征收率减按 1% 征收；按规定预缴的项目，当期在预缴地实现的全部价款、"
                "预收款（不含税）合计达到起征点的，减按 1% 预征率预缴"
            ),
        },
        "sources": [
            _source(
                "《中华人民共和国增值税法》施行，小规模纳税人适用简易计税方法（3% 征收率）",
                "《中华人民共和国增值税法》（2026-01-01 施行）及《增值税法实施条例》",
                "2026-01-01",
                "长期",
                applies_to="vat",
            ),
            _source(
                "增值税起征点：按月 10 万元 / 按季 30 万元 / 按次（日）1,000 元",
                "财政部 税务总局公告2026年第10号 第一条",
                "2026-01-01",
                "2027-12-31",
                url=URL_MOF_2026_10,
            ),
            _source(
                "小规模纳税人 3% 征收率减按 1% 征收（不含销售/出租不动产、转让土地使用权）",
                "财政部 税务总局公告2026年第10号 第三条第三项第 6 目",
                "2026-01-01",
                "2027-12-31",
                url=URL_MOF_2026_10,
            ),
            _source(
                "技术转让、技术开发及与之相关的技术咨询、技术服务免征增值税（需认定并备查）",
                "财政部 税务总局公告2026年第10号 第二条第二项第 2 目",
                "2026-01-01",
                "2027-12-31",
                url=URL_MOF_2026_10,
                applies_to="vat",
            ),
            _source(
                "增值税小规模纳税人、小型微利企业和个体工商户减半征收“六税两费”",
                "财政部 税务总局公告2023年第12号",
                "2023-01-01",
                "2027-12-31",
                applies_to="surtax",
                note="不属于增值税优惠文件，不因《增值税法》施行而停止执行",
            ),
            _source(
                "小型微利企业减按 25% 计算应纳税所得额、按 20% 税率缴纳企业所得税",
                "财政部 税务总局公告2023年第12号",
                "2023-01-01",
                "2027-12-31",
                applies_to="cit",
            ),
            _source(
                "研发费用未形成无形资产按 100% 加计扣除、形成无形资产按 200% 摊销",
                "财政部 税务总局公告2023年第7号",
                "2023-01-01",
                "长期",
                applies_to="rd",
            ),
        ],
        "notes": [
            "2026 年第 10 号公告第六条：除本公告、增值税法及实施条例、2025 年第 17 号公告外，"
            "在 2025-12-31 前制发的国内环节增值税优惠政策文件自 2026-01-01 起停止执行。",
            "小规模纳税人 3% 减按 1% 不适用于销售不动产、出租不动产、转让土地使用权（适用 5% 征收率）。",
            "按次纳税起征点为每次（日）销售额 1,000 元。",
        ],
        "supersedes": ["2023-2025"],
        "superseded_documents": [
            "财政部 税务总局公告2023年第19号（自 2026-01-01 起停止执行）",
        ],
    },
]


# =============================================================================
# 期间解析与版本匹配
# =============================================================================
def _parse_period_start(period: str | date | None) -> date:
    """把期间字符串转换为期间起始日。"""
    if isinstance(period, date):
        return period
    text = str(period or "").strip().upper()
    if not text:
        return date.today()
    match = re.match(r"^(\d{4})Q([1-4])$", text)
    if match:
        return date(int(match.group(1)), (int(match.group(2)) - 1) * 3 + 1, 1)
    match = re.match(r"^(\d{4})-(\d{1,2})$", text)
    if match:
        month = min(max(int(match.group(2)), 1), 12)
        return date(int(match.group(1)), month, 1)
    match = re.match(r"^(\d{4})$", text)
    if match:
        return date(int(match.group(1)), 1, 1)
    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return date.today()
    return date.today()


def _match_version(start: date) -> tuple[dict[str, Any], str | None]:
    """匹配适用版本；返回 ``(版本, 越界警告)``。"""
    for version in POLICY_VERSIONS:
        from_date = date.fromisoformat(version["effective_from"])
        to_date = date.fromisoformat(version["effective_to"])
        if from_date <= start <= to_date:
            return version, None
    if start > date.fromisoformat(POLICY_VERSIONS[-1]["effective_to"]):
        warning = (
            f"期间 {start.isoformat()} 不在已验证的政策参数区间"
            f"（{VERIFIED_FROM.isoformat()} ~ {VERIFIED_TO.isoformat()}）内，"
            f"已按 {POLICY_VERSIONS[-1]['id']} 版参数估算，请务必核对最新政策（可能已出台衔接公告）。"
        )
        return POLICY_VERSIONS[-1], warning
    warning = (
        f"期间 {start.isoformat()} 不在已验证的政策参数区间"
        f"（{VERIFIED_FROM.isoformat()} ~ {VERIFIED_TO.isoformat()}）内，"
        f"已按 {POLICY_VERSIONS[0]['id']} 版参数估算，仅供参考。"
    )
    return POLICY_VERSIONS[0], warning


def get_version(period: str | date | None = None) -> dict[str, Any]:
    """返回期间适用的政策版本（含依据列表），只读副本。"""
    version, _ = _match_version(_parse_period_start(period))
    return copy.deepcopy(version)


def list_versions() -> list[dict[str, Any]]:
    """返回全部政策版本摘要（供界面展示政策沿革）。"""
    return [
        {
            "id": version["id"],
            "label": version["label"],
            "effective_from": version["effective_from"],
            "effective_to": version["effective_to"],
            "region": version.get("region", "全国"),
            "sources_count": len(version.get("sources") or []),
            "superseded_documents": list(version.get("superseded_documents") or []),
        }
        for version in POLICY_VERSIONS
    ]


def get_params(period: str | date | None = None) -> dict[str, Any]:
    """获取适用期间的政策参数（含政策版本、依据列表与告警）。"""
    params = json.loads(json.dumps(config.POLICY, ensure_ascii=False))
    start = _parse_period_start(period)
    version, boundary_warning = _match_version(start)
    warnings: list[str] = []
    if boundary_warning:
        warnings.append(boundary_warning)

    # 版本级参数覆盖（按次起征点、1% 适用范围等）
    for key, value in (version.get("params") or {}).items():
        params[key] = copy.deepcopy(value)

    params["policy_version"] = version["label"]
    params["policy_version_id"] = version["id"]
    params["policy_effective_from"] = version["effective_from"]
    params["policy_effective_to"] = version["effective_to"]
    params["policy_sources"] = copy.deepcopy(version.get("sources") or [])
    # 兼容旧字段名（历史代码可能读取 policy_basis）
    params["policy_basis"] = params["policy_sources"]
    params["policy_notes"] = list(version.get("notes") or [])
    params["superseded_documents"] = list(version.get("superseded_documents") or [])
    params["last_verified"] = config.POLICY.get("last_verified", "")
    params["warnings"] = warnings
    return params


def sources_for(period: str | date | None = None, applies_to: str | None = None) -> list[dict[str, str]]:
    """返回期间适用的政策依据，可按税种过滤（vat / surtax / cit / rd）。"""
    sources = get_version(period).get("sources") or []
    if applies_to:
        sources = [item for item in sources if item.get("applies_to") in (applies_to, "all")]
    return sources


def policy_basis_text(period: str | date | None = None, top: int = 3, applies_to: str | None = None) -> str:
    """把期间适用依据拼成一句中文摘要（供 Agent / 界面引用）。"""
    sources = sources_for(period, applies_to=applies_to)
    parts: list[str] = []
    seen: set[str] = set()
    for item in sources:
        source = str(item.get("source") or "").strip()
        effective = str(item.get("effective_date") or "").strip()
        expiry = str(item.get("expiry_date") or "").strip()
        if not source or source in seen:
            continue
        seen.add(source)
        period_text = f"{effective} 起" if effective else ""
        if expiry and expiry not in {"长期", ""}:
            period_text += f"，至 {expiry}"
        parts.append(f"{source}（{period_text}）" if period_text else source)
        if len(parts) >= top:
            break
    if not parts:
        return "以主管税务机关公布的最新政策为准"
    return "；".join(parts)


def vat_basis_text(period: str | date | None = None) -> str:
    """返回增值税优惠的核心依据（一句话，供算税结果与 Agent 回复引用）。"""
    sources = sources_for(period, applies_to="vat")
    if not sources:
        return "以主管税务机关公布的最新政策为准"
    item = sources[0]
    text = f"{item['source']}"
    if item.get("effective_date"):
        text += f"（{item['effective_date']} 起"
        if item.get("expiry_date") and item["expiry_date"] not in {"长期", ""}:
            text += f"，至 {item['expiry_date']}"
        text += "）"
    return text


# =============================================================================
# 界面用：税率表与政策摘要
# =============================================================================
def get_rate_table(period: str | date | None = None) -> list[dict[str, str]]:
    """供界面展示的适用税率/限额表（依据随期间动态切换）。"""
    params = get_params(period)
    sources = params["policy_sources"]
    region = config.get_region()
    by_vat = [item for item in sources if item.get("applies_to") == "vat"]

    def _find(keyword: str) -> dict[str, str]:
        for item in by_vat:
            if keyword in str(item.get("title") or ""):
                return item
        return by_vat[0] if by_vat else {}

    def _cite(item: dict[str, str]) -> str:
        if item.get("source"):
            return f"{item['source']}（{item['effective_date']} 起）"
        return "—"

    levy_item = _find("减按")
    threshold_item = _find("起征点")
    surtax_source = next((item for item in sources if item.get("applies_to") == "surtax"), {})
    cit_source = next((item for item in sources if item.get("applies_to") == "cit"), {})
    rd_source = next((item for item in sources if item.get("applies_to") == "rd"), {})

    levy_scope = "适用 3% 征收率的一般情形"
    if params["policy_version_id"] == "2026-2027":
        levy_scope = "除销售/出租不动产、转让土地使用权外"

    return [
        {
            "项目": "增值税征收率（小规模纳税人）",
            "取值": f"{params['vat_levy_rate'] * 100:.0f}%（3% 减按 1%，{levy_scope}）",
            "依据": _cite(levy_item),
            "有效期": f"{params['policy_effective_from']} ~ {params['policy_effective_to']}",
        },
        {
            "项目": "增值税起征点（按季申报）",
            "取值": f"{params['vat_exemption_limit_quarter']:,.0f} 元（不含税销售额）",
            "依据": _cite(threshold_item),
            "有效期": f"{params['policy_effective_from']} ~ {params['policy_effective_to']}",
        },
        {
            "项目": "增值税起征点（按次纳税）",
            "取值": f"{params.get('vat_threshold_per_transaction', 0):,.0f} 元/次（日）",
            "依据": _cite(threshold_item),
            "有效期": f"{params['policy_effective_from']} ~ {params['policy_effective_to']}",
        },
        {
            "项目": "附加税费（城建 + 教育 + 地方教育）",
            "取值": (
                f"城建税 {region['city_maintenance_tax_rate'] * 100:.0f}% + 教育费附加 3% + 地方教育附加 2%"
                f" = {region['surtax_rate_full'] * 100:.0f}%，六税两费减半后 {region['surtax_rate_discounted'] * 100:.0f}%"
                f"（{region['label']}）"
            ),
            "依据": surtax_source.get("source", "—"),
            "有效期": "2023-01-01 ~ 2027-12-31",
        },
        {
            "项目": "企业所得税（小型微利企业）",
            "取值": f"实际税负 {params['small_low_profit_rate'] * 100:.0f}%",
            "依据": cit_source.get("source", "—"),
            "有效期": "2023-01-01 ~ 2027-12-31",
        },
        {
            "项目": "研发费用加计扣除",
            "取值": f"未形成无形资产 {params['rd_deduction_ratio'] * 100:.0f}%；形成无形资产按 {params['rd_amortization_ratio'] * 100:.0f}% 摊销",
            "依据": rd_source.get("source", "—"),
            "有效期": "长期",
        },
        {
            "项目": "印花税（小规模减半）",
            "取值": "技术合同/买卖合同 0.3‰，减半后 0.15‰",
            "依据": "《中华人民共和国印花税法》；财政部 税务总局公告2023年第12号",
            "有效期": "2022-07-01 起 / 减半 2023-01-01 ~ 2027-12-31",
        },
    ]


def describe(period: str | date | None = None) -> dict[str, Any]:
    """返回期间适用的政策摘要（供 UI / Agent 引用）。"""
    params = get_params(period)
    return {
        "period": period,
        "policy_version": params["policy_version"],
        "policy_version_id": params["policy_version_id"],
        "effective_from": params["policy_effective_from"],
        "effective_to": params["policy_effective_to"],
        "last_verified": params["last_verified"],
        "sources": params["policy_sources"],
        "notes": params["policy_notes"],
        "superseded_documents": params["superseded_documents"],
        "vat_reduction_scope": params.get("vat_reduction_scope", ""),
        "vat_reduction_exclusions": params.get("vat_reduction_exclusions", []),
        "vat_threshold_per_transaction": params.get("vat_threshold_per_transaction", 0),
        "rates": get_rate_table(period),
        "region": config.get_region(),
        "warnings": params["warnings"],
        "disclaimer": config.DISCLAIMER,
    }
