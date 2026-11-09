"""台账管理：收入台账、费用台账、研发辅助账、凭证分录、合同台账与申报记录。

数据统一存放在 ``data/`` 下的 CSV 文件中，结构定义见 :mod:`ledger.models`，
读写实现见 :mod:`ledger.store`。
"""

from __future__ import annotations

from ledger import models
from ledger import store
from ledger.models import (
    CONTRACT_TYPES,
    EXPENSE_CATEGORIES,
    FILING_STATUSES,
    INCOME_CATEGORIES,
    INVOICE_TYPES,
    TABLE_DEFS,
    VAT_LEVY_RATE,
    bool_text,
    derive_exclusive,
    mask_client,
    normalize_contract,
    normalize_date,
    normalize_expense,
    normalize_filing,
    normalize_income,
    normalize_journal_entry,
    normalize_journal_line,
    normalize_period,
    normalize_rd,
    parse_bool,
)
from ledger.store import (
    append_log,
    backup_all,
    init_storage,
    read_log,
    reset_cache,
    storage_stats,
    table,
)

__all__ = [
    "models",
    "store",
    "table",
    "init_storage",
    "reset_cache",
    "backup_all",
    "storage_stats",
    "append_log",
    "read_log",
    "INVOICE_TYPES",
    "INCOME_CATEGORIES",
    "EXPENSE_CATEGORIES",
    "CONTRACT_TYPES",
    "FILING_STATUSES",
    "TABLE_DEFS",
    "VAT_LEVY_RATE",
    "parse_bool",
    "bool_text",
    "derive_exclusive",
    "mask_client",
    "normalize_date",
    "normalize_period",
    "normalize_income",
    "normalize_expense",
    "normalize_rd",
    "normalize_contract",
    "normalize_journal_entry",
    "normalize_journal_line",
    "normalize_filing",
]
