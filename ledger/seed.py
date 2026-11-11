"""示例数据生成：为演示与联调准备一套真实感的 2026 年 Q1—Q4 台账数据。

企业：某科技有限公司（小规模纳税人，网络科技行业）
数据口径（含税金额）：

===========  ==============  ==========================================
期间          季度含税收入     说明
===========  ==============  ==========================================
2026Q1        约 20 万         全部普通发票，享受季度免税
2026Q2        约 28 万         含 1 张 10.1 万元专用发票，专票部分缴税
2026Q3        约 25 万         全部普通发票，享受季度免税
2026Q4（截至10月）约 12 万      含 1 笔未开票收入
===========  ==============  ==========================================

费用每月 4 笔（工资、社保、房租 + 软件订阅/研发材料/办公/差旅轮换），
研发相关费用标记 ``is_rd_related=True``，并按季度生成研发辅助账手工归集记录；
同时按收入/费用自动生成记账凭证，并在季末生成免征增值税结转与缴税凭证。
"""

from __future__ import annotations

from typing import Any

from engine.money import round2
from ledger import models
from ledger.audit import log_user_action
from ledger.journal import (
    build_expense_entry,
    build_income_entry,
    build_vat_exemption_entry,
    build_vat_payment_entry,
    post_entries,
)
from ledger.store import table

COMPANY_DEMO_NAME = "某科技有限公司"

#: 与业务无关的辅助表（为判断「台账是否为空」只看业务数据表）
_BUSINESS_TABLES = ("income", "expense", "rd", "contracts", "journal_entries")

#: 收入示例数据（含税金额，发票类型 / 类别 / 客户 / 日期）
_INCOME_SPECS: list[dict[str, Any]] = [
    # ---- 2026Q1：全普票，合计 200,000 元 ----
    {"date": "2026-01-08", "client": "云图科技", "description": "税务数据平台一期技术服务费", "inclusive_amount": 46000.0, "category": "技术服务", "invoice_type": "普票", "invoice_no": "053001900001"},
    {"date": "2026-01-22", "client": "滇南数据", "description": "数据治理模块开发费", "inclusive_amount": 26000.0, "category": "软件开发", "invoice_type": "普票", "invoice_no": "053001900002"},
    {"date": "2026-02-09", "client": "昆明智联", "description": "系统运维服务费（1—2月）", "inclusive_amount": 38000.0, "category": "运维服务", "invoice_type": "普票", "invoice_no": "053001900003"},
    {"date": "2026-02-25", "client": "云图科技", "description": "电子税务局接口调试服务费", "inclusive_amount": 30000.0, "category": "技术服务", "invoice_type": "普票", "invoice_no": "053001900004"},
    {"date": "2026-03-12", "client": "滇南数据", "description": "报表模块定制开发费", "inclusive_amount": 40000.0, "category": "软件开发", "invoice_type": "普票", "invoice_no": "053001900005"},
    {"date": "2026-03-26", "client": "云南远卓", "description": "业务流程梳理咨询服务费", "inclusive_amount": 20000.0, "category": "技术服务", "invoice_type": "普票", "invoice_no": "053001900006"},
    # ---- 2026Q2：合计 280,000 元，其中 1 张专票 101,000 元 ----
    {"date": "2026-04-10", "client": "云图科技", "description": "税务数据平台二期技术服务费", "inclusive_amount": 52000.0, "category": "技术服务", "invoice_type": "普票", "invoice_no": "053001900007"},
    {"date": "2026-04-24", "client": "滇南数据", "description": "智能申报模块开发费", "inclusive_amount": 46000.0, "category": "软件开发", "invoice_type": "普票", "invoice_no": "053001900008"},
    {"date": "2026-05-08", "client": "昆明智联", "description": "系统运维服务费（4—5月）", "inclusive_amount": 30000.0, "category": "运维服务", "invoice_type": "普票", "invoice_no": "053001900009"},
    {"date": "2026-05-26", "client": "云南远卓", "description": "数据接口开发服务费", "inclusive_amount": 41000.0, "category": "技术服务", "invoice_type": "普票", "invoice_no": "053001900010"},
    {"date": "2026-06-15", "client": "云图科技", "description": "税务数据平台二期开发费（专用发票）", "inclusive_amount": 101000.0, "category": "软件开发", "invoice_type": "专票", "invoice_no": "053001900456"},
    {"date": "2026-06-29", "client": "云南远卓", "description": "系统上线支持服务费", "inclusive_amount": 10000.0, "category": "技术服务", "invoice_type": "普票", "invoice_no": "053001900011"},
    # ---- 2026Q3：全普票，合计 250,000 元 ----
    {"date": "2026-07-09", "client": "云图科技", "description": "税务数据平台运维技术服务费", "inclusive_amount": 44000.0, "category": "技术服务", "invoice_type": "普票", "invoice_no": "053001900012"},
    {"date": "2026-07-23", "client": "滇南数据", "description": "数据归集模块开发费", "inclusive_amount": 36000.0, "category": "软件开发", "invoice_type": "普票", "invoice_no": "053001900013"},
    {"date": "2026-08-11", "client": "昆明智联", "description": "系统运维服务费（7—8月）", "inclusive_amount": 52000.0, "category": "运维服务", "invoice_type": "普票", "invoice_no": "053001900014"},
    {"date": "2026-08-25", "client": "云南远卓", "description": "电子档案接口技术服务费", "inclusive_amount": 28000.0, "category": "技术服务", "invoice_type": "普票", "invoice_no": "053001900015"},
    {"date": "2026-09-10", "client": "云图科技", "description": "税务风险指标模型服务费", "inclusive_amount": 50000.0, "category": "技术服务", "invoice_type": "普票", "invoice_no": "053001900016"},
    {"date": "2026-09-24", "client": "滇南数据", "description": "经营分析看板开发费", "inclusive_amount": 40000.0, "category": "软件开发", "invoice_type": "普票", "invoice_no": "053001900017"},
    # ---- 2026Q4（截至 10 月）：合计 120,000 元，含 1 笔未开票收入 ----
    {"date": "2026-10-13", "client": "云图科技", "description": "税务数据平台四季度技术服务费", "inclusive_amount": 68000.0, "category": "技术服务", "invoice_type": "普票", "invoice_no": "053001900018"},
    {"date": "2026-10-21", "client": "滇南数据", "description": "数据治理二期开发费", "inclusive_amount": 30000.0, "category": "软件开发", "invoice_type": "普票", "invoice_no": "053001900019"},
    {"date": "2026-10-28", "client": "昆明智联", "description": "10月系统运维服务费（未开票）", "inclusive_amount": 22000.0, "category": "运维服务", "invoice_type": "未开票", "invoice_no": "", "note": "已收款，发票于次月补开"},
]

