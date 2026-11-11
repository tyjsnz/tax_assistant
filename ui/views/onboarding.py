"""🚀 新手引导：从零完成公司配置 → 税务基础 → 第一笔账 → 第一次申报。"""

from __future__ import annotations

import streamlit as st

import config
from onboarding import state as onboarding
from ui.components import company_api, ledger_api, nav, theme, ui

_STEP_KEY = "onboarding_step"


def _next_step() -> None:
    st.session_state[_STEP_KEY] = min(int(st.session_state.get(_STEP_KEY, 0)) + 1, onboarding.TOTAL_STEPS - 1)
    st.rerun()


def _prev_step() -> None:
    st.session_state[_STEP_KEY] = max(int(st.session_state.get(_STEP_KEY, 0)) - 1, 0)
    st.rerun()


def _step_header(index: int) -> None:
    st.caption(f"第 {index + 1} / {onboarding.TOTAL_STEPS} 步 · {onboarding.STEP_LABELS[index]}")


def _render_step_company(company_id: str) -> None:
    _step_header(0)
    company = company_api.active_company()
    presets = company_api.region_presets()
    preset_ids = [str(item.get("id")) for item in presets]
    current_region = str(company.get("region_id") or preset_ids[0])
    index = preset_ids.index(current_region) if current_region in preset_ids else 0
    st.info("先确认公司名称、统一社会信用代码和所在地区；地区决定城建税税率（市区 7%、县城/镇 5%、其他 1%）。")
    with st.form("onboarding_company_form"):
        cols = st.columns(2)
        with cols[0]:
            name = st.text_input("企业名称 *", value=str(company.get("name") or ""), key="onboard_company_name")
            credit = st.text_input(
                "统一社会信用代码", value=str(company.get("unified_social_credit_code") or ""),
                key="onboard_company_credit",
            )
            tax_authority = st.text_input(
                "主管税务机关", value=str(company.get("tax_authority") or ""), key="onboard_company_authority"
            )
        with cols[1]:
            province = st.text_input("省份", value=str(company.get("province") or ""), key="onboard_company_province")
            city = st.text_input("城市", value=str(company.get("city") or ""), key="onboard_company_city")
            region_id = st.selectbox(
                "地区预设", preset_ids, index=index,
                format_func=lambda pid: company_api.region_label(company_api.preset_by_id(pid)),
                key="onboard_company_region",
            )
        submitted = st.form_submit_button("保存公司信息并继续", type="primary", width="stretch")
    if submitted:
        if not name.strip():
            st.error("企业名称不能为空。")
            return
        try:
            company_api.update_company(
                company_id,
                {
                    "name": name.strip(),
                    "unified_social_credit_code": credit.strip(),
                    "tax_authority": tax_authority.strip(),
                    "province": province.strip(),
                    "city": city.strip(),
                    "region_id": region_id,
                },
            )
            onboarding.mark_step(company_id, 0, {"company_name": name.strip(), "region_id": region_id})
            _next_step()
        except Exception as exc:  # noqa: BLE001
            st.error(f"保存失败：{exc}")


def _render_step_tax(company_id: str) -> None:
    _step_header(1)
    st.info(
        "确认你已经完成或了解这些前置事项。系统无法代替税务登记/税种认定，但会帮你检查顺序，避免“还没核定就申报”。"
    )
    registered = st.checkbox("已完成税务登记/实名认证", key="onboard_registered")
    tax_types = st.multiselect(
        "本企业涉及的税种",
        ["增值税及附加税费", "个人所得税（工资）", "企业所得税", "印花税", "社保/残保金"],
        default=["增值税及附加税费", "个人所得税（工资）", "企业所得税"],
        key="onboard_tax_types",
    )
    tripartite = st.checkbox("已签订银行三方协议（用于扣税）", key="onboard_tripartite")
    invoice = st.checkbox("已确认开票方式（数电票/税控设备）", key="onboard_invoice")
    st.caption("没签三方协议也能申报，但缴款时需要选择其他缴款方式；建议提前签好，避免逾期。")
    cols = st.columns([1, 1])
    with cols[0]:
        if st.button("上一步", key="onboard_tax_prev", width="stretch"):
            _prev_step()
    with cols[1]:
        if st.button("保存并继续", key="onboard_tax_next", type="primary", width="stretch"):
            onboarding.mark_step(
                company_id, 1,
                {"registered": registered, "tax_types": tax_types, "tripartite": tripartite, "invoice": invoice},
            )
            _next_step()


def _render_step_ledger(company_id: str) -> None:
    _step_header(2)
    st.info("录入第一笔收入或费用，系统才能开始算税。真实业务数据只存本地。")
    try:
        stats = ledger_api.storage_stats()
    except Exception:  # noqa: BLE001
        stats = {}
    cols = st.columns(3)
    with cols[0]:
        st.metric("收入笔数", f"{stats.get('income', 0)} 笔")
    with cols[1]:
        st.metric("费用笔数", f"{stats.get('expense', 0)} 笔")
    with cols[2]:
        st.metric("票据数量", f"{stats.get('invoices', 0)} 笔")
    action_cols = st.columns(3)
    with action_cols[0]:
        if st.button("📒 去记一笔收入/费用", key="onboard_goto_ledger", width="stretch"):
            nav.goto(nav.PAGE_LEDGER)
    with action_cols[1]:
        if st.button("📥 去导入发票/流水", key="onboard_goto_ingest", width="stretch"):
            nav.goto(nav.PAGE_INGEST)
    with action_cols[2]:
        if st.button("我没有业务，直接下一步", key="onboard_ledger_next", width="stretch"):
            onboarding.mark_step(company_id, 2)
            _next_step()
    cols = st.columns([1, 1])
    with cols[0]:
        if st.button("上一步", key="onboard_ledger_prev", width="stretch"):
            _prev_step()
    with cols[1]:
        if st.button("我已录入第一笔，继续", key="onboard_ledger_done", type="primary", width="stretch"):
            onboarding.mark_step(company_id, 2)
            _next_step()


