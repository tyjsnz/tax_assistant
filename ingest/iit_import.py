"""自然人电子税务局「综合所得申报表」导入（v1.42）。

用途：旧电脑损坏/新系统重建时，从扣缴端导出的综合所得申报表里识别人员，
自动建立员工档案；工资/扣除明细只做预览，不替代扣缴端申报。

安全：
* 只读取上传文件，不联网；
* 身份证号落库前脱敏；
* 不写入日志中的身份证原文。
"""

from __future__ import annotations

import io
import re
from typing import Any

import pandas as pd

from ledger import employees

NAME_ALIASES = ("姓名", "员工姓名", "纳税人姓名", "人员姓名")
ID_ALIASES = ("身份证件号码", "身份证号码", "身份证号", "证件号码", "纳税人识别号")
PHONE_ALIASES = ("手机号码", "手机号", "联系电话")
HIRE_ALIASES = ("入职日期", "任职受雇日期", "受雇日期")
LEAVE_ALIASES = ("离职日期",)
STATUS_ALIASES = ("任职受雇状态", "人员状态", "状态")
INCOME_ALIASES = ("本期收入", "本期收入额", "收入额", "收入", "应发工资", "工资薪金收入")
PENSION_ALIASES = ("基本养老保险费", "养老保险费", "个人养老")
MEDICAL_ALIASES = ("基本医疗保险费", "医疗保险费", "个人医疗")
UNEMPLOY_ALIASES = ("失业保险费", "个人失业")
SOCIAL_TOTAL_ALIASES = ("专项扣除合计", "个人社保合计", "社保合计")
HOUSING_ALIASES = ("住房公积金", "个人住房公积金", "公积金")
SPECIAL_ALIASES = ("专项附加扣除", "专项附加扣除合计", "累计专项附加扣除", "子女教育", "赡养老人", "住房贷款利息", "住房租金", "继续教育", "3岁以下婴幼儿照护")
TAX_ALIASES = ("本期应预扣预缴税额", "应补(退)税额", "本期应纳税额", "已缴税额", "应纳所得税额")


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "nat"} else text


def _norm(value: Any) -> str:
    return re.sub(r"[\s\u3000（）()：:]+", "", _text(value))


def _number(value: Any) -> float:
    text = _text(value).replace(",", "").replace("￥", "").replace("¥", "").replace("元", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else 0.0


def _find_column(columns: list[str], aliases: tuple[str, ...]) -> str | None:
    normalized = {_norm(col): col for col in columns}
    for alias in aliases:
        key = _norm(alias)
        if key in normalized:
            return normalized[key]
    for alias in aliases:
        key = _norm(alias)
        for norm_col, col in normalized.items():
            if key and (key in norm_col or norm_col in key):
                return col
    return None


def _read_frames(data: bytes, filename: str) -> list[tuple[str, pd.DataFrame]]:
    suffix = filename.lower().rsplit(".", 1)[-1]
    if suffix == "csv":
        frame = pd.read_csv(io.BytesIO(data), header=None, dtype=str)
        return [("CSV", frame)]
    if suffix in ("xlsx", "xlsm"):
        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=str)
        return [(str(name), frame) for name, frame in sheets.items()]
    if suffix == "xls":
        raise ValueError("暂不支持旧版 .xls，请在 Excel/WPS 中另存为 .xlsx 后再导入。")
    raise ValueError("只支持 .xlsx / .xlsm / .csv 文件")


def _header_row(frame: pd.DataFrame) -> int | None:
    """找表头起始行：必须同时出现姓名列和身份证/证件列，避免把数据行误判成表头。"""
    best: tuple[int, int] | None = None
    for index in range(min(30, len(frame))):
        row = [_text(value) for value in frame.iloc[index].tolist()]
        name_col = _find_column(row, NAME_ALIASES)
        id_col = _find_column(row, ID_ALIASES)
        if not name_col or not id_col:
            continue
        matched_columns = {
            col_index
            for alias in (NAME_ALIASES + ID_ALIASES + INCOME_ALIASES + PENSION_ALIASES + HOUSING_ALIASES)
            for col_index, value in enumerate(row)
            if _find_column([value], (alias,))
        }
        score = len(matched_columns) + 2
        if best is None or score > best[0]:
            best = (score, index)
    return best[1] if best else None


