"""生成开源演示账套（全部为虚构数据，用于快速体验完整流程）。

用法：
```bash
.venv/bin/python scripts/seed_demo.py          # 创建/复用 demo 演示公司
.venv/bin/python scripts/seed_demo.py --reset  # 删除旧演示数据后重建
```
生成内容：2026 年收入/费用/研发辅助账/合同、2 名员工与 2 个月工资表（含累计预扣与工资凭证）、
示例发票与银行流水匹配入账、季度申报表记录、10 月结账、Q3 申报包归档、数据备份、汇算输入与调整。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import workspace  # noqa: E402

DEMO_ID = "demo"
DEMO_NAME = "某科技有限公司"


def _ensure_demo_company(reset: bool) -> dict:
    workspace.ensure_default()
    if reset:
        try:
            workspace.activate(workspace.DEFAULT_COMPANY_ID)
            workspace.delete_company(DEMO_ID, hard=True)
        except (KeyError, ValueError):
            pass
    existing = workspace.get_company(DEMO_ID)
    if existing:
        workspace.activate(DEMO_ID)
        return existing
    record = workspace.add_company(
        {
            "name": DEMO_NAME,
            "short_name": "某科技",
            "taxpayer_type": "小规模纳税人",
            "industry": "网络科技（技术服务、软件开发）",
            "unified_social_credit_code": "91530000MA0000000X",
            "tax_authority": "国家税务总局某市税务局",
            "province": "云南省",
            "city": "昆明市",
            "region_id": "yunnan-kunming-urban",
            "note": "开源演示账套（虚构数据，可随时删除）",
        },
        company_id=DEMO_ID,
        activate_new=True,
    )
    return record


def _clear_company_tables() -> None:
    from ledger import store

    for name in ("income", "expense", "rd", "contracts", "employees", "payroll", "journal_entries", "journal_lines",
                 "invoices", "bank_transactions", "matches", "periods", "filings", "tax_adjustments",
                 "loss_carryforwards", "annual_settlements"):
        store.table(name).write([])


def _seed_ledgers() -> dict:
    from ledger import expense_ledger, income_ledger, rd_auxiliary, reports

    incomes = [
        ("2026-01-12", "某客户A", 61800, "普票", "技术服务", "技术服务费"),
        ("2026-02-15", "某客户B", 51500, "普票", "软件开发", "软件开发费"),
        ("2026-03-10", "某客户C", 87000, "未开票", "运维服务", "运维服务费"),
        ("2026-04-09", "某客户A", 82400, "普票", "技术服务", "技术服务费"),
        ("2026-05-18", "某客户C", 101000, "专票", "软件开发", "软件定制开发"),
        ("2026-06-20", "某客户B", 96000, "普票", "技术服务", "数据服务费"),
        ("2026-07-08", "某客户A", 92700, "普票", "技术服务", "技术服务费"),
        ("2026-08-14", "某客户C", 76000, "未开票", "运维服务", "系统运维"),
        ("2026-09-22", "某客户B", 84000, "普票", "软件开发", "软件开发费"),
        ("2026-10-16", "某客户A", 68000, "普票", "技术服务", "技术服务费"),
        ("2026-11-06", "某客户C", 55000, "普票", "运维服务", "运维服务费"),
    ]
    for date, client, amount, invoice_type, category, desc in incomes:
        income_ledger.add_income(
            date, client, amount, invoice_type=invoice_type, category=category, description=desc,
            note="演示数据",
        )

    expenses = [
        ("工资", "员工工资", 20000, False),
        ("社保", "社保公积金单位部分", 5600, False),
        ("房租", "办公室租金", 6000, False),
        ("办公", "办公用品", 1200, False),
        ("软件订阅", "云服务订阅", 800, False),
        ("研发材料", "研发测试设备材料", 3000, True),
        ("差旅", "客户拜访差旅", 900, False),
    ]
    for month in range(1, 12):
        for category, desc, amount, rd_related in expenses:
            expense_ledger.add_expense(
                f"2026-{month:02d}-05", f"{desc}（{month} 月）", amount, category=category,
                is_rd_related=rd_related, vendor="某供应商", note="演示数据",
            )
    rd_auxiliary.add_rd_record(
        "智慧税务助手", "2026Q1", personnel_cost=40000, material_cost=6000, depreciation=2000, other_cost=3000,
        note="演示研发项目",
    )
    rd_auxiliary.add_rd_record(
        "智慧税务助手", "2026Q2", personnel_cost=42000, material_cost=9000, depreciation=2000, other_cost=3500,
        note="演示研发项目",
    )
    contracts = [
        ("2026-01-08", "YSDEMO-001", "某客户A", "技术合同", 61800),
        ("2026-05-06", "YSDEMO-002", "某客户C", "技术合同", 101000),
        ("2026-07-02", "YSDEMO-003", "某客户B", "技术合同", 120000),
        ("2026-09-01", "YSDEMO-004", "某供应商", "买卖合同", 18000),
    ]
    from ledger import reports

    for date, contract_no, counterparty, contract_type, amount in contracts:
        reports.add_contract(
            date=date, contract_no=contract_no, counterparty=counterparty,
            contract_type=contract_type, amount=amount, note="演示数据",
        )
    return {"incomes": len(incomes), "expenses": len(expenses) * 7, "contracts": len(contracts)}


def _seed_employees_and_payroll() -> dict:
    from ledger import employees, payroll

    people = [
        ("张三", "E001", "研发部", "工程师", 10000, {"children_education_children": 1}),
        ("李四", "E002", "运营部", "运营专员", 7000, {"housing_rent": "市辖区户籍人口>100万"}),
        ("王五", "E003", "研发部", "测试工程师", 8000, {"elderly_care": "独生"}),
    ]
    created = []
    for name, no, department, position, base, special in people:
        created.append(
            employees.add_employee(
                {
                    "name": name,
                    "employee_no": no,
                    "department": department,
                    "position": position,
                    "hire_date": "2026-01-01",
                    "social_insurance_base": base,
                    "housing_fund_base": base,
                    "special_deduction_json": special,
                    "note": "演示员工",
                }
            )
        )
    confirmed = []
    for period, factor in (("2026-10", 1.0), ("2026-11", 1.05)):
        payroll.build_payroll(period)
        for row in payroll.list_payroll(period):
            name = str(row["employee_name"])
            gross = {"张三": 20000, "李四": 12000, "王五": 15000}.get(name, 10000)
            gross = round(gross * factor)
            payroll.update_payroll_row(
                row["id"],
                {
                    "gross_salary": gross,
                    "social_insurance": round(gross * 0.08),
                    "housing_fund": round(gross * 0.04),
                },
            )
        payroll.recalc_period(period)
        payroll.confirm_period(period)
        payroll.mark_period_declared(period)
        confirmed.append(period)
    return {"employees": len(created), "payroll_periods": confirmed}


def _seed_ingest() -> dict:
    """导入 examples 下的示例发票与流水并确认匹配（演示 P0-2 流程）。"""
    from ingest import importer, matcher

    files = []
    for path in sorted((ROOT / "examples" / "invoices").glob("*.xml")):
        files.append((path.name, path.read_bytes()))
    for path in sorted((ROOT / "examples" / "bank").glob("*.csv")):
        files.append((path.name, path.read_bytes()))
    if not files:
        return {"invoices": 0, "transactions": 0, "matches": 0}
    result = importer.import_files(files, auto_match=True)
    confirmed = 0
    for match in matcher.list_matches(status="建议"):
        try:
            matcher.confirm_match(match["id"])
            confirmed += 1
        except ValueError:
            continue
    return {"invoices": result.get("invoices"), "transactions": result.get("transactions"), "matches": confirmed}


def _seed_filings_and_archives() -> dict:
    from filing import progress, registry
    from archiving import backup, filing_archive

    saved = 0
    for period, status in (("2026Q1", "已申报"), ("2026Q2", "已申报"), ("2026Q3", "已确认")):
        for bundle in registry.build_all(period):
            progress.save_filing(bundle, status=status)
            saved += 1
    archived = filing_archive.archive_filing_package("2026Q3", note="演示归档")
    created_backup = backup.create_backup(tag="demo", note="演示备份")
    return {"filings": saved, "archive": archived.get("manifest_path"), "backup": created_backup.get("path")}


def _seed_closing_and_settlement() -> dict:
    from closing import close
    from ledger import settlement

    close.close_period("2026-10", operator="demo", force=True)
    settlement.save_annual_inputs(
        "2026",
        {
            "entertainment_actual": 12000,
            "advertising_actual": 50000,
            "welfare_actual": 30000,
            "education_actual": 18000,
            "union_actual": 6000,
            "employees": 3,
            "total_assets": 2000000,
            "prepaid_tax": 8000,
            "note": "演示数据",
        },
    )
    settlement.add_adjustment("2026", "税收罚款", "调增", 5000, basis="税收罚款不得税前扣除", source="企业所得税法第十条")
    settlement.add_loss("2024", 50000, note="演示亏损台账")
    result = settlement.compute_settlement("2026")
    return {"closed": "2026-10", "taxable_income": result.get("taxable_income"), "corporate_tax": result.get("corporate_tax")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成税小智演示账套（虚构数据）")
    parser.add_argument("--reset", action="store_true", help="删除旧演示公司后重建")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    config.ensure_dirs()
    try:
        record = _ensure_demo_company(args.reset)
    except ValueError as exc:
        print(f"❌ 创建演示公司失败：{exc}")
        return 1
    _clear_company_tables()
    print(f"✅ 演示公司：{record.get('name')}（{record.get('id')}）")
    print("   数据目录：", config.DATA_DIR)
    ledgers = _seed_ledgers()
    people = _seed_employees_and_payroll()
    ingest = _seed_ingest()
    filings = _seed_filings_and_archives()
    closing = _seed_closing_and_settlement()
    print(f"   台账：收入 {ledgers['incomes']} 笔，费用 {ledgers['expenses']} 笔，合同 {ledgers['contracts']} 份")
    print(f"   薪酬：员工 {people['employees']} 人，工资表 {', '.join(people['payroll_periods'])}")
    print(f"   票据：发票 {ingest['invoices']} 张，流水 {ingest['transactions']} 笔，匹配入账 {ingest['matches']} 笔")
    print(f"   申报：报表记录 {filings['filings']} 张，Q3 归档已生成")
    print(f"   结账：{closing['closed']} 已结账；汇算应纳税所得额 ¥{float(closing['taxable_income'] or 0):,.2f}")
    print("\n提示：启动界面后侧边栏「当前公司」选择「某科技」即可体验完整演示数据；")
    print("      演示数据均为虚构，可在「设置 → 企业信息」中删除演示公司。")
    print(config.DISCLAIMER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
