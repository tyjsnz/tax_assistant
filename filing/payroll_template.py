"""导出自然人电子税务局扣缴端「正常工资薪金所得」模板（v1.46）。

以税务局下载的空白 .xls 为底稿，填入工资表数据，导出后可在扣缴端申报页导入。
注意：
* 证件号码需要完整号码，系统默认只保存脱敏值；导出时由用户临时填写，不落库；
* 个人社保已拆分养老/医疗/失业时按拆分列导出；未拆分时合并到基本养老保险费并提示；
* 累计专项附加扣除目前无法按模板分项拆分，导出为 0 并提示人工补齐。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import config
from ledger import employees, payroll

TEMPLATE_PATH = Path(config.RESOURCE_DIR) / "templates" / "正常工资薪金所得.xls"
SHEET_NAME = "正常工资薪金收入"

#: 模板列名 → 取值说明
COLUMNS = [
    "工号", "*姓名", "*证件类型", "*证件号码", "本期收入", "本期免税收入",
    "基本养老保险费", "基本医疗保险费", "失业保险费", "住房公积金",
    "累计子女教育", "累计继续教育", "累计住房贷款利息", "累计住房租金",
    "累计赡养老人", "累计3岁以下婴幼儿照护", "累计个人养老金",
    "企业(职业)年金", "商业健康保险", "税延养老保险", "公务交通费用", "通讯费用",
    "律师办案费用", "住房公积金和年金调整", "西藏附加减除费用", "其他",
    "准予扣除的捐赠额", "减免税额", "协定减免", "备注",
]


def _text(value: Any) -> str:
    return str(value or "").strip()


def build_rows(period: str, id_cards: dict[str, str] | None = None) -> dict[str, Any]:
    """构造模板行，返回 rows/warnings/missing_ids。"""
    id_cards = id_cards or {}
    rows = [row for row in payroll.list_payroll(period) if _text(row.get("employee_id")) != "ZERO"]
    result_rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    missing_ids: list[str] = []
    for row in rows:
        employee = employees.get_employee(_text(row.get("employee_id"))) or {}
        employee_id = _text(row.get("employee_id"))
        full_id = _text(id_cards.get(employee_id))
        if not full_id:
            missing_ids.append(_text(row.get("employee_name")))
        pension = round(float(row.get("pension_insurance") or 0), 2)
        medical = round(float(row.get("medical_insurance") or 0), 2)
        unemployment = round(float(row.get("unemployment_insurance") or 0), 2)
        social_total = round(float(row.get("social_insurance") or 0), 2)
        if social_total and not (pension or medical or unemployment):
            pension = social_total
            warnings.append(f"{row.get('employee_name')}：社保未拆分，暂按基本养老保险费导出，请在模板中核对拆分")
        income = round(
            float(row.get("gross_salary") or 0) + float(row.get("bonus") or 0) + float(row.get("allowance") or 0), 2
        )
        result_rows.append({
            "工号": _text(employee.get("employee_no")) or employee_id,
            "*姓名": _text(row.get("employee_name")),
            "*证件类型": "居民身份证",
            "*证件号码": full_id,
            "本期收入": income,
            "本期免税收入": 0.0,
            "基本养老保险费": pension,
            "基本医疗保险费": medical,
            "失业保险费": unemployment,
            "住房公积金": round(float(row.get("housing_fund") or 0), 2),
            "其他": round(float(row.get("other_deduction") or 0), 2),
            "备注": "税小智导出，导入前请核对",
        })
    if missing_ids:
        warnings.insert(0, f"有 {len(missing_ids)} 人缺少完整证件号码，请在导出前填写；否则扣缴端无法导入。")
    return {"rows": result_rows, "warnings": warnings, "missing_ids": missing_ids, "count": len(result_rows)}


def export_xls(period: str, id_cards: dict[str, str] | None = None, path: str | Path | None = None) -> dict[str, Any]:
    """以官方空白模板为底稿，写入工资数据，生成可导入的 .xls。"""
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"缺少扣缴端模板：{TEMPLATE_PATH}")
    import xlrd
    from xlutils.copy import copy

    bundle = build_rows(period, id_cards)
    target = Path(path) if path else Path(config.EXPORT_DIR) / f"正常工资薪金所得_{period}.xls"
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook = xlrd.open_workbook(str(TEMPLATE_PATH), formatting_info=True)
    sheet_index = workbook.sheet_names().index(SHEET_NAME) if SHEET_NAME in workbook.sheet_names() else 0
    writable = copy(workbook)
    sheet = writable.get_sheet(sheet_index)
    source_sheet = workbook.sheet_by_index(sheet_index)
    header = [_text(source_sheet.cell_value(0, col)) for col in range(source_sheet.ncols)]
    column_index = {name: index for index, name in enumerate(header)}
    for row_index, row in enumerate(bundle["rows"], start=1):
        for name, value in row.items():
            col = column_index.get(name)
            if col is not None:
                sheet.write(row_index, col, value)
    writable.save(str(target))
    return {**bundle, "path": str(target), "period": period}


__all__ = ["TEMPLATE_PATH", "COLUMNS", "build_rows", "export_xls"]
