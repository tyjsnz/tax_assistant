"""申报填报：申报表生成、规则校验、进度跟踪、导出与填报指引。

模块职责
--------
* :mod:`filing.registry`  报表注册表与批量生成（``build_form`` / ``build_all``）；
* :mod:`filing.vat_return` / :mod:`filing.cit_prepay` / :mod:`filing.stamp_return` /
  :mod:`filing.fin_report` 各税种申报表 bundle 构造器；
* :mod:`filing.validator`  栏次勾稽、与台账比对、与 OCR/手工栏次值比对；
* :mod:`filing.progress`   申报记录保存、状态流转与季度完成度；
* :mod:`filing.export`     Excel / JSON / Markdown 导出；
* :mod:`filing.guide`      中文填报步骤与逐栏复制文本。

所有申报数据均来自台账与确定性算税引擎，不含任何模型估算；输出均带免责声明。
"""

from __future__ import annotations

from filing import cit_prepay, export, fin_report, guide, progress, registry, stamp_return, validator, vat_return
from filing.cit_prepay import build_cit_prepay
from filing.export import (
    export_bundle_excel,
    export_bundle_json,
    export_checklist_markdown,
    export_package_excel,
)
from filing.fin_report import build_fin_report
from filing.guide import build_guide, export_submission_text
from filing.progress import (
    get_filing,
    list_filings,
    mark_submitted,
    progress as filing_progress,
    save_filing,
    update_status,
)

#: 顶层便捷别名（避免与 :mod:`filing.progress` 模块同名）
period_progress = filing_progress
from filing.registry import FORMS, build_all, build_form, get_form, list_forms
from filing.stamp_return import build_stamp_return
from filing.validator import (
    check_ledger_consistency,
    validate_against_fields,
    validate_bundle,
    validate_vat_return,
)
from filing.vat_return import build_vat_return

__all__ = [
    # 模块
    "registry",
    "vat_return",
    "cit_prepay",
    "stamp_return",
    "fin_report",
    "validator",
    "export",
    "progress",
    "guide",
    # 注册表
    "FORMS",
    "list_forms",
    "get_form",
    "build_form",
    "build_all",
    # 构造器
    "build_vat_return",
    "build_cit_prepay",
    "build_stamp_return",
    "build_fin_report",
    # 校验
    "validate_bundle",
    "validate_vat_return",
    "check_ledger_consistency",
    "validate_against_fields",
    # 进度
    "save_filing",
    "list_filings",
    "get_filing",
    "update_status",
    "filing_progress",
    "period_progress",
    "mark_submitted",
    # 导出
    "export_bundle_excel",
    "export_package_excel",
    "export_bundle_json",
    "export_checklist_markdown",
    # 指引
    "build_guide",
    "export_submission_text",
]