def _render_step_filing(company_id: str) -> None:
    _step_header(3)
    st.info("第一次申报建议按「首次报税全流程」操作；即使没有收入，也要按期做零申报。")
    st.markdown(
        "1. 登录电子税务局，确认企业身份和税款所属期；\n"
        "2. 找到对应税种（增值税/个税/企业所得税）的申报入口；\n"
        "3. 零申报或按实际数据填写；\n"
        "4. 提交申报，确认申报成功后完成缴款；\n"
        "5. 回到本系统归档申报表与缴款凭证。"
    )
    cols = st.columns([1, 1])
    with cols[0]:
        if st.button("📖 打开「首次报税全流程」指引", key="onboard_goto_guide", type="primary", width="stretch"):
            st.session_state["guide_workflow"] = "🆕 首次报税全流程"
            nav.goto(nav.PAGE_GUIDE)
    with cols[1]:
        if st.button("📋 去生成申报表", key="onboard_goto_filing", width="stretch"):
            nav.goto(nav.PAGE_FILING)
    st.caption("提示：「📖 操作指引」里还包含增值税、个税、所得税预缴/汇算、离职更正等流程，可随时回来查。")
    cols = st.columns([1, 1])
    with cols[0]:
        if st.button("上一步", key="onboard_filing_prev", width="stretch"):
            _prev_step()
    with cols[1]:
        if st.button("我已完成第一次申报，继续", key="onboard_filing_next", type="primary", width="stretch"):
            onboarding.mark_step(company_id, 3)
            _next_step()


def _render_step_done(company_id: str) -> None:
    _step_header(4)
    st.success("🎉 新手引导完成，可以开始自主报税了。")
    st.markdown(
        "- 每月/每季度在「🏠 首页驾驶舱」看申报倒计时；\n"
        "- 有异常会出现在「🧯 离职与纠错」与首页异常卡片；\n"
        "- 不确定怎么操作，点「📖 操作指引」按步骤走；\n"
        "- **AI 助手不需要配置 API Key 也能用**：点任意页面右下角 🤖，或打开「🤖 AI 助手」。"
    )
    if not onboarding.is_complete(company_id):
        onboarding.mark_step(company_id, 4)
    cols = st.columns(3)
    with cols[0]:
        if st.button("🏠 进入驾驶舱", key="onboard_done_dashboard", type="primary", width="stretch"):
            nav.goto(nav.PAGE_DASHBOARD)
    with cols[1]:
        if st.button("🤖 打开 AI 助手", key="onboard_done_ai", width="stretch"):
            nav.goto(nav.PAGE_ASSISTANT)
    with cols[2]:
        if st.button("🔄 重新引导", key="onboard_done_reset", width="stretch"):
            onboarding.reset(company_id)
            st.session_state[_STEP_KEY] = 0
            st.rerun()


def render() -> None:
    """渲染新手引导页。"""
    ui.page_header(
        "新手引导",
        "第一次使用？按 5 步完成公司配置、税务基础、第一笔账和第一次申报。",
        icon="🚀",
    )
    try:
        company_id = company_api.active_company_id()
        company = company_api.active_company()
    except Exception as exc:  # noqa: BLE001
        st.error(f"公司信息不可用：{exc}")
        return
    state = onboarding.get_state(company_id)
    done = sum(1 for item in state["steps"] if item)
    st.progress(done / onboarding.TOTAL_STEPS, text=f"完成进度：{done} / {onboarding.TOTAL_STEPS} 步")
    st.caption(f"当前公司：{company.get('name') or company_id} · 数据只保存在本机")

    if onboarding.is_complete(company_id):
        _render_step_done(company_id)
        theme.disclaimer()
        return

    index = int(st.session_state.get(_STEP_KEY, 0))
    index = max(0, min(index, onboarding.TOTAL_STEPS - 1))
    step_cols = st.columns(onboarding.TOTAL_STEPS)
    for step_index, label in enumerate(onboarding.STEP_LABELS):
        with step_cols[step_index]:
            marker = "✅" if state["steps"][step_index] else ("▶️" if step_index == index else "⬜")
            st.caption(f"{marker} {step_index + 1}. {label}")
    st.markdown("---")
    render_step = {
        0: _render_step_company,
        1: _render_step_tax,
        2: _render_step_ledger,
        3: _render_step_filing,
        4: _render_step_done,
    }[index]
    render_step(company_id)

    if index == 0:
        st.caption("如果你已经配置过，可点下方「跳过引导」直接进入驾驶舱。")
        if st.button("跳过引导", key="onboard_skip", width="content"):
            for step_index in range(onboarding.TOTAL_STEPS):
                onboarding.mark_step(company_id, step_index)
            nav.goto(nav.PAGE_DASHBOARD)
    theme.disclaimer()
