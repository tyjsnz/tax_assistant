"""研发费用加计扣除计算与归集（确定性代码）。

政策口径
--------
* 财政部 税务总局公告2023年第7号：企业研发费用未形成无形资产计入当期损益的，
  在据实扣除基础上，自 2023-01-01 起再按实际发生额的 100% 在税前加计扣除；
  形成无形资产的，按无形资产成本的 200% 在税前摊销。
* 国家税务总局公告2021年第28号：其他相关费用限额 = 人员人工等五项费用之和 × 10% / (1 - 10%)，
  按全部研发项目统一计算限额。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

import config
from engine.money import money, pct, round2, yuan
from engine.policy_params import get_params

DISCLAIMER = config.DISCLAIMER

#: 研发费用六个归集科目
RD_ITEMS: tuple[str, ...] = (
    "personnel_cost",
    "material_cost",
    "depreciation",
    "amortization",
    "design_cost",
    "other_cost",
)

RD_ITEM_LABELS: dict[str, str] = {
    "personnel_cost": "人员人工费用",
    "material_cost": "直接投入费用",
    "depreciation": "折旧费用",
    "amortization": "无形资产摊销",
    "design_cost": "新产品设计费等",
    "other_cost": "其他相关费用",
}

#: 费用台账类别 → 研发归集科目
RD_CATEGORY_MAP: dict[str, str] = {
    "工资": "personnel_cost",
    "社保": "personnel_cost",
    "研发材料": "material_cost",
    "折旧": "depreciation",
    "软件订阅": "amortization",
    "差旅": "other_cost",
    "办公": "other_cost",
    "其他": "other_cost",
}

#: 不属于研发费用归集范围的费用类别
RD_EXCLUDED_CATEGORIES: tuple[str, ...] = ("房租",)


def calc_rd_deduction(
    personnel_cost: float = 0.0,
    material_cost: float = 0.0,
    depreciation: float = 0.0,
    amortization: float = 0.0,
    design_cost: float = 0.0,
    other_cost: float = 0.0,
    *,
    formed_intangible: bool = False,
    amortization_years: int | None = None,
    period: str = "",
    project_name: str = "",
) -> dict[str, Any]:
    """计算研发费用归集额与加计扣除额。

    参数
    ----
    personnel_cost/material_cost/depreciation/amortization/design_cost/other_cost : 六类研发费用
    formed_intangible : 是否形成无形资产（否则按 100% 加计扣除）
    amortization_years : 无形资产摊销年限（默认 10 年）

    返回
    ----
    dict：``total_collectible``（可加计扣除的研发费用总额）、``deduction_amount``（可加计扣除额）、
    限额校验结果与解释文本。
    """
    params = get_params(period)
    warnings: list[str] = list(params.get("warnings") or [])

    items = {
        "personnel_cost": money(personnel_cost),
        "material_cost": money(material_cost),
        "depreciation": money(depreciation),
        "amortization": money(amortization),
        "design_cost": money(design_cost),
        "other_cost": money(other_cost),
    }
    for key, value in items.items():
        if value < 0:
            warnings.append(f"{RD_ITEM_LABELS[key]}为负数，已按 0 处理。")
            items[key] = 0.0

    five_keys = ("personnel_cost", "material_cost", "depreciation", "amortization", "design_cost")
    five_total = round2(sum(items[key] for key in five_keys))
    other_limit = round2(Decimal(str(five_total)) * Decimal(str(params["rd_other_cost_ratio"])) / (Decimal("1") - Decimal(str(params["rd_other_cost_ratio"]))))
    other_claimed = items["other_cost"]
    other_allowed = min(other_claimed, other_limit)
    if other_claimed > other_limit:
        warnings.append(
            f"其他相关费用 {yuan(other_claimed)} 元超过限额 {yuan(other_limit)} 元，按限额归集，超出部分不得加计扣除。"
        )
    items["other_cost"] = round2(other_allowed)

    total_collectible = round2(five_total + other_allowed)
    ratio = float(params["rd_deduction_ratio"])
    years = int(amortization_years or params["rd_intangible_amortization_years"])
    years = max(years, 1)

    if formed_intangible:
        annual_amortization = round2(Decimal(str(total_collectible)) / Decimal(years))
        deduction_amount = round2(Decimal(str(annual_amortization)) * Decimal(str(ratio)))
        note = (
            f"研发形成无形资产：成本合计 {yuan(total_collectible)} 元，按 {years} 年摊销，"
            f"每年摊销 {yuan(annual_amortization)} 元，并按 {pct(ratio)} 加计摊销 "
            f"{yuan(deduction_amount)} 元/年（即税前按无形资产成本的 {int(100 * (1 + ratio))}% 摊销）。"
        )
    else:
        annual_amortization = 0.0
        deduction_amount = round2(Decimal(str(total_collectible)) * Decimal(str(ratio)))
        note = (
            f"研发费用未形成无形资产：可加计扣除的研发费用合计 {yuan(total_collectible)} 元，"
            f"按 {pct(ratio)} 加计扣除 {yuan(deduction_amount)} 元。"
            if total_collectible > 0
            else "本期无研发费用归集，无可加计扣除金额。"
        )

    return {
        "period": period,
        "project_name": project_name,
        "items": {RD_ITEM_LABELS[key]: round2(value) for key, value in items.items()},
        "items_detail": [
            {"item": RD_ITEM_LABELS[key], "amount": round2(items[key])}
            for key in RD_ITEMS
        ],
        "five_total": round2(five_total),
        "other_cost_claimed": round2(other_claimed),
        "other_cost_limit": round2(other_limit),
        "other_cost_allowed": round2(other_allowed),
        "total_collectible": total_collectible,
        "deduction_ratio": ratio,
        "deduction_amount": deduction_amount,
        "formed_intangible": bool(formed_intangible),
        "intangible_cost": total_collectible if formed_intangible else 0.0,
        "amortization_years": years if formed_intangible else None,
        "annual_amortization": annual_amortization if formed_intangible else 0.0,
        "note": note,
        "warnings": warnings,
        "policy_version": params["policy_version"],
        "last_verified": params["last_verified"],
        "disclaimer": DISCLAIMER,
    }


def aggregate_rd_expenses(
    expenses: Iterable[dict[str, Any]],
    *,
    project_name: str = "研发项目",
    period: str = "",
    formed_intangible: bool = False,
    include_unmarked: bool = False,
) -> dict[str, Any]:
    """从费用台账自动归集研发费用并计算加计扣除额。

    参数
    ----
    expenses : 费用台账记录（dict 列表，含 category / amount / is_rd_related / date / description）
    include_unmarked : 为 True 时，即使未标记「研发相关」也按类别归集（默认仅归集已标记记录）

    返回
    ----
    :func:`calc_rd_deduction` 的结果，并附带 ``detail``（逐笔归集明细）、``skipped``（被剔除的记录）、
    ``source_count``（参与归集的记录数）。
    """
    buckets = {key: 0.0 for key in RD_ITEMS}
    detail: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    source_count = 0

    for record in expenses:
        if not include_unmarked and not _is_rd(record):
            continue
        category = str(record.get("category") or "其他").strip()
        amount = money(record.get("amount"))
        if category in RD_EXCLUDED_CATEGORIES:
            skipped.append(
                {
                    "date": record.get("date", ""),
                    "description": record.get("description", ""),
                    "category": category,
                    "amount": amount,
                    "reason": f"{category}不属于研发费用加计扣除归集范围",
                }
            )
            continue
        bucket = RD_CATEGORY_MAP.get(category, "other_cost")
        buckets[bucket] = round2(buckets[bucket] + amount)
        source_count += 1
        detail.append(
            {
                "date": record.get("date", ""),
                "description": record.get("description", ""),
                "category": category,
                "amount": amount,
                "bucket": RD_ITEM_LABELS[bucket],
            }
        )

    result = calc_rd_deduction(
        **buckets,
        formed_intangible=formed_intangible,
        period=period,
        project_name=project_name,
    )
    result["detail"] = detail
    result["skipped"] = skipped
    result["source_count"] = source_count
    if skipped:
        result["warnings"] = list(result.get("warnings") or []) + [
            f"有 {len(skipped)} 笔费用不属于研发费用归集范围，已剔除（明细见 skipped）。"
        ]
    return result


def _is_rd(record: dict[str, Any]) -> bool:
    """判断费用记录是否标记为研发相关（兼容 bool 与 是/否 文本）。"""
    from ledger.models import parse_bool

    return parse_bool(record.get("is_rd_related"))
