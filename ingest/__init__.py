"""票据与银行流水导入：解析 → 票据/流水池 → 自动匹配 → 确认入账。

模块职责
--------
* :mod:`ingest.invoice_parser`  发票解析（数电票 XML / OFD / PDF）与入库；
* :mod:`ingest.bank_parser`     银行流水解析（CSV / XLSX，多编码、表头探测）与入库；
* :mod:`ingest.matcher`         确定性评分 + 贪心一对一匹配建议、确认入账 / 忽略；
* :mod:`ingest.importer`        多文件导入编排、手动录入发票、池汇总与清空；
* :mod:`ingest.demo`            读取 ``examples/`` 示例文件一键生成演示票据池。

所有匹配与入账均基于本地确定性规则，不经过模型；输出仅供参考，需人工核对原始票据与流水。
"""

from __future__ import annotations

from ingest import bank_parser, demo, importer, invoice_parser, matcher
from ingest.bank_parser import (
    BANK_COLUMN_ALIASES,
    detect_columns,
    normalize_transaction,
    parse_bank_csv,
    parse_bank_excel,
    parse_bank_file,
    save_transactions,
)
from ingest.demo import seed_demo_pool
from ingest.importer import (
    add_manual_invoice,
    delete_pool_record,
    import_bank_files,
    import_files,
    import_invoice_files,
    list_pool_records,
    pool_summary,
    reset_pool,
    update_pool_status,
)
from ingest.invoice_parser import (
    normalize_invoice,
    parse_invoice_bytes,
    parse_invoice_file,
    parse_ofd_invoice,
    parse_pdf_invoice,
    parse_xml_invoice,
    save_invoices,
)
from ingest.matcher import (
    confirm_match,
    ignore_match,
    list_matches,
    match_summary,
    score_pair,
    suggest_matches,
)

__all__ = [
    # 模块
    "invoice_parser",
    "bank_parser",
    "matcher",
    "importer",
    "demo",
    # 发票
    "parse_invoice_file",
    "parse_invoice_bytes",
    "parse_xml_invoice",
    "parse_ofd_invoice",
    "parse_pdf_invoice",
    "normalize_invoice",
    "save_invoices",
    # 银行流水
    "BANK_COLUMN_ALIASES",
    "detect_columns",
    "parse_bank_file",
    "parse_bank_csv",
    "parse_bank_excel",
    "normalize_transaction",
    "save_transactions",
    # 匹配
    "score_pair",
    "suggest_matches",
    "list_matches",
    "confirm_match",
    "ignore_match",
    "match_summary",
    # 编排 / 录入 / 池
    "import_invoice_files",
    "import_bank_files",
    "import_files",
    "add_manual_invoice",
    "list_pool_records",
    "delete_pool_record",
    "update_pool_status",
    "pool_summary",
    "reset_pool",
    "seed_demo_pool",
]
