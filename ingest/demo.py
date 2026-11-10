"""演示数据：读取项目 ``examples/`` 下的示例发票与银行流水，导入票据/流水池并自动匹配。

示例文件（均为虚构数据）：
* ``examples/invoices/销项_云图科技_2026Q1.xml`` —— 销项数电票（普票），金额 20,600 元；
* ``examples/invoices/进项_办公用品_2026Q1.xml`` —— 进项数电票（专票），金额 11,300 元；
* ``examples/bank/银行流水_2026Q1.csv`` —— 招商银行风格流水（收入/支出分列）。

UI「📥 票据与流水」页面的「加载示例票据/流水」按钮即调用 :func:`seed_demo_pool`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import config
from ledger.audit import log_user_action

from ingest.importer import import_bank_files, import_invoice_files, reset_pool
from ingest.matcher import suggest_matches

#: 示例银行账号（虚构，仅用于展示）
DEMO_ACCOUNT_NO = "6222020200012345"


def _example_dir() -> Path:
    """示例文件目录（``config.BASE_DIR/examples``，与测试临时数据目录无关）。"""
    return Path(config.BASE_DIR) / "examples"


def seed_demo_pool(reset: bool = False) -> dict[str, Any]:
    """加载 ``examples/`` 下的 2 张 XML 发票 + 1 个银行流水 CSV，导入并自动匹配。

    参数
    ----
    reset : 为 True 时先清空票据池/流水池/匹配建议再导入，避免与已有演示数据混在一起。

    返回
    ----
    dict：``{"invoices": 新增发票数, "transactions": 新增流水数, "suggestions": 新增建议数,
    "files": [文件路径...], "pool": {池统计}}``。
    """
    example_dir = _example_dir()
    invoice_paths = sorted((example_dir / "invoices").glob("*.xml"))
    bank_paths = sorted((example_dir / "bank").glob("*.csv"))
    if not invoice_paths and not bank_paths:
        raise ValueError(
            f"未找到示例文件：请确认 {example_dir} 下的 invoices/*.xml 与 bank/*.csv 是否存在。"
        )

    if reset:
        reset_pool()

    invoice_files = [(path.name, path.read_bytes()) for path in invoice_paths]
    bank_files = [(path.name, path.read_bytes()) for path in bank_paths]
    invoice_result = import_invoice_files(invoice_files) if invoice_files else {"saved_records": [], "errors": []}
    bank_result = import_bank_files(bank_files, account_no=DEMO_ACCOUNT_NO) if bank_files else {"saved_records": [], "errors": []}
    match_result = suggest_matches()

    from ingest.importer import pool_summary

    pool = pool_summary()
    log_user_action(
        "加载示例票据流水",
        f"发票 {len(invoice_result['saved_records'])} 张、流水 {len(bank_result['saved_records'])} 笔、"
        f"匹配建议 {match_result['count']} 条",
    )
    return {
        "invoices": len(invoice_result["saved_records"]),
        "transactions": len(bank_result["saved_records"]),
        "suggestions": match_result["count"],
        "files": [str(path) for path in invoice_paths + bank_paths],
        "pool": pool,
        "errors": list(invoice_result.get("errors") or []) + list(bank_result.get("errors") or []),
    }


__all__ = ["seed_demo_pool", "DEMO_ACCOUNT_NO"]
