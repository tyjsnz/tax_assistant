"""台账数据模型：CSV 表结构、字段类型、数据校验与轻量数据类。

这里是全部台账 / 申报记录 schema 的**唯一来源**，``ledger/store.py`` 依据
``TABLE_DEFS`` 创建与读写 CSV；其它模块只通过 store / ledger 的 CRUD 函数访问数据。

字段类型约定
------------
``id``      字符串编号（前缀 + 6 位序号，如 INC000001）
``date``    ``YYYY-MM-DD`` 字符串
``period``  季度/期间字符串，如 ``2026Q1``、``2026-01``
``str``     普通文本
``money``   金额，内部按 Decimal 四舍五入保留 2 位小数，CSV 中存 ``1234.56``
``bool``    CSV 中存 ``是`` / ``否``，读写时转换为 Python ``bool``
``json``    复杂结构，CSV 中存 JSON 字符串
``int``     整数

一借多贷约定
------------
凭证拆为两张表：``journal_entries``（凭证头）与 ``journal_lines``（分录行），
从而支持「借：银行存款 / 贷：主营业务收入 + 应交税费」等多行凭证。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

from engine.money import round2

# =============================================================================
# 枚举与常量
# =============================================================================
INVOICE_TYPES: tuple[str, ...] = ("普票", "专票", "未开票")

INCOME_CATEGORIES: tuple[str, ...] = ("技术服务", "软件开发", "运维服务", "其他")

EXPENSE_CATEGORIES: tuple[str, ...] = (
    "工资",
    "社保",
    "房租",
    "办公",
    "差旅",
    "软件订阅",
    "研发材料",
    "折旧",
    "其他",
)

CONTRACT_TYPES: tuple[str, ...] = (
    "买卖合同",
    "技术合同",
    "建设工程合同",
    "运输合同",
    "借款合同",
    "租赁合同",
    "产权转移书据",
    "营业账簿",
)

FILING_STATUSES: tuple[str, ...] = ("草稿", "已确认", "已申报")

#: 发票方向（销项 = 开给客户；进项 = 供应商开给本企业）
INVOICE_DIRECTIONS: tuple[str, ...] = ("销项", "进项")

#: 发票类型（数电票/电子发票统一归入 普票/专票/其他）
INVOICE_KINDS: tuple[str, ...] = ("数电票", "专票", "普票", "其他")

#: 票据 / 流水池状态
INGEST_STATUSES: tuple[str, ...] = ("待匹配", "已匹配", "已入账", "已忽略")

#: 匹配建议状态
MATCH_STATUSES: tuple[str, ...] = ("建议", "已确认", "已忽略")

#: 会计期间类型（月结 / 季结）
PERIOD_TYPES: tuple[str, ...] = ("月", "季")

#: 会计期间状态机：未结账 → 已结账 →（申报）已申报 → 已归档；反结账回到未结账
PERIOD_STATUSES: tuple[str, ...] = ("未结账", "已结账", "已申报", "已归档")

#: 归档包类型
ARCHIVE_KINDS: tuple[str, ...] = ("申报包", "结账包", "其他")

#: 员工状态
EMPLOYEE_STATUSES: tuple[str, ...] = ("在职", "离职")

#: 工资表状态
PAYROLL_STATUSES: tuple[str, ...] = ("草稿", "已确认", "已申报")

#: 减除费用扣除方式
BASIC_DEDUCTION_CHOICES: tuple[str, ...] = ("累计预扣", "一次性60000")

#: 纳税调整方向
ADJUSTMENT_DIRECTIONS: tuple[str, ...] = ("调增", "调减")

#: 汇算清缴状态
SETTLEMENT_STATUSES: tuple[str, ...] = ("草稿", "已确认")

DIRECTIONS: tuple[str, ...] = ("借", "贷")

# 小规模纳税人征收率（3% 减按 1%）
VAT_LEVY_RATE = 0.01

TRUE_VALUES = {"是", "true", "1", "yes", "y", "t", "on"}
FALSE_VALUES = {"否", "false", "0", "no", "n", "f", "off", ""}


# =============================================================================
# 通用解析 / 规范化
# =============================================================================
def parse_bool(value: Any, default: bool = False) -> bool:
    """把 ``是/否``、``true/false``、``1/0`` 等转换为 bool。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return default


def bool_text(value: Any) -> str:
    """bool 的 CSV 存储形式（是/否）。"""
    return "是" if parse_bool(value) else "否"


