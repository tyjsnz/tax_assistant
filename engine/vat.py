"""增值税及附加税费计算（确定性代码，禁止 LLM 参与计算）。

适用对象：按季申报的增值税小规模纳税人（3% 征收率减按 1%，季度销售额 30 万元以下免征）。

计算口径
--------
1. 含税销售额换算为不含税销售额：``不含税 = 含税 / (1 + 1%)``。
2. 免税判定：以**全部销售额（含开具专用发票的销售额）**判断是否超过季度免税额度。
   依据按所属期间动态匹配（见 :mod:`engine.policy_params`）：
   - 2023-01-01 ~ 2025-12-31：财政部 税务总局公告2023年第19号；
   - 2026-01-01 ~ 2027-12-31：《增值税法》施行后，按财政部 税务总局公告2026年第10号第一条，
     按季申报起征点为季度销售额 30 万元（不含税）。
   可用 ``limit_basis="ordinary"`` 切换为仅以普票+未开票销售额判断（与部分简化口径一致）。
3. 免税额度内：普通发票与未开票部分免征增值税；**专用发票部分不免**，按 1% 缴纳。
4. 超过免税额度：全部销售额按 1% 缴纳，不再免税。
5. 应纳税额减征额 = 应征销售额 × (3% - 1%)，用于申报表第22栏。
   2026 年起 3% 减按 1% 仅适用于**除销售、出租不动产或者转让土地使用权之外**的应税交易
   （2026 年第 10 号公告第三条第三项第 6 目）。
6. 附加税费 = 实际缴纳增值税 × 12%（城建7% + 教育3% + 地方教育2%）× 六税两费减半 50%
   （六税两费依据财政部 税务总局公告2023年第12号，不属于增值税优惠文件，继续有效）。
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import config
from engine.money import dec, pct, round2, yuan
from engine.policy_params import get_params

DISCLAIMER = config.DISCLAIMER


def _vat_policy_source(params: dict[str, Any]) -> str:
    """取本期间增值税优惠的核心依据（文号 + 条款 + 期间），避免硬编码文号。

    2023—2025 年引用财政部 税务总局公告2023年第19号；
    2026 年起引用增值税法 + 财政部 税务总局公告2026年第10号（第一条起征点、
    第三条第三项第 6 目减按 1%）。
    """
    items = [item for item in (params.get("policy_sources") or []) if item.get("applies_to") == "vat"]
    wanted = [
        item
        for item in items
        if "起征点" in str(item.get("title") or "") or "减按 1%" in str(item.get("title") or "")
    ] or items[:1]
    groups: dict[str, dict[str, Any]] = {}
    for item in wanted:
        source_text = str(item.get("source") or "").strip()
        match = re.search(r"\s+(第.+)$", source_text)
        base = source_text[: match.start()].strip() if match else source_text
        clause = match.group(1).strip() if match else ""
        group = groups.setdefault(
            base,
            {"clauses": [], "effective": item.get("effective_date") or "", "expiry": item.get("expiry_date") or ""},
        )
        if clause and clause not in group["clauses"]:
            group["clauses"].append(clause)
    parts: list[str] = []
    for base, info in groups.items():
        text = base
        if info["clauses"]:
            text += "（" + "、".join(info["clauses"]) + "）"
        if info["effective"]:
            text += f"：{info['effective']} 起"
            if info["expiry"] and info["expiry"] not in {"长期", ""}:
                text += f"至 {info['expiry']}"
        parts.append(text)
    return "；".join(parts) if parts else "以主管税务机关公布的最新政策为准"


def calc_vat(
    quarterly_total_inclusive: float,
    special_invoice_inclusive: float = 0.0,
    period: str = "",
    *,
    levy_rate: float | None = None,
    exemption_limit: float | None = None,
    surtax_rate: float | None = None,
    liutax_discount: float | None = None,
    limit_basis: str = "total",
) -> dict[str, Any]:
    """计算季度增值税及附加税费。

    参数
    ----
    quarterly_total_inclusive : 季度含税总销售额（元）
    special_invoice_inclusive : 其中开具增值税专用发票的含税金额（元），默认 0
    period                    : 所属季度，如 ``2026Q1``，用于政策版本匹配
    levy_rate                 : 覆盖征收率（默认 1%）
    exemption_limit           : 覆盖季度免税额度（默认 300,000 元）
    surtax_rate               : 覆盖附加税费综合率（默认 6%，已含减半）
    liutax_discount           : 覆盖六税两费减半比例（默认 0.5）
    limit_basis               : ``total``（全部销售额，法定口径）或 ``ordinary``（仅普票+未开票）

    返回
    ----
    dict：含 ``vat_payable``、``vat_exempt``、``surtax``、``within_exemption_limit`` 等字段，
    以及可直接用于申报表填报的栏次数据与解释性 ``note``。
    """
    params = get_params(period)
    warnings: list[str] = list(params.get("warnings") or [])

    levy = Decimal(str(levy_rate if levy_rate is not None else params["vat_levy_rate"]))
    standard_rate = Decimal(str(params["vat_standard_rate"]))
    limit = Decimal(str(exemption_limit if exemption_limit is not None else params["vat_exemption_limit_quarter"]))
    full_surtax_rate = Decimal(str(params["surtax_rate_full"]))
    discount = Decimal(str(liutax_discount if liutax_discount is not None else params["liutax_liangfei_discount"]))
    if surtax_rate is not None:
        # 外部直接给定综合附加税率时，视为已含减半，不再二次打折
        effective_surtax_rate = Decimal(str(surtax_rate))
        discount = Decimal("1")
    else:
        effective_surtax_rate = full_surtax_rate * discount

    total_inclusive = Decimal(str(round2(quarterly_total_inclusive)))
    special_inclusive = Decimal(str(round2(special_invoice_inclusive)))
    if total_inclusive < 0 or special_inclusive < 0:
        raise ValueError("销售额不能为负数")
    if special_inclusive > total_inclusive:
        warnings.append("专票含税金额大于含税总销售额，已按总销售额封顶计算，请核对录入数据。")
        special_inclusive = total_inclusive

    divisor = Decimal("1") + levy
    total_exclusive = (total_inclusive / divisor).quantize(Decimal("0.01"))
    special_exclusive = (special_inclusive / divisor).quantize(Decimal("0.01"))
    ordinary_exclusive = (total_exclusive - special_exclusive).quantize(Decimal("0.01"))

    # ---- 免税判定 -----------------------------------------------------------
    basis_amount = total_exclusive if limit_basis == "total" else ordinary_exclusive
    within_limit = basis_amount <= limit
    basis_label = "全部销售额（含专票）" if limit_basis == "total" else "普票+未开票销售额"

    # ---- 应纳税额 -----------------------------------------------------------
    if within_limit:
        taxable_sales = special_exclusive          # 专票部分不免
        exempt_sales = ordinary_exclusive          # 普票+未开票免征
    else:
        taxable_sales = total_exclusive
        exempt_sales = Decimal("0.00")

    vat_payable = (taxable_sales * levy).quantize(Decimal("0.01"))
    vat_at_standard = (taxable_sales * standard_rate).quantize(Decimal("0.01"))
    vat_reduction = (vat_at_standard - vat_payable).quantize(Decimal("0.01"))
    if vat_reduction < 0:
        vat_reduction = Decimal("0.00")

    # 展示口径：按 1% 计算的减免额（与规范文档示例一致）
    vat_exempt = (exempt_sales * levy).quantize(Decimal("0.01"))
    # 申报表口径：第19栏「本期免税额」= 免税销售额 × 3%
    vat_exempt_at_standard = (exempt_sales * standard_rate).quantize(Decimal("0.01"))

    # ---- 附加税费 -----------------------------------------------------------
    if vat_payable > 0:
        surtax_full = (vat_payable * full_surtax_rate).quantize(Decimal("0.01"))
        surtax = (vat_payable * effective_surtax_rate).quantize(Decimal("0.01"))
        surtax_discount = (surtax_full - surtax).quantize(Decimal("0.01"))
        if vat_payable > 0 and within_limit:
            warnings.append(
                "本期仅有专票部分缴纳增值税：城建税按实际缴纳税额计征并减半；"
                "教育费附加、地方教育附加在月销售额不超过10万元时可免征，此处按综合 6% 估列，"
                "实际以申报表为准。"
            )
    else:
        surtax_full = Decimal("0.00")
        surtax = Decimal("0.00")
        surtax_discount = Decimal("0.00")

    total_tax = (vat_payable + surtax).quantize(Decimal("0.01"))

    # ---- 解释文本 -----------------------------------------------------------
    if total_inclusive == 0:
        note = "本期无销售额，可进行增值税零申报（零申报也需按期提交）。"
    elif within_limit and vat_payable == 0:
        note = (
            f"{basis_label}不含税 {yuan(exempt_sales)} 元 ≤ 免税额度 {yuan(limit)} 元，"
            f"免征增值税 {yuan(vat_exempt)} 元，附加税费同步免征。"
        )
    elif within_limit and vat_payable > 0:
        note = (
            f"{basis_label}不含税 {yuan(basis_amount)} 元 ≤ 免税额度 {yuan(limit)} 元；"
            f"其中专票不含税 {yuan(special_exclusive)} 元不免税，应缴增值税 {yuan(vat_payable)} 元，"
            f"普票+未开票部分免征增值税 {yuan(vat_exempt)} 元。"
        )
    else:
        note = (
            f"{basis_label}不含税 {yuan(basis_amount)} 元 > 免税额度 {yuan(limit)} 元，"
            f"不再享受免税，全部销售额按 {pct(levy)} 征收率缴纳增值税 {yuan(vat_payable)} 元。"
        )

    return {
        # ---- 规范文档要求的字段 ----
        "quarter": period or params["policy_start"],
        "period": period or params["policy_start"],
        "total_inclusive": round2(total_inclusive),
        "total_exclusive": round2(total_exclusive),
        "ordinary_exclusive": round2(ordinary_exclusive),
        "special_exclusive": round2(special_exclusive),
        "vat_payable": round2(vat_payable),
        "vat_exempt": round2(vat_exempt),
        "surtax": round2(surtax),
        "within_exemption_limit": bool(within_limit),
        "note": note,
        "disclaimer": DISCLAIMER,
        # ---- 扩展字段：申报表栏次与明细 ----
        "special_inclusive": round2(special_inclusive),
        "ordinary_inclusive": round2(total_inclusive - special_inclusive),
        "exemption_limit": round2(limit),
        "limit_basis": basis_label,
        "levy_rate": float(levy),
        "standard_rate": float(standard_rate),
        "vat_at_standard": round2(vat_at_standard),
        "vat_reduction": round2(vat_reduction),
        "vat_exempt_at_standard": round2(vat_exempt_at_standard),
        "surtax_full": round2(surtax_full),
        "surtax_discount": round2(surtax_discount),
        "surtax_rate": round2(effective_surtax_rate * 100) / 100,
        "liutax_discount": float(discount),
        "total_tax": round2(total_tax),
        "tax_burden": (round2(total_tax / total_exclusive) if total_exclusive > 0 else 0.0),
        "policy_version": params["policy_version"],
        "policy_version_id": params["policy_version_id"],
        "policy_sources": params["policy_sources"],
        "policy_basis": params["policy_sources"],
        "vat_policy_source": _vat_policy_source(params),
        "vat_reduction_scope": params.get("vat_reduction_scope", ""),
        "vat_reduction_exclusions": params.get("vat_reduction_exclusions", []),
        "vat_threshold_per_transaction": params.get("vat_threshold_per_transaction", 0.0),
        "superseded_documents": params.get("superseded_documents", []),
        "last_verified": params["last_verified"],
        "warnings": warnings,
        # ---- 申报表栏次（小规模纳税人增值税及附加税费申报表） ----
        "return_lines": {
            "line_1_taxable_sales": round2(taxable_sales),          # 第1栏 应征增值税不含税销售额(3%征收率)
            "line_18_tax_amount": round2(vat_at_standard),          # 第18栏 应纳税额
            "line_19_tax_exempt_amount": round2(vat_exempt_at_standard),  # 第19栏 本期免税额
            "line_22_reduction": round2(vat_reduction),            # 第22栏 应纳税额减征额
            "line_24_payable": round2(vat_payable),                # 第24栏 本期应补（退）税额
            "line_10_exempt_sales": round2(exempt_sales),          # 第10栏 小微企业免税销售额
            "surtax_city": round2(vat_payable * Decimal("0.07") * discount),  # 城建税
            "surtax_education": round2(vat_payable * Decimal("0.03") * discount),
            "surtax_local_education": round2(vat_payable * Decimal("0.02") * discount),
        },
        "breakdown": [
            {"label": "季度含税销售额", "value": round2(total_inclusive)},
            {"label": "不含税销售额", "value": round2(total_exclusive)},
            {"label": "其中：专票不含税", "value": round2(special_exclusive)},
            {"label": "其中：普票+未开票不含税", "value": round2(ordinary_exclusive)},
            {"label": "应缴增值税", "value": round2(vat_payable)},
            {"label": "减免增值税", "value": round2(vat_exempt)},
            {"label": "应缴附加税费", "value": round2(surtax)},
            {"label": "合计应纳税费", "value": round2(total_tax)},
        ],
    }
