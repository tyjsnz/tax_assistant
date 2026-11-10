"""记账凭证生成与登记（一借多贷），以及凭证查询、试算平衡。

凭证模型
--------
* 凭证头写 ``journal_entries``，分录行写 ``journal_lines``，通过 ``entry_no`` 关联。
* 凭证号格式 ``记-YYYYMM-0001``，按月顺序编号。
* 每一张凭证在登记前都校验**借贷平衡**（差额 > 0.005 元即拒绝），保证试算平衡。

常用场景
--------
* 收入：借 银行存款（未开票为应收账款），贷 主营业务收入 + 应交税费—应交增值税。
* 费用：借 管理费用/研发支出，贷 银行存款 / 库存现金 / 应付账款。
* 工资计提：借 管理费用—工资薪金，贷 应付职工薪酬 + 代扣个税 + 社保。
* 季度末：免征增值税转其他收益；缴纳增值税及附加税费。
"""

from __future__ import annotations

from typing import Any

from engine.money import round2
from engine.tax_calendar import period_label, quarter_range
from ledger import locks, models
from ledger.audit import log_journal, log_user_action
from ledger.income_ledger import period_totals as income_period_totals
from ledger.store import table

VOUCHER_TYPES: tuple[str, ...] = ("收入", "费用", "工资", "计提", "结转", "其他")

TABLE_ENTRIES = "journal_entries"
TABLE_LINES = "journal_lines"

#: 收入收款方式：未开票默认计入应收账款，其余计入银行存款
_INCOME_DEBIT_ACCOUNT = {"未开票": "应收账款"}
_INCOME_DEBIT_DEFAULT = "银行存款"

#: 费用类别 → 会计科目（非研发）
_EXPENSE_ACCOUNT = {
    "工资": "管理费用—工资薪金",
    "社保": "管理费用—社会保险费",
    "房租": "管理费用—房租",
    "办公": "管理费用—办公费",
    "差旅": "管理费用—差旅费",
    "软件订阅": "管理费用—软件服务费",
    "研发材料": "研发支出—直接投入",
    "折旧": "管理费用—折旧费",
    "其他": "管理费用—其他",
}

#: 研发相关费用类别 → 研发支出明细科目
_RD_EXPENSE_ACCOUNT = {
    "工资": "研发支出—人员人工",
    "社保": "研发支出—人员人工",
    "研发材料": "研发支出—直接投入",
    "折旧": "研发支出—折旧",
    "软件订阅": "研发支出—无形资产摊销",
    "差旅": "研发支出—其他相关费用",
    "办公": "研发支出—其他相关费用",
    "其他": "研发支出—其他相关费用",
    "房租": "管理费用—房租",
}

#: 付款方式 → 贷方科目
_PAY_ACCOUNT = {
    "bank": "银行存款",
    "cash": "库存现金",
    "credit": "应付账款",
    "银行存款": "银行存款",
    "库存现金": "库存现金",
    "应付账款": "应付账款",
}


# =============================================================================
# 工具
# =============================================================================
def _line(account: str, direction: str, amount: Any) -> dict[str, Any]:
    """构造一条分录行。"""
    if direction not in models.DIRECTIONS:
        raise ValueError(f"分录方向「{direction}」不合法，应为 借 或 贷")
    if not str(account or "").strip():
        raise ValueError("会计科目不能为空")
    return {"account": str(account).strip(), "direction": direction, "amount": round2(amount)}


def _entry(
    date: Any,
    description: str,
    lines: list[dict[str, Any]],
    *,
    voucher_type: str = "其他",
    source_ref: str = "",
) -> dict[str, Any]:
    """构造一张凭证（未登记），返回 ``build_*`` 函数约定的结构。"""
    if voucher_type not in VOUCHER_TYPES:
        raise ValueError(f"凭证类型「{voucher_type}」不合法，可选：{'、'.join(VOUCHER_TYPES)}")
    return {
        "date": models.normalize_date(date),
        "description": str(description or "").strip(),
        "source_ref": str(source_ref or "").strip(),
        "voucher_type": voucher_type,
        "lines": lines,
    }


def _month_prefix(date_text: str) -> str:
    return f"记-{str(date_text)[:7].replace('-', '')}-"


