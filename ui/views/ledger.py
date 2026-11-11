"""📒 台账管理：收入 / 费用 / 研发辅助账 / 合同台账 四个 tab。

每个 tab 提供：新增表单（中文标签、日期、枚举下拉、研发勾选）、可编辑表格（保存 / 删除行）、
CSV 导入导出、汇总指标卡；收入自动展示含税 → 不含税换算。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Sequence

import pandas as pd
import streamlit as st

import config
from engine import calc_stamp_tax, current_quarter, period_label
from ledger.models import derive_exclusive
from ui.components import cache, charts, formatting as F, ledger_api as LA, ui

# =============================================================================
# 字段与列配置
# =============================================================================
Field = dict[str, Any]

INCOME_FIELDS: list[Field] = [
    {"field": "date", "label": "日期", "type": "date"},
    {"field": "client", "label": "客户名称", "type": "text"},
    {"field": "description", "label": "业务描述", "type": "text"},
    {"field": "category", "label": "业务类别", "type": "select", "options": LA.INCOME_CATEGORIES},
    {"field": "invoice_type", "label": "发票类型", "type": "select", "options": LA.INVOICE_TYPES},
    {"field": "invoice_no", "label": "发票号码", "type": "text"},
    {"field": "inclusive_amount", "label": "含税金额", "type": "money"},
    {"field": "exclusive_amount", "label": "不含税金额", "type": "money", "editable": False},
    {"field": "vat_amount", "label": "增值税额", "type": "money", "editable": False},
    {"field": "is_rd_related", "label": "研发相关", "type": "bool"},
    {"field": "note", "label": "备注", "type": "text"},
]

EXPENSE_FIELDS: list[Field] = [
    {"field": "date", "label": "日期", "type": "date"},
    {"field": "description", "label": "费用描述", "type": "text"},
    {"field": "category", "label": "费用类别", "type": "select", "options": LA.EXPENSE_CATEGORIES},
    {"field": "amount", "label": "金额（含税）", "type": "money"},
    {"field": "is_rd_related", "label": "研发相关", "type": "bool"},
    {"field": "vendor", "label": "供应商", "type": "text"},
    {"field": "receipt_no", "label": "凭证号", "type": "text"},
    {"field": "note", "label": "备注", "type": "text"},
]

RD_FIELDS: list[Field] = [
    {"field": "project_name", "label": "研发项目", "type": "text"},
    {"field": "period", "label": "所属期间", "type": "text"},
    {"field": "personnel_cost", "label": "人员人工", "type": "money"},
    {"field": "material_cost", "label": "直接投入", "type": "money"},
    {"field": "depreciation", "label": "折旧费用", "type": "money"},
    {"field": "amortization", "label": "无形资产摊销", "type": "money"},
    {"field": "design_cost", "label": "新产品设计费", "type": "money"},
    {"field": "other_cost", "label": "其他相关费用", "type": "money"},
    {"field": "total", "label": "合计", "type": "money", "editable": False},
    {"field": "deduction_amount", "label": "加计扣除额", "type": "money", "editable": False},
    {"field": "note", "label": "备注", "type": "text"},
]

CONTRACT_FIELDS: list[Field] = [
    {"field": "date", "label": "签订日期", "type": "date"},
    {"field": "contract_no", "label": "合同编号", "type": "text"},
    {"field": "counterparty", "label": "对方单位", "type": "text"},
    {"field": "contract_type", "label": "合同类型", "type": "select", "options": LA.CONTRACT_TYPES},
    {"field": "amount", "label": "计税金额", "type": "money"},
    {"field": "is_rd_related", "label": "研发相关", "type": "bool"},
    {"field": "note", "label": "备注", "type": "text"},
]

ID_LABEL = "编号"

FLASH_KEY = "_ledger_flash"


def _flash(message: str, level: str = "success") -> None:
    """保存成功提示（rerun 后仍可见）。"""
    st.session_state.setdefault(FLASH_KEY, []).append({"message": message, "level": level})


def _show_flash() -> None:
    for item in st.session_state.pop(FLASH_KEY, []) or []:
        if item["level"] == "warning":
            st.warning(item["message"])
        elif item["level"] == "error":
            st.error(item["message"])
        else:
            st.success(item["message"])


# =============================================================================
# 编辑器通用逻辑
# =============================================================================
def _to_iso(value: Any) -> str:
    """任意日期值 → ``YYYY-MM-DD``。"""
    if value is None or value == "" or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return ""
    return text[:10]


def _is_blank(value: Any) -> bool:
    return value is None or value == "" or (isinstance(value, float) and pd.isna(value))


def _frame(rows: Sequence[dict], fields: Sequence[Field]) -> pd.DataFrame:
    """台账记录 → data_editor 数据框（中文列头，日期列转换为日期类型）。"""
    data: dict[str, list[Any]] = {ID_LABEL: [str(row.get("id") or "") for row in rows]}
    for spec in fields:
        field, kind = spec["field"], spec["type"]
        if kind == "date":
            data[spec["label"]] = pd.to_datetime(
                [date.fromisoformat(_to_iso(row.get(field))) if _to_iso(row.get(field)) else None for row in rows],
                errors="coerce",
            )
        elif kind == "money":
            data[spec["label"]] = pd.Series(
                [F.to_float(row.get(field)) for row in rows], dtype="float64"
            )
        elif kind == "bool":
            data[spec["label"]] = pd.Series(
                [bool(row.get(field)) for row in rows], dtype="bool"
            )
        else:
            data[spec["label"]] = pd.Series(
                ["" if _is_blank(row.get(field)) else str(row.get(field)) for row in rows],
                dtype="object",
            )
    return pd.DataFrame(data)


def _column_config(fields: Sequence[Field]) -> dict[str, Any]:
    """data_editor 列配置。"""
    config_map: dict[str, Any] = {
        ID_LABEL: st.column_config.TextColumn(ID_LABEL, help="系统自动生成的编号", width="small")
    }
    for spec in fields:
        kind, label = spec["type"], spec["label"]
        if kind == "date":
            config_map[label] = st.column_config.DateColumn(label, format="YYYY-MM-DD", width="small")
        elif kind == "money":
            config_map[label] = st.column_config.NumberColumn(label, format="¥%.2f", step=0.01)
        elif kind == "bool":
            config_map[label] = st.column_config.CheckboxColumn(label, width="small")
        elif kind == "select":
            config_map[label] = st.column_config.SelectboxColumn(label, options=list(spec.get("options") or []))
        else:
            config_map[label] = st.column_config.TextColumn(label)
    return config_map


def _payload(record: pd.Series, fields: Sequence[Field]) -> dict:
    """编辑行 → 台账 payload（只包含可编辑字段）。"""
    result: dict[str, Any] = {}
    for spec in fields:
        if spec.get("editable") is False:
            continue
        value = record.get(spec["label"])
        kind = spec["type"]
        if kind == "date":
            result[spec["field"]] = _to_iso(value)
        elif kind == "money":
            result[spec["field"]] = 0.0 if _is_blank(value) else round(float(value), 2)
        elif kind == "bool":
            result[spec["field"]] = bool(value) if not _is_blank(value) else False
        else:
            result[spec["field"]] = "" if _is_blank(value) else str(value).strip()
    return result


def _same_value(old: Any, new: Any) -> bool:
    if isinstance(old, float) or isinstance(new, float):
        try:
            return abs(F.to_float(old) - F.to_float(new)) < 0.005
        except (TypeError, ValueError):
            return False
    if isinstance(old, bool) or isinstance(new, bool):
        return bool(old) == bool(new)
    return str(old or "") == str(new or "")


def _apply_editor(
    rows: Sequence[dict],
    fields: Sequence[Field],
    edited: pd.DataFrame,
    *,
    add_func: Callable[[dict], Any],
    update_func: Callable[[str, dict], Any],
    delete_func: Callable[[str], Any],
) -> dict:
    """把 data_editor 的修改回写台账（新增 / 更新 / 删除）。"""
    existing = {str(row.get("id")): row for row in rows}
    seen: set[str] = set()
    result = {"added": 0, "updated": 0, "deleted": 0, "errors": []}
    for _, record in edited.iterrows():
        raw_id = record.get(ID_LABEL)
        record_id = "" if _is_blank(raw_id) else str(raw_id).strip()
        try:
            payload = _payload(record, fields)
        except (TypeError, ValueError) as exc:
            result["errors"].append(f"第 {_ + 1} 行数据无法解析：{exc}")
            continue
        if record_id and record_id in existing:
            seen.add(record_id)
            current = existing[record_id]
            changed = {
                key: value for key, value in payload.items() if not _same_value(current.get(key), value)
            }
            if changed:
                try:
                    update_func(record_id, changed)
                    result["updated"] += 1
                except Exception as exc:  # noqa: BLE001
                    result["errors"].append(f"更新 {record_id} 失败：{exc}")
        elif not record_id:
            try:
                add_func(payload)
                result["added"] += 1
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"新增记录失败：{exc}")
        else:
            result["errors"].append(f"编号 {record_id} 不存在，已忽略该行")
    for record_id in set(existing) - seen:
        try:
            delete_func(record_id)
            result["deleted"] += 1
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"删除 {record_id} 失败：{exc}")
    return result


def _finish_editor(result: dict) -> None:
    """统一展示编辑器保存结果并刷新。"""
    for message in result.get("errors") or []:
        st.warning(message)
    changed = result.get("added", 0) + result.get("updated", 0) + result.get("deleted", 0)
    if changed:
        cache.clear()
        _flash(
            f"已保存：新增 {result['added']} 条、更新 {result['updated']} 条、删除 {result['deleted']} 条"
        )
        st.rerun()
    elif result.get("errors"):
        st.error("未能保存修改，请检查上方提示。")
    else:
        st.info("没有检测到修改。")


# =============================================================================
# 通用：CSV 工具
# =============================================================================
def _csv_tools(
    kind: str,
    label: str,
    *,
    import_func: Callable[[str | Path], int],
    export_func: Callable[[], Path],
) -> None:
    """CSV 导入 / 导出。"""
    with st.expander(f"📥 {label} CSV 导入 / 导出", expanded=False):
        left, right = st.columns(2)
        with left:
            st.markdown("**导入 CSV**")
            uploaded = st.file_uploader(
                "选择 CSV 文件",
                type=["csv"],
                key=f"{kind}_upload",
                label_visibility="collapsed",
            )
            st.caption("支持中英文表头（如「含税金额」或 inclusive_amount）；重复记录请手动核对。")
            if st.button("导入 CSV", key=f"{kind}_do_import", disabled=uploaded is None, width="stretch"):
                if uploaded is not None:
                    try:
                        target = Path(config.UPLOAD_DIR) / f"{kind}_{datetime.now():%Y%m%d%H%M%S}_{uploaded.name}"
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(uploaded.getvalue())
                        count = import_func(target)
                    except Exception as exc:  # noqa: BLE001
                        ui.failure("CSV 导入失败，请检查文件格式与内容。", exc)
                    else:
                        cache.clear()
                        _flash(f"成功导入 {count} 条记录")
                        st.rerun()
        with right:
            st.markdown("**导出 CSV**")
            if st.button("生成导出文件", key=f"{kind}_do_export", width="stretch"):
                try:
                    path = export_func()
                except Exception as exc:  # noqa: BLE001
                    ui.failure("CSV 导出失败。", exc)
                else:
                    st.session_state[f"{kind}_export"] = str(path)
            stored = st.session_state.get(f"{kind}_export")
            if stored and Path(stored).exists():
                export_path = Path(stored)
                st.download_button(
                    "下载 CSV",
                    data=export_path.read_bytes(),
                    file_name=export_path.name,
                    mime="text/csv",
                    key=f"{kind}_download",
                    width="stretch",
                )


# =============================================================================
# 收入台账
# =============================================================================
def _income_form() -> None:
    """新增收入表单（含税金额自动换算）。"""
    with st.form("income_add_form", clear_on_submit=True):
        st.markdown("**新增收入**")
        col1, col2, col3 = st.columns(3)
        with col1:
            income_date = st.date_input("收款日期", value=config.today(), key="income_form_date")
            client = st.text_input("客户名称", key="income_form_client", placeholder="如：云图科技")
            category = st.selectbox("业务类别", LA.INCOME_CATEGORIES, key="income_form_category")
        with col2:
            inclusive = st.number_input(
                "含税金额（元）", min_value=0.0, step=100.0, format="%.2f", key="income_form_amount"
            )
            invoice_type = st.selectbox("发票类型", LA.INVOICE_TYPES, key="income_form_invoice_type")
            invoice_no = st.text_input("发票号码", key="income_form_invoice_no")
        with col3:
            description = st.text_input("业务描述", key="income_form_description")
            is_rd = st.checkbox("与研发相关", key="income_form_is_rd")
            note = st.text_input("备注", key="income_form_note")
        st.caption("换算规则：不含税金额 = 含税金额 ÷ (1 + 1%)，增值税额 = 含税金额 − 不含税金额。")
        submitted = st.form_submit_button("保存收入", type="primary", width="stretch")
    if not submitted:
        return
    if inclusive <= 0:
        st.error("含税金额必须大于 0。")
        return
    try:
        record = LA.add_income(
            {
                "date": _to_iso(income_date),
                "client": client,
                "inclusive_amount": inclusive,
                "invoice_type": invoice_type,
                "category": category,
                "description": description,
                "invoice_no": invoice_no,
                "is_rd_related": is_rd,
                "note": note,
            }
        )
    except Exception as exc:  # noqa: BLE001
        ui.failure("保存收入失败，请检查输入。", exc)
        return
    cache.clear()
    _flash(
        f"已保存收入 {record.get('id', '')}：含税 ¥{F.fmt_amount(record.get('inclusive_amount'))} → "
        f"不含税 ¥{F.fmt_amount(record.get('exclusive_amount'))}，增值税 ¥{F.fmt_amount(record.get('vat_amount'))}"
    )
    st.rerun()


def _render_income() -> None:
    ui.section(
        "💰 收入台账",
        "记录每笔收入；含税金额按 1% 征收率自动换算不含税金额与增值税额，供算税与申报取数。",
    )
    try:
        rows = LA.list_income()
        summary = cache.income_summary()
    except Exception as exc:  # noqa: BLE001
        ui.failure("收入台账读取失败。", exc)
        rows, summary = [], {}

    ui.metric_row(
        [
            {"label": "收入笔数", "value": f"{int(summary.get('count', 0))}"},
            {"label": "含税合计", "value": F.fmt_money(summary.get("total_inclusive"))},
            {"label": "不含税合计", "value": F.fmt_money(summary.get("total_exclusive"))},
            {"label": "增值税合计", "value": F.fmt_money(summary.get("vat_amount"))},
            {"label": "研发相关收入", "value": F.fmt_money(summary.get("rd_inclusive"))},
        ]
    )

    if not rows:
        ui.empty_state(
            "还没有收入记录",
            "可在下方「新增收入」录入第一笔，或加载示例数据快速体验。",
            icon="💰",
            seed_key="income_seed_demo",
        )

    _income_form()

    # 含税 ↔ 不含税换算小工具（实时）
    with st.expander("💡 含税 → 不含税换算小工具", expanded=False):
        amount = st.number_input("含税金额（元）", min_value=0.0, step=100.0, format="%.2f", key="income_convert_amount")
        exclusive, vat = derive_exclusive(amount)
        st.caption(f"不含税金额：**{F.fmt_money(exclusive)}** · 增值税额（1%）：**{F.fmt_money(vat)}**")

    if rows:
        st.markdown("**收入明细**（可直接编辑单元格、增删行，修改后点击保存）")
        display_rows = list(reversed(rows))
        edited = st.data_editor(
            _frame(display_rows, INCOME_FIELDS),
            column_config=_column_config(INCOME_FIELDS),
            disabled=[ID_LABEL] + [spec["label"] for spec in INCOME_FIELDS if spec.get("editable") is False],
            num_rows="dynamic",
            hide_index=True,
            key="income_editor",
            width="stretch",
            height=min(460, 80 + 36 * max(len(display_rows), 1)),
        )
        if st.button("💾 保存收入表格修改", key="income_editor_save", type="primary", width="stretch"):
            _finish_editor(
                _apply_editor(
                    display_rows,
                    INCOME_FIELDS,
                    edited,
                    add_func=LA.add_income,
                    update_func=LA.update_income,
                    delete_func=LA.delete_income,
                )
            )

    _csv_tools(
        "income",
        "收入台账",
        import_func=LA.import_income_csv,
        export_func=LA.export_income_csv,
    )
    theme_disclaimer()


# =============================================================================
# 费用台账
# =============================================================================
def _expense_form() -> None:
    with st.form("expense_add_form", clear_on_submit=True):
        st.markdown("**新增费用**")
        col1, col2, col3 = st.columns(3)
        with col1:
            expense_date = st.date_input("发生日期", value=config.today(), key="expense_form_date")
            description = st.text_input("费用描述", key="expense_form_description")
            category = st.selectbox("费用类别", LA.EXPENSE_CATEGORIES, key="expense_form_category")
        with col2:
            amount = st.number_input(
                "金额（含税，元）", min_value=0.0, step=100.0, format="%.2f", key="expense_form_amount"
            )
            vendor = st.text_input("供应商", key="expense_form_vendor")
            receipt_no = st.text_input("凭证号", key="expense_form_receipt")
        with col3:
            is_rd = st.checkbox("可归入研发费用", key="expense_form_is_rd")
            note = st.text_input("备注", key="expense_form_note")
            st.caption("小规模纳税人进项不可抵扣，费用金额即成本金额。")
        submitted = st.form_submit_button("保存费用", type="primary", width="stretch")
    if not submitted:
        return
    if amount <= 0:
        st.error("费用金额必须大于 0。")
        return
    try:
        LA.add_expense(
            {
                "date": _to_iso(expense_date),
                "description": description,
                "amount": amount,
                "category": category,
                "is_rd_related": is_rd,
                "vendor": vendor,
                "receipt_no": receipt_no,
                "note": note,
            }
        )
    except Exception as exc:  # noqa: BLE001
        ui.failure("保存费用失败，请检查输入。", exc)
        return
    cache.clear()
    _flash("费用已保存")
    st.rerun()


def _render_expense() -> None:
    ui.section(
        "🧾 费用台账",
        "记录工资、社保、房租、研发材料等费用；标记「研发相关」后自动参与研发费用归集。",
    )
    try:
        rows = LA.list_expenses()
        summary = cache.expense_summary()
    except Exception as exc:  # noqa: BLE001
        ui.failure("费用台账读取失败。", exc)
        rows, summary = [], {}

    ui.metric_row(
        [
            {"label": "费用笔数", "value": f"{int(summary.get('count', 0))}"},
            {"label": "费用合计", "value": F.fmt_money(summary.get("total"))},
            {"label": "研发相关费用", "value": F.fmt_money(summary.get("rd_total"))},
            {"label": "费用类别数", "value": f"{len(summary.get('by_category') or {})}"},
        ]
    )

    if not rows:
        ui.empty_state(
            "还没有费用记录",
            "先在下方录入一笔工资或办公费用，或加载示例数据。",
            icon="🧾",
            seed_key="expense_seed_demo",
        )

    _expense_form()

    if rows:
        st.markdown("**费用明细**（可直接编辑、增删行）")
        display_rows = list(reversed(rows))
        edited = st.data_editor(
            _frame(display_rows, EXPENSE_FIELDS),
            column_config=_column_config(EXPENSE_FIELDS),
            disabled=[ID_LABEL],
            num_rows="dynamic",
            hide_index=True,
            key="expense_editor",
            width="stretch",
            height=min(460, 80 + 36 * max(len(display_rows), 1)),
        )
        if st.button("💾 保存费用表格修改", key="expense_editor_save", type="primary", width="stretch"):
            _finish_editor(
                _apply_editor(
                    display_rows,
                    EXPENSE_FIELDS,
                    edited,
                    add_func=LA.add_expense,
                    update_func=LA.update_expense,
                    delete_func=LA.delete_expense,
                )
            )

    by_category = summary.get("by_category") or {}
    if by_category:
        with st.container(border=True):
            st.markdown("**费用类别分布**")
            charts.show(charts.expense_by_category(by_category), alt="费用类别分布图")
            category_frame = pd.DataFrame(
                [
                    {"费用类别": name, "笔数": bucket.get("count", 0), "金额": F.fmt_money(bucket.get("amount"))}
                    for name, bucket in by_category.items()
                ]
            )
            st.dataframe(category_frame, hide_index=True, width="stretch")

    _csv_tools(
        "expense",
        "费用台账",
        import_func=LA.import_expense_csv,
        export_func=LA.export_expense_csv,
    )
    theme_disclaimer()


# =============================================================================
# 研发辅助账
# =============================================================================
def _rd_form() -> None:
    with st.form("rd_add_form", clear_on_submit=True):
        st.markdown("**新增研发归集记录**")
        col1, col2, col3 = st.columns(3)
        with col1:
            project_name = st.text_input("研发项目名称", value="研发项目", key="rd_form_project")
            period = st.selectbox(
                "所属期间", LA.periods(), index=0, key="rd_form_period"
            )
            personnel_cost = st.number_input("人员人工费用（元）", min_value=0.0, step=1000.0, format="%.2f", key="rd_form_personnel")
            material_cost = st.number_input("直接投入费用（元）", min_value=0.0, step=1000.0, format="%.2f", key="rd_form_material")
        with col2:
            depreciation = st.number_input("折旧费用（元）", min_value=0.0, step=100.0, format="%.2f", key="rd_form_depreciation")
            amortization = st.number_input("无形资产摊销（元）", min_value=0.0, step=100.0, format="%.2f", key="rd_form_amortization")
            design_cost = st.number_input("新产品设计费等（元）", min_value=0.0, step=100.0, format="%.2f", key="rd_form_design")
            other_cost = st.number_input("其他相关费用（元）", min_value=0.0, step=100.0, format="%.2f", key="rd_form_other")
        with col3:
            note = st.text_input("备注", key="rd_form_note")
            st.caption("合计与加计扣除额由系统按 100% 加计扣除自动计算。")
        submitted = st.form_submit_button("保存研发记录", type="primary", width="stretch")
    if not submitted:
        return
    try:
        LA.add_rd_record(
            {
                "project_name": project_name or "研发项目",
                "period": period,
                "personnel_cost": personnel_cost,
                "material_cost": material_cost,
                "depreciation": depreciation,
                "amortization": amortization,
                "design_cost": design_cost,
                "other_cost": other_cost,
                "note": note,
            }
        )
    except Exception as exc:  # noqa: BLE001
        ui.failure("保存研发记录失败，请检查输入。", exc)
        return
    cache.clear()
    _flash("研发归集记录已保存")
    st.rerun()


def _render_rd() -> None:
    ui.section(
        "🔬 研发辅助账",
        "按研发项目归集六类费用，自动计算加计扣除额（未形成无形资产按 100% 加计扣除）。",
    )
    try:
        rows = LA.list_rd_records()
    except Exception as exc:  # noqa: BLE001
        ui.failure("研发辅助账读取失败。", exc)
        rows = []

    period_options = LA.periods()
    summary_period = st.selectbox(
        "汇总期间", period_options, index=0, key="rd_summary_period"
    )
    try:
        summary = cache.rd_summary(summary_period)
    except Exception as exc:  # noqa: BLE001
        ui.failure("研发费用归集失败。", exc)
        summary = {}

    ui.metric_row(
        [
            {"label": "归集笔数", "value": f"{len(rows)}"},
            {"label": "可加计扣除研发费用", "value": F.fmt_money(summary.get("total_collectible"))},
            {"label": "加计扣除额", "value": F.fmt_money(summary.get("deduction_amount"))},
            {"label": "费用台账自动归集", "value": F.fmt_money((summary.get("from_expenses") or {}).get("total_collectible"))},
        ]
    )
    st.caption(f"当前汇总期间：{period_label(summary_period)}；研发费用加计扣除政策依据：财政部 税务总局公告2023年第7号。")

    if not rows:
        ui.empty_state(
            "还没有研发归集记录",
            "费用台账中标记「研发相关」的费用会自动归集；也可在此手工登记研发辅助账。",
            icon="🔬",
            seed_key="rd_seed_demo",
        )

    _rd_form()

    if rows:
        st.markdown("**研发辅助账明细**（可直接编辑、增删行）")
        display_rows = list(reversed(rows))
        edited = st.data_editor(
            _frame(display_rows, RD_FIELDS),
            column_config=_column_config(RD_FIELDS),
            disabled=[ID_LABEL] + [spec["label"] for spec in RD_FIELDS if spec.get("editable") is False],
            num_rows="dynamic",
            hide_index=True,
            key="rd_editor",
            width="stretch",
            height=min(460, 80 + 36 * max(len(display_rows), 1)),
        )
        if st.button("💾 保存研发表格修改", key="rd_editor_save", type="primary", width="stretch"):
            _finish_editor(
                _apply_editor(
                    display_rows,
                    RD_FIELDS,
                    edited,
                    add_func=LA.add_rd_record,
                    update_func=LA.update_rd_record,
                    delete_func=LA.delete_rd_record,
                )
            )

    _csv_tools(
        "rd",
        "研发辅助账",
        import_func=LA.import_rd_csv,
        export_func=LA.export_rd_csv,
    )
    theme_disclaimer()


# =============================================================================
# 合同台账
# =============================================================================
def _contract_form() -> None:
    with st.form("contract_add_form", clear_on_submit=True):
        st.markdown("**新增合同（印花税计税依据）**")
        col1, col2, col3 = st.columns(3)
        with col1:
            contract_date = st.date_input("签订日期", value=config.today(), key="contract_form_date")
            contract_no = st.text_input("合同编号", key="contract_form_no")
            counterparty = st.text_input("对方单位", key="contract_form_counterparty")
        with col2:
            contract_type = st.selectbox("合同类型", LA.CONTRACT_TYPES, key="contract_form_type")
            amount = st.number_input(
                "计税金额（元）", min_value=0.0, step=1000.0, format="%.2f", key="contract_form_amount"
            )
            is_rd = st.checkbox("与研发相关", key="contract_form_is_rd")
        with col3:
            note = st.text_input("备注", key="contract_form_note")
            st.caption("印花税按合同金额 × 税率 × 六税两费减半计算。")
        submitted = st.form_submit_button("保存合同", type="primary", width="stretch")
    if not submitted:
        return
    if amount <= 0:
        st.error("计税金额必须大于 0。")
        return
    try:
        LA.add_contract(
            {
                "date": _to_iso(contract_date),
                "contract_no": contract_no,
                "counterparty": counterparty,
                "contract_type": contract_type,
                "amount": amount,
                "is_rd_related": is_rd,
                "note": note,
            }
        )
    except Exception as exc:  # noqa: BLE001
        ui.failure("保存合同失败，请检查输入。", exc)
        return
    cache.clear()
    _flash("合同已保存")
    st.rerun()


def _render_contracts() -> None:
    ui.section(
        "📄 合同台账",
        "登记应税合同，用于印花税申报；小规模纳税人享受「六税两费」减半征收。",
    )
    try:
        rows = LA.list_contracts()
    except Exception as exc:  # noqa: BLE001
        ui.failure("合同台账读取失败。", exc)
        rows = []

    try:
        stamp = calc_stamp_tax(rows, period=current_quarter())
    except Exception as exc:  # noqa: BLE001
        ui.failure("印花税试算失败。", exc)
        stamp = {}

    ui.metric_row(
        [
            {"label": "合同份数", "value": f"{len(rows)}"},
            {"label": "计税金额合计", "value": F.fmt_money(stamp.get("total_amount"))},
            {"label": "减免税额", "value": F.fmt_money(stamp.get("discount_amount"))},
            {"label": "应缴印花税", "value": F.fmt_money(stamp.get("total_payable"))},
        ]
    )
    if stamp.get("note"):
        st.caption(stamp["note"])

    if not rows:
        ui.empty_state(
            "还没有合同记录",
            "登记已签订的买卖合同、技术合同等，系统会自动试算印花税。",
            icon="📄",
            seed_key="contract_seed_demo",
        )

    _contract_form()

    if rows:
        st.markdown("**合同明细**（可直接编辑、增删行）")
        display_rows = list(reversed(rows))
        edited = st.data_editor(
            _frame(display_rows, CONTRACT_FIELDS),
            column_config=_column_config(CONTRACT_FIELDS),
            disabled=[ID_LABEL],
            num_rows="dynamic",
            hide_index=True,
            key="contract_editor",
            width="stretch",
            height=min(460, 80 + 36 * max(len(display_rows), 1)),
        )
        if st.button("💾 保存合同表格修改", key="contract_editor_save", type="primary", width="stretch"):
            _finish_editor(
                _apply_editor(
                    display_rows,
                    CONTRACT_FIELDS,
                    edited,
                    add_func=LA.add_contract,
                    update_func=LA.update_contract,
                    delete_func=LA.delete_contract,
                )
            )

    if stamp.get("items"):
        with st.container(border=True):
            st.markdown("**印花税分合同明细**")
            ui.records_table(
                stamp["items"],
                [
                    ("contract_no", "合同编号"),
                    ("counterparty", "对方单位"),
                    ("contract_type", "合同类型"),
                    ("amount", "计税金额"),
                    ("rate_text", "税率"),
                    ("tax_before_discount", "税额（减免前）"),
                    ("discount_amount", "减免额"),
                    ("tax_payable", "应缴税额"),
                ],
                key="contract_stamp_table",
                money_fields={"amount", "tax_before_discount", "discount_amount", "tax_payable"},
            )
        theme_disclaimer()

    _csv_tools(
        "contracts",
        "合同台账",
        import_func=LA.import_contracts_csv,
        export_func=LA.export_contracts_csv,
    )
    theme_disclaimer()


# =============================================================================
# 页面入口
# =============================================================================
def theme_disclaimer() -> None:
    """局部免责声明（延迟导入避免循环）。"""
    from ui.components import theme

    theme.disclaimer()


def render() -> None:
    """渲染台账管理页（四个 tab）。"""
    from ui.components import explainers

    ui.page_header(
        "台账管理",
        "收入 / 费用 / 研发辅助账 / 合同台账，全部保存在本地 CSV，不上传云端。",
        icon="📒",
        right_text=f"当前季度：{period_label(current_quarter())}",
    )
    explainers.basics()
    _show_flash()
    tabs = st.tabs(["💰 收入台账", "🧾 费用台账", "🔬 研发辅助账", "📄 合同台账"])
    with tabs[0]:
        _render_income()
    with tabs[1]:
        _render_expense()
    with tabs[2]:
        _render_rd()
    with tabs[3]:
        _render_contracts()
