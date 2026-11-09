"""确定性算税引擎。

**核心原则**：算税逻辑全部由本包内的确定性代码完成，LLM 只负责解释结果，
绝不允许模型参与税额计算。所有函数返回的字典均带 ``disclaimer`` 字段。
"""

from __future__ import annotations

from engine.corporate_tax import calc_cit_prepay, calc_corp_tax
from engine.money import (
    clamp,
    dec,
    pct,
    round2,
    safe_div,
    sum_money,
    yuan,
    yuan_cn,
)
from engine.policy_params import (
    describe as describe_policy,
    get_params,
    get_rate_table,
)
from engine.rd_deduction import (
    RD_CATEGORY_MAP,
    RD_EXCLUDED_CATEGORIES,
    RD_ITEM_LABELS,
    RD_ITEMS,
    aggregate_rd_expenses,
    calc_rd_deduction,
)
from engine.stamp_tax import calc_stamp_tax
from engine.tax_calendar import (
    completion_checklist,
    current_month,
    current_quarter,
    deadline_status,
    filing_deadlines,
    format_date_cn,
    month_options,
    next_deadline,
    period_label,
    period_range,
    prev_quarter,
    quarter_end,
    quarter_of,
    quarter_options,
    quarter_range,
    quarter_start,
    reminder_text,
    reminders,
    shift_to_workday,
    upcoming_deadlines,
)
from engine.vat import calc_vat

__all__ = [
    # 算税
    "calc_vat",
    "calc_corp_tax",
    "calc_cit_prepay",
    "calc_rd_deduction",
    "aggregate_rd_expenses",
    "calc_stamp_tax",
    # 政策参数
    "get_params",
    "get_rate_table",
    "describe_policy",
    # 金额工具
    "dec",
    "round2",
    "yuan",
    "yuan_cn",
    "pct",
    "safe_div",
    "clamp",
    "sum_money",
    # 期间与日历
    "quarter_of",
    "quarter_start",
    "quarter_end",
    "quarter_range",
    "period_range",
    "period_label",
    "current_quarter",
    "current_month",
    "prev_quarter",
    "quarter_options",
    "month_options",
    "filing_deadlines",
    "upcoming_deadlines",
    "next_deadline",
    "reminders",
    "reminder_text",
    "deadline_status",
    "format_date_cn",
    "shift_to_workday",
    "completion_checklist",
    # 常量
    "RD_ITEMS",
    "RD_ITEM_LABELS",
    "RD_CATEGORY_MAP",
    "RD_EXCLUDED_CATEGORIES",
]
