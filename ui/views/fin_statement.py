"""📊 财务报表工作台：期初/期末余额、应收应付、现金流量、核对与导出（v1.57）。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from engine import current_quarter, quarter_options
from filing import fin_statement
from ui.components import downloads, theme, ui


def _num(value: float, key: str, label: str) -> float:
    return st.number_input(label, min_value=0.0, step=100.0, format="%.2f", value=float(value or 0), key=key)


def _render_inputs(period: str) -> dict:
    inputs = fin_statement.get_inputs(period)
    with st.form(f"fin_inputs_{period}"):
        st.caption("录入银行对账单、往来余额、固定资产等；留空按 0 处理。未分配利润留 0 时自动倒挤。")
        cols = st.columns(3)
        with cols[0]:
            st.markdown("**期初余额**")
            inputs["cash_opening"] = _num(inputs.get("cash_opening"), f"fst_cash_open_{period}", "货币资金")
            inputs["ar_opening"] = _num(inputs.get("ar_opening"), f"fst_ar_open_{period}", "应收账款")
            inputs["inventory_opening"] = _num(inputs.get("inventory_opening"), f"fst_inv_open_{period}", "存货")
            inputs["fixed_assets_opening"] = _num(inputs.get("fixed_assets_opening"), f"fst_fa_open_{period}", "固定资产")
            inputs["ap_opening"] = _num(inputs.get("ap_opening"), f"fst_ap_open_{period}", "应付账款")
            inputs["tax_payable_opening"] = _num(inputs.get("tax_payable_opening"), f"fst_tax_open_{period}", "应交税费")
            inputs["other_liabilities_opening"] = _num(inputs.get("other_liabilities_opening"), f"fst_ol_open_{period}", "其他负债")
        with cols[1]:
            st.markdown("**期末余额**")
            inputs["cash_ending"] = _num(inputs.get("cash_ending"), f"fst_cash_end_{period}", "货币资金")
            inputs["ar_ending"] = _num(inputs.get("ar_ending"), f"fst_ar_end_{period}", "应收账款")
            inputs["inventory_ending"] = _num(inputs.get("inventory_ending"), f"fst_inv_end_{period}", "存货")
            inputs["fixed_assets_ending"] = _num(inputs.get("fixed_assets_ending"), f"fst_fa_end_{period}", "固定资产")
            inputs["other_assets_ending"] = _num(inputs.get("other_assets_ending"), f"fst_oa_end_{period}", "其他资产")
            inputs["ap_ending"] = _num(inputs.get("ap_ending"), f"fst_ap_end_{period}", "应付账款")
            inputs["tax_payable_ending"] = _num(inputs.get("tax_payable_ending"), f"fst_tax_end_{period}", "应交税费")
        with cols[2]:
            st.markdown("**权益与其他**")
            inputs["short_loan_ending"] = _num(inputs.get("short_loan_ending"), f"fst_loan_end_{period}", "短期借款")
            inputs["other_liabilities_ending"] = _num(inputs.get("other_liabilities_ending"), f"fst_ol_end_{period}", "其他负债")
            inputs["paid_in_capital"] = _num(inputs.get("paid_in_capital"), f"fst_capital_{period}", "实收资本")
            inputs["retained_earnings_ending"] = _num(inputs.get("retained_earnings_ending"), f"fst_retained_{period}", "未分配利润（留空倒挤）")
            inputs["note"] = st.text_input("备注", value=str(inputs.get("note") or ""), key=f"fst_note_{period}")
            st.caption("完整证件与银行账号不要写进备注。")
        submitted = st.form_submit_button("保存并生成报表预览", type="primary", width="stretch")
    if submitted:
        try:
            fin_statement.save_inputs(period, inputs)
            st.success("余额已保存，已重新生成预览。")
        except Exception as exc:  # noqa: BLE001
            st.error(f"保存失败：{exc}")
    return fin_statement.get_inputs(period)


def _table(rows: list[dict], columns: dict[str, str]) -> pd.DataFrame:
    return pd.DataFrame([{label: row.get(key) for key, label in columns.items()} for row in rows])


def _render_statement(period: str, data: dict) -> None:
    closing = data["closing"]
    ui.metric_row(
        [
            {"label": "资产总计", "value": f"¥{closing['total_assets']:,.2f}"},
            {"label": "负债合计", "value": f"¥{closing['liabilities']:,.2f}"},
            {"label": "所有者权益", "value": f"¥{closing['equity']:,.2f}"},
            {"label": "平衡差额", "value": f"¥{closing['total_assets'] - closing['total']:,.2f}"},
        ],
        columns=4,
    )
    tabs = st.tabs(["资产负债表", "利润表", "现金流量表", "核对清单"])
    with tabs[0]:
        frame = pd.DataFrame([
            {"项目": row["name"], "期末余额": row["value"], "年初余额": opening["value"]}
            for row, opening in zip(closing["rows"], data["opening"]["rows"])
        ])
        st.dataframe(frame, hide_index=True, width="stretch", column_config={"期末余额": st.column_config.NumberColumn(format="¥%.2f"), "年初余额": st.column_config.NumberColumn(format="¥%.2f")})
    with tabs[1]:
        frame = pd.DataFrame([
            {"项目": row["name"], "本期金额": row["value"], "本年累计": ytd["value"]}
            for row, ytd in zip(data["income_period"], data["income_ytd"])
        ])
        st.dataframe(frame, hide_index=True, width="stretch", column_config={"本期金额": st.column_config.NumberColumn(format="¥%.2f"), "本年累计": st.column_config.NumberColumn(format="¥%.2f")})
    with tabs[2]:
        st.caption("按本期银行流水方向 + 摘要关键词简化分类；借款/投资/设备等需人工核对。")
        frame = pd.DataFrame([{"项目": row["name"], "本期金额": row["value"]} for row in data["cash_flow"]["rows"]])
        st.dataframe(frame, hide_index=True, width="stretch", column_config={"本期金额": st.column_config.NumberColumn(format="¥%.2f")})
    with tabs[3]:
        frame = pd.DataFrame([{"检查项": item["name"], "结果": "✅ 通过" if item["status"] == "PASS" else "⚠️ 需核对", "说明": item["detail"]} for item in data["checks"]])
        st.dataframe(frame, hide_index=True, width="stretch")
        if any(item["status"] != "PASS" for item in data["checks"]):
            st.warning("有核对项未通过，请先核对银行余额、往来款、现金流后再导出。")
    st.caption(data["note"])


def render() -> None:
    ui.page_header(
        "财务报表",
        "录入银行、应收应付等余额，生成资产负债表、利润表和简化现金流量表；导出标准 Excel 草稿。",
        icon="📊",
    )
    current = current_quarter()
    options = list(quarter_options(count=8))
    index = options.index(current) if current in options else 0
    period = st.selectbox("报表所属季度", options, index=index, key="fin_statement_period")
    inputs = _render_inputs(period)
    try:
        data = fin_statement.build_statement(period)
    except Exception as exc:  # noqa: BLE001
        st.error(f"生成报表失败：{exc}")
        theme.disclaimer()
        return
    _render_statement(period, data)
    if st.button("📤 生成标准 Excel 草稿", key="fin_statement_export", width="stretch"):
        try:
            path = fin_statement.export_statement(period)
            st.session_state["_fin_statement_export"] = str(path)
            st.success(f"已生成：{path}")
        except Exception as exc:  # noqa: BLE001
            st.error(f"导出失败：{exc}")
    export_raw = st.session_state.get("_fin_statement_export")
    if export_raw and Path(export_raw).exists():
        downloads.download_or_save(
            "取用财务报表 Excel",
            file_path=Path(export_raw),
            file_name=Path(export_raw).name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="fin_statement_download",
        )
    st.caption("报表为草稿，正式申报前请与总账、银行对账单、发票、工资表核对。")
    theme.disclaimer()