#: 每月固定三笔费用：工资（研发）、社保、房租
_FIXED_EXPENSES: list[dict[str, Any]] = [
    {
        "description": "研发人员工资（{month}月）",
        "category": "工资",
        "amount": 20000.0,
        "is_rd_related": True,
        "vendor": "",
        "receipt_no": "XZ2026{month:02d}",
        "day": 25,
    },
    {
        "description": "职工社会保险费（{month}月）",
        "category": "社保",
        "amount": 5600.0,
        "is_rd_related": False,
        "vendor": "禄劝彝族苗族自治县社会保险中心",
        "receipt_no": "SB2026{month:02d}",
        "day": 25,
    },
    {
        "description": "办公场地房租（{month}月）",
        "category": "房租",
        "amount": 6000.0,
        "is_rd_related": False,
        "vendor": "禄劝县城市建设投资有限公司",
        "receipt_no": "FZ2026{month:02d}",
        "day": 5,
    },
]

#: 每月一笔轮换费用（软件订阅 / 研发材料 / 办公 / 差旅）
_ROTATING_EXPENSES: dict[int, dict[str, Any]] = {
    1: {"description": "云服务器与开发工具订阅费", "category": "软件订阅", "amount": 680.0, "is_rd_related": True, "vendor": "阿里云计算有限公司", "receipt_no": "RJ202601", "day": 18},
    2: {"description": "研发测试用电子元件采购", "category": "研发材料", "amount": 3800.0, "is_rd_related": True, "vendor": "昆明电子元件商行", "receipt_no": "CL202602", "day": 20},
    3: {"description": "办公耗材与打印纸采购", "category": "办公", "amount": 1250.0, "is_rd_related": False, "vendor": "禄劝文汇办公用品店", "receipt_no": "BG202603", "day": 15},
    4: {"description": "云服务器与开发工具订阅费", "category": "软件订阅", "amount": 680.0, "is_rd_related": True, "vendor": "阿里云计算有限公司", "receipt_no": "RJ202604", "day": 18},
    5: {"description": "研发样机材料采购", "category": "研发材料", "amount": 4200.0, "is_rd_related": True, "vendor": "昆明电子元件商行", "receipt_no": "CL202605", "day": 20},
    6: {"description": "昆明—禄劝往返差旅及技术会议费", "category": "差旅", "amount": 2300.0, "is_rd_related": True, "vendor": "", "receipt_no": "CL202606", "day": 16},
    7: {"description": "云服务器与开发工具订阅费", "category": "软件订阅", "amount": 680.0, "is_rd_related": True, "vendor": "阿里云计算有限公司", "receipt_no": "RJ202607", "day": 18},
    8: {"description": "研发测试设备配件采购", "category": "研发材料", "amount": 4500.0, "is_rd_related": True, "vendor": "昆明电子元件商行", "receipt_no": "CL202608", "day": 20},
    9: {"description": "办公桌椅与耗材采购", "category": "办公", "amount": 1680.0, "is_rd_related": False, "vendor": "禄劝文汇办公用品店", "receipt_no": "BG202609", "day": 15},
    10: {"description": "研发数据采集模块材料采购", "category": "研发材料", "amount": 2600.0, "is_rd_related": True, "vendor": "昆明电子元件商行", "receipt_no": "CL202610", "day": 19},
}