def _name_column(frame: pd.DataFrame, header_row: int) -> int | None:
    row = [_text(value) for value in frame.iloc[header_row].tolist()]
    for index, value in enumerate(row):
        if _find_column([value], NAME_ALIASES):
            return index
    return None


def _data_start(frame: pd.DataFrame, header_row: int, name_col: int | None) -> int:
    """跳过表头下可能的多行子表头/列号行，找到第一行真实人员数据。"""
    if name_col is None:
        return header_row + 1
    header_tokens = {_norm(alias) for alias in (NAME_ALIASES + ID_ALIASES + INCOME_ALIASES + ("序号", "合计", "总计", "小计"))}
    for index in range(header_row + 1, min(len(frame), header_row + 12)):
        name = _text(frame.iat[index, name_col])
        if not name or _norm(name) in header_tokens:
            continue
        if name.isdigit():
            continue
        if any(token in name for token in ("税款所属期", "扣缴义务人", "金额单位", "填表说明")):
            continue
        return index
    return header_row + 1


def _leaf_headers(frame: pd.DataFrame, header_row: int, data_start: int, width: int) -> list[str]:
    """合并多行表头：每列取表头块中最后一个非空单元格作为字段名。"""
    headers: list[str] = []
    for col in range(width):
        values = []
        for row in range(header_row, data_start):
            text = _text(frame.iat[row, col]) if col < frame.shape[1] else ""
            if text and not text.isdigit():
                values.append(text)
        headers.append(values[-1] if values else "")
    # 避免重复列名导致 pandas 自动改列
    seen: dict[str, int] = {}
    unique: list[str] = []
    for name in headers:
        if not name:
            unique.append("")
            continue
        count = seen.get(name, 0) + 1
        seen[name] = count
        unique.append(name if count == 1 else f"{name}_{count}")
    return unique


def parse_file(data: bytes, filename: str) -> dict[str, Any]:
    """解析综合所得申报表，返回人员/工资预览（身份证仅用于识别，展示时脱敏）。"""
    frames = _read_frames(data, filename)
    records: list[dict[str, Any]] = []
    used_sheets: list[str] = []
    for sheet_name, frame in frames:
        header = _header_row(frame)
        if header is None:
            continue
        name_col_index = _name_column(frame, header)
        data_start = _data_start(frame, header, name_col_index)
        header_values = _leaf_headers(frame, header, data_start, frame.shape[1])
        body = frame.iloc[data_start:].copy()
        body.columns = header_values
        columns = list(header_values)
        name_col = _find_column(columns, NAME_ALIASES)
        id_col = _find_column(columns, ID_ALIASES)
        if not name_col:
            continue
        income_col = _find_column(columns, INCOME_ALIASES)
        pension_col = _find_column(columns, PENSION_ALIASES)
        medical_col = _find_column(columns, MEDICAL_ALIASES)
        unemploy_col = _find_column(columns, UNEMPLOY_ALIASES)
        social_total_col = _find_column(columns, SOCIAL_TOTAL_ALIASES)
        housing_col = _find_column(columns, HOUSING_ALIASES)
        special_cols = [col for col in columns if col and any(_norm(alias) in _norm(col) for alias in ("子女教育", "赡养老人", "住房贷款利息", "住房租金", "继续教育", "3岁以下婴幼儿照护"))]
        tax_col = _find_column(columns, TAX_ALIASES)
        phone_col = _find_column(columns, PHONE_ALIASES)
        hire_col = _find_column(columns, HIRE_ALIASES)
        leave_col = _find_column(columns, LEAVE_ALIASES)
        status_col = _find_column(columns, STATUS_ALIASES)
        for _, row in body.iterrows():
            name = _text(row.get(name_col))
            if not name or name in {"合计", "总计", "小计"}:
                continue
            raw_id = _text(row.get(id_col)) if id_col else ""
            social = sum(_number(row.get(col)) for col in (pension_col, medical_col, unemploy_col) if col)
            social = social or (_number(row.get(social_total_col)) if social_total_col else 0.0)
            records.append({
                "sheet": sheet_name,
                "name": name,
                "id_card": raw_id,
                "id_card_masked": employees.mask_id_card(raw_id),
                "phone": _text(row.get(phone_col)) if phone_col else "",
                "hire_date": _text(row.get(hire_col)) if hire_col else "",
                "leave_date": _text(row.get(leave_col)) if leave_col else "",
                "status": _text(row.get(status_col)) if status_col else "",
                "gross_salary": _number(row.get(income_col)) if income_col else 0.0,
                "social_insurance": round(social, 2),
                "housing_fund": _number(row.get(housing_col)) if housing_col else 0.0,
                "special_deduction": round(sum(_number(row.get(col)) for col in special_cols), 2),
                "tax_withheld": _number(row.get(tax_col)) if tax_col else 0.0,
            })
        used_sheets.append(sheet_name)
    if not records:
        raise ValueError("没有识别到人员数据。请确认文件包含「姓名」「身份证件号码」等列，或另存为 .xlsx 后重试。")
    # 同名/同证件去重
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        # 扣缴端导出的身份证常被脱敏成「5****2」，多个人可能同尾号；
        # 脱敏证件号不能单独作为去重键，必须叠加姓名。
        masked = record["id_card_masked"]
        key = record["name"] if (not masked or "*" in masked) else masked
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return {"rows": deduped, "sheets": used_sheets, "raw_count": len(records)}


