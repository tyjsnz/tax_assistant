"""政策参数版本化：按所属期间匹配适用税率与限额，并给出政策依据、生效日期与告警。

所有算税函数都应通过 :func:`get_params` 获取参数，而不是直接读 ``config.POLICY``，
这样才能在政策变化时按期间精确切换，并向用户标注适用依据。
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

import config

#: 已验证的政策参数区间（超出区间时会在结果中给出 warnings）
VERIFIED_FROM = date(2023, 1, 1)
VERIFIED_TO = date(2027, 12, 31)

#: 按期间生效的政策参数版本（当前仅一个版本，后续可在此追加历史版本）
POLICY_VERSIONS: list[dict[str, Any]] = [
    {
        "effective_from": "2023-01-01",
        "effective_to": "2027-12-31",
        "label": "2023—2027 年小规模纳税人 & 小型微利企业优惠",
        "sources": config.POLICY["policy_basis"],
    }
]


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


def get_params(period: str | date | None = None) -> dict[str, Any]:
    """获取适用期间的政策参数（含政策依据与告警）。"""
    params = json.loads(json.dumps(config.POLICY, ensure_ascii=False))
    start = _parse_period_start(period)
    warnings: list[str] = []
    version = None
    for candidate in POLICY_VERSIONS:
        if date.fromisoformat(candidate["effective_from"]) <= start <= date.fromisoformat(candidate["effective_to"]):
            version = candidate
            break
    if version is None:
        version = POLICY_VERSIONS[-1]
        warnings.append(
            f"期间 {period or start.isoformat()} 不在已验证的政策参数区间"
            f"（{VERIFIED_FROM.isoformat()} ~ {VERIFIED_TO.isoformat()}）内，已按现行参数估算，"
            "请务必核对最新政策或咨询主管税务机关。"
        )
    params["policy_version"] = version["label"]
    params["policy_sources"] = version["sources"]
    params["policy_start"] = start.isoformat()
    params["last_verified"] = config.POLICY.get("last_verified", "")
    params["warnings"] = warnings
    return params


def get_rate_table(period: str | date | None = None) -> list[dict[str, str]]:
    """供界面展示的适用税率/限额表。"""
    params = get_params(period)
    return [
        {
            "项目": "增值税征收率（小规模）",
            "取值": f"{params['vat_levy_rate'] * 100:.0f}%（3% 减按 1%）",
            "依据": "财政部 税务总局公告2023年第19号",
            "有效期": "2023-01-01 ~ 2027-12-31",
        },
        {
            "项目": "增值税季度免税额度",
            "取值": f"{params['vat_exemption_limit_quarter']:,.0f} 元（不含税）",
            "依据": "财政部 税务总局公告2023年第19号",
            "有效期": "2023-01-01 ~ 2027-12-31",
        },
        {
            "项目": "附加税费（城建+教育+地方教育）",
            "取值": f"{params['surtax_rate_full'] * 100:.0f}%，六税两费减半后 {params['surtax_rate'] * 100:.0f}%",
            "依据": "财政部 税务总局公告2023年第12号",
            "有效期": "2023-01-01 ~ 2027-12-31",
        },
        {
            "项目": "企业所得税（小型微利）",
            "取值": f"实际税负 {params['small_low_profit_rate'] * 100:.0f}%",
            "依据": "财政部 税务总局公告2023年第12号",
            "有效期": "2023-01-01 ~ 2027-12-31",
        },
        {
            "项目": "研发费用加计扣除",
            "取值": f"未形成无形资产 {params['rd_deduction_ratio'] * 100:.0f}%；形成无形资产按 {params['rd_amortization_ratio'] * 100:.0f}% 摊销",
            "依据": "财政部 税务总局公告2023年第7号",
            "有效期": "长期",
        },
        {
            "项目": "印花税（小规模减半）",
            "取值": "技术合同/买卖合同 0.3‰，减半后 0.15‰",
            "依据": "财政部 税务总局公告2023年第12号",
            "有效期": "2023-01-01 ~ 2027-12-31",
        },
    ]


def describe(period: str | date | None = None) -> dict[str, Any]:
    """返回期间适用的政策摘要（供 UI / Agent 引用）。"""
    params = get_params(period)
    return {
        "period": period,
        "policy_version": params["policy_version"],
        "last_verified": params["last_verified"],
        "sources": params["policy_sources"],
        "rates": get_rate_table(period),
        "warnings": params["warnings"],
        "disclaimer": config.DISCLAIMER,
    }