# =============================================================================
# 凭证生成
# =============================================================================
def build_income_entry(income_record: dict[str, Any]) -> dict[str, Any]:
    """根据一笔收入台账记录生成确认收入的凭证。

    借：银行存款 / 应收账款（含税金额）
    贷：主营业务收入（不含税金额）+ 应交税费—应交增值税（税额）
    """
    if not isinstance(income_record, dict):
        raise ValueError("收入记录必须是 dict")
    inclusive = round2(income_record.get("inclusive_amount"))
    exclusive = round2(income_record.get("exclusive_amount"))
    if inclusive > 0 and exclusive <= 0:
        exclusive, _ = models.derive_exclusive(inclusive)
    exclusive = min(exclusive, inclusive)
    vat = round2(inclusive - exclusive)
    invoice_type = str(income_record.get("invoice_type") or "普票")
    debit_account = _INCOME_DEBIT_ACCOUNT.get(invoice_type, _INCOME_DEBIT_DEFAULT)

    lines = [_line(debit_account, "借", inclusive), _line("主营业务收入", "贷", exclusive)]
    if vat > 0:
        lines.append(_line("应交税费—应交增值税", "贷", vat))
    client = str(income_record.get("client") or "").strip()
    description = str(income_record.get("description") or "").strip()
    summary = "确认收入"
    if client:
        summary += f"：{client}"
    if description:
        summary += f"（{description}）"
    return _entry(
        income_record.get("date"),
        summary,
        lines,
        voucher_type="收入",
        source_ref=str(income_record.get("id") or ""),
    )


def build_expense_entry(expense_record: dict[str, Any], pay_method: str = "bank") -> dict[str, Any]:
    """根据一笔费用记录生成费用凭证。

    借：管理费用—XX / 研发支出—XX
    贷：银行存款（默认）/ 库存现金 / 应付账款
    """
    if not isinstance(expense_record, dict):
        raise ValueError("费用记录必须是 dict")
    category = str(expense_record.get("category") or "其他")
    amount = round2(expense_record.get("amount"))
    if category not in _EXPENSE_ACCOUNT:
        category = "其他"
    if models.parse_bool(expense_record.get("is_rd_related")):
        debit_account = _RD_EXPENSE_ACCOUNT.get(category, "研发支出—其他相关费用")
    else:
        debit_account = _EXPENSE_ACCOUNT[category]
    pay_key = str(pay_method or "bank").strip()
    credit_account = _PAY_ACCOUNT.get(pay_key)
    if credit_account is None:
        raise ValueError(f"付款方式「{pay_method}」不合法，可选：bank / cash / credit")
    description = str(expense_record.get("description") or "").strip() or f"{category}支出"
    lines = [_line(debit_account, "借", amount), _line(credit_account, "贷", amount)]
    return _entry(
        expense_record.get("date"),
        f"支付{description}",
        lines,
        voucher_type="费用",
        source_ref=str(expense_record.get("id") or ""),
    )


def build_salary_entry(
    amount: float,
    date: Any = None,
    description: str = "计提工资",
    personal_tax: float = 0.0,
    social_insurance: float = 0.0,
) -> dict[str, Any]:
    """生成工资计提凭证：借 管理费用—工资薪金，贷 应付职工薪酬 + 代扣个税 + 个人社保。"""
    total = round2(amount)
    if total <= 0:
        raise ValueError("工资计提金额必须大于 0")
    tax = round2(personal_tax)
    social = round2(social_insurance)
    if tax < 0 or social < 0:
        raise ValueError("代扣个税与社保金额不能为负数")
    payable = round2(total - tax - social)
    if payable < 0:
        raise ValueError("代扣个税与社保合计不能大于工资总额")
    lines = [_line("管理费用—工资薪金", "借", total)]
    if payable > 0:
        lines.append(_line("应付职工薪酬—工资", "贷", payable))
    if tax > 0:
        lines.append(_line("应交税费—应交个人所得税", "贷", tax))
    if social > 0:
        lines.append(_line("其他应付款—社会保险费", "贷", social))
    return _entry(date, description, lines, voucher_type="工资")


def build_vat_exemption_entry(period: Any) -> dict[str, Any] | None:
    """生成季度末「免征增值税转其他收益」凭证；本期无免征时返回 ``None``。

    金额取台账中普票+未开票部分已计提的增值税额，保证与收入凭证的贷方金额勾稽。
    """
    text = models.normalize_period(period, default="")
    try:
        start, end = quarter_range(text)
    except ValueError as exc:
        raise ValueError(f"期间格式不正确：{period}，请使用 2026Q1 形式") from exc
    from engine import calc_vat

    totals = income_period_totals(text)
    calc = calc_vat(totals["total_inclusive"], totals["special_inclusive"], period=text)
    # 超过季度免税额度时全部销售额均应纳税，不存在免征增值税结转
    if not calc["within_exemption_limit"] or float(calc["vat_exempt"]) <= 0:
        return None
    exempt_vat = 0.0
    for row in table("income").filter(start=start.isoformat(), end=end.isoformat()):
        if str(row.get("invoice_type") or "普票") != "专票":
            exempt_vat = round2(exempt_vat + round2(row.get("vat_amount")))
    if exempt_vat <= 0:
        return None
    return _entry(
        end.isoformat(),
        f"免征增值税转其他收益（{period_label(text)}）",
        [
            _line("应交税费—应交增值税", "借", exempt_vat),
            _line("其他收益", "贷", exempt_vat),
        ],
        voucher_type="结转",
        source_ref=f"VAT-EXEMPT-{text}",
    )


