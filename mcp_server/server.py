#!/usr/bin/env python3
"""税小智 MCP Server（只读，v1.56）。

用途：让支持 MCP 的大模型客户端（DeepSeek Harness / Claude Desktop / Cursor 等）
安全地查询本地账套、申报日历、进度、测算与政策，不修改任何数据。

运行：
    python mcp_server/server.py                 # stdio（推荐本地）
    python mcp_server/server.py --http          # 本机 HTTP，127.0.0.1:8766/mcp

安全：
* 默认只读；不提供写入/申报/删除工具；
* 不返回完整身份证、API Key、密码；
* 默认只监听 127.0.0.1；不要在公网裸奔。
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from engine import current_quarter, period_label  # noqa: E402
from engine import tax_calendar  # noqa: E402
from filing import progress as filing_progress  # noqa: E402
from knowledge import retriever  # noqa: E402
from ledger import employees, payroll, store  # noqa: E402
from ledger import reports  # noqa: E402
from drafting import center as draft_center  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP(
    "税小智",
    instructions=(
        "税小智是本地小规模纳税人税务辅助工具。工具只读，不会修改账套或申报。"
        "涉及金额时以确定性引擎结果为准，不自行编造；不确定时提示用户核对电子税务局。"
    ),
    host="127.0.0.1",
    port=8766,
    streamable_http_path="/mcp",
)


def _disclaimer() -> str:
    return config.DISCLAIMER


def _company() -> dict[str, Any]:
    company = config.get_company()
    # 不返回任何密钥；只给税务相关公开字段
    return {
        "id": company.get("id"),
        "name": company.get("name"),
        "taxpayer_type": company.get("taxpayer_type"),
        "industry": company.get("industry"),
        "region": " ".join(str(company.get(k) or "") for k in ("province", "city", "county")).strip(),
        "vat_levy_method": company.get("vat_levy_method"),
        "accounting_standard": company.get("accounting_standard"),
    }


def _month_or_quarter(period: str) -> str:
    text = str(period or "").strip().upper()
    if text.endswith("Q1") or text.endswith("Q2") or text.endswith("Q3") or text.endswith("Q4"):
        return text
    day = date.today()
    if len(text) == 7 and text[4] == "-":
        return f"{text[:4]}Q{(int(text[5:7]) - 1) // 3 + 1}"
    return current_quarter(day)


@mcp.tool()
def get_company_profile() -> dict[str, Any]:
    """读取当前账套的企业基本信息（不含身份证、API Key、密码）。"""
    return {"company": _company(), "disclaimer": _disclaimer()}


@mcp.tool()
def get_period_summary(period: str) -> dict[str, Any]:
    """查询某期间（2026-10 或 2026Q4）的收入、费用、笔数和利润汇总。"""
    text = str(period or "").strip()
    try:
        income = store.storage_stats()
        from ledger import expense_ledger, income_ledger

        inc = income_ledger.period_totals(text)
        exp = expense_ledger.period_totals(text)
        return {
            "period": text,
            "income_inclusive": inc.get("total_inclusive", 0),
            "income_exclusive": inc.get("total_exclusive", 0),
            "expense": exp.get("total", 0),
            "profit": round(float(inc.get("total_exclusive", 0)) - float(exp.get("total", 0)), 2),
            "storage": income,
            "disclaimer": _disclaimer(),
        }
    except Exception as exc:  # noqa: BLE001
        return {"period": text, "error": f"查询失败：{exc}", "disclaimer": _disclaimer()}


@mcp.tool()
def get_calendar_deadlines(year: int | None = None, lookahead_days: int = 60) -> dict[str, Any]:
    """查询申报截止日：默认从今天起未来 60 天，可指定年份。"""
    if year:
        items = tax_calendar.filing_deadlines(int(year))
    else:
        items = tax_calendar.upcoming_deadlines(lookahead_days=int(lookahead_days))
    return {"items": items, "disclaimer": _disclaimer()}


@mcp.tool()
def get_filing_progress(period: str) -> dict[str, Any]:
    """查询某季度申报完成进度（不包含实际提交）。"""
    return {**filing_progress.progress(period), "disclaimer": _disclaimer()}


@mcp.tool()
def estimate_vat(period: str) -> dict[str, Any]:
    """按本期收入台账估算增值税及附加；period 可为季度或月份（月份自动转季度）。"""
    try:
        quarter = _month_or_quarter(period)
        summary = reports.quarterly_summary(quarter)
        return {
            "period": quarter,
            "income_inclusive": summary.get("income_inclusive"),
            "income_exclusive": summary.get("income_exclusive"),
            "vat": summary.get("vat"),
            "surtax": summary.get("surtax"),
            "total_tax": summary.get("total_tax"),
            "within_exemption_limit": summary.get("within_exemption_limit"),
            "note": "估算值，须以电子税务局申报表为准",
            "disclaimer": _disclaimer(),
        }
    except Exception as exc:  # noqa: BLE001
        return {"period": period, "error": f"测算失败：{exc}", "disclaimer": _disclaimer()}


@mcp.tool()
def estimate_cit(period: str) -> dict[str, Any]:
    """按本期利润表估算企业所得税预缴参考，不代替汇算/申报。"""
    try:
        quarter = _month_or_quarter(period)
        report = reports.profit_statement(quarter)
        profit = next((row.get("amount", 0) for row in report if row.get("row") == "三、利润总额"), 0)
        income_tax = next((row.get("amount", 0) for row in report if row.get("row") == "减：所得税费用"), 0)
        return {"period": quarter, "profit": profit, "estimated_income_tax": income_tax, "lines": report, "disclaimer": _disclaimer()}
    except Exception as exc:  # noqa: BLE001
        return {"period": period, "error": f"测算失败：{exc}", "disclaimer": _disclaimer()}


@mcp.tool()
def calculate_iit(period: str) -> dict[str, Any]:
    """汇总某月工资表人数、应发、社保、公积金与本期预扣个税（只读汇总）。"""
    text = str(period or "").strip()
    rows = payroll.list_payroll(text)
    result = [{
        "employee_name": row.get("employee_name"),
        "gross": round(float(row.get("gross_salary") or 0) + float(row.get("bonus") or 0) + float(row.get("allowance") or 0), 2),
        "social_insurance": row.get("social_insurance"),
        "housing_fund": row.get("housing_fund"),
        "tax_withheld": row.get("tax_withheld"),
        "status": row.get("status"),
    } for row in rows]
    return {
        "period": text,
        "headcount": len(result),
        "total_gross": round(sum(item["gross"] for item in result), 2),
        "total_tax": round(sum(float(item["tax_withheld"] or 0) for item in result), 2),
        "employees": result,
        "disclaimer": _disclaimer(),
    }


@mcp.tool()
def get_employee_summary() -> dict[str, Any]:
    """查询员工人数和脱敏身份证状态；不会返回完整身份证。"""
    rows = employees.list_employees(status=None, include_left=True)
    return {
        "total": len(rows),
        "active": sum(1 for row in rows if str(row.get("status")) == "在职"),
        "with_full_id": sum(1 for row in rows if row.get("id_card_full")),
        "employees": [
            {
                "name": row.get("name"),
                "employee_no": row.get("employee_no"),
                "status": row.get("status"),
                "id_card_masked": row.get("id_card_masked"),
            }
            for row in rows
        ],
        "disclaimer": _disclaimer(),
    }


@mcp.tool()
def search_policy(query: str, top_k: int = 5) -> dict[str, Any]:
    """检索本地政策知识库，返回相关条目和来源。"""
    try:
        items = retriever.search(str(query or ""), top_k=max(1, min(int(top_k), 10)))
        return {"query": query, "items": items, "disclaimer": _disclaimer()}
    except Exception as exc:  # noqa: BLE001
        return {"query": query, "error": f"政策检索失败：{exc}", "disclaimer": _disclaimer()}


@mcp.tool()
def get_draft_center(month: str | None = None, quarter: str | None = None) -> dict[str, Any]:
    """获取申报草稿中心：本月/本季可准备什么、缺什么数据。"""
    result = draft_center.build_center(month, quarter)
    return {**result, "disclaimer": result.get("disclaimer") or _disclaimer()}


@mcp.tool()
def list_invoices(period: str = "", limit: int = 50) -> dict[str, Any]:
    """列出近期发票（只读，脱敏，不返回完整身份证）。period 可填 2026-10 或 2026Q4。"""
    text = str(period or "").strip()
    rows = store.table("invoices").read()
    if text:
        prefix = text[:7] if len(text) >= 7 else text[:4]
        rows = [row for row in rows if str(row.get("invoice_date") or "").startswith(prefix)]
    rows = rows[-max(1, min(int(limit), 200)):]
    return {
        "period": text,
        "items": [
            {
                "invoice_no": row.get("invoice_no"),
                "invoice_date": row.get("invoice_date"),
                "direction": row.get("direction"),
                "seller": row.get("seller"),
                "total_inclusive": row.get("total_inclusive"),
                "status": row.get("status"),
            }
            for row in rows
        ],
        "disclaimer": _disclaimer(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="税小智 MCP Server（只读）")
    parser.add_argument("--http", action="store_true", help="启动本机 HTTP MCP；默认 stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if args.http:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
