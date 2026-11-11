"""🧾 发票合规判断：这张票能不能入账、能不能抵、能不能税前扣除。"""

from __future__ import annotations

import streamlit as st

import config
from engine.invoice_rules import DISCLAIMER, INVOICE_TYPES, PURPOSES, assess_invoice
from ui.components import theme, ui

_RESULT_KEY = "invoice_check_result"


def _badge(value: bool) -> str:
    return "✅ 可以" if value else "❌ 不可以"


def render() -> None:
    """渲染发票合规判断页。"""
    ui.page_header(
        "发票合规判断",
        "用规则回答小白最常问的三个问题：能不能入账？能不能抵扣增值税？能不能企业所得税税前扣除？",
        icon="🧾",
    )
    st.info(
        "本系统面向小规模纳税人：通常不能抵扣增值税进项税；发票更多是成本费用与企业所得税扣除凭证。"
        "判断仅作入门参考，复杂业务请咨询专业人士或主管税务机关。",
        icon="📌",
    )
    cols = st.columns(2)
    with cols[0]:
        invoice_type = st.selectbox("发票/凭证类型", INVOICE_TYPES, key="invoice_type")
        amount = st.number_input("金额（元，可选）", min_value=0.0, value=0.0, step=100.0, key="invoice_amount")
        buyer = st.checkbox("发票抬头是本公司", value=True, key="invoice_buyer")
        compliant = st.checkbox("发票信息真实、合规（非虚开/项目相符）", value=True, key="invoice_compliant")
    with cols[1]:
        purpose = st.selectbox("用途", PURPOSES, key="invoice_purpose")
        cross_year = st.checkbox("跨年度发票", value=False, key="invoice_cross_year")
        small_scale = st.checkbox("本企业是小规模纳税人", value=True, key="invoice_small_scale")
    if st.button("🔎 判断这张票", type="primary", key="invoice_assess", width="stretch"):
        try:
            result = assess_invoice(
                invoice_type,
                buyer_is_company=buyer,
                compliant=compliant,
                purpose=purpose,
                cross_year=cross_year,
                small_scale=small_scale,
                amount=float(amount or 0) or None,
            )
            st.session_state[_RESULT_KEY] = result
        except Exception as exc:  # noqa: BLE001
            st.error(f"判断失败：{exc}")

    result = st.session_state.get(_RESULT_KEY)
    if not result:
        st.caption("选择上方条件后点击「判断这张票」。")
        theme.disclaimer()
        return
    ui.section("📋 判断结果", f"类型：{result.get('invoice_type')} · 用途：{result.get('purpose')}")
    cols = st.columns(3)
    with cols[0]:
        st.metric("会计入账", _badge(bool(result.get("bookable"))))
    with cols[1]:
        st.metric("增值税抵扣", _badge(bool(result.get("vat_deductible"))))
    with cols[2]:
        st.metric("企业所得税扣除", _badge(bool(result.get("cit_deductible"))))
    risk = str(result.get("risk") or "low")
    if risk == "high":
        st.error(f"风险等级：{result.get('risk_label')}")
    elif risk == "medium":
        st.warning(f"风险等级：{result.get('risk_label')}")
    else:
        st.success(f"风险等级：{result.get('risk_label')}")
    st.markdown("**说明**")
    for note in result.get("notes") or []:
        st.markdown(f"- {note}")
    st.caption(DISCLAIMER)
    st.markdown("---")
    st.caption(
        "建议：把发票、合同、付款记录、入库/验收单一起留存（四流一致）。"
        "本系统「📥 票据与流水」可导入发票并匹配入账。"
    )
    theme.disclaimer()