def build_vat_payment_entry(period: Any) -> dict[str, Any] | None:
    """生成季度末缴纳增值税及附加税费的凭证；本期无需缴纳时返回 ``None``。"""
    text = models.normalize_period(period, default="")
    try:
        _, end = quarter_range(text)
    except ValueError as exc:
        raise ValueError(f"期间格式不正确：{period}，请使用 2026Q1 形式") from exc
    from engine import calc_vat

    totals = income_period_totals(text)
    calc = calc_vat(totals["total_inclusive"], totals["special_inclusive"], period=text)
    vat = round2(calc["vat_payable"])
    surtax = round2(calc["surtax"])
    total = round2(vat + surtax)
    if total <= 0:
        return None
    lines = []
    if vat > 0:
        lines.append(_line("应交税费—应交增值税", "借", vat))
    if surtax > 0:
        lines.append(_line("税金及附加", "借", surtax))
    lines.append(_line("银行存款", "贷", total))
    return _entry(
        end.isoformat(),
        f"缴纳增值税及附加税费（{period_label(text)}）",
        lines,
        voucher_type="其他",
        source_ref=f"VAT-PAY-{text}",
    )


# =============================================================================
# 凭证登记
# =============================================================================
def next_entry_no(date: Any = None) -> str:
    """生成下一个凭证号，如 ``记-202601-0001``。"""
    date_text = models.normalize_date(date)
    prefix = _month_prefix(date_text)
    max_seq = 0
    for row in table(TABLE_ENTRIES).read():
        entry_no = str(row.get("entry_no") or "")
        if not entry_no.startswith(prefix):
            continue
        tail = entry_no[len(prefix):]
        if tail.isdigit():
            max_seq = max(max_seq, int(tail))
    return f"{prefix}{max_seq + 1:04d}"


def post_entry(entry: dict[str, Any], *, check_lock: bool = True) -> dict[str, Any]:
    """登记一张凭证（写凭证头与分录行），返回登记后的完整凭证。

    校验：至少一行分录、科目非空、金额非负、借贷平衡、凭证号不重复。
    ``check_lock=False`` 时跳过期间锁校验，仅供结账模块生成/删除结转凭证内部使用。
    """
    if not isinstance(entry, dict):
        raise ValueError("凭证必须是 dict")
    date_text = models.normalize_date(entry.get("date"))
    if check_lock:
        locks.ensure_open(date_text, action="生成凭证")
    description = str(entry.get("description") or "").strip()
    voucher_type = str(entry.get("voucher_type") or "其他").strip()
    if voucher_type not in VOUCHER_TYPES:
        raise ValueError(f"凭证类型「{voucher_type}」不合法，可选：{'、'.join(VOUCHER_TYPES)}")
    raw_lines = entry.get("lines") or []
    if not raw_lines:
        raise ValueError("凭证至少需要一行分录")
    lines: list[dict[str, Any]] = []
    for raw_line in raw_lines:
        line = models.normalize_journal_line(
            {
                "entry_no": "",
                "date": date_text,
                "description": description,
                "account": raw_line.get("account"),
                "direction": raw_line.get("direction", "借"),
                "amount": raw_line.get("amount"),
                "line_no": 0,
            }
        )
        if not line["account"]:
            raise ValueError("会计科目不能为空")
        if line["amount"] < 0:
            raise ValueError("分录金额不能为负数")
        lines.append(line)
    debit_total = round2(sum(line["amount"] for line in lines if line["direction"] == "借"))
    credit_total = round2(sum(line["amount"] for line in lines if line["direction"] == "贷"))
    if debit_total == 0 and credit_total == 0:
        raise ValueError("凭证金额不能为 0")
    if abs(debit_total - credit_total) > 0.005:
        raise ValueError(f"分录借贷不平衡：借方 {debit_total:.2f} 元，贷方 {credit_total:.2f} 元")
    total_amount = round2(max(debit_total, credit_total))

    entry_no = str(entry.get("entry_no") or "").strip() or next_entry_no(date_text)
    existing = {str(row.get("entry_no") or "") for row in table(TABLE_ENTRIES).read()}
    if entry_no in existing:
        raise ValueError(f"凭证号 {entry_no} 已存在，请更换凭证号或以空值自动生成")

    header = table(TABLE_ENTRIES).append(
        {
            "date": date_text,
            "entry_no": entry_no,
            "description": description,
            "total_amount": total_amount,
            "source_ref": str(entry.get("source_ref") or "").strip(),
        }
    )
    line_records = []
    for index, line in enumerate(lines, start=1):
        line_records.append({**line, "entry_no": entry_no, "line_no": index})
    added_lines = table(TABLE_LINES).append_many(line_records)
    posted = {
        "id": header.get("id", ""),
        "date": header.get("date", date_text),
        "entry_no": entry_no,
        "description": description,
        "total_amount": total_amount,
        "source_ref": header.get("source_ref", ""),
        "voucher_type": voucher_type,
        "created_at": header.get("created_at", ""),
        "lines": [
            {
                "account": line["account"],
                "direction": line["direction"],
                "amount": line["amount"],
                "line_no": line["line_no"],
            }
            for line in added_lines
        ],
    }
    log_journal([posted])
    return posted


