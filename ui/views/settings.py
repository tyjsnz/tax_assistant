"""⚙️ 设置：DeepSeek API Key、模型、企业信息、隐私模式、数据备份、示例数据、审计日志与关于。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

import config
import ledger
from ui.components import cache, formatting as F, ledger_api as LA, runtime, theme, ui

FLASH_KEY = "_settings_flash"
PRIVACY_KEY = "settings_privacy_mode"


# =============================================================================
# 提示消息
# =============================================================================
def _flash(message: str, level: str = "success") -> None:
    st.session_state.setdefault(FLASH_KEY, []).append({"message": message, "level": level})


def _show_flash() -> None:
    for item in st.session_state.pop(FLASH_KEY, []) or []:
        if item["level"] == "error":
            st.error(item["message"])
        elif item["level"] == "warning":
            st.warning(item["message"])
        else:
            st.success(item["message"])


# =============================================================================
# API Key 与模型
# =============================================================================
def _save_api_key() -> None:
    value = str(st.session_state.get("settings_api_key_value") or "").strip()
    if not value:
        _flash("请输入 API Key 后再保存。", "warning")
        return
    try:
        config.set_api_key(value)
    except Exception as exc:  # noqa: BLE001
        _flash(f"保存 API Key 失败：{exc}", "error")
        return
    # 清理密码输入框（需在下一次 rerun 的 widget 实例化之前完成）
    st.session_state["_settings_clear_key_input"] = True
    _flash(f"API Key 已保存（{config.mask_key()}），仅存储在本机 data/user_settings.json。")
    st.rerun()


def _clear_api_key() -> None:
    try:
        config.set_api_key("")
    except Exception as exc:  # noqa: BLE001
        _flash(f"清除 API Key 失败：{exc}", "error")
        return
    _flash("API Key 已清除，AI 功能将使用离线确定性回复。")
    st.rerun()


def _test_connection() -> None:
    llm_client = runtime.load("agent.llm_client")
    if llm_client is None or not hasattr(llm_client, "get_client"):
        ui.module_missing("AI 连接测试", "agent.llm_client", detail=runtime.error_of("agent.llm_client"))
        return
    try:
        client = llm_client.get_client()
        with st.spinner("正在连接 DeepSeek…"):
            result = dict(client.test_connection())
    except Exception as exc:  # noqa: BLE001
        st.error(f"连接测试失败：{exc}")
        return
    if result.get("ok"):
        st.success(
            f"连接成功 · 模型 {result.get('model')} · 延迟 {result.get('latency_ms')} ms · "
            f"{result.get('message') or ''}"
        )
        if result.get("reply"):
            st.caption(f"模型回复：{F.truncate(result.get('reply'), 120)}")
    else:
        st.error(f"连接失败：{result.get('message') or '未知原因'}")


def _save_model_settings(model: str, base_url: str, temperature: float, max_tokens: int) -> None:
    try:
        config.save_user_settings(
            {
                "model": model,
                "base_url": base_url.strip() or config.LLM_CONFIG["base_url"],
                "temperature": float(temperature),
                "max_tokens": int(max_tokens),
            }
        )
    except Exception as exc:  # noqa: BLE001
        _flash(f"保存模型设置失败：{exc}", "error")
        return
    _flash("模型设置已保存。")
    st.rerun()


def _render_api_tab() -> None:
    ui.section("🔑 DeepSeek API Key", "仅保存在本机 data/user_settings.json（权限 600），不会上传或写入日志。")
    snapshot = {}
    try:
        snapshot = config.settings_snapshot()
    except Exception:  # noqa: BLE001
        snapshot = {}
    configured = bool(snapshot.get("api_key_configured"))
    if st.session_state.pop("_settings_clear_key_input", False):
        st.session_state.pop("settings_api_key_value", None)
    cols = st.columns([2, 1, 1])
    with cols[0]:
        st.text_input(
            "API Key（sk-…）",
            key="settings_api_key_value",
            type="password",
            placeholder="粘贴 DeepSeek API Key 后点击保存",
            help="Key 只用于调用 DeepSeek 官方 API，不会显示明文。",
        )
    with cols[1]:
        st.write("")
        if st.button("💾 保存 Key", key="settings_save_key", type="primary", width="stretch"):
            _save_api_key()
    with cols[2]:
        st.write("")
        if st.button("🗑️ 清除 Key", key="settings_clear_key", width="stretch"):
            _clear_api_key()

    st.caption(
        f"当前状态：{'✅ 已配置' if configured else '⚠️ 未配置'} · 脱敏显示：{snapshot.get('api_key_masked') or config.mask_key()}"
    )
    if st.button("🔌 测试连接", key="settings_test_connection", width="content"):
        _test_connection()
    if not configured:
        st.info(
            "未配置 API Key 时，AI 助手使用本地确定性回复；算税、台账、申报、日历、政策检索全部不受影响。",
            icon="🔌",
        )

    st.markdown("---")
    ui.section("🧠 模型设置", "默认使用 deepseek-chat；需要更强推理时可切换 deepseek-reasoner（响应更慢）。")
    llm = {}
    try:
        llm = config.get_llm_config()
    except Exception:  # noqa: BLE001
        llm = dict(config.LLM_CONFIG)
    options = list(llm.get("model_options") or config.LLM_CONFIG["model_options"])
    current_model = str(llm.get("model") or options[0])
    with st.form("settings_model_form"):
        form_cols = st.columns(3)
        with form_cols[0]:
            model = st.selectbox(
                "默认模型",
                options,
                index=options.index(current_model) if current_model in options else 0,
                key="settings_model_select",
            )
        with form_cols[1]:
            base_url = st.text_input("API 地址", value=str(llm.get("base_url") or config.LLM_CONFIG["base_url"]), key="settings_base_url")
        with form_cols[2]:
            temperature = st.slider("温度（temperature）", 0.0, 1.0, float(llm.get("temperature") or 0.3), 0.05, key="settings_temperature")
        max_tokens = st.number_input(
            "最大回复 token 数", min_value=256, max_value=8192, value=int(llm.get("max_tokens") or 2000), step=256,
            key="settings_max_tokens",
        )
        submitted = st.form_submit_button("保存模型设置", type="primary", width="content")
    if submitted:
        _save_model_settings(model, base_url, temperature, max_tokens)

    st.markdown("---")
    ui.section("🙈 隐私模式", "开启后，驾驶舱等页面的客户名称会脱敏显示（如「云***」）。")
    if PRIVACY_KEY not in st.session_state:
        try:
            st.session_state[PRIVACY_KEY] = bool(config.is_privacy_mode())
        except Exception:  # noqa: BLE001
            st.session_state[PRIVACY_KEY] = False
    st.toggle(
        "开启隐私模式（客户名脱敏）",
        key=PRIVACY_KEY,
        on_change=_save_privacy,
    )
    st.caption("隐私模式只影响界面展示，不修改台账原始数据。")


def _save_privacy() -> None:
    value = bool(st.session_state.get(PRIVACY_KEY))
    try:
        config.save_user_settings({"privacy_mode": value})
    except Exception:  # noqa: BLE001
        return
    _flash(f"隐私模式已{'开启' if value else '关闭'}。")


# =============================================================================
# 企业信息
# =============================================================================
def _render_company_tab() -> None:
    ui.section("🏢 企业信息", "修改后保存在本地设置中，用于申报表抬头、提醒与 AI 上下文。")
    company = ui.company_snapshot()
    with st.form("settings_company_form"):
        cols = st.columns(2)
        with cols[0]:
            name = st.text_input("企业名称", value=str(company.get("name") or ""), key="settings_company_name")
            short_name = st.text_input("简称", value=str(company.get("short_name") or ""), key="settings_company_short")
            taxpayer_type = st.text_input(
                "纳税人类型", value=str(company.get("taxpayer_type") or "小规模纳税人"), key="settings_company_type"
            )
            industry = st.text_input("所属行业", value=str(company.get("industry") or ""), key="settings_company_industry")
            credit_code = st.text_input(
                "统一社会信用代码",
                value=str(company.get("unified_social_credit_code") or ""),
                key="settings_company_credit",
            )
        with cols[1]:
            tax_authority = st.text_input(
                "主管税务机关", value=str(company.get("tax_authority") or ""), key="settings_company_authority"
            )
            city = st.text_input("城市", value=str(company.get("city") or ""), key="settings_company_city")
            county = st.text_input("区县", value=str(company.get("county") or ""), key="settings_company_county")
            stamp_rate = st.number_input(
                "城建税税率（市区 7% / 县城 5%）",
                min_value=0.01,
                max_value=0.07,
                value=float(company.get("city_maintenance_tax_rate") or 0.07),
                step=0.01,
                format="%.2f",
                key="settings_company_stamp_rate",
            )
            accounting_standard = st.text_input(
                "会计准则", value=str(company.get("accounting_standard") or "小企业会计准则"),
                key="settings_company_accounting",
            )
        submitted = st.form_submit_button("保存企业信息", type="primary", width="content")
    if submitted:
        patch = {
            "name": name,
            "short_name": short_name,
            "taxpayer_type": taxpayer_type,
            "industry": industry,
            "unified_social_credit_code": credit_code,
            "tax_authority": tax_authority,
            "city": city,
            "county": county,
            "city_maintenance_tax_rate": float(stamp_rate),
            "accounting_standard": accounting_standard,
        }
        try:
            config.save_user_settings({"company": patch})
        except Exception as exc:  # noqa: BLE001
            _flash(f"保存企业信息失败：{exc}", "error")
        else:
            _flash("企业信息已保存。")
            st.rerun()
    st.caption(f"配置目录：{config.DATA_DIR} · 设置文件：{Path(config.SETTINGS_FILE).name}")


# =============================================================================
# 数据管理
# =============================================================================
def _render_data_tab() -> None:
    ui.section("💾 数据管理", "所有台账与设置均保存在本地；备份会复制到 data/backups。")
    try:
        stats = LA.storage_stats()
    except Exception:  # noqa: BLE001
        stats = {}
    labels = {
        "income": "收入台账",
        "expense": "费用台账",
        "rd": "研发辅助账",
        "journal_entries": "凭证头",
        "journal_lines": "分录行",
        "contracts": "合同台账",
        "filings": "申报记录",
    }
    if stats:
        st.dataframe(
            pd.DataFrame([{"数据表": labels.get(name, name), "记录数": count} for name, count in stats.items()]),
            hide_index=True,
            width="stretch",
        )

    cols = st.columns(3)
    with cols[0]:
        if st.button("🗄️ 备份全部台账", key="settings_backup", width="stretch"):
            try:
                backups = LA.backup_all("manual")
            except Exception as exc:  # noqa: BLE001
                ui.failure("备份失败。", exc)
            else:
                st.success(f"已备份 {len(backups)} 个数据文件到 {config.BACKUP_DIR}")
                with st.expander("备份文件列表", expanded=False):
                    for path in backups:
                        st.caption(str(path))
    with cols[1]:
        if st.button("🧪 加载示例数据", key="settings_seed", width="stretch"):
            try:
                LA.seed_demo_data(reset=False)
            except Exception as exc:  # noqa: BLE001
                ui.failure("加载示例数据失败。", exc)
            else:
                cache.clear()
                _flash("示例数据已加载。")
                st.rerun()
    with cols[2]:
        if st.button("♻️ 重新生成示例数据", key="settings_seed_reset", width="stretch"):
            try:
                LA.seed_demo_data(reset=True)
            except Exception as exc:  # noqa: BLE001
                ui.failure("重新生成示例数据失败。", exc)
            else:
                cache.clear()
                _flash("示例数据已重新生成（原有台账已清空）。")
                st.rerun()

    with st.container(border=True):
        st.markdown("**清空全部台账**")
        confirm = st.checkbox("我确认要清空全部台账数据（收入 / 费用 / 研发 / 合同 / 申报记录，不含日志）", key="settings_clear_confirm")
        if st.button("🗑️ 清空全部台账", key="settings_clear", disabled=not confirm, width="content"):
            try:
                LA.clear_all()
            except Exception as exc:  # noqa: BLE001
                ui.failure("清空台账失败。", exc)
            else:
                cache.clear()
                _flash("全部台账已清空。", "warning")
                st.rerun()

    st.caption(
        f"数据目录：{config.DATA_DIR} · 导出目录：{config.EXPORT_DIR} · 备份目录：{config.BACKUP_DIR} · 日志目录：{config.LOGS_DIR}"
    )
    theme.disclaimer()


# =============================================================================
# 审计日志
# =============================================================================
def _render_audit_tab() -> None:
    ui.section("🧾 审计日志", "算税、分录、申报与关键操作均写入本地日志，便于追溯。")
    audit = runtime.load("ledger.audit")
    if audit is not None and hasattr(audit, "recent_logs"):
        try:
            logs = dict(audit.recent_logs(limit=50))
        except Exception as exc:  # noqa: BLE001
            ui.failure("读取审计日志失败。", exc)
            logs = {}
    else:
        logs = {}
        for name in ledger.store.LOG_DEFS:
            try:
                logs[name] = ledger.read_log(name, limit=50)
            except Exception:  # noqa: BLE001
                logs[name] = []

    log_names = list(ledger.store.LOG_DEFS)
    labels = {name: ledger.store.log_label(name) for name in log_names}
    selected = st.selectbox("日志类型", log_names, format_func=lambda name: labels.get(name, name), key="settings_log_type")
    rows = logs.get(selected) or []
    if not rows:
        st.info("该日志暂无记录。算税、保存申报或生成分录后会自动记录。", icon="🧾")
        return
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption(f"共展示最近 {len(rows)} 条记录（最新的在前）。")


# =============================================================================
# 关于
# =============================================================================
def _render_about_tab() -> None:
    ui.section("ℹ️ 关于税小智")
    st.markdown(
        f"""
        **{config.APP_NAME}** · 版本 `{config.APP_VERSION}`

        面向小规模纳税人的本地自主税务填报工具，核心能力：

        - **确定性算税引擎**：增值税及附加、企业所得税、研发加计扣除、印花税，全部由本地 Python 代码计算，AI 不参与算税。
        - **台账管理**：收入 / 费用 / 研发辅助账 / 合同台账，CSV 本地存储，支持导入导出。
        - **申报填报**：自动生成增值税、企业所得税预缴、财务报表、印花税申报表，校验并导出 Excel 包。
        - **申报核对**：本地 OCR 识别申报表截图或粘贴文本，与台账重算结果逐栏比对。
        - **政策知识库**：本地政策文件检索，标注来源与生效日期，并检查时效。
        - **AI 助手（DeepSeek）**：解释算税结果、回答税务问题、辅助记账；无 Key 时自动降级为离线确定性回复。

        **数据安全**：所有财务数据仅保存在本机 `data/`、`exports/`、`logs/` 目录；
        除 AI 对话 / 申报表文字解析会向 DeepSeek 发送必要文本片段外，数据不出内网。
        API Key 仅保存在本地 `data/user_settings.json`（权限 600）。
        """
    )
    theme.disclaimer()
    with st.expander("📌 使用提示与免责声明", expanded=True):
        st.markdown(
            "- 本系统不能替代电子税务局申报，也不提供税务鉴证服务；请登录电子税务局完成正式申报。\n"
            "- 政策参数按 2026 年现行口径内置，若政策调整请以税务机关公告为准。\n"
            "- 零申报也需按期提交，逾期可能产生罚款与信用影响。\n"
            "- 涉及税务处理判断的事项，建议咨询主管税务机关或专业税务顾问。"
        )
        st.caption(f"免责声明：{config.DISCLAIMER}")
    st.caption(f"当前时间：{config.now_text()} · 数据目录：{config.DATA_DIR}")


# =============================================================================
# 页面入口
# =============================================================================
def render() -> None:
    """渲染设置页。"""
    ui.page_header(
        "设置",
        "API Key、模型、企业信息、隐私模式、数据备份与审计日志。",
        icon="⚙️",
        right_text=f"{config.APP_SHORT_NAME} v{config.APP_VERSION}",
    )
    _show_flash()
    tabs = st.tabs(["🔑 API 与模型", "🏢 企业信息", "💾 数据管理", "🧾 审计日志", "ℹ️ 关于"])
    with tabs[0]:
        _render_api_tab()
        theme.disclaimer()
    with tabs[1]:
        _render_company_tab()
    with tabs[2]:
        _render_data_tab()
    with tabs[3]:
        _render_audit_tab()
    with tabs[4]:
        _render_about_tab()