def _existing_index() -> tuple[set[str], set[str]]:
    rows = employees.list_employees(status=None, include_left=True)
    # 已脱敏的证件号（含 *）不能作为唯一键，避免同尾号员工互相覆盖
    ids = {
        str(row.get("id_card_masked") or "")
        for row in rows
        if row.get("id_card_masked") and "*" not in str(row.get("id_card_masked"))
    }
    names = {str(row.get("name") or "") for row in rows if row.get("name")}
    return ids, names


def import_employees(rows: list[dict[str, Any]], *, period: str = "") -> dict[str, Any]:
    """从解析结果创建员工档案；已存在的按证件号/姓名跳过。"""
    existing_ids, existing_names = _existing_index()
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[str] = []
    for row in rows:
        name = _text(row.get("name"))
        masked = _text(row.get("id_card_masked")) or employees.mask_id_card(row.get("id_card"))
        if not name:
            continue
        if masked and "*" not in masked and masked in existing_ids:
            skipped.append({"name": name, "reason": "身份证号已存在"})
            continue
        if name in existing_names:
            skipped.append({"name": name, "reason": "姓名已存在"})
            continue
        leave_date = _text(row.get("leave_date"))
        status = "离职" if leave_date or "离职" in _text(row.get("status")) else "在职"
        hire_date = _text(row.get("hire_date")) or (f"{period}-01" if period else "")
        try:
            employee = employees.add_employee({
                "name": name,
                "id_card": row.get("id_card") or masked,
                "phone": row.get("phone") or "",
                "hire_date": hire_date or None,
                "leave_date": leave_date or None,
                "status": status,
                "note": f"从综合所得申报表导入（{row.get('sheet') or '文件'}）" + (f"；{period}" if period else ""),
            })
            created.append(employee)
            if masked and "*" not in masked:
                existing_ids.add(masked)
            existing_names.add(name)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}：{exc}")
    return {"created": created, "skipped": skipped, "errors": errors}


def create_payroll_drafts(rows: list[dict[str, Any]], *, period: str) -> dict[str, Any]:
    """为已确认人员生成月度工资表草稿（仅本期数据，累计数留待核对）。"""
    from ledger import payroll as payroll_module

    all_employees = employees.list_employees(status=None, include_left=True)
    by_name: dict[str, dict[str, Any]] = {}
    by_id: dict[str, dict[str, Any]] = {}
    for employee in all_employees:
        by_name.setdefault(str(employee.get("name") or ""), employee)
        by_id.setdefault(str(employee.get("id") or ""), employee)
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[str] = []
    for row in rows:
        name = _text(row.get("name"))
        employee = by_name.get(name)
        if not employee:
            errors.append(f"{name}：员工档案不存在，请先导入员工")
            continue
        try:
            draft = payroll_module.create_import_draft(
                period,
                str(employee.get("id")),
                name,
                gross_salary=float(row.get("gross_salary") or 0),
                social_insurance=float(row.get("social_insurance") or 0),
                housing_fund=float(row.get("housing_fund") or 0),
                special_deduction=float(row.get("special_deduction") or 0),
                tax_withheld=float(row.get("tax_withheld") or 0),
            )
            created.append(draft)
        except Exception as exc:  # noqa: BLE001
            skipped.append({"name": name, "reason": str(exc)})
    return {"created": created, "skipped": skipped, "errors": errors}


__all__ = ["parse_file", "import_employees", "create_payroll_drafts"]