#: 研发辅助账手工归集（项目级折旧、摊销、设计费，避免与费用台账重复归集）
_RD_SPECS: list[dict[str, Any]] = [
    {
        "project_name": "税务数据智能归集平台V2",
        "period": "2026Q1",
        "personnel_cost": 0.0,
        "material_cost": 0.0,
        "depreciation": 1800.0,
        "amortization": 1200.0,
        "design_cost": 2400.0,
        "other_cost": 600.0,
        "note": "人员人工、材料与软件订阅已由费用台账研发标记自动归集，此处补充项目级折旧、摊销与设计费。",
    },
    {
        "project_name": "税务数据智能归集平台V2",
        "period": "2026Q2",
        "personnel_cost": 0.0,
        "material_cost": 0.0,
        "depreciation": 1800.0,
        "amortization": 1200.0,
        "design_cost": 2400.0,
        "other_cost": 600.0,
        "note": "同 Q1，按季度分摊研发设备折旧与无形资产摊销。",
    },
    {
        "project_name": "电子税务局数据对接组件",
        "period": "2026Q3",
        "personnel_cost": 0.0,
        "material_cost": 0.0,
        "depreciation": 1500.0,
        "amortization": 900.0,
        "design_cost": 3000.0,
        "other_cost": 800.0,
        "note": "新立项项目：对接电子税务局申报接口，归集设计费与摊销。",
    },
    {
        "project_name": "电子税务局数据对接组件",
        "period": "2026Q4",
        "personnel_cost": 0.0,
        "material_cost": 0.0,
        "depreciation": 1500.0,
        "amortization": 900.0,
        "design_cost": 1800.0,
        "other_cost": 800.0,
        "note": "对接组件联调阶段，归集折旧、摊销与设计费。",
    },
]

#: 合同台账（技术合同为主，用于印花税申报）
_CONTRACT_SPECS: list[dict[str, Any]] = [
    {"date": "2026-01-10", "contract_no": "YR-2026-001", "counterparty": "云图科技", "contract_type": "技术合同", "amount": 180000.0, "is_rd_related": True, "note": "技术开发（委托）合同，分阶段验收"},
    {"date": "2026-03-05", "contract_no": "YR-2026-002", "counterparty": "昆明锐捷办公设备有限公司", "contract_type": "买卖合同", "amount": 18000.0, "is_rd_related": False, "note": "办公设备采购合同"},
    {"date": "2026-04-08", "contract_no": "YR-2026-003", "counterparty": "滇南数据", "contract_type": "技术合同", "amount": 120000.0, "is_rd_related": True, "note": "数据治理技术服务合同"},
    {"date": "2026-07-15", "contract_no": "YR-2026-004", "counterparty": "昆明智联", "contract_type": "技术合同", "amount": 90000.0, "is_rd_related": False, "note": "系统运维技术服务合同"},
    {"date": "2026-10-09", "contract_no": "YR-2026-005", "counterparty": "云南远卓", "contract_type": "技术合同", "amount": 60000.0, "is_rd_related": True, "note": "软件定制开发合同"},
]


# =============================================================================
# 数据构造
# =============================================================================
def _income_payloads() -> list[dict[str, Any]]:
    return [models.normalize_income(spec) for spec in _INCOME_SPECS]


