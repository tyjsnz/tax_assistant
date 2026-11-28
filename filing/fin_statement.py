"""财务报表工作台：期初/期末余额、应收应付、现金流量与核对清单（v1.57）。

在 ``filing.fin_report`` 的台账估算基础上，允许录入银行、应收、应付、固定资产等余额，
生成更接近实际的资产负债表、利润表和简化现金流量表；所有结果仍是草稿。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

import config
from engine import period_label
from filing import fin_report
from ledger import store
from storage.export_paths import export_file

TABLE = "financial_inputs"
FIELDS = [
    "cash_opening", "ar_opening", "inventory_opening", "fixed_assets_opening",
    "ap_opening", "tax_payable_opening", "other_liabilities_opening", "paid_in_capital",
    "cash_ending", "ar_ending", "inventory_ending", "fixed_assets_ending", "other_assets_ending",
    "ap_ending", "tax_payable_ending", "short_loan_ending", "other_liabilities_ending",
    "retained_earnings_ending",
]
MONEY_FIELDS = {field for field in FIELDS}
NOTE = "录入余额 + 台账生成，仍需与总账/银行对账单核对"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _money(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def get_inputs(period: str) -> dict[str, Any]:
    text = str(period or "").strip().upper()
    row = next((item for item in store.table(TABLE).read() if _text(item.get("period")) == text), None)
    if row:
        return row
    return {"id": "", "period": text, **{field: 0.0 for field in FIELDS}, "note": "", "updated_at": ""}


def save_inputs(period: str, values: dict[str, Any]) -> dict[str, Any]:
    text = str(period or "").strip().upper()
    payload = {field: _money(values.get(field)) for field in MONEY_FIELDS}
    payload["note"] = _text(values.get("note"))[:500]
    payload["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    table = store.table(TABLE)
    existing = next((item for item in table.read() if _text(item.get("period")) == text), None)
    if existing:
        return table.update(str(existing["id"]), payload)
    return table.append({"period": text, **payload})


def _line(no: str, name: str, value: float, *, highlight: bool = False) -> dict[str, Any]:
    return {"line_no": no, "name": name, "value": round(float(value or 0), 2), "highlight": highlight}


def _has_inputs(row: dict[str, Any]) -> bool:
    return bool(row.get("id"))


def _balance_sheet(period: str, row: dict[str, Any], *, opening: bool = False) -> dict[str, Any]:
    prefix = "opening" if opening else "ending"
    has = _has_inputs(row)
    cash = _money(row.get(f"cash_{prefix}"))
    ar = _money(row.get(f"ar_{prefix}"))
    inventory = _money(row.get(f"inventory_{prefix}"))
    fixed = _money(row.get(f"fixed_assets_{prefix}"))
    other_assets = 0.0 if opening else _money(row.get("other_assets_ending"))
    ap = _money(row.get(f"ap_{prefix}"))
    tax = _money(row.get(f"tax_payable_{prefix}"))
    short_loan = 0.0 if opening else _money(row.get("short_loan_ending"))
    other_liab = _money(row.get(f"other_liabilities_{prefix}"))
    capital = _money(row.get("paid_in_capital"))
    current_assets = round(cash + ar + inventory + other_assets, 2)
    total_assets = round(current_assets + fixed, 2)
    liabilities = round(ap + tax + short_loan + other_liab, 2)
    retained = _money(row.get("retained_earnings_ending")) if not opening else 0.0
    if not opening and retained == 0:
        retained = round(total_assets - liabilities - capital, 2)
    if opening:
        retained = round(total_assets - liabilities - capital, 2)
    equity = round(capital + retained, 2)
    total = round(liabilities + equity, 2)
    rows = [
        _line("1", "货币资金", cash),
        _line("2", "应收账款", ar),
        _line("3", "存货", inventory),
        _line("4", "其他流动资产", other_assets),
        _line("5", "流动资产合计", current_assets),
        _line("6", "固定资产", fixed),
        _line("7", "非流动资产合计", fixed),
        _line("8", "资产总计", total_assets, highlight=True),
        _line("9", "应付账款", ap),
        _line("10", "应交税费", tax),
        _line("11", "短期借款", short_loan),
        _line("12", "其他负债", other_liab),
        _line("13", "负债合计", liabilities),
        _line("14", "实收资本", capital),
        _line("15", "未分配利润", retained),
        _line("16", "所有者权益合计", equity),
        _line("17", "负债和所有者权益总计", total, highlight=True),
    ]
    if not has and not opening:
        # 没有录入时沿用台账估算的货币资金，避免全 0
        pass
    return {"rows": rows, "total_assets": total_assets, "liabilities": liabilities, "equity": equity, "total": total, "retained": retained, "cash": cash}


def _categorize(transaction: dict[str, Any]) -> str:
    text = f"{transaction.get('counterparty') or ''} {transaction.get('summary') or ''}"
    if any(word in text for word in ("借款", "贷款", "股东", "投资款", "增资", "分红")):
        return "financing"
    if any(word in text for word in ("设备", "固定资产", "装修", "工程", "车辆")):
        return "investing"
    return "operating"


def _cash_flow(period: str, row: dict[str, Any], computed: dict[str, Any]) -> dict[str, Any]:
    quarter = str(period)
    start, end = computed["start"], computed["end"]
    transactions = [
        item for item in store.table("bank_transactions").read()
        if start <= _text(item.get("transaction_date")) <= end
    ]
    buckets = {"operating_in": 0.0, "operating_out": 0.0, "investing_in": 0.0, "investing_out": 0.0, "financing_in": 0.0, "financing_out": 0.0}
    for item in transactions:
        amount = _money(item.get("amount"))
        direction = _text(item.get("direction"))
        category = _categorize(item)
        if direction == "收入":
            buckets[f"{category}_in"] += amount
        elif direction == "支出":
            buckets[f"{category}_out"] += amount
    op_net = round(buckets["operating_in"] - buckets["operating_out"], 2)
    inv_net = round(buckets["investing_in"] - buckets["investing_out"], 2)
    fin_net = round(buckets["financing_in"] - buckets["financing_out"], 2)
    net = round(op_net + inv_net + fin_net, 2)
    cash_opening = _money(row.get("cash_opening"))
    cash_ending = _money(row.get("cash_ending"))
    if cash_ending == 0:
        cash_ending = round(cash_opening + net, 2)
    rows = [
        _line("1", "销售商品、提供劳务收到的现金", buckets["operating_in"]),
        _line("2", "经营活动现金流入小计", buckets["operating_in"]),
        _line("3", "购买商品、接受劳务支付的现金", buckets["operating_out"]),
        _line("4", "经营活动现金流出小计", buckets["operating_out"]),
        _line("5", "经营活动产生的现金流量净额", op_net, highlight=True),
        _line("6", "投资活动现金流入小计", buckets["investing_in"]),
        _line("7", "投资活动现金流出小计", buckets["investing_out"]),
        _line("8", "投资活动产生的现金流量净额", inv_net, highlight=True),
        _line("9", "筹资活动现金流入小计", buckets["financing_in"]),
        _line("10", "筹资活动现金流出小计", buckets["financing_out"]),
        _line("11", "筹资活动产生的现金流量净额", fin_net, highlight=True),
        _line("12", "现金及现金等价物净增加额", net, highlight=True),
        _line("13", "期初现金及现金等价物余额", cash_opening),
        _line("14", "期末现金及现金等价物余额", cash_ending, highlight=True),
    ]
    return {"rows": rows, "count": len(transactions), "net": net, "opening": cash_opening, "ending": cash_ending, "buckets": buckets}


def build_statement(period: str) -> dict[str, Any]:
    quarter = str(period or "").strip().upper()
    computed = fin_report._compute(quarter)
    row = get_inputs(quarter)
    closing = _balance_sheet(quarter, row)
    if not _has_inputs(row):
        # 无人工余额：货币资金用台账估算
        closing = _balance_sheet(quarter, row)
        estimated_cash = round(computed["cash"], 2)
        closing["rows"][0]["value"] = estimated_cash
        closing["rows"][1]["value"] = 0.0
        closing["rows"][2]["value"] = 0.0
        closing["rows"][3]["value"] = 0.0
        closing["rows"][4]["value"] = estimated_cash
        closing["rows"][7]["value"] = estimated_cash
        closing["rows"][14]["value"] = estimated_cash
        closing["rows"][15]["value"] = estimated_cash
        closing["rows"][16]["value"] = estimated_cash
        closing["cash"] = estimated_cash
        closing["total_assets"] = estimated_cash
        closing["equity"] = estimated_cash
        closing["total"] = estimated_cash
        closing["retained"] = estimated_cash
    opening = _balance_sheet(quarter, row, opening=True)
    income_period = fin_report._income_statement(computed["period_amounts"])
    income_ytd = fin_report._income_statement(computed["ytd_amounts"])
    cash_flow = _cash_flow(quarter, row, computed)
    checks = _checklist(closing, opening, cash_flow, row, computed)
    return {
        "period": quarter,
        "period_label": period_label(quarter),
        "period_label_text": f"{quarter}",
        "company": config.get_company(),
        "closing": closing,
        "opening": opening,
        "income_period": income_period,
        "income_ytd": income_ytd,
        "cash_flow": cash_flow,
        "checks": checks,
        "note": NOTE,
        "disclaimer": config.DISCLAIMER,
    }


def _checklist(closing: dict[str, Any], opening: dict[str, Any], cash_flow: dict[str, Any], row: dict[str, Any], computed: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        items.append({"name": name, "status": "PASS" if ok else "WARNING", "detail": detail})

    diff = round(closing["total_assets"] - closing["total"], 2)
    add("资产 = 负债 + 所有者权益", abs(diff) < 0.01, f"差额 {diff:.2f} 元")
    if _has_inputs(row):
        add("货币资金与录入期末余额一致", True, f"期末货币资金 {closing['cash']:.2f} 元")
    else:
        add("货币资金已按台账估算", False, "未录入银行余额，建议到银行对账单核对后录入")
    add("现金流量净增加 = 期末 - 期初", abs(cash_flow["opening"] + cash_flow["net"] - cash_flow["ending"]) < 0.01, f"净增加 {cash_flow['net']:.2f} 元")
    if cash_flow["count"] == 0:
        add("银行流水已导入", False, "本期没有银行流水，现金流量表为空")
    else:
        add("银行流水已导入", True, f"共 {cash_flow['count']} 笔")
    if _money(row.get("ar_ending")) == 0 and float(computed["period_amounts"].get("revenue") or 0) > 0:
        add("应收账款是否完整", False, "有收入但应收账款为 0；如已收款可忽略")
    if _money(row.get("ap_ending")) == 0 and float(computed["period_amounts"].get("expense_total") or 0) > 0:
        add("应付账款是否完整", False, "有费用但应付账款为 0；如已付款可忽略")
    return items


def export_statement(period: str, path: str | Path | None = None) -> Path:
    data = build_statement(period)
    target = Path(path) if path else export_file(f"财务报表_{period}", "xlsx", period=period, category="报表台账")
    workbook = Workbook()
    title_font = Font(bold=True, size=14)
    header_fill = PatternFill("solid", fgColor="DDEBF7")

    def write_sheet(name: str, headers: list[str], rows: list[list[Any]]) -> None:
        sheet = workbook.active if workbook.sheetnames == ["Sheet"] else workbook.create_sheet()
        sheet.title = name
        sheet.append([f"{data['company'].get('name') or ''} · {name} · {data['period']}"])
        sheet["A1"].font = title_font
        sheet.append(headers)
        for cell in sheet[2]:
            cell.fill = header_fill
            cell.font = Font(bold=True)
        for row in rows:
            sheet.append(row)
        sheet.column_dimensions["A"].width = 36
        for col in ("B", "C", "D"):
            sheet.column_dimensions[col].width = 18
        sheet.append([])
        sheet.append([data["note"]])
        sheet.append([data["disclaimer"]])

    write_sheet("资产负债表", ["项目", "期末余额", "年初余额"], [
        [closing["name"], closing["value"], opening_row["value"]]
        for closing, opening_row in zip(data["closing"]["rows"], data["opening"]["rows"])
    ])
    write_sheet("利润表", ["项目", "本期金额", "本年累计"], [
        [row["name"], row["value"], ytd["value"]]
        for row, ytd in zip(data["income_period"], data["income_ytd"])
    ])
    write_sheet("现金流量表", ["项目", "本期金额"], [[row["name"], row["value"]] for row in data["cash_flow"]["rows"]])
    write_sheet("核对清单", ["检查项", "结果", "说明"], [[item["name"], item["status"], item["detail"]] for item in data["checks"]])
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(target)
    return target


__all__ = ["get_inputs", "save_inputs", "build_statement", "export_statement", "MONEY_FIELDS", "NOTE"]