def post_entries(
    entries: list[dict[str, Any]], *, check_lock: bool = True
) -> list[dict[str, Any]]:
    """批量登记凭证（逐张校验并写入），返回已登记凭证列表。"""
    posted = []
    for entry in entries or []:
        posted.append(post_entry(entry, check_lock=check_lock))
    if posted:
        log_user_action("批量生成分录", f"共登记 {len(posted)} 张凭证")
    return posted


# =============================================================================
# 查询与试算平衡
# =============================================================================
def list_entries(start: Any = None, end: Any = None) -> list[dict[str, Any]]:
    """查询凭证（含分录行），按日期、凭证号升序。"""
    headers = table(TABLE_ENTRIES).filter(start=start, end=end)
    lines = table(TABLE_LINES).filter(start=start, end=end)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for line in lines:
        grouped.setdefault(str(line.get("entry_no") or ""), []).append(line)
    result = []
    for header in headers:
        entry_no = str(header.get("entry_no") or "")
        entry_lines = sorted(grouped.get(entry_no, []), key=lambda line: int(line.get("line_no") or 0))
        result.append(
            {
                **header,
                "lines": [
                    {
                        "account": line.get("account", ""),
                        "direction": line.get("direction", ""),
                        "amount": line.get("amount", 0.0),
                        "line_no": line.get("line_no", 0),
                    }
                    for line in entry_lines
                ],
            }
        )
    return sorted(result, key=lambda row: (str(row.get("date") or ""), str(row.get("entry_no") or "")))


def get_entry(entry_no: str) -> dict[str, Any] | None:
    """按凭证号读取一张凭证（含分录行），不存在返回 ``None``。"""
    entry_no = str(entry_no or "").strip()
    if not entry_no:
        return None
    header = None
    for row in table(TABLE_ENTRIES).read():
        if str(row.get("entry_no") or "") == entry_no:
            header = row
            break
    if header is None:
        return None
    lines = [line for line in table(TABLE_LINES).read() if str(line.get("entry_no") or "") == entry_no]
    lines.sort(key=lambda line: int(line.get("line_no") or 0))
    return {
        **header,
        "lines": [
            {
                "account": line.get("account", ""),
                "direction": line.get("direction", ""),
                "amount": line.get("amount", 0.0),
                "line_no": line.get("line_no", 0),
            }
            for line in lines
        ],
    }


def trial_balance(start: Any = None, end: Any = None) -> dict[str, Any]:
    """试算平衡：按会计科目汇总借贷发生额与余额，并给出是否平衡。"""
    rows = table(TABLE_LINES).filter(start=start, end=end)
    accounts: dict[str, dict[str, float]] = {}
    total_debit = 0.0
    total_credit = 0.0
    for row in rows:
        account = str(row.get("account") or "（未填科目）")
        amount = round2(row.get("amount"))
        bucket = accounts.setdefault(account, {"debit": 0.0, "credit": 0.0})
        direction = str(row.get("direction") or "借")
        if direction == "借":
            bucket["debit"] = round2(bucket["debit"] + amount)
            total_debit = round2(total_debit + amount)
        else:
            bucket["credit"] = round2(bucket["credit"] + amount)
            total_credit = round2(total_credit + amount)
    account_list = [
        {
            "account": account,
            "debit": values["debit"],
            "credit": values["credit"],
            "balance": round2(values["debit"] - values["credit"]),
        }
        for account, values in accounts.items()
    ]
    account_list.sort(key=lambda item: item["account"])
    return {
        "accounts": account_list,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "balanced": abs(total_debit - total_credit) <= 0.005,
    }


__all__ = [
    "VOUCHER_TYPES",
    "build_income_entry",
    "build_expense_entry",
    "build_salary_entry",
    "build_vat_exemption_entry",
    "build_vat_payment_entry",
    "next_entry_no",
    "post_entry",
    "post_entries",
    "list_entries",
    "get_entry",
    "trial_balance",
]