def _expense_payloads() -> list[dict[str, Any]]:
    """生成 1—10 月每月 4 笔费用（固定 3 笔 + 轮换 1 笔）。"""
    payloads: list[dict[str, Any]] = []
    for month in range(1, 11):
        rotating = _ROTATING_EXPENSES[month]
        specs = [
            {
                "date": f"2026-{month:02d}-{int(fixed['day']):02d}",
                "description": fixed["description"].format(month=month),
                "category": fixed["category"],
                "amount": fixed["amount"],
                "is_rd_related": fixed["is_rd_related"],
                "vendor": fixed["vendor"],
                "receipt_no": fixed["receipt_no"].format(month=month),
                "note": "",
            }
            for fixed in _FIXED_EXPENSES
        ]
        specs.append(
            {
                "date": f"2026-{month:02d}-{int(rotating['day']):02d}",
                "description": rotating["description"],
                "category": rotating["category"],
                "amount": rotating["amount"],
                "is_rd_related": rotating["is_rd_related"],
                "vendor": rotating["vendor"],
                "receipt_no": rotating["receipt_no"],
                "note": "研发相关" if rotating["is_rd_related"] else "",
            }
        )
        for spec in specs:
            payloads.append(models.normalize_expense(spec))
    return payloads


def _rd_payloads() -> list[dict[str, Any]]:
    return [models.normalize_rd(spec) for spec in _RD_SPECS]


def _contract_payloads() -> list[dict[str, Any]]:
    return [models.normalize_contract(spec) for spec in _CONTRACT_SPECS]


# =============================================================================
# 对外接口
# =============================================================================
def is_empty() -> bool:
    """判断业务台账是否全部为空（收入、费用、研发、合同、凭证）。"""
    return all(table(name).count() == 0 for name in _BUSINESS_TABLES)


def clear_all() -> None:
    """清空全部台账与申报记录（不删除审计日志）。"""
    for name in models.TABLE_NAMES:
        table(name).replace_all([])
    log_user_action("清空台账", "已清空收入、费用、研发、合同、凭证与申报记录")


def seed_demo_data(reset: bool = False) -> dict[str, int]:
    """生成 2026 年 Q1—Q4 示例数据（企业：某科技有限公司）。

    参数
    ----
    reset : 为 ``True`` 时先清空全部台账与申报记录再生成；台账已有数据且未指定
            ``reset=True`` 时抛 ``ValueError``，避免重复追加。

    返回
    ----
    dict：各表生成条数，如 ``{"income": 21, "expense": 40, "rd": 4, "contracts": 5,
    "journal_entries": 65, "journal_lines": 137, "filings": 0}``。
    """
    if not reset and not is_empty():
        raise ValueError("台账已有数据；如需重新生成示例数据，请传入 reset=True 清空后重建。")

    if reset:
        clear_all()

    incomes = table("income").append_many(_income_payloads())
    expenses = table("expense").append_many(_expense_payloads())
    rd_records = table("rd").append_many(_rd_payloads())
    contracts = table("contracts").append_many(_contract_payloads())

    # ---- 自动生成记账凭证：收入、费用、季度末增值税结转与缴税 ----
    entries: list[dict[str, Any]] = []
    for record in incomes:
        entries.append(build_income_entry(record))
    for record in expenses:
        entries.append(build_expense_entry(record))
    for period in ("2026Q1", "2026Q2", "2026Q3", "2026Q4"):
        exemption_entry = build_vat_exemption_entry(period)
        if exemption_entry:
            entries.append(exemption_entry)
        payment_entry = build_vat_payment_entry(period)
        if payment_entry:
            entries.append(payment_entry)
    entries.sort(key=lambda entry: (str(entry.get("date") or ""), str(entry.get("voucher_type") or "")))
    posted = post_entries(entries)

    log_user_action(
        "加载示例数据",
        f"企业 {COMPANY_DEMO_NAME}：收入 {len(incomes)} 笔、费用 {len(expenses)} 笔、"
        f"研发归集 {len(rd_records)} 条、合同 {len(contracts)} 份、凭证 {len(posted)} 张",
    )
    return {
        "income": len(incomes),
        "expense": len(expenses),
        "rd": len(rd_records),
        "contracts": len(contracts),
        "journal_entries": len(posted),
        "journal_lines": table("journal_lines").count(),
        "filings": table("filings").count(),
        "income_inclusive_total": round2(sum(record["inclusive_amount"] for record in incomes)),
    }


__all__ = ["seed_demo_data", "is_empty", "clear_all", "COMPANY_DEMO_NAME"]
