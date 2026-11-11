"""👥 员工与工资：员工档案 → 月度工资表（累计预扣）→ 工资凭证 → 扣缴申报辅助表。

页面只做展示与调用编排，个税计算全部由 ``engine.calc_iit_withholding``（确定性代码）完成；
实际扣缴申报需在自然人电子税务局扣缴端完成，本系统仅生成辅助核对表。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd
import streamlit as st

import config
from ui.components import cache, formatting as F, payroll_api as PA, runtime, theme, ui

FLASH_KEY = "_payroll_flash"
STATE_IIT_BUNDLE = "payroll_iit_bundle"
STATE_CONFIRM_RESULT = "payroll_confirm_result"
STATE_EXPORT_PATH = "payroll_iit_export_path"

EMPLOYEE_STATUS_FILTERS = ["在职", "离职", "全部"]

EMPLOYEE_TABLE_COLUMNS: list[tuple[str, str]] = [
    ("employee_no", "工号"),
    ("name", "姓名"),
    ("department", "部门"),
    ("position", "岗位"),
    ("status", "状态"),
    ("hire_date", "入职日期"),
    ("social_insurance_base", "社保基数"),
    ("housing_fund_base", "公积金基数"),
    ("special_text", "专项附加/月"),
    ("deduction_choice", "减除方式"),
    ("note", "备注"),
]

PAYROLL_MONEY_COLUMNS = [
    "应发工资",
    "奖金",
    "补贴",
    "个人社保",
    "个人公积金",
    "专项附加扣除",
    "其他扣除",
    "累计应纳税所得额",
    "本期应预扣个税",
    "实发工资",
]

BUNDLE_TOTAL_LABELS = {
    "total_income": "本期收入合计",
    "total_basic_deduction": "减除费用合计",
    "total_deduction_basic": "减除费用合计",
    "total_social": "专项扣除合计",
    "total_housing_fund": "公积金合计",
    "total_special": "专项附加扣除合计",
    "total_other": "其他扣除合计",
    "total_taxable": "应纳税所得额",
    "total_cumulative_tax": "累计应纳税额",
    "total_tax": "应预扣税额",
    "total_tax_withheld": "已预扣税额",
    "tax_payable": "本期应补（退）税额",
    "headcount": "申报人数",
    "total_net": "实发合计",
    "total_gross": "应发合计",
}

#: 员工明细行的字段顺序（中文标签）
EMPLOYEE_DETAIL_KEYS: list[tuple[str, str]] = [
    ("employee_name", "姓名"),
    ("employee_no", "工号"),
    ("income", "本期收入"),
    ("social_insurance", "个人社保"),
    ("housing_fund", "个人公积金"),
    ("special_deduction", "专项附加扣除"),
    ("other_deduction", "其他扣除"),
    ("cumulative_taxable", "累计应纳税所得额"),
    ("rate", "预扣率"),
    ("applicable_rate", "预扣率"),
    ("tax_withheld", "本期应预扣"),
    ("tax", "税额"),
    ("net_pay", "实发工资"),
    ("note", "备注"),
]

BUNDLE_FIELD_LABELS = {
    "employee_name": "姓名",
    "name": "姓名",
    "employee_no": "工号",
    "income": "本期收入",
    "gross_salary": "应发工资",
    "bonus": "奖金",
    "allowance": "补贴",
    "social_insurance": "个人社保",
    "housing_fund": "个人公积金",
    "special_deduction": "专项附加扣除",
    "other_deduction": "其他扣除",
    "basic_deduction": "减除费用",
    "cumulative_taxable": "累计应纳税所得额",
    "cumulative_taxable_income": "累计应纳税所得额",
    "rate": "预扣率",
    "applicable_rate": "预扣率",
    "tax_withheld": "本期应预扣",
    "tax": "税额",
    "net_pay": "实发工资",
    "note": "备注",
}

BUNDLE_MONEY_KEYS = {
    "income",
    "gross_salary",
    "bonus",
    "allowance",
    "social_insurance",
    "housing_fund",
    "special_deduction",
    "other_deduction",
    "basic_deduction",
    "cumulative_taxable",
    "cumulative_taxable_income",
    "tax_withheld",
    "tax",
    "net_pay",
    "total_income",
    "total_deduction_basic",
    "total_social",
    "total_special",
    "total_other",
    "total_taxable",
    "total_tax",
    "total_net",
    "total_gross",
}


# =============================================================================
# 提示
# =============================================================================
def _flash(message: str, level: str = "success") -> None:
    st.session_state.setdefault(FLASH_KEY, []).append({"message": message, "level": level})


def _show_flash() -> None:
    for item in st.session_state.pop(FLASH_KEY, []) or []:
        level = item.get("level", "success")
        if level == "warning":
            st.warning(item["message"])
        elif level == "error":
            st.error(item["message"])
        else:
            st.success(item["message"])


def _option_index(options: Sequence[str], value: Any, default: int = 0) -> int:
    text = str(value or "")
    return options.index(text) if text in options else default


def _employee_label(employee: dict) -> str:
    name = str(employee.get("name") or "未命名")
    number = str(employee.get("employee_no") or employee.get("id") or "")
    return f"{name}（{number}）" if number else name


# =============================================================================
# 专项附加扣除输入
# =============================================================================
def _special_deduction_inputs(prefix: str, defaults: dict | None = None) -> dict:
    """专项附加扣除明细的输入控件（子女/婴幼儿/继续教育/住房/赡养老人）。"""
    defaults = defaults or {}
    try:
        children_default = int(defaults.get("children") or 0)
    except (TypeError, ValueError):
        children_default = 0
    try:
        infants_default = int(defaults.get("infants") or 0)
    except (TypeError, ValueError):
        infants_default = 0
    cols = st.columns(5)
    with cols[0]:
        children = st.number_input(
            "子女数（个）", min_value=0, max_value=10, value=children_default, step=1,
            key=f"{prefix}_children",
            help="每个子女 2,000 元/月",
        )
    with cols[1]:
        infants = st.number_input(
            "3 岁以下婴幼儿数（个）", min_value=0, max_value=10, value=infants_default, step=1,
            key=f"{prefix}_infants",
            help="每个婴幼儿 2,000 元/月",
        )
    with cols[2]:
        continuing = st.selectbox(
            "继续教育",
            PA.CONTINUING_EDUCATION_OPTIONS,
            index=_option_index(PA.CONTINUING_EDUCATION_OPTIONS, defaults.get("continuing_education")),
            key=f"{prefix}_continuing",
        )
    with cols[3]:
        housing = st.selectbox(
            "住房贷款 / 租金",
            PA.HOUSING_OPTIONS,
            index=_option_index(PA.HOUSING_OPTIONS, defaults.get("housing")),
            key=f"{prefix}_housing",
            help="住房贷款利息与住房租金不可同时享受，系统按较高标准计入",
        )
    with cols[4]:
        elderly = st.selectbox(
            "赡养老人",
            PA.ELDERLY_OPTIONS,
            index=_option_index(PA.ELDERLY_OPTIONS, defaults.get("elderly")),
            key=f"{prefix}_elderly",
            help="独生子女 3,000 元/月；非独生分摊上限 1,500 元/月",
        )
    return {
        "children": int(children),
        "infants": int(infants),
        "continuing_education": continuing,
        "housing": housing,
        "elderly": elderly,
    }


def _special_deduction_json_from_inputs(values: dict) -> dict:
    return PA.special_deduction_json(
        children=values.get("children", 0),
        infants=values.get("infants", 0),
        continuing_education=values.get("continuing_education", "不享受"),
        housing=values.get("housing", "不享受"),
        elderly=values.get("elderly", "不享受"),
    )


# =============================================================================
# 员工管理
# =============================================================================
def _render_new_employee_form() -> None:
    """新增员工表单。"""
    with st.form("payroll_new_employee_form", clear_on_submit=True):
        cols = st.columns(3)
        with cols[0]:
            name = st.text_input("姓名 *", key="payroll_new_name", placeholder="员工姓名")
            employee_no = st.text_input("工号", key="payroll_new_no")
            id_card = st.text_input(
                "身份证号", key="payroll_new_id_card",
                placeholder="仅保存脱敏值（前 6 + 后 4）",
                help="系统不会保存身份证原文，只保存脱敏后的号码。",
            )
            phone = st.text_input("手机号", key="payroll_new_phone")
            department = st.text_input("部门", key="payroll_new_department")
        with cols[1]:
            position = st.text_input("岗位", key="payroll_new_position")
            hire_date = st.date_input("入职日期", value=config.today(), key="payroll_new_hire_date")
            status = st.selectbox("状态", PA.EMPLOYEE_STATUSES, index=0, key="payroll_new_status")
            new_leave_date = st.date_input(
                "离职日期（状态=离职时必填）", value=config.today(), key="payroll_new_leave_date"
            )
            social_base = st.number_input(
                "社保缴费基数（元）", min_value=0.0, step=100.0, format="%.2f", key="payroll_new_social"
            )
            fund_base = st.number_input(
                "公积金缴费基数（元）", min_value=0.0, step=100.0, format="%.2f", key="payroll_new_fund"
            )
        with cols[2]:
            deduction_choice = st.selectbox(
                "减除费用方式", PA.BASIC_DEDUCTION_CHOICES, index=0, key="payroll_new_deduction_choice"
            )
            qualified = st.checkbox("享受基本减除费用", value=True, key="payroll_new_qualified")
            note = st.text_input("备注", key="payroll_new_note")
            st.caption("专项附加扣除按月计入累计预扣；大病医疗仅在年度汇算扣除。")
        special_values = _special_deduction_inputs("payroll_new_special", {})
        submitted = st.form_submit_button("保存员工", type="primary", width="content")
    if not submitted:
        return
    if not name.strip():
        st.error("员工姓名不能为空。")
        return
    if not PA.employees_ready():
        st.warning("员工档案模块尚未就绪，暂无法保存。")
        return
    payload = {
        "name": name.strip(),
        "employee_no": employee_no.strip(),
        "id_card_masked": PA.mask_id_card(id_card) if id_card.strip() else "",
        "phone": phone.strip(),
        "department": department.strip(),
        "position": position.strip(),
        "hire_date": hire_date.isoformat(),
        "status": status,
        "leave_date": new_leave_date.isoformat() if status == "离职" else "",
        "social_insurance_base": float(social_base),
        "housing_fund_base": float(fund_base),
        "special_deduction_json": _special_deduction_json_from_inputs(special_values),
        "deduction_choice": deduction_choice,
        "is_deduction_qualified": bool(qualified),
        "note": note.strip(),
    }
    try:
        record = PA.add_employee(payload)
    except Exception as exc:  # noqa: BLE001 - ValueError 中文提示
        st.error(f"保存员工失败：{exc}")
        return
    cache.clear()
    _flash(f"已新增员工 {record.get('name') or name}（{record.get('id') or ''}）")
    st.rerun()


def _render_edit_employee_form(employees: Sequence[dict]) -> None:
    """编辑员工：先选员工，再展示预填表单。"""
    if not employees:
        st.caption("暂无员工可编辑。")
        return
    ids = [str(item.get("id")) for item in employees]
    label_map = {str(item.get("id")): _employee_label(item) for item in employees}
    selected_id = st.selectbox(
        "选择要编辑的员工",
        ids,
        format_func=lambda employee_id: label_map.get(str(employee_id), str(employee_id)),
        key="payroll_edit_select",
    )
    employee = next((item for item in employees if str(item.get("id")) == str(selected_id)), {})
    if not employee:
        return
    employee_id = str(employee.get("id"))
    special_defaults = PA.parse_special_deduction(employee)
    with st.form(f"payroll_edit_form_{employee_id}"):
        cols = st.columns(3)
        with cols[0]:
            name = st.text_input(
                "姓名 *", value=str(employee.get("name") or ""), key=f"payroll_edit_name_{employee_id}"
            )
            employee_no = st.text_input(
                "工号", value=str(employee.get("employee_no") or ""), key=f"payroll_edit_no_{employee_id}"
            )
            phone = st.text_input(
                "手机号", value=str(employee.get("phone") or ""), key=f"payroll_edit_phone_{employee_id}"
            )
            department = st.text_input(
                "部门", value=str(employee.get("department") or ""), key=f"payroll_edit_department_{employee_id}"
            )
            position = st.text_input(
                "岗位", value=str(employee.get("position") or ""), key=f"payroll_edit_position_{employee_id}"
            )
        with cols[1]:
            hire_raw = str(employee.get("hire_date") or "")
            try:
                hire_value = date.fromisoformat(hire_raw[:10]) if hire_raw else config.today()
            except ValueError:
                hire_value = config.today()
            hire_date = st.date_input("入职日期", value=hire_value, key=f"payroll_edit_hire_{employee_id}")
            status = st.selectbox(
                "状态",
                PA.EMPLOYEE_STATUSES,
                index=_option_index(PA.EMPLOYEE_STATUSES, employee.get("status"), 0),
                key=f"payroll_edit_status_{employee_id}",
                help="改为「离职」时必须填写离职日期（实际最后工作日）",
            )
            leave_raw = str(employee.get("leave_date") or "")
            try:
                leave_value = date.fromisoformat(leave_raw[:10]) if leave_raw else config.today()
            except ValueError:
                leave_value = config.today()
            edit_leave_date = st.date_input(
                "离职日期（状态=离职时必填）", value=leave_value, key=f"payroll_edit_leave_{employee_id}"
            )
            social_base = st.number_input(
                "社保缴费基数（元）", min_value=0.0, step=100.0, format="%.2f",
                value=F.to_float(employee.get("social_insurance_base")),
                key=f"payroll_edit_social_{employee_id}",
            )
            fund_base = st.number_input(
                "公积金缴费基数（元）", min_value=0.0, step=100.0, format="%.2f",
                value=F.to_float(employee.get("housing_fund_base")),
                key=f"payroll_edit_fund_{employee_id}",
            )
        with cols[2]:
            deduction_choice = st.selectbox(
                "减除费用方式",
                PA.BASIC_DEDUCTION_CHOICES,
                index=_option_index(PA.BASIC_DEDUCTION_CHOICES, employee.get("deduction_choice"), 0),
                key=f"payroll_edit_choice_{employee_id}",
            )
            qualified = st.checkbox(
                "享受基本减除费用",
                value=bool(employee.get("is_deduction_qualified", True)),
                key=f"payroll_edit_qualified_{employee_id}",
            )
            note = st.text_input(
                "备注", value=str(employee.get("note") or ""), key=f"payroll_edit_note_{employee_id}"
            )
            st.caption(f"身份证（脱敏）：{employee.get('id_card_masked') or '—'}")
        special_values = _special_deduction_inputs(f"payroll_edit_special_{employee_id}", special_defaults)
        submitted = st.form_submit_button("保存修改", type="primary", width="content")
    if not submitted:
        return
    if not name.strip():
        st.error("员工姓名不能为空。")
        return
    patch = {
        "name": name.strip(),
        "employee_no": employee_no.strip(),
        "phone": phone.strip(),
        "department": department.strip(),
        "position": position.strip(),
        "hire_date": hire_date.isoformat(),
        "status": status,
        "leave_date": edit_leave_date.isoformat() if status == "离职" else "",
        "social_insurance_base": float(social_base),
        "housing_fund_base": float(fund_base),
        "special_deduction_json": _special_deduction_json_from_inputs(special_values),
        "deduction_choice": deduction_choice,
        "is_deduction_qualified": bool(qualified),
        "note": note.strip(),
    }
    try:
        record = PA.update_employee(employee_id, patch)
    except Exception as exc:  # noqa: BLE001
        st.error(f"保存员工失败：{exc}")
        return
    cache.clear()
    _flash(f"员工 {record.get('name') or name} 信息已更新")
    st.rerun()


def _render_employee_actions(employees: Sequence[dict]) -> None:
    """员工操作：标记离职 / 删除（删除需确认）。"""
    if not employees:
        return
    ids = [str(item.get("id")) for item in employees]
    label_map = {str(item.get("id")): _employee_label(item) for item in employees}
    st.markdown("**员工操作（全部员工）**")
    selected_id = st.selectbox(
        "选择员工",
        ids,
        format_func=lambda employee_id: label_map.get(str(employee_id), str(employee_id)),
        key="payroll_employee_action_select",
    )
    employee = next((item for item in employees if str(item.get("id")) == str(selected_id)), {})
    is_left = str(employee.get("status")) == "离职"
    leave_date = st.date_input(
        "离职日期（标记离职必填）", value=date.today(), key="payroll_employee_leave_date"
    )
    cols = st.columns([1, 1, 1, 3])
    with cols[0]:
        if st.button(
            "标记离职",
            key="payroll_employee_leave",
            width="stretch",
            disabled=is_left or not PA.employees_ready(),
        ):
            try:
                PA.update_employee(str(selected_id), {"status": "离职", "leave_date": str(leave_date)})
            except Exception as exc:  # noqa: BLE001
                st.error(f"标记离职失败：{exc}")
            else:
                cache.clear()
                _flash(f"{label_map.get(str(selected_id), selected_id)} 已标记为离职", "warning")
                st.rerun()
    with cols[1]:
        confirm = st.checkbox("确认删除", key="payroll_employee_delete_confirm")
    with cols[2]:
        if st.button(
            "删除员工",
            key="payroll_employee_delete",
            width="stretch",
            disabled=not confirm or not PA.employees_ready(),
            help="有工资记录的员工不能删除，应改为「离职」",
        ):
            try:
                PA.delete_employee(str(selected_id))
            except Exception as exc:  # noqa: BLE001 - 后端 ValueError 中文提示
                st.error(str(exc))
            else:
                cache.clear()
                _flash(f"已删除员工 {label_map.get(str(selected_id), selected_id)}", "warning")
                st.rerun()
    with cols[3]:
        st.caption("有工资记录的员工不可删除（请改为离职）；删除仅适用于录入错误的员工。")


def _render_employee_tab() -> None:
    """员工管理 tab。"""
    ui.section(
        "👥 员工管理",
        "维护员工档案、社保/公积金基数与专项附加扣除；身份证号仅保存脱敏值（前 6 + 后 4）。",
    )
    if not PA.employees_ready():
        ui.module_missing("员工档案", "ledger.employees", detail=PA.missing_detail())
    summary = PA.summarize_employees()
    ui.metric_row(
        [
            {"label": "在职员工", "value": f"{int(summary.get('active') or 0)} 人"},
            {"label": "离职员工", "value": f"{int(summary.get('left') or 0)} 人"},
            {"label": "社保基数合计", "value": F.fmt_money(summary.get("total_social_base"))},
            {"label": "公积金基数合计", "value": F.fmt_money(summary.get("total_housing_fund_base"))},
            {"label": "专项附加扣除/月", "value": F.fmt_money(summary.get("total_special_deduction"))},
        ]
    )
    if not summary.get("active"):
        ui.empty_state(
            "还没有员工档案",
            "请先用下方「新增员工」录入员工信息，再到「月度工资表」生成工资表。",
            icon="👥",
            show_seed=False,
        )

    all_employees = PA.list_employees(status="全部", include_left=True)
    with st.expander("➕ 新增员工", expanded=not all_employees):
        _render_new_employee_form()
    with st.expander("✏️ 编辑员工", expanded=False):
        _render_edit_employee_form(all_employees)

    ui.section("📋 员工列表", "可按在职/离职筛选。")
    status_filter = st.radio(
        "员工范围", EMPLOYEE_STATUS_FILTERS, horizontal=True, key="payroll_employee_status_filter"
    )
    rows = PA.list_employees(status=status_filter, include_left=(status_filter != "在职"))
    display_rows = []
    for row in rows:
        item = dict(row)
        item["special_text"] = PA.special_deduction_text(row)
        display_rows.append(item)
    if display_rows:
        ui.records_table(
            display_rows,
            EMPLOYEE_TABLE_COLUMNS,
            key="payroll_employee_table",
            empty_text="暂无员工记录",
            money_fields={"social_insurance_base", "housing_fund_base"},
        )
    else:
        st.info(f"没有{'' if status_filter != '全部' else ''}员工记录。", icon="👥")
    _render_employee_actions(all_employees)

    with st.expander("📚 累计预扣率表与专项附加扣除标准", expanded=False):
        rates = PA.rate_table()
        if rates:
            st.dataframe(pd.DataFrame(rates), hide_index=True, width="stretch")
        standards = PA.special_standards()
        if standards:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "扣除项目": value.get("label", key),
                            "标准": value.get("note", "—"),
                        }
                        for key, value in standards.items()
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
        st.caption(
            f"基本减除费用 {F.fmt_money(PA.basic_deduction_per_month())}/月；"
            "2026 年起减除费用按实际申报月份累计，未申报月份额度作废。"
        )
    theme.disclaimer()


# =============================================================================
# 月度工资表
# =============================================================================
def _build_payroll(period: str) -> None:
    try:
        result = PA.build_payroll(period)
    except Exception as exc:  # noqa: BLE001
        st.error(f"生成工资表失败：{exc}")
        return
    cache.clear()
    created = int(result.get("created") or 0)
    skipped = result.get("skipped") or []
    message = f"{PA.month_label(period)} 工资表已生成：新增 {created} 条"
    if skipped:
        message += f"，跳过 {len(skipped)} 条（已存在或不在职）"
    _flash(message)
    st.rerun()


def _recalc_payroll(period: str) -> None:
    try:
        result = PA.recalc_period(period)
    except Exception as exc:  # noqa: BLE001
        st.error(f"重新计算个税失败：{exc}")
        return
    cache.clear()
    _flash(f"{PA.month_label(period)} 个税重算完成，本期应预扣合计 {F.fmt_money(result.get('total_tax'))}")
    for warning in result.get("warnings") or []:
        _flash(str(warning), "warning")
    st.rerun()


def _confirm_payroll(period: str) -> None:
    try:
        result = PA.confirm_period(period)
    except Exception as exc:  # noqa: BLE001 - 期间锁等中文提示
        st.error(f"确认工资表失败：{exc}")
        return
    st.session_state[STATE_CONFIRM_RESULT] = {"period": period, "result": result}
    cache.clear()
    entries = result.get("entries") or []
    _flash(f"{PA.month_label(period)} 工资表已确认，生成 {len(entries)} 张工资凭证")
    st.rerun()


def _declare_payroll(period: str) -> None:
    try:
        result = PA.mark_period_declared(period)
    except Exception as exc:  # noqa: BLE001
        st.error(f"标记已申报失败：{exc}")
        return
    cache.clear()
    _flash(f"{PA.month_label(period)} 已标记为「已申报」，共 {int(result.get('count') or 0)} 条")
    st.rerun()


def _zero_declare_payroll(period: str) -> None:
    try:
        PA.mark_zero_declaration(period)
    except Exception as exc:  # noqa: BLE001
        st.error(f"本期零申报失败：{exc}")
        return
    cache.clear()
    _flash(f"{PA.month_label(period)} 已做零申报（收入 0），保留本月减除费用额度")
    st.rerun()


def _payroll_frame(rows: Sequence[dict]) -> pd.DataFrame:
    """工资表记录 → data_editor 数据框（中文表头，金额为数值列）。"""
    records = []
    for row in rows:
        is_zero = str(row.get("employee_id")) == PA.ZERO_EMPLOYEE_ID
        record = {
            "编号": str(row.get("id") or ""),
            "姓名": "（零申报）" if is_zero else str(row.get("employee_name") or ""),
            "员工编号": "" if is_zero else str(row.get("employee_id") or ""),
            "应发工资": F.to_float(row.get("gross_salary")),
            "奖金": F.to_float(row.get("bonus")),
            "补贴": F.to_float(row.get("allowance")),
            "个人社保": F.to_float(row.get("social_insurance")),
            "个人公积金": F.to_float(row.get("housing_fund")),
            "专项附加扣除": F.to_float(row.get("special_deduction")),
            "其他扣除": F.to_float(row.get("other_deduction")),
            "累计应纳税所得额": F.to_float(row.get("cumulative_taxable")),
            "预扣率": PA.tax_rate_for(row.get("cumulative_taxable")),
            "本期应预扣个税": F.to_float(row.get("tax_withheld")),
            "实发工资": F.to_float(row.get("net_pay")),
            "状态": str(row.get("status") or "草稿"),
            "凭证号": str(row.get("journal_entry_no") or ""),
            "备注": str(row.get("note") or ""),
        }
        records.append(record)
    return pd.DataFrame(records)


def _payroll_column_config() -> dict[str, Any]:
    config_map: dict[str, Any] = {
        "编号": st.column_config.TextColumn("编号", width="small"),
        "员工编号": st.column_config.TextColumn("员工编号", width="small"),
        "累计应纳税所得额": st.column_config.NumberColumn("累计应纳税所得额", format="¥%.2f"),
        "预扣率": st.column_config.TextColumn("预扣率", width="small"),
        "本期应预扣个税": st.column_config.NumberColumn("本期应预扣个税", format="¥%.2f"),
        "实发工资": st.column_config.NumberColumn("实发工资", format="¥%.2f"),
        "状态": st.column_config.TextColumn("状态", width="small"),
        "凭证号": st.column_config.TextColumn("凭证号"),
        "备注": st.column_config.TextColumn("备注"),
    }
    for label in PAYROLL_MONEY_COLUMNS:
        if label in {"累计应纳税所得额", "本期应预扣个税", "实发工资"}:
            continue
        config_map[label] = st.column_config.NumberColumn(label, format="¥%.2f", step=0.01)
    return config_map


def _save_payroll_edits(rows: Sequence[dict], edited: pd.DataFrame) -> None:
    originals = {str(row.get("id")): row for row in rows}
    updated = 0
    errors: list[str] = []
    for _, record in edited.iterrows():
        payroll_id = str(record.get("编号") or "").strip()
        original = originals.get(payroll_id)
        if not payroll_id or original is None:
            continue
        patch: dict[str, Any] = {}
        for label, field in PA.EDITABLE_PAYROLL_FIELDS:
            new_value = F.to_float(record.get(label))
            if abs(new_value - F.to_float(original.get(field))) > 0.004:
                patch[field] = new_value
        new_note = "" if pd.isna(record.get("备注")) else str(record.get("备注")).strip()
        if new_note != str(original.get("note") or ""):
            patch["note"] = new_note
        if not patch:
            continue
        try:
            PA.update_payroll_row(payroll_id, patch)
            updated += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{original.get('employee_name') or payroll_id}：{exc}")
    for message in errors:
        st.error(message)
    if updated:
        cache.clear()
        _flash(f"已保存 {updated} 条工资记录，请点击「重新计算个税」刷新累计预扣结果。")
        st.rerun()
    elif not errors:
        st.info("没有检测到工资表修改。")


def _render_confirm_result(period: str) -> None:
    payload = st.session_state.get(STATE_CONFIRM_RESULT) or {}
    if str(payload.get("period") or "") != str(period):
        return
    result = payload.get("result") or {}
    entries = result.get("entries") or []
    with st.container(border=True):
        st.markdown("**最近一次确认工资表结果**")
        rows = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            amount = entry.get("total_amount") or entry.get("total")
            if amount in (None, ""):
                lines = entry.get("lines") or []
                amount = sum(
                    F.to_float(line.get("amount"))
                    for line in lines
                    if isinstance(line, dict) and str(line.get("direction")) == "借"
                )
            rows.append(
                {
                    "凭证号": entry.get("entry_no") or "—",
                    "摘要": entry.get("description") or "工资凭证",
                    "金额": F.fmt_money(amount),
                }
            )
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        totals = result.get("totals") or {}

        def _total(*keys: str) -> Any:
            for key in keys:
                if totals.get(key) not in (None, ""):
                    return totals.get(key)
            return 0.0

        if totals:
            ui.metric_row(
                [
                    {"label": "应发合计", "value": F.fmt_money(_total("total_gross", "gross"))},
                    {"label": "实发合计", "value": F.fmt_money(_total("total_net", "net"))},
                    {"label": "预扣个税合计", "value": F.fmt_money(_total("total_tax", "tax"))},
                ],
                columns=3,
            )
        st.caption("工资计提与发放代扣凭证已生成；凭证号可在工资表「凭证号」列核对。")


def _render_payroll_tab() -> None:
    """月度工资表 tab。"""
    ui.section(
        "💰 月度工资表",
        "生成工资表 → 编辑应发/扣除 → 重新计算个税（累计预扣法）→ 确认生成工资凭证 → 扣缴端申报后标记已申报。",
    )
    if not PA.payroll_ready():
        ui.module_missing("月度工资表", "ledger.payroll", detail=PA.missing_detail())
    months = PA.month_options_list(12)
    current = PA.current_month_key()
    month_index = months.index(current) if current in months else 0
    period = st.selectbox(
        "工资月份", months, index=month_index, format_func=PA.month_label, key="payroll_month"
    )

    action_cols = st.columns([2, 2, 2, 2, 2, 3])
    actions = [
        ("🧮 生成工资表", "payroll_build", PA.payroll_ready(), lambda: _build_payroll(period)),
        ("🔁 重新计算个税", "payroll_recalc", PA.recalc_ready(), lambda: _recalc_payroll(period)),
        ("✅ 确认并生成凭证", "payroll_confirm", PA.confirm_ready(), lambda: _confirm_payroll(period)),
        ("📤 标记已申报", "payroll_declare", PA.declare_ready(), lambda: _declare_payroll(period)),
        ("🧾 本期零申报", "payroll_zero", PA.zero_declaration_ready(), lambda: _zero_declare_payroll(period)),
    ]
    for column, (label, key, ready, handler) in zip(action_cols, actions):
        with column:
            if st.button(label, key=key, width="stretch", disabled=not ready, type="primary" if key == "payroll_build" else "secondary"):
                handler()
    with action_cols[5]:
        st.caption("确认凭证前请先「重新计算个税」；已确认/已申报行不可修改。")

    summary = PA.summarize_period(period)
    status_counts = summary.get("status_counts") or {}
    status_text = "、".join(f"{name} {count}" for name, count in status_counts.items()) or "未生成"
    ui.metric_row(
        [
            {"label": "发薪人数", "value": f"{int(summary.get('headcount') or 0)} 人"},
            {"label": "应发合计", "value": F.fmt_money(summary.get("total_gross"))},
            {"label": "社保+公积金", "value": F.fmt_money((summary.get("total_social") or 0) + (summary.get("total_housing_fund") or 0))},
            {"label": "本期应预扣个税", "value": F.fmt_money(summary.get("total_tax"))},
            {"label": "实发合计", "value": F.fmt_money(summary.get("total_net"))},
            {"label": "工资表状态", "value": status_text},
        ]
    )
    st.caption(f"税款所属期：{PA.month_label(period)} · 基本减除费用 {F.fmt_money(PA.basic_deduction_per_month())}/月/人")

    rows = PA.list_payroll(period)
    if not rows:
        st.info(
            f"{PA.month_label(period)} 暂无工资表。",
            icon="💰",
        )
        st.caption("若本月未发工资，建议点击「🧾 本期零申报」以保留减除费用额度。")
    else:
        frame = _payroll_frame(rows)
        edited = st.data_editor(
            frame,
            column_config=_payroll_column_config(),
            disabled=[column for column in frame.columns if column not in {label for label, _ in PA.EDITABLE_PAYROLL_FIELDS} | {"备注"}],
            hide_index=True,
            num_rows="fixed",
            key="payroll_editor",
            width="stretch",
            height=min(520, 100 + 38 * max(len(frame), 1)),
        )
        save_cols = st.columns([2, 2, 5])
        with save_cols[0]:
            if st.button("💾 保存工资表修改", key="payroll_save", type="primary", width="stretch"):
                _save_payroll_edits(rows, edited)
        with save_cols[1]:
            st.caption("金额均为元；编辑后请重新计算个税再确认。")

    _render_confirm_result(period)

    with st.expander("📌 2026 年扣缴端减除费用规则", expanded=False):
        st.info(PA.zero_declaration_note(), icon="📌")
    with st.expander("📚 累计预扣率表", expanded=False):
        table = PA.rate_table()
        if table:
            st.dataframe(pd.DataFrame(table), hide_index=True, width="stretch")
        st.caption("个税计算由本地确定性引擎完成，不经过 AI；结果仅供参考，以扣缴端口径为准。")
    theme.disclaimer()


# =============================================================================
# 扣缴申报辅助表
# =============================================================================
def _bundle_line_value(value: Any, key: str) -> Any:
    """bundle 行值格式化（金额加千分位 / 税率百分比）。"""
    if key in BUNDLE_MONEY_KEYS or key in {"value", "expected", "actual"}:
        try:
            return F.fmt_money(value)
        except Exception:  # noqa: BLE001
            return value
    if key in {"rate", "applicable_rate"}:
        try:
            return F.fmt_pct(float(value))
        except (TypeError, ValueError):
            return value
    return value


def _render_bundle_section(section: dict) -> None:
    """渲染申报表 bundle 的一个分区（主表 / 员工明细 / 政策依据）。"""
    lines = section.get("lines") or []
    title = section.get("title") or "明细"
    if not lines:
        return
    st.markdown(f"**{title}**")
    # 政策依据分区：只展示名称 / 来源文号 / 口径说明（金额无意义）
    if "政策" in title:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "政策依据": line.get("name") or "",
                        "来源 / 文号": line.get("source") or "",
                        "口径说明": line.get("rule") or "",
                    }
                    for line in lines
                    if isinstance(line, dict)
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        return
    # 员工明细分区：带 employee_name / income 等明细字段
    if any(
        isinstance(line, dict) and ("employee_name" in line or "income" in line) for line in lines
    ):
        records = []
        for line in lines:
            if not isinstance(line, dict):
                continue
            record = {}
            for key, label in EMPLOYEE_DETAIL_KEYS:
                if key in line and label not in record:
                    record[label] = _bundle_line_value(line.get(key), key)
            records.append(record)
        if records:
            st.dataframe(pd.DataFrame(records), hide_index=True, width="stretch")
        return
    # 标准申报表栏次分区
    if any(isinstance(line, dict) and "line_no" in line for line in lines):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "栏次": ("⭐ " if line.get("highlight") else "") + str(line.get("line_no") or ""),
                        "名称": line.get("name") or "",
                        "金额": F.fmt_money(line.get("value")),
                        "来源": line.get("source") or "",
                        "口径": line.get("rule") or "",
                    }
                    for line in lines
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        return
    records = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        record = {}
        for key, value in line.items():
            if key in {"highlight", "line_no"}:
                continue
            label = BUNDLE_FIELD_LABELS.get(key, key)
            record[label] = _bundle_line_value(value, key)
        records.append(record)
    if records:
        st.dataframe(pd.DataFrame(records), hide_index=True, width="stretch")


def _render_iit_bundle(period: str) -> None:
    payload = st.session_state.get(STATE_IIT_BUNDLE) or {}
    if str(payload.get("period") or "") != str(period):
        return
    bundle = payload.get("bundle") or {}
    if not bundle:
        return
    st.caption(
        f"{bundle.get('form_name') or '个人所得税扣缴申报辅助表'} · "
        f"税款所属期：{bundle.get('period_label') or PA.month_label(period)} · "
        f"生成时间：{bundle.get('generated_at') or '—'}"
    )
    summary = bundle.get("summary") or {}
    if summary:
        ui.status_alert(summary.get("level") or "PASS", summary.get("text") or "辅助表已生成。")
    totals = bundle.get("totals") or {}
    if totals:
        items = []
        for key, value in totals.items():
            label = BUNDLE_TOTAL_LABELS.get(key, key)
            if key == "headcount" or "count" in key:
                text = f"{int(value)} 人" if isinstance(value, (int, float)) else str(value)
            elif isinstance(value, (int, float)):
                text = F.fmt_money(value)
            else:
                continue
            items.append({"label": label, "value": text})
            if len(items) >= 6:
                break
        if items:
            ui.metric_row(items)
    for section in bundle.get("sections") or []:
        _render_bundle_section(section)
    checks = bundle.get("checks") or []
    if isinstance(checks, list) and checks:
        st.markdown("**校验结果**")
        ui.status_table(checks)
    guide = bundle.get("guide") or []
    if guide:
        with st.expander("📖 扣缴端操作步骤", expanded=False):
            for step in guide:
                st.markdown(f"- {step}")
    sources = bundle.get("policy_sources") or PA.iit_sources()
    if sources:
        with st.expander("📚 政策依据与预扣率表", expanded=False):
            for source in sources:
                st.markdown(
                    f"- **{source.get('title')}**\n"
                    f"  来源：{source.get('source')} · 生效：{source.get('effective_date')} · "
                    f"到期：{source.get('expiry_date')}"
                )
            table = PA.rate_table()
            if table:
                st.dataframe(pd.DataFrame(table), hide_index=True, width="stretch")
    theme.disclaimer(bundle.get("disclaimer"))


def _build_iit_return_bundle(period: str) -> None:
    try:
        bundle = PA.build_iit_return(period)
    except Exception as exc:  # noqa: BLE001
        st.error(f"生成扣缴申报辅助表失败：{exc}")
        return
    st.session_state[STATE_IIT_BUNDLE] = {"period": period, "bundle": bundle}
    st.session_state.pop(STATE_EXPORT_PATH, None)
    cache.clear()
    _flash(f"{PA.month_label(period)} 扣缴申报辅助表已生成")
    st.rerun()


def _export_iit_excel(period: str) -> None:
    payload = st.session_state.get(STATE_IIT_BUNDLE) or {}
    bundle = payload.get("bundle") or {}
    if str(payload.get("period") or "") != str(period) or not bundle:
        st.warning("请先生成当月扣缴申报辅助表，再导出 Excel。")
        return
    try:
        path = PA.export_iit_excel(bundle)
    except Exception as exc:  # noqa: BLE001
        st.error(f"导出 Excel 失败：{exc}")
        return
    st.session_state[STATE_EXPORT_PATH] = str(path)
    st.rerun()


def _render_iit_return_tab() -> None:
    """扣缴申报辅助表 tab。"""
    ui.section(
        "📋 扣缴申报辅助表",
        "生成工资薪金扣缴辅助表用于核对；**实际申报需在自然人电子税务局扣缴端完成**，本系统不代替申报。",
    )
    if not PA.iit_return_ready():
        ui.module_missing("扣缴申报辅助表", "filing.iit_return", detail=PA.missing_detail())
    months = PA.month_options_list(12)
    period = st.selectbox(
        "申报月份", months, index=0, format_func=PA.month_label, key="payroll_return_month"
    )
    cols = st.columns([2, 2, 5])
    with cols[0]:
        if st.button(
            "📋 生成辅助表",
            key="payroll_return_build",
            type="primary",
            width="stretch",
            disabled=not PA.iit_return_ready(),
        ):
            _build_iit_return_bundle(period)
    with cols[1]:
        if st.button(
            "📊 生成 Excel",
            key="payroll_return_export",
            width="stretch",
            disabled=not PA.iit_export_ready(),
        ):
            _export_iit_excel(period)
    with cols[2]:
        st.caption("导出 Excel 含主表、员工明细、政策依据与操作步骤 3 个工作表。")

    export_path_raw = st.session_state.get(STATE_EXPORT_PATH)
    if export_path_raw and Path(str(export_path_raw)).exists():
        export_path = Path(str(export_path_raw))
        st.download_button(
            "⬇️ 下载扣缴申报辅助表 Excel",
            data=export_path.read_bytes(),
            file_name=export_path.name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="payroll_return_download",
            width="content",
        )

    _render_iit_bundle(period)
    if not (st.session_state.get(STATE_IIT_BUNDLE) or {}):
        st.info(
            "点击「生成辅助表」后，可查看主表汇总、员工明细、校验结果与扣缴端操作步骤。",
            icon="📋",
        )
    st.caption(
        "说明：DeepSeek 与本地系统均不代替电子税务局申报；请在扣缴端完成人员信息采集、"
        "综合所得申报、税款计算与缴款后，回到「月度工资表」点击「标记已申报」。"
    )
    theme.disclaimer()




# =============================================================================
# 离职与纠错（P1-E）
# =============================================================================
def _render_correction_tab() -> None:
    """离职后多申报扫描、更正方案与个税异议申诉台账。"""
    st.caption(
        "员工离职后仍做工资/申报个税时：先停申报，再逐月更正；员工个税 APP 撤销申诉不等于数据已更正。"
        "本页只做扫描、测算与操作单，实际更正请在自然人电子税务局（扣缴端）完成。"
    )
    st.markdown(
        """
        **零基础 3 步走**
        1. **先看扫描结果**：下方列出“离职后还在申报”的员工与月份；没有记录说明暂时不用处理。
        2. **点「生成更正方案」**：系统自动算出差额（多缴可退/少缴需补），并生成《更正操作单》。
        3. **照着操作单做**：打开扣缴端，按“九步操作”逐月更正；退税/补税、企业所得税与账务按提示处理。
        """
    )
    try:
        from ledger import corrections, employees as employee_api
    except Exception as exc:  # noqa: BLE001
        st.warning(f"离职纠错模块不可用：{exc}")
        return

    try:
        scan = corrections.scan_departed_overpayments()
    except Exception as exc:  # noqa: BLE001
        st.error(f"扫描失败：{exc}")
        return
    summary = scan.get("summary") or {}
    ui.metric_row(
        [
            {"label": "涉及员工", "value": f"{int(summary.get('employees') or 0)} 人"},
            {"label": "需更正月份", "value": f"{int(summary.get('months') or 0)} 个"},
            {"label": "涉及工资", "value": F.fmt_money(summary.get("income"))},
            {"label": "已预扣个税", "value": F.fmt_money(summary.get("tax"))},
        ],
        columns=4,
    )

    plan = st.session_state.get("correction_plan")
    if not scan.get("items"):
        st.success("✅ 未发现「已离职但离职后仍有工资/个税申报」的记录。", icon="🧯")
    else:
        frame = pd.DataFrame(
            [
                {
                    "员工": item["employee_name"],
                    "离职日期": item["leave_date"],
                    "需更正月份": "、".join(item["periods"]),
                    "涉及工资": item["income_total"],
                    "已预扣个税": item["tax_withheld"],
                    "记录状态": "、".join(item["row_statuses"]),
                }
                for item in scan["items"]
            ]
        )
        st.dataframe(frame, width="stretch", hide_index=True)
        options = {f"{item['employee_name']}（{item['employee_id']}）": item["employee_id"] for item in scan["items"]}
        selected = st.selectbox("选择员工生成更正方案", list(options), key="correction_employee")
        if st.button("🧮 生成更正方案", key="correction_build", type="primary", width="stretch"):
            try:
                st.session_state["correction_plan"] = corrections.build_correction_plan(options[selected])
                plan = st.session_state["correction_plan"]
            except Exception as exc:  # noqa: BLE001
                st.error(f"生成失败：{exc}")

    pending = scan.get("pending_leave_date") or []
    if pending:
        st.warning(
            "有员工已标记「离职」但未填写离职日期，系统无法自动扫描其离职后申报："
            + "、".join(item["employee_name"] for item in pending)
            + "。请补录离职日期，或使用下方「手动生成更正方案」。"
        )
        with st.expander("📝 补录离职日期", expanded=False):
            pending_map = {f"{item['employee_name']}（{item['employee_id']}）": item["employee_id"] for item in pending}
            pending_label = st.selectbox("员工", list(pending_map), key="pending_leave_employee")
            pending_date = st.date_input("离职日期", value=date.today(), key="pending_leave_date")
            if st.button("💾 保存离职日期", key="pending_leave_save", type="primary"):
                try:
                    employee_api.update_employee(
                        pending_map[pending_label],
                        {"status": "离职", "leave_date": str(pending_date)},
                    )
                    st.success("已补录离职日期，正在重新扫描…")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"保存失败：{exc}")

    with st.expander("🛠️ 员工忘记登记离职 / 没有离职日期？手动生成更正方案", expanded=False):
        st.caption(
            "适用于：员工实际已离职，但系统里仍是「在职」或没有离职日期，无法自动扫描；"
            "可手动指定离职月份，或直接勾选员工在个税 APP 申诉/误申报的月份。"
            "建议先用银行流水、考勤、离职沟通记录确认实际离职月份。"
        )
        try:
            from ledger import payroll as payroll_api

            all_people = employee_api.list_employees(status=None, include_left=True)
        except Exception as exc:  # noqa: BLE001
            all_people = []
            st.warning(f"员工列表不可用：{exc}")
        if not all_people:
            st.caption("请先在「员工管理」录入员工。")
        else:
            people_map = {f"{item['name']}（{item['id']}）": item["id"] for item in all_people}
            manual_label = st.selectbox("员工", list(people_map), key="manual_employee")
            manual_id = people_map[manual_label]
            try:
                manual_periods_available = sorted(
                    {str(row.get("period")) for row in payroll_api.list_payroll(None) if str(row.get("employee_id")) == str(manual_id)}
                )
            except Exception:  # noqa: BLE001
                manual_periods_available = []
            if not manual_periods_available:
                st.info("该员工暂无工资记录，无需更正。")
            else:
                employee_for_manual = next(
                    (item for item in all_people if str(item.get("id")) == str(manual_id)), {}
                )
                raw_leave = str(employee_for_manual.get("leave_date") or "")
                try:
                    default_leave = date.fromisoformat(raw_leave[:10]) if raw_leave else date.today()
                except ValueError:
                    default_leave = date.today()
                manual_leave_date = st.date_input(
                    "离职日期（按此日期自动计算需更正月份，可自行选择）",
                    value=default_leave,
                    key=f"manual_leave_date_{manual_id}",
                )
                leave_month_text = manual_leave_date.isoformat()[:7]
                computed_removed = [item for item in manual_periods_available if item > leave_month_text]
                computed_kept = [item for item in manual_periods_available if item <= leave_month_text]
                st.caption(
                    f"该员工工资记录共 {len(manual_periods_available)} 个月"
                    f"（{manual_periods_available[0]} ~ {manual_periods_available[-1]}）；"
                    f"按离职日期 {manual_leave_date.isoformat()}："
                    f"保留月份 {'、'.join(computed_kept) or '无'}；"
                    f"应更正月份 {'、'.join(computed_removed) or '无'}。"
                )
                if not computed_removed:
                    st.info("按所选离职日期，没有需要更正的月份（离职日期晚于或等于最后工资月份）。")
                manual_removed = st.multiselect(
                    "可手动调整需更正月份（默认已按离职日期自动带出）",
                    manual_periods_available,
                    default=computed_removed,
                    key=f"manual_removed_{manual_id}_{leave_month_text}",
                )
                sync_leave = st.checkbox(
                    "同时把该离职日期写入员工档案（推荐，便于以后自动扫描）",
                    value=False,
                    key=f"manual_sync_leave_{manual_id}",
                )
                if st.button("🧮 生成手动更正方案", key="manual_build", type="primary", width="stretch"):
                    if not manual_removed:
                        st.warning("请至少选择一个需更正月份；如无需更正，说明该员工没有误申报。")
                    else:
                        try:
                            if sync_leave:
                                employee_api.update_employee(
                                    manual_id,
                                    {"status": "离职", "leave_date": manual_leave_date.isoformat()},
                                )
                            removed_override = manual_removed if manual_removed != computed_removed else None
                            st.session_state["correction_plan"] = corrections.build_correction_plan(
                                manual_id,
                                leave_month=leave_month_text,
                                removed_periods=removed_override,
                            )
                            st.success("已生成更正方案")
                            st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"生成失败：{exc}")

    if plan:
        totals = plan.get("totals") or {}
        leave_label = plan.get("leave_date") or (
            f"未登记（按 {plan.get('leave_month')} 手动指定）" if plan.get("cutoff_source") else ""
        )
        ui.section("更正方案", f"{plan.get('employee_name')} · 离职日期 {leave_label}")
        ui.metric_row(
            [
                {"label": "需更正月份", "value": f"{int(totals.get('months_to_correct') or 0)} 个"},
                {"label": "多缴个税（可退）", "value": F.fmt_money(totals.get("tax_refund"))},
                {"label": "少缴个税（需补）", "value": F.fmt_money(totals.get("tax_payable"))},
                {"label": "涉及多列工资", "value": F.fmt_money(totals.get("removed_wage_total"))},
            ],
            columns=4,
        )
        cit = plan.get("cit_impact") or {}
        st.info(
            f"企业所得税影响估算：小微 5% 约 {F.fmt_money(cit.get('small_low_profit_tax'))} 元；"
            f"一般 25% 约 {F.fmt_money(cit.get('standard_tax'))} 元。"
        )
        detail = pd.DataFrame(
            [
                {
                    "所属期": row["period"],
                    "处理动作": row["action"],
                    "原申报收入": row["original_income"],
                    "更正后收入": row["corrected_income"],
                    "原已预扣": row["original_tax"],
                    "更正后应预扣": row["corrected_tax"],
                    "差额": row["tax_diff"],
                }
                for row in plan.get("rows") or []
            ]
        )
        st.dataframe(detail, width="stretch", hide_index=True)
        with st.expander("扣缴端更正操作步骤（逐月）", expanded=True):
            for index, step in enumerate(plan.get("steps") or [], start=1):
                st.markdown(f"{index}. {step}")
        for warning in plan.get("warnings") or []:
            st.warning(warning)
        download_cols = st.columns(2)
        with download_cols[0]:
            try:
                path = corrections.export_correction_plan(plan)
                st.download_button(
                    "📊 导出《更正操作单》Excel",
                    data=Path(path).read_bytes(),
                    file_name=Path(path).name,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="correction_download",
                    width="stretch",
                )
            except Exception as exc:  # noqa: BLE001
                st.warning(f"Excel 导出失败：{exc}")
        with download_cols[1]:
            try:
                pdf_path = corrections.export_correction_plan_pdf(plan)
                st.download_button(
                    "📄 导出 PDF 操作单（可打印）",
                    data=Path(pdf_path).read_bytes(),
                    file_name=Path(pdf_path).name,
                    mime="application/pdf",
                    key="correction_pdf_download",
                    width="stretch",
                )
            except Exception as exc:  # noqa: BLE001
                st.warning(f"PDF 导出失败：{exc}")

        # 勾选式进度清单
        try:
            progress_rows = corrections.list_correction_progress(
                str(plan.get("employee_id") or ""), plan.get("steps") or []
            )
            progress_summary = corrections.correction_progress_summary(str(plan.get("employee_id") or ""))
        except Exception as exc:  # noqa: BLE001
            progress_rows, progress_summary = [], {"done": 0, "total": 0, "percent": 0}
            st.warning(f"进度清单不可用：{exc}")
        if progress_rows:
            ui.section("✅ 操作进度清单（做完一步勾一步，防止漏步）")
            st.progress(
                min(max(int(progress_summary.get("percent") or 0), 0), 100) / 100.0,
                text=f"已完成 {progress_summary.get('done')} / {progress_summary.get('total')} 步"
                f"（{progress_summary.get('percent')}%）",
            )
            for item in progress_rows:
                checked = st.checkbox(
                    item.get("step_title") or f"第 {item.get('step_no')} 步",
                    value=bool(item.get("done")),
                    key=f"corr_step_{plan.get('employee_id')}_{item.get('step_no')}",
                )
                if checked != bool(item.get("done")):
                    try:
                        corrections.set_correction_step(plan.get("employee_id"), item.get("step_no"), checked)
                    except Exception as exc:  # noqa: BLE001
                        st.warning(f"保存进度失败：{exc}")
            if st.button("↩️ 重置全部进度", key="correction_progress_reset"):
                try:
                    corrections.reset_correction_progress(str(plan.get("employee_id") or ""))
                    st.success("进度已重置")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"重置失败：{exc}")

        # 离职补偿金分项测算
        with st.expander("💰 离职补偿金分项测算（经济补偿金参考 + 一次性补偿个税）", expanded=False):
            st.caption(
                "经济补偿金按《劳动合同法》第四十七条测算；一次性补偿收入在当地上年职工平均工资 3 倍以内免征个税，"
                "超过部分单独按综合所得税率表计税（财税〔2018〕164号第五条），不并入当年综合所得。"
            )
            cols = st.columns(3)
            with cols[0]:
                monthly_wage = st.number_input("离职前月工资（元）", min_value=0.0, value=10_000.0, step=500.0, key="sev_wage")
                years = st.number_input("工作年限（整年）", min_value=0, value=2, step=1, key="sev_years")
            with cols[1]:
                months = st.number_input("零头月数（0–11）", min_value=0, max_value=11, value=3, step=1, key="sev_months")
                local_avg = st.number_input("当地上年职工月平均工资（元）", min_value=0.0, value=7_000.0, step=500.0, key="sev_avg")
            with cols[2]:
                override_amount = st.number_input(
                    "实际补偿金额（元，0=按法定参考）", min_value=0.0, value=0.0, step=1_000.0, key="sev_amount"
                )
            if st.button("🧮 测算补偿金与个税", key="sev_calc", width="stretch"):
                try:
                    from engine import calc_severance

                    st.session_state["severance_result"] = calc_severance(
                        monthly_wage,
                        years,
                        months=months,
                        local_average_wage=local_avg,
                        amount=override_amount or None,
                    )
                except Exception as exc:  # noqa: BLE001
                    st.error(f"测算失败：{exc}")
            sev = st.session_state.get("severance_result")
            if sev:
                pay, tax = sev.get("pay") or {}, sev.get("tax") or {}
                ui.metric_row(
                    [
                        {"label": "经济补偿金参考", "value": F.fmt_money(pay.get("amount"))},
                        {"label": "免税额度（3 倍社平）", "value": F.fmt_money(tax.get("exemption_limit"))},
                        {"label": "应税金额", "value": F.fmt_money(tax.get("taxable_amount"))},
                        {"label": "一次性补偿个税", "value": F.fmt_money(tax.get("tax"))},
                        {"label": "税后到手", "value": F.fmt_money(sev.get("net_amount"))},
                    ],
                    columns=5,
                )
                st.info(sev.get("note") or "")
                for warning in sev.get("warnings") or []:
                    st.warning(warning)
                st.caption("提示：该测算不含当月正常工资、社保公积金与离职补偿的税前扣除处理；以当地口径为准。")

    st.divider()
    ui.section("个税异议申诉台账", "记录员工在个税 APP 的申诉、撤销、沟通与更正闭环")
    try:
        disputes = corrections.list_disputes()
    except Exception as exc:  # noqa: BLE001
        st.warning(f"申诉台账不可用：{exc}")
        disputes = []
    if disputes:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "编号": row.get("id"),
                        "员工": row.get("employee_name"),
                        "申诉类型": row.get("dispute_type"),
                        "涉及月份": row.get("period"),
                        "申诉日期": row.get("filed_date"),
                        "状态": row.get("status"),
                        "撤销日期": row.get("withdrawn_date"),
                        "处理结果": row.get("resolution"),
                    }
                    for row in disputes
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        dispute_options = {
            f"{row.get('employee_name')} · {row.get('dispute_type')} · {row.get('filed_date')}（{row.get('id')}）": row
            for row in disputes
        }
        selected_dispute = st.selectbox("选择申诉记录更新状态", list(dispute_options), key="dispute_selected")
        new_status = st.selectbox("处理状态", corrections.DISPUTE_STATUSES, key="dispute_status")
        resolution = st.text_input("处理结果/备注", key="dispute_resolution")
        cols = st.columns(2)
        with cols[0]:
            if st.button("💾 更新申诉状态", key="dispute_update", width="stretch"):
                try:
                    corrections.update_dispute(
                        str(dispute_options[selected_dispute].get("id")),
                        {"status": new_status, "resolution": resolution},
                    )
                    st.success("已更新")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"更新失败：{exc}")
        with cols[1]:
            if st.button("🗑️ 删除申诉记录", key="dispute_delete", width="stretch"):
                try:
                    corrections.delete_dispute(str(dispute_options[selected_dispute].get("id")))
                    st.success("已删除")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"删除失败：{exc}")
    else:
        st.info("暂无申诉记录。员工撤销申诉后，建议在此登记留痕。")

    with st.expander("➕ 新增申诉记录", expanded=False):
        try:
            people = employee_api.list_employees(status=None, include_left=True)
        except Exception as exc:  # noqa: BLE001
            people = []
            st.warning(f"员工列表不可用：{exc}")
        if not people:
            st.caption("请先在「员工管理」录入员工。")
        else:
            people_map = {f"{item['name']}（{item['id']}）": item["id"] for item in people}
            employee_label = st.selectbox("员工", list(people_map), key="dispute_employee")
            dispute_type = st.selectbox("申诉类型", corrections.DISPUTE_TYPES, key="dispute_type")
            filed_date = st.date_input("申诉日期", value=date.today(), key="dispute_filed")
            period = st.text_input("涉及月份（可选，如 2026-03）", key="dispute_period")
            resolution = st.text_input("处理结果/说明", key="dispute_new_resolution")
            if st.button("保存申诉记录", key="dispute_add", type="primary", width="stretch"):
                try:
                    corrections.add_dispute(
                        people_map[employee_label],
                        dispute_type,
                        str(filed_date),
                        period=period,
                        resolution=resolution,
                    )
                    st.success("已登记申诉记录")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"登记失败：{exc}")
    st.caption("提示：员工撤销路径——个人所得税 APP → 服务 → 收入纳税明细查询 → 异议申诉记录 → 撤销申诉。撤销后请保存截图。")

# =============================================================================
# 页面入口
# =============================================================================
def render() -> None:
    """渲染「👥 员工与工资」页面。"""
    ui.page_header(
        "员工与工资",
        "员工档案 → 月度工资表 → 累计预扣个税 → 工资凭证 → 扣缴申报辅助表（实际申报在扣缴端完成）。",
        icon="👥",
        right_text=f"当前月份：{PA.current_month_key()}",
    )
    _show_flash()
    if not PA.any_ready():
        ui.module_missing("员工与工资", "ledger.employees / ledger.payroll / filing.iit_return", detail=PA.missing_detail())
    tabs = st.tabs(["👥 员工管理", "💰 月度工资表", "📋 扣缴申报辅助表", "🧯 离职与纠错"])
    with tabs[0]:
        _render_employee_tab()
    with tabs[1]:
        _render_payroll_tab()
    with tabs[2]:
        _render_iit_return_tab()
    with tabs[3]:
        _render_correction_tab()