def money(value: Any) -> float:
    """金额规范化：四舍五入 2 位小数。"""
    return round2(value)


_DATE_PATTERNS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y%m%d",
    "%Y年%m月%d日",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
)


def normalize_date(value: Any, default: str | None = None) -> str:
    """把常见日期写法规范为 ``YYYY-MM-DD``，无法解析时返回 default 或今天。"""
    if value is None or value == "":
        return default or date.today().isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    for pattern in _DATE_PATTERNS:
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    match = re.search(r"(\d{4})[-/.年]?(\d{1,2})[-/.月]?(\d{1,2})", text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            pass
    return default or date.today().isoformat()


_PERIOD_RE = re.compile(r"(\d{4})\s*[-年]?\s*(?:[Qq第]?\s*([1-4一二三四])\s*(?:季度|Q|q)?|([1-9]|1[0-2])\s*月?)")


def normalize_period(value: Any, default: str | None = None) -> str:
    """把 ``2026Q1``、``2026-Q1``、``2026年第一季度``、``2026-01`` 规范为期间字符串。

    季度形式返回 ``2026Q1``；月份形式返回 ``2026-01``；无法解析时返回 default。
    """
    if value is None or value == "":
        return default or ""
    if isinstance(value, (date, datetime)):
        value = value.isoformat()
    text = str(value).strip().replace(" ", "")
    cn_digits = {"一": "1", "二": "2", "三": "3", "四": "4"}
    for cn, digit in cn_digits.items():
        text = text.replace(f"第{cn}季度", f"Q{digit}")
    match = re.match(r"^(\d{4})[-年]?[Qq]([1-4])$", text)
    if match:
        return f"{match.group(1)}Q{match.group(2)}"
    match = re.match(r"^(\d{4})[-年]?([1-4])季度$", text)
    if match:
        return f"{match.group(1)}Q{match.group(2)}"
    match = re.match(r"^(\d{4})[-年]?(\d{1,2})月?$", text)
    if match:
        month = int(match.group(2))
        if 1 <= month <= 12:
            return f"{match.group(1)}-{month:02d}"
    match = re.match(r"^(\d{4})$", text)
    if match:
        return f"{match.group(1)}"
    return default or text


def derive_exclusive(inclusive_amount: Any, levy_rate: float = VAT_LEVY_RATE) -> tuple[float, float]:
    """含税金额 → (不含税金额, 增值税额)，征收率默认 1%。"""
    inclusive = money(inclusive_amount)
    if inclusive == 0:
        return 0.0, 0.0
    exclusive = round2(inclusive / (1 + levy_rate))
    vat = round2(inclusive - exclusive)
    return exclusive, vat


def mask_client(client: str, index: int | None = None) -> str:
    """隐私模式下可选的客户名脱敏。"""
    name = (client or "").strip()
    if not name:
        return name
    if len(name) <= 2:
        return name[0] + "某"
    return f"{name[0]}{'*' * (len(name) - 1)}"


def _one_of(value: Any, choices: Iterable[str], default: str, field_label: str) -> str:
    text = str(value or "").strip()
    if not text:
        return default
    for choice in choices:
        if text == choice:
            return choice
    for choice in choices:
        if text in choice or choice in text:
            return choice
    raise ValueError(f"{field_label} 取值「{text}」不合法，可选：{'、'.join(choices)}")


# =============================================================================
# 表结构定义
# =============================================================================
def _col(name: str, kind: str, label: str) -> dict[str, str]:
    return {"name": name, "type": kind, "label": label}


TABLE_DEFS: dict[str, dict[str, Any]] = {
    # 收入台账
    "income": {
        "file": "income_ledger.csv",
        "prefix": "INC",
        "label": "收入台账",
        "columns": [
            _col("id", "id", "编号"),
            _col("date", "date", "收款日期"),
            _col("client", "str", "客户名称"),
            _col("description", "str", "业务描述"),
            _col("category", "str", "业务类别"),
            _col("invoice_type", "str", "发票类型"),
            _col("invoice_no", "str", "发票号码"),
            _col("inclusive_amount", "money", "含税金额"),
            _col("exclusive_amount", "money", "不含税金额"),
            _col("vat_amount", "money", "增值税额"),
            _col("is_rd_related", "bool", "研发相关"),
            _col("note", "str", "备注"),
            _col("created_at", "str", "创建时间"),
        ],
    },
    # 费用台账
    "expense": {
        "file": "expense_ledger.csv",
        "prefix": "EXP",
        "label": "费用台账",
        "columns": [
            _col("id", "id", "编号"),
            _col("date", "date", "发生日期"),
            _col("description", "str", "费用描述"),
            _col("category", "str", "费用类别"),
            _col("amount", "money", "金额（含税）"),
            _col("is_rd_related", "bool", "研发相关"),
            _col("vendor", "str", "供应商"),
            _col("receipt_no", "str", "凭证号"),
            _col("note", "str", "备注"),
            _col("created_at", "str", "创建时间"),
        ],
    },
    # 研发辅助账
    "rd": {
        "file": "rd_auxiliary.csv",
        "prefix": "RD",
        "label": "研发辅助账",
        "columns": [
            _col("id", "id", "编号"),
            _col("project_name", "str", "研发项目"),
            _col("period", "period", "所属期间"),
            _col("personnel_cost", "money", "人员人工"),
            _col("material_cost", "money", "直接投入"),
            _col("depreciation", "money", "折旧费用"),
            _col("amortization", "money", "无形资产摊销"),
            _col("design_cost", "money", "新产品设计费"),
            _col("other_cost", "money", "其他相关费用"),
            _col("total", "money", "合计"),
            _col("deduction_amount", "money", "加计扣除额"),
            _col("note", "str", "备注"),
            _col("created_at", "str", "创建时间"),
        ],
    },
    # 凭证头
    "journal_entries": {
        "file": "journal_entries.csv",
        "prefix": "JV",
        "label": "凭证头",
        "columns": [
            _col("id", "id", "编号"),
            _col("date", "date", "凭证日期"),
            _col("entry_no", "str", "凭证号"),
            _col("description", "str", "摘要"),
            _col("total_amount", "money", "金额合计"),
            _col("source_ref", "str", "来源单据"),
            _col("created_at", "str", "创建时间"),
        ],
    },
    # 分录行
    "journal_lines": {
        "file": "journal_lines.csv",
        "prefix": "JL",
        "label": "分录行",
        "columns": [
            _col("id", "id", "编号"),
            _col("entry_no", "str", "凭证号"),
            _col("date", "date", "凭证日期"),
            _col("description", "str", "摘要"),
            _col("account", "str", "会计科目"),
            _col("direction", "str", "方向"),
            _col("amount", "money", "金额"),
            _col("line_no", "int", "行号"),
            _col("created_at", "str", "创建时间"),
        ],
    },
    # 合同 / 印花税台账
    "contracts": {
        "file": "contracts.csv",
        "prefix": "CT",
        "label": "合同台账（印花税）",
        "columns": [
            _col("id", "id", "编号"),
            _col("date", "date", "签订日期"),
            _col("contract_no", "str", "合同编号"),
            _col("counterparty", "str", "对方单位"),
            _col("contract_type", "str", "合同类型"),
            _col("amount", "money", "计税金额"),
            _col("is_rd_related", "bool", "研发相关"),
            _col("note", "str", "备注"),
            _col("created_at", "str", "创建时间"),
        ],
    },
    # 发票池（数电票 / 专票 / 普票，销项与进项）
    "invoices": {
        "file": "invoice_pool.csv",
        "prefix": "INV",
        "label": "发票池",
        "columns": [
            _col("id", "id", "编号"),
            _col("invoice_no", "str", "发票号码"),
            _col("invoice_code", "str", "发票代码"),
            _col("invoice_date", "date", "开票日期"),
            _col("invoice_type", "str", "发票类型"),
            _col("direction", "str", "方向"),
            _col("seller", "str", "销售方"),
            _col("buyer", "str", "购买方"),
            _col("amount_exclusive", "money", "不含税金额"),
            _col("tax_amount", "money", "税额"),
            _col("total_inclusive", "money", "价税合计"),
            _col("status", "str", "状态"),
            _col("source_file", "str", "来源文件"),
            _col("source_format", "str", "来源格式"),
            _col("raw_json", "json", "原始数据"),
            _col("note", "str", "备注"),
            _col("created_at", "str", "创建时间"),
        ],
    },
    # 银行流水池（收入 / 支出）
    "bank_transactions": {
        "file": "bank_transactions.csv",
        "prefix": "BTX",
        "label": "银行流水池",
        "columns": [
            _col("id", "id", "编号"),
            _col("transaction_date", "date", "交易日期"),
            _col("direction", "str", "收支方向"),
            _col("amount", "money", "金额"),
            _col("counterparty", "str", "对方户名"),
            _col("account_no", "str", "账号"),
            _col("summary", "str", "摘要"),
            _col("transaction_no", "str", "交易流水号"),
            _col("balance", "money", "余额"),
            _col("status", "str", "状态"),
            _col("source_file", "str", "来源文件"),
            _col("source_format", "str", "来源格式"),
            _col("note", "str", "备注"),
            _col("created_at", "str", "创建时间"),
        ],
    },
    # 发票 ↔ 流水 匹配建议
    "matches": {
        "file": "match_suggestions.csv",
        "prefix": "MAT",
        "label": "匹配建议",
        "columns": [
            _col("id", "id", "编号"),
            _col("invoice_id", "str", "发票编号"),
            _col("invoice_no", "str", "发票号码"),
            _col("transaction_id", "str", "流水编号"),
            _col("transaction_no", "str", "交易流水号"),
            _col("score", "int", "匹配分"),
            _col("match_type", "str", "匹配类型"),
            _col("status", "str", "状态"),
            _col("amount_diff", "money", "金额差异"),
            _col("date_diff", "int", "日期差异(天)"),
            _col("note", "str", "备注"),
            _col("created_at", "str", "创建时间"),
            _col("confirmed_at", "str", "确认时间"),
        ],
    },
    # 员工档案（P1-B 个税扣缴）
    "employees": {
        "file": "employees.csv",
        "prefix": "EMP",
        "label": "员工档案",
        "columns": [
            _col("id", "id", "编号"),
            _col("employee_no", "str", "工号"),
            _col("name", "str", "姓名"),
            _col("id_card_masked", "str", "身份证号(脱敏)"),
            _col("phone", "str", "手机号"),
            _col("department", "str", "部门"),
            _col("position", "str", "岗位"),
            _col("hire_date", "date", "入职日期"),
            _col("leave_date", "date", "离职日期"),
            _col("status", "str", "状态"),
            _col("social_insurance_base", "money", "社保缴费基数"),
            _col("housing_fund_base", "money", "公积金缴费基数"),
            _col("special_deduction_json", "json", "专项附加扣除明细"),
            _col("deduction_choice", "str", "减除费用方式"),
            _col("is_deduction_qualified", "bool", "是否扣除减除费用"),
            _col("note", "str", "备注"),
            _col("created_at", "str", "创建时间"),
            _col("updated_at", "str", "更新时间"),
        ],
    },
    # 月度工资表（P1-B 个税扣缴，含累计预扣计算结果快照）
    "payroll": {
        "file": "payroll.csv",
        "prefix": "PAY",
        "label": "月度工资表",
        "columns": [
            _col("id", "id", "编号"),
            _col("period", "period", "所属月份"),
            _col("employee_id", "str", "员工编号"),
            _col("employee_name", "str", "姓名"),
            _col("gross_salary", "money", "应发工资"),
            _col("bonus", "money", "奖金"),
            _col("allowance", "money", "补贴"),
            _col("social_insurance", "money", "个人社保"),
            _col("housing_fund", "money", "个人公积金"),
            _col("special_deduction", "money", "专项附加扣除"),
            _col("other_deduction", "money", "其他扣除"),
            _col("cumulative_income", "money", "累计收入"),
            _col("cumulative_basic_deduction", "money", "累计减除费用"),
            _col("cumulative_social", "money", "累计专项扣除"),
            _col("cumulative_special", "money", "累计专项附加扣除"),
            _col("cumulative_other", "money", "累计其他扣除"),
            _col("cumulative_taxable", "money", "累计应纳税所得额"),
            _col("cumulative_tax", "money", "累计应纳税额"),
            _col("tax_withheld", "money", "本期应预扣税额"),
            _col("net_pay", "money", "实发工资"),
            _col("zero_declaration", "bool", "零申报"),
            _col("status", "str", "状态"),
            _col("journal_entry_no", "str", "凭证号"),
            _col("note", "str", "备注"),
            _col("created_at", "str", "创建时间"),
            _col("updated_at", "str", "更新时间"),
        ],
    },
    # 企业所得税汇算：纳税调整清单（P1-C）
    "tax_adjustments": {
        "file": "tax_adjustments.csv",
        "prefix": "ADJ",
        "label": "纳税调整清单",
        "columns": [
            _col("id", "id", "编号"),
            _col("year", "str", "年度"),
            _col("item_type", "str", "调整项目"),
            _col("direction", "str", "方向"),
            _col("accounting_amount", "money", "会计金额"),
            _col("tax_amount", "money", "税法可扣除/确认金额"),
            _col("adjustment_amount", "money", "调整金额"),
            _col("basis", "str", "计算依据"),
            _col("source", "str", "政策依据"),
            _col("note", "str", "备注"),
            _col("created_at", "str", "创建时间"),
        ],
    },
    # 企业所得税汇算：亏损弥补台账（P1-C）
    "loss_carryforwards": {
        "file": "loss_carryforwards.csv",
        "prefix": "LOS",
        "label": "亏损弥补台账",
        "columns": [
            _col("id", "id", "编号"),
            _col("year", "str", "亏损年度"),
            _col("loss_amount", "money", "可弥补亏损额"),
            _col("used_amount", "money", "已弥补金额"),
            _col("remaining", "money", "剩余可弥补"),
            _col("expire_year", "str", "到期年度"),
            _col("note", "str", "备注"),
            _col("created_at", "str", "创建时间"),
            _col("updated_at", "str", "更新时间"),
        ],
    },
    # 企业所得税汇算：年度输入与测算快照（P1-C）
    "annual_settlements": {
        "file": "annual_settlements.csv",
        "prefix": "CIT",
        "label": "年度汇算输入",
        "columns": [
            _col("id", "id", "编号"),
            _col("year", "str", "年度"),
            _col("data_json", "json", "输入数据"),
            _col("status", "str", "状态"),
            _col("note", "str", "备注"),
            _col("created_at", "str", "创建时间"),
            _col("updated_at", "str", "更新时间"),
        ],
    },
    # 个税异议申诉台账（P1-E：离职多申报/员工投诉处理）
    "tax_disputes": {
        "file": "tax_disputes.csv",
        "prefix": "DSP",
        "label": "个税异议申诉台账",
        "columns": [
            _col("id", "id", "编号"),
            _col("employee_id", "str", "员工编号"),
            _col("employee_name", "str", "员工姓名"),
            _col("period", "str", "涉及月份"),
            _col("dispute_type", "str", "申诉类型"),
            _col("filed_date", "str", "申诉日期"),
            _col("status", "str", "处理状态"),
            _col("withdrawn_date", "str", "撤销日期"),
            _col("resolution", "str", "处理结果"),
            _col("note", "str", "备注"),
            _col("created_at", "str", "创建时间"),
            _col("updated_at", "str", "更新时间"),
        ],
    },
    # 离职纠错：勾选式进度清单（P2）
    "correction_progress": {
        "file": "correction_progress.csv",
        "prefix": "CPG",
        "label": "离职纠错进度",
        "columns": [
            _col("id", "id", "编号"),
            _col("employee_id", "str", "员工编号"),
            _col("employee_name", "str", "员工姓名"),
            _col("step_no", "str", "步骤序号"),
            _col("step_title", "str", "步骤内容"),
            _col("done", "str", "已完成"),
            _col("done_at", "str", "完成时间"),
            _col("updated_at", "str", "更新时间"),
        ],
    },
    # AI 问答历史 / 本地缓存 / 反馈（v1.14）
    "qa_history": {
        "file": "qa_history.csv",
        "prefix": "QA",
        "label": "AI 问答历史",
        "columns": [
            _col("id", "id", "编号"),
            _col("question", "str", "问题"),
            _col("normalized", "str", "规范化问题"),
            _col("answer", "str", "回答"),
            _col("intent", "str", "意图"),
            _col("source", "str", "来源"),
            _col("model", "str", "模型"),
            _col("tool_calls_json", "json", "工具调用"),
            _col("feedback", "str", "反馈"),
            _col("hit_count", "int", "命中次数"),
            _col("policy_version", "str", "政策版本"),
            _col("created_at", "str", "创建时间"),
            _col("updated_at", "str", "更新时间"),
        ],
    },
    # 会计期间（月结/季结状态机，P0-3）
    "periods": {
        "file": "periods.csv",
        "prefix": "PER",
        "label": "会计期间",
        "columns": [
            _col("id", "id", "编号"),
            _col("period", "period", "期间"),
            _col("period_type", "str", "期间类型"),
            _col("status", "str", "状态"),
            _col("closed_at", "str", "结账时间"),
            _col("closed_by", "str", "结账人"),
            _col("reopened_at", "str", "反结账时间"),
            _col("snapshot_hash", "str", "快照哈希"),
            _col("snapshot_json", "json", "快照数据"),
            _col("note", "str", "备注"),
            _col("created_at", "str", "创建时间"),
            _col("updated_at", "str", "更新时间"),
        ],
    },
    # 归档包（申报包 / 结账包，P0-3）
    "archives": {
        "file": "archives.csv",
        "prefix": "ARC",
        "label": "归档包",
        "columns": [
            _col("id", "id", "编号"),
            _col("period", "period", "期间"),
            _col("kind", "str", "类型"),
            _col("path", "str", "文件路径"),
            _col("file_hash", "str", "文件哈希"),
            _col("size_bytes", "int", "文件大小"),
            _col("manifest_json", "json", "清单"),
            _col("note", "str", "备注"),
            _col("created_at", "str", "创建时间"),
        ],
    },
    # 数据备份（P0-3）
    "backups": {
        "file": "backups.csv",
        "prefix": "BAK",
        "label": "数据备份",
        "columns": [
            _col("id", "id", "编号"),
            _col("path", "str", "文件路径"),
            _col("file_hash", "str", "文件哈希"),
            _col("size_bytes", "int", "文件大小"),
            _col("manifest_json", "json", "清单"),
            _col("note", "str", "备注"),
            _col("created_at", "str", "创建时间"),
        ],
    },
    # 申报记录（填报进度）
    "filings": {
        "file": "filing_records.csv",
        "prefix": "FIL",
        "label": "申报记录",
        "columns": [
            _col("id", "id", "编号"),
            _col("period", "period", "所属期间"),
            _col("form_id", "str", "报表编码"),
            _col("form_name", "str", "报表名称"),
            _col("status", "str", "状态"),
            _col("data_json", "json", "申报数据"),
            _col("created_at", "str", "创建时间"),
            _col("updated_at", "str", "更新时间"),
            _col("note", "str", "备注"),
        ],
    },
}

TABLE_NAMES: tuple[str, ...] = tuple(TABLE_DEFS)


# =============================================================================
# 规范化函数（供 CRUD 调用，返回可直接写库的字典）
# =============================================================================
def normalize_income(payload: dict[str, Any]) -> dict[str, Any]:
    """规范化收入台账记录；自动推导不含税金额与增值税额。"""
    invoice_type = _one_of(payload.get("invoice_type"), INVOICE_TYPES, "普票", "发票类型")
    inclusive = money(payload.get("inclusive_amount"))
    if inclusive < 0:
        raise ValueError("含税金额不能为负数")
    exclusive = payload.get("exclusive_amount")
    vat = payload.get("vat_amount")
    if exclusive in (None, ""):
        exclusive, vat = derive_exclusive(inclusive)
    else:
        exclusive = money(exclusive)
        vat = money(vat if vat not in (None, "") else inclusive - exclusive)
    return {
        "date": normalize_date(payload.get("date")),
        "client": str(payload.get("client") or "").strip(),
        "description": str(payload.get("description") or "").strip(),
        "category": _one_of(payload.get("category"), INCOME_CATEGORIES, "技术服务", "业务类别"),
        "invoice_type": invoice_type,
        "invoice_no": str(payload.get("invoice_no") or "").strip(),
        "inclusive_amount": inclusive,
        "exclusive_amount": exclusive,
        "vat_amount": vat,
        "is_rd_related": parse_bool(payload.get("is_rd_related")),
        "note": str(payload.get("note") or "").strip(),
    }


def normalize_expense(payload: dict[str, Any]) -> dict[str, Any]:
    """规范化费用台账记录（小规模纳税人进项不可抵扣，金额即成本）。"""
    amount = money(payload.get("amount"))
    if amount < 0:
        raise ValueError("费用金额不能为负数")
    return {
        "date": normalize_date(payload.get("date")),
        "description": str(payload.get("description") or "").strip(),
        "category": _one_of(payload.get("category"), EXPENSE_CATEGORIES, "办公", "费用类别"),
        "amount": amount,
        "is_rd_related": parse_bool(payload.get("is_rd_related")),
        "vendor": str(payload.get("vendor") or "").strip(),
        "receipt_no": str(payload.get("receipt_no") or "").strip(),
        "note": str(payload.get("note") or "").strip(),
    }


def normalize_rd(payload: dict[str, Any]) -> dict[str, Any]:
    """规范化研发辅助账记录；合计 = 各费用项之和。"""
    record = {
        "project_name": str(payload.get("project_name") or "研发项目").strip(),
        "period": normalize_period(payload.get("period"), default="") or normalize_period(date.today()),
        "personnel_cost": money(payload.get("personnel_cost")),
        "material_cost": money(payload.get("material_cost")),
        "depreciation": money(payload.get("depreciation")),
        "amortization": money(payload.get("amortization")),
        "design_cost": money(payload.get("design_cost")),
        "other_cost": money(payload.get("other_cost")),
        "note": str(payload.get("note") or "").strip(),
    }
    record["total"] = money(
        sum(record[key] for key in ("personnel_cost", "material_cost", "depreciation", "amortization", "design_cost", "other_cost"))
    )
    record["deduction_amount"] = money(payload.get("deduction_amount", record["total"]))
    return record


def normalize_contract(payload: dict[str, Any]) -> dict[str, Any]:
    """规范化合同台账记录。"""
    return {
        "date": normalize_date(payload.get("date")),
        "contract_no": str(payload.get("contract_no") or "").strip(),
        "counterparty": str(payload.get("counterparty") or "").strip(),
        "contract_type": _one_of(payload.get("contract_type"), CONTRACT_TYPES, "技术合同", "合同类型"),
        "amount": money(payload.get("amount")),
        "is_rd_related": parse_bool(payload.get("is_rd_related")),
        "note": str(payload.get("note") or "").strip(),
    }


def normalize_journal_entry(payload: dict[str, Any]) -> dict[str, Any]:
    """规范化凭证头。"""
    return {
        "date": normalize_date(payload.get("date")),
        "entry_no": str(payload.get("entry_no") or "").strip(),
        "description": str(payload.get("description") or "").strip(),
        "total_amount": money(payload.get("total_amount")),
        "source_ref": str(payload.get("source_ref") or "").strip(),
    }


def normalize_journal_line(payload: dict[str, Any]) -> dict[str, Any]:
    """规范化分录行（方向仅允许 借/贷）。"""
    direction = str(payload.get("direction") or "借").strip()
    if direction not in DIRECTIONS:
        raise ValueError(f"分录方向「{direction}」不合法，应为 借 或 贷")
    return {
        "entry_no": str(payload.get("entry_no") or "").strip(),
        "date": normalize_date(payload.get("date")),
        "description": str(payload.get("description") or "").strip(),
        "account": str(payload.get("account") or "").strip(),
        "direction": direction,
        "amount": money(payload.get("amount")),
        "line_no": int(payload.get("line_no") or 0),
    }


def normalize_filing(payload: dict[str, Any]) -> dict[str, Any]:
    """规范化申报记录。"""
    status = str(payload.get("status") or "草稿").strip()
    if status not in FILING_STATUSES:
        status = "草稿"
    data = payload.get("data_json") or {}
    if not isinstance(data, (dict, list)):
        raise ValueError("申报数据必须是 dict 或 list")
    return {
        "period": normalize_period(payload.get("period"), default=""),
        "form_id": str(payload.get("form_id") or "").strip(),
        "form_name": str(payload.get("form_name") or "").strip(),
        "status": status,
        "data_json": data,
        "note": str(payload.get("note") or "").strip(),
    }


# =============================================================================
# 数据类（类型化访问，可选使用；存储层以 dict 为准）
# =============================================================================
@dataclass
class IncomeRecord:
    date: str
    client: str
    inclusive_amount: float
    description: str = ""
    category: str = "技术服务"
    invoice_type: str = "普票"
    invoice_no: str = ""
    exclusive_amount: float = 0.0
    vat_amount: float = 0.0
    is_rd_related: bool = False
    note: str = ""
    id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExpenseRecord:
    date: str
    description: str
    amount: float
    category: str = "办公"
    is_rd_related: bool = False
    vendor: str = ""
    receipt_no: str = ""
    note: str = ""
    id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JournalEntry:
    date: str
    description: str
    lines: list[dict[str, Any]] = field(default_factory=list)
    entry_no: str = ""
    source_ref: str = ""

    @property
    def total_amount(self) -> float:
        return money(sum(line.get("amount", 0) for line in self.lines if line.get("direction") == "借"))
