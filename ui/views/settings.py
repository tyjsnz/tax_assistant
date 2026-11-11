"""⚙️ 设置：DeepSeek API Key、模型、企业信息、隐私模式、数据备份、示例数据、审计日志与关于。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

import config
import ledger
from ui.components import archive_api as AA, cache, company_api, formatting as F, ledger_api as LA, runtime, theme, ui

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
# 企业信息（多公司管理，P1-A）
# =============================================================================
def _rate_percent(value: Any) -> str:
    try:
        return f"{float(value or 0) * 100:.0f}%"
    except (TypeError, ValueError):
        return "—"


def _render_region_caption(company: dict | None = None) -> None:
    """展示当前地区与附加税费参数（城建税 / 教育费附加 / 六税两费减半）。"""
    region = company_api.region()
    try:
        full = float(region.get("surtax_rate_full") or 0.12)
        discounted = float(region.get("surtax_rate_discounted") or 0.06)
    except (TypeError, ValueError):
        full, discounted = 0.12, 0.06
    st.caption(
        f"当前地区：{region.get('label')} · 城建税 {_rate_percent(region.get('city_maintenance_tax_rate'))} · "
        f"附加税费综合 {full * 100:.0f}%（六税两费减半后 {discounted * 100:.1f}%）"
    )


def _render_current_company_form(company: dict) -> None:
    """当前公司信息编辑（含地区预设；保存走 workspace.update_company）。"""
    active_id = str(company.get("id") or company_api.active_company_id())
    presets = company_api.region_presets()
    preset_ids = [str(item.get("id")) for item in presets]
    current_region = str(
        company.get("region_id")
        or company_api.infer_region_id(company.get("province"), company.get("city_maintenance_tax_rate"))
    )
    index = preset_ids.index(current_region) if current_region in preset_ids else 0
    _render_region_caption(company)
    with st.form("settings_company_form"):
        cols = st.columns(2)
        with cols[0]:
            name = st.text_input("企业名称 *", value=str(company.get("name") or ""), key="settings_company_name")
            short_name = st.text_input("简称", value=str(company.get("short_name") or ""), key="settings_company_short")
            credit_code = st.text_input(
                "统一社会信用代码",
                value=str(company.get("unified_social_credit_code") or ""),
                key="settings_company_credit",
            )
            tax_authority = st.text_input(
                "主管税务机关", value=str(company.get("tax_authority") or ""), key="settings_company_authority"
            )
            industry = st.text_input("所属行业", value=str(company.get("industry") or ""), key="settings_company_industry")
        with cols[1]:
            province = st.text_input("省份", value=str(company.get("province") or ""), key="settings_company_province")
            city = st.text_input("城市", value=str(company.get("city") or ""), key="settings_company_city")
            county = st.text_input("区县", value=str(company.get("county") or ""), key="settings_company_county")
            region_id = st.selectbox(
                "地区预设（决定城建税税率）",
                preset_ids,
                index=index,
                format_func=lambda preset_id: company_api.region_label(company_api.preset_by_id(preset_id)),
                key="settings_company_region",
                help="市区 7%、县城/镇 5%、其他 1%；附加税费 = 增值税 ×（城建税 + 3% + 2%）× 六税两费减半",
            )
            accounting_standard = st.text_input(
                "会计准则",
                value=str(company.get("accounting_standard") or "小企业会计准则"),
                key="settings_company_accounting",
            )
            note = st.text_input("备注", value=str(company.get("note") or ""), key="settings_company_note")
        submitted = st.form_submit_button("保存当前公司信息", type="primary", width="content")
    if not submitted:
        return
    if not name.strip():
        st.error("企业名称不能为空。")
        return
    patch = {
        "name": name.strip(),
        "short_name": short_name.strip(),
        "unified_social_credit_code": credit_code.strip(),
        "tax_authority": tax_authority.strip(),
        "industry": industry.strip(),
        "province": province.strip(),
        "city": city.strip(),
        "county": county.strip(),
        "region_id": region_id,
        "accounting_standard": accounting_standard.strip(),
        "note": note.strip(),
    }
    try:
        record = company_api.update_company(active_id, patch)
    except Exception as exc:  # noqa: BLE001 - 后端 ValueError 中文提示
        st.error(f"保存企业信息失败：{exc}")
        return
    cache.clear()
    _flash(f"企业信息已保存（{record.get('name') or name}），当前地区：{company_api.region_text(record)}")
    st.rerun()


def _render_company_list(companies: list[dict], active_id: str) -> None:
    """公司列表：表格概览 + 每行切换 / 删除（删除需确认，后端会拒绝删当前 / 唯一公司）。"""
    st.markdown("**公司列表**")
    if not companies:
        st.info("尚未创建公司，请在下方「新建公司」。", icon="🏢")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "编号": item.get("id"),
                    "名称": item.get("name"),
                    "地区": company_api.region_text(item),
                    "是否当前": "✅ 当前" if str(item.get("id")) == str(active_id) else "",
                    "已有数据": "有" if item.get("has_data") else "空",
                    "账套目录": item.get("data_dir") or "—",
                }
                for item in companies
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    for item in companies:
        company_id = str(item.get("id"))
        with st.container(border=True):
            info, switch_col, delete_col = st.columns([5, 1, 1])
            with info:
                st.markdown(f"**{item.get('name') or company_id}**（{company_id}）")
                st.caption(
                    f"地区：{company_api.region_text(item)}"
                    f" · 数据：{'已有台账' if item.get('has_data') else '空账套'}"
                    f" · 目录：{item.get('data_dir') or '—'}"
                )
            with switch_col:
                if st.button(
                    "切换",
                    key=f"settings_company_switch_{company_id}",
                    width="stretch",
                    disabled=str(item.get("id")) == str(active_id),
                ):
                    st.session_state["_pending_company"] = company_id
                    _flash(f"正在切换到 {item.get('name') or company_id}…")
                    st.rerun()
            with delete_col:
                confirm = st.checkbox(
                    "确认删除",
                    key=f"settings_company_delete_confirm_{company_id}",
                    help="删除为软删除（目录移到 companies/_deleted）；当前 / 唯一公司会被后端拒绝",
                )
                if st.button(
                    "删除",
                    key=f"settings_company_delete_{company_id}",
                    width="stretch",
                    disabled=not confirm,
                ):
                    try:
                        company_api.delete_company(company_id)
                    except Exception as exc:  # noqa: BLE001 - 展示后端中文 ValueError
                        st.error(str(exc))
                    else:
                        _flash(f"已删除公司 {item.get('name') or company_id}（软删除）", "warning")
                        st.rerun()


def _render_new_company_form() -> None:
    """新建公司表单（可选立即切换）。"""
    st.markdown("**新建公司**")
    presets = company_api.region_presets()
    preset_ids = [str(item.get("id")) for item in presets]
    with st.form("settings_new_company_form", clear_on_submit=True):
        cols = st.columns(3)
        with cols[0]:
            name = st.text_input("公司名称 *", key="settings_new_company_name", placeholder="如：某某科技有限公司")
            short_name = st.text_input("简称", key="settings_new_company_short")
            industry = st.text_input("所属行业", key="settings_new_company_industry")
        with cols[1]:
            credit_code = st.text_input("统一社会信用代码", key="settings_new_company_credit")
            region_id = st.selectbox(
                "地区预设",
                preset_ids,
                index=0,
                format_func=lambda preset_id: company_api.region_label(company_api.preset_by_id(preset_id)),
                key="settings_new_company_region",
            )
            note = st.text_input("备注", key="settings_new_company_note")
        with cols[2]:
            activate_new = st.checkbox("创建后立即切换", value=False, key="settings_new_company_activate")
            st.caption("新公司拥有独立账套目录；创建后可在侧边栏切换。")
        submitted = st.form_submit_button("新建公司", type="primary", width="content")
    if not submitted:
        return
    if not name.strip():
        st.error("公司名称不能为空。")
        return
    payload = {
        "name": name.strip(),
        "short_name": short_name.strip(),
        "unified_social_credit_code": credit_code.strip(),
        "industry": industry.strip(),
        "region_id": region_id,
        "note": note.strip(),
    }
    try:
        record = company_api.add_company(payload, activate_new=bool(activate_new))
    except Exception as exc:  # noqa: BLE001 - 后端 ValueError 中文提示
        st.error(f"新建公司失败：{exc}")
        return
    cache.clear()
    if activate_new:
        st.session_state["_pending_company"] = str(record.get("id") or "")
    _flash(
        f"已新建公司「{record.get('name') or name}」（{record.get('id')}）"
        + ("，并已切换到新公司" if activate_new else "")
    )
    st.rerun()


def _render_workspace_info() -> None:
    """工作区信息：数据根目录 / schema 版本 / 当前公司数据目录 / 日志目录。"""
    st.markdown("**工作区信息**")
    summary = company_api.workspace_summary()
    companies = summary.get("companies") or []
    rows = [
        {"项目": "数据根目录", "值": str(summary.get("data_root") or company_api.data_root())},
        {"项目": "Schema 版本", "值": str(summary.get("schema_version") or "—")},
        {"项目": "当前公司", "值": str(summary.get("active_company_id") or company_api.active_company_id())},
        {"项目": "当前公司数据目录", "值": str(summary.get("active_data_dir") or company_api.current_data_dir())},
        {"项目": "当前公司备份目录", "值": str(company_api.current_backup_dir())},
        {"项目": "当前公司上传目录", "值": str(company_api.current_upload_dir())},
        {"项目": "日志目录（全局）", "值": str(summary.get("logs_dir") or config.LOGS_DIR)},
        {"项目": "公司数量", "值": str(len(companies))},
    ]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption(
        f"全局设置文件：{Path(config.SETTINGS_FILE).name}（API Key / 模型不随公司切换） · "
        "切换公司只影响台账、归档、备份与上传目录。"
    )


def _render_company_tab() -> None:
    """企业信息 tab：当前公司编辑 + 公司列表 + 新建公司 + 工作区信息。"""
    ui.section(
        "🏢 企业信息（多公司）",
        "每家公司拥有独立账套（台账 / 归档 / 备份）；地区预设决定城建税税率并影响附加税费计算。",
    )
    if not company_api.workspace_ready():
        ui.module_missing("多公司管理", "workspace", detail=company_api.missing_detail())
    try:
        companies = company_api.list_companies()
    except Exception as exc:  # noqa: BLE001
        st.error(f"读取公司列表失败：{exc}")
        companies = []
    active_id = company_api.active_company_id()
    active = next((item for item in companies if str(item.get("id")) == str(active_id)), None) or (
        companies[0] if companies else company_api.active_company()
    )

    st.markdown("**当前公司信息**")
    _render_current_company_form(active)

    st.markdown("---")
    _render_company_list(companies, active_id)

    st.markdown("---")
    _render_new_company_form()

    st.markdown("---")
    _render_workspace_info()


# =============================================================================
# 数据管理（含 P0-3 备份 / 恢复 / 归档）
# =============================================================================
STATE_BACKUP_RESULT = "_settings_backup_result"
STATE_BACKUP_VERIFY = "_settings_backup_verify_result"
STATE_RESTORE_INSPECT = "_settings_restore_inspect"
STATE_RESTORE_PATH = "_settings_restore_path"
STATE_RESTORE_RESULT = "_settings_restore_result"
STATE_ARCHIVE_VERIFY = "_settings_archive_verify_result"


def _backup_path(result: dict) -> Path:
    raw = result.get("path") or result.get("backup_path") or ""
    return Path(str(raw)) if raw else Path("")


def _backup_sha(result: dict, path: Path) -> str:
    for key in ("file_hash", "sha256", "hash"):
        value = result.get(key)
        if value:
            return str(value)
    return AA.sha256_file(path) if path and path.exists() else ""


def _render_backup_result(result: dict) -> None:
    """展示最近一次创建的备份：路径、大小、SHA-256、清单。"""
    if not isinstance(result, dict) or not result:
        return
    path = _backup_path(result)
    size = result.get("size_bytes") or result.get("size") or (
        path.stat().st_size if path and path.exists() else 0
    )
    sha = _backup_sha(result, path)
    with st.container(border=True):
        st.markdown("**最近一次备份**")
        ui.metric_row(
            [
                {"label": "文件大小", "value": AA.fmt_size(size)},
                {"label": "备份文件数", "value": f"{len(result.get('files') or [])} 个"},
                {"label": "创建时间", "value": str(result.get("created_at") or "—")},
            ],
            columns=3,
        )
        st.caption(f"备份路径：{path or '—'}")
        st.caption(f"SHA-256：{sha or '—'}")
        tables = result.get("tables") or {}
        if isinstance(tables, dict) and tables:
            st.dataframe(
                pd.DataFrame([{"数据表": name, "记录数": count} for name, count in tables.items()]),
                hide_index=True,
                width="stretch",
            )
        if path and path.exists():
            st.download_button(
                "⬇️ 下载备份 zip",
                data=path.read_bytes(),
                file_name=path.name,
                mime="application/zip",
                key="settings_backup_download",
                width="content",
            )


def _create_backup(tag: str, note: str, include_logs: bool) -> None:
    try:
        result = AA.create_backup(tag=tag.strip(), include_logs=bool(include_logs), note=note.strip())
    except Exception as exc:  # noqa: BLE001
        st.error(f"创建备份失败：{exc}")
        return
    st.session_state[STATE_BACKUP_RESULT] = result
    cache.clear()
    _flash(f"备份已创建：{_backup_path(result).name or result.get('path') or ''}")
    st.rerun()


def _verify_backup(target: str, label: str = "") -> None:
    try:
        result = AA.verify_backup(target)
    except Exception as exc:  # noqa: BLE001
        st.error(f"备份校验失败：{exc}")
        return
    st.session_state[STATE_BACKUP_VERIFY] = {"label": label, "result": result}
    st.rerun()


def _delete_backup(target: str) -> None:
    try:
        AA.delete_backup(target)
    except Exception as exc:  # noqa: BLE001
        st.error(f"删除备份失败：{exc}")
        return
    cache.clear()
    _flash("备份已删除。", "warning")
    st.rerun()


def _render_backup_verify() -> None:
    payload = st.session_state.get(STATE_BACKUP_VERIFY) or {}
    result = payload.get("result") or {}
    if not result:
        return
    with st.container(border=True):
        st.markdown(f"**备份校验结果{('：' + payload.get('label')) if payload.get('label') else ''}**")
        ui.status_alert(
            "PASS" if result.get("ok") else "ERROR",
            "备份文件与 manifest 哈希一致。" if result.get("ok") else "备份校验未通过，请检查 tampered / missing 明细。",
        )
        files = result.get("files") or []
        if files:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "文件": item.get("name"),
                            "期望哈希": AA.short_hash(item.get("expected"), 16),
                            "实际哈希": AA.short_hash(item.get("actual"), 16),
                            "结果": "✅ 一致" if item.get("ok") else "❌ 不一致",
                        }
                        for item in files
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
        for name in result.get("tampered") or []:
            st.error(f"文件被篡改：{name}")
        for name in result.get("missing") or []:
            st.warning(f"文件缺失：{name}")


def _render_backup_section() -> None:
    with st.container(border=True):
        st.markdown("**创建备份**")
        st.caption(
            "备份内容：data/ 下全部台账 CSV + manifest.json + README.txt（可选审计日志）；"
            "**绝不包含** API Key（user_settings.json）、上传目录与历史备份。"
        )
        cols = st.columns([2, 2, 1, 2])
        with cols[0]:
            tag = st.text_input("标签（可选）", key="settings_backup_tag", placeholder="如 monthly")
        with cols[1]:
            note = st.text_input("备注（可选）", key="settings_backup_note")
        with cols[2]:
            include_logs = st.checkbox("包含审计日志", value=True, key="settings_backup_logs")
        with cols[3]:
            st.write("")
            if st.button(
                "🗄️ 创建备份",
                key="settings_backup_create",
                type="primary",
                width="stretch",
                disabled=not AA.backup_ready(),
            ):
                _create_backup(tag, note, include_logs)
        if not AA.backup_ready():
            ui.module_missing("创建备份", "archiving.backup", detail=AA.missing_detail())
        result = st.session_state.get(STATE_BACKUP_RESULT)
        if result:
            _render_backup_result(result)

    st.markdown("**备份列表**")
    try:
        backups = AA.list_backups()
    except Exception as exc:  # noqa: BLE001
        ui.failure("读取备份列表失败。", exc, show_traceback=False)
        backups = []
    if not backups:
        st.caption("暂无备份记录。点击上方「创建备份」生成第一份。")
        return
    st.caption(f"共 {len(backups)} 份备份 · 目录：{AA.backup_dir()}")
    for index, backup in enumerate(backups[:10]):
        backup_id = str(backup.get("id") or backup.get("path") or index)
        label = str(backup.get("file_name") or Path(str(backup.get("path") or "")).name or backup_id)
        with st.container(border=True):
            info, verify_col, delete_col = st.columns([6, 1, 1])
            with info:
                st.markdown(f"**{label}**")
                st.caption(
                    f"创建时间：{backup.get('created_at') or '—'} · 大小：{backup.get('size_text') or AA.fmt_size(backup.get('size_bytes'))}"
                    f" · 文件数：{backup.get('file_count') or 0} · 存在：{'是' if backup.get('exists') else '否'}"
                )
                st.caption(
                    f"哈希摘要：{backup.get('file_hash_short') or AA.short_hash(backup.get('file_hash'))}"
                    + (f" · 备注：{backup.get('note')}" if backup.get("note") else "")
                )
            with verify_col:
                if st.button(
                    "校验",
                    key=f"settings_backup_verify_{backup_id}",
                    width="stretch",
                    disabled=not AA.backup_manage_ready(),
                ):
                    _verify_backup(str(backup.get("id") or backup.get("path") or ""), label)
            with delete_col:
                if st.button(
                    "删除",
                    key=f"settings_backup_delete_{backup_id}",
                    width="stretch",
                    disabled=not AA.backup_manage_ready(),
                ):
                    _delete_backup(str(backup.get("id") or backup.get("path") or ""))
    _render_backup_verify()


def _render_restore_preview(inspect_result: dict, staged_path: str) -> None:
    """展示备份预览 + 恢复确认。"""
    if not isinstance(inspect_result, dict) or not inspect_result:
        return
    verify = inspect_result.get("verify") or {}
    ok = bool(verify.get("ok", inspect_result.get("ok", False)))
    with st.container(border=True):
        st.markdown("**备份内容预览**")
        ui.status_alert(
            "PASS" if ok else "ERROR",
            "备份 manifest 校验通过，可以恢复。" if ok else "备份校验未通过，已禁止恢复。",
        )
        st.caption(
            f"备份文件：{Path(str(inspect_result.get('path') or staged_path or '')).name or '—'}"
            f" · 创建时间：{inspect_result.get('created_at') or '—'}"
            f" · 应用版本：{inspect_result.get('app_version') or '—'}"
        )
        tables = inspect_result.get("tables") or {}
        if isinstance(tables, dict) and tables:
            st.dataframe(
                pd.DataFrame([{"数据表": name, "记录数": count} for name, count in tables.items()]),
                hide_index=True,
                width="stretch",
            )
        files = inspect_result.get("files") or []
        if files:
            with st.expander(f"备份文件清单（{len(files)} 个）", expanded=False):
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "文件": item.get("name"),
                                "大小": AA.fmt_size(item.get("size")),
                                "SHA-256": AA.short_hash(item.get("sha256"), 20),
                            }
                            for item in files
                        ]
                    ),
                    hide_index=True,
                    width="stretch",
                )
        for warning in inspect_result.get("warnings") or []:
            st.warning(str(warning))
        confirm = st.checkbox(
            "我已阅读并确认：恢复将覆盖当前全部台账数据（系统会先自动创建安全备份）",
            key="settings_restore_confirm",
        )
        restore_target = str(inspect_result.get("path") or staged_path or "")
        if st.button(
            "♻️ 执行恢复",
            key="settings_restore_run",
            type="primary",
            width="content",
            disabled=not confirm or not ok or not AA.restore_ready(),
        ):
            try:
                with st.spinner("正在恢复备份（含安全备份）…"):
                    result = AA.restore_backup(restore_target, dry_run=False, safety_backup=True)
            except Exception as exc:  # noqa: BLE001
                st.error(f"恢复失败：{exc}")
            else:
                st.session_state[STATE_RESTORE_RESULT] = result
                cache.clear()
                _flash("备份恢复完成，台账已更新。")
                st.rerun()
        if not AA.restore_ready():
            ui.module_missing("备份恢复", "archiving.restore", detail=AA.missing_detail())
        theme.disclaimer(inspect_result.get("disclaimer"))


def _render_restore_result(result: dict) -> None:
    if not isinstance(result, dict) or not result:
        return
    with st.container(border=True):
        st.markdown("**最近一次恢复结果**")
        restored = result.get("restored") or []
        ui.metric_row(
            [
                {"label": "恢复数据表", "value": f"{len(restored)} 个"},
                {"label": "安全备份", "value": "已创建" if result.get("safety_backup") else "未创建"},
            ],
            columns=2,
        )
        if restored:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "数据表": item.get("table"),
                            "恢复条数": item.get("rows"),
                        }
                        for item in restored
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
        if result.get("safety_backup"):
            st.caption(f"恢复前安全备份：{result.get('safety_backup')}")
        if result.get("message"):
            st.info(str(result.get("message")), icon="♻️")
        for warning in result.get("warnings") or []:
            st.warning(str(warning))


def _render_restore_section() -> None:
    with st.container(border=True):
        st.markdown("**恢复备份**")
        st.caption(
            "上传 backup_*.zip → 预览表与条数并校验 → 勾选确认 → 恢复。"
            "恢复前会自动创建安全备份；仅恢复 manifest 白名单内的台账 CSV（拒绝 zip-slip / 哈希不符）。"
        )
        uploaded = st.file_uploader("上传备份 zip", type=["zip"], key="settings_restore_upload")
        inspect_result = st.session_state.get(STATE_RESTORE_INSPECT)
        staged_path = str(st.session_state.get(STATE_RESTORE_PATH) or "")
        cols = st.columns([2, 2, 4])
        with cols[0]:
            if st.button(
                "🔍 预览备份内容",
                key="settings_restore_inspect_btn",
                width="stretch",
                disabled=uploaded is None or not AA.restore_ready(),
            ):
                try:
                    staged = AA.stage_upload(uploaded.getvalue(), uploaded.name)
                    result = AA.inspect_backup(str(staged))
                except Exception as exc:  # noqa: BLE001
                    st.error(f"预览备份失败：{exc}")
                else:
                    st.session_state[STATE_RESTORE_INSPECT] = result
                    st.session_state[STATE_RESTORE_PATH] = str(staged)
                    st.session_state.pop(STATE_RESTORE_RESULT, None)
                    st.rerun()
        with cols[1]:
            st.caption("上传文件仅暂存本地 data/uploads/，不联网。")
        if inspect_result:
            _render_restore_preview(inspect_result, staged_path)
        restore_result = st.session_state.get(STATE_RESTORE_RESULT)
        if restore_result:
            _render_restore_result(restore_result)


def _verify_archive_record(target: str, label: str = "") -> None:
    try:
        result = AA.verify_archive(target)
    except Exception as exc:  # noqa: BLE001
        st.error(f"归档校验失败：{exc}")
        return
    st.session_state[STATE_ARCHIVE_VERIFY] = {"label": label, "result": result}
    st.rerun()


def _render_archive_verify() -> None:
    payload = st.session_state.get(STATE_ARCHIVE_VERIFY) or {}
    result = payload.get("result") or {}
    if not result:
        return
    with st.container(border=True):
        st.markdown(f"**归档校验结果{('：' + payload.get('label')) if payload.get('label') else ''}**")
        ui.status_alert(
            "PASS" if result.get("ok") else "ERROR",
            "归档文件与 manifest 一致，未被篡改。" if result.get("ok") else "归档校验未通过，请检查 tampered / missing。",
        )
        files = result.get("files") or []
        if files:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "文件": item.get("name"),
                            "期望哈希": AA.short_hash(item.get("expected"), 16),
                            "实际哈希": AA.short_hash(item.get("actual"), 16),
                            "结果": "✅ 一致" if item.get("ok") else "❌ 不一致",
                        }
                        for item in files
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
        for name in result.get("tampered") or []:
            st.error(f"文件被篡改：{name}")
        for name in result.get("missing") or []:
            st.warning(f"文件缺失：{name}")
        st.caption(f"校验时间：{result.get('checked_at') or '—'} · 归档目录：{AA.archive_dir()}")


def _render_archive_section() -> None:
    with st.container(border=True):
        st.markdown("**归档包**")
        st.caption(f"归档目录：{AA.archive_dir()} · 归档包为只读快照（Excel + PDF + manifest 哈希）。")
        try:
            archives = AA.list_archives()
        except Exception as exc:  # noqa: BLE001
            ui.failure("读取归档列表失败。", exc, show_traceback=False)
            archives = []
        if not archives:
            st.caption("暂无归档记录。可在「📋 申报填报」页点击「📦 归档本期申报包」生成。")
            return
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "期间": row.get("period"),
                        "类型": row.get("kind"),
                        "创建时间": row.get("created_at"),
                        "文件": row.get("file_name") or Path(str(row.get("path") or "")).name,
                        "大小": row.get("size_text") or AA.fmt_size(row.get("size_bytes")),
                        "哈希摘要": row.get("file_hash_short") or AA.short_hash(row.get("file_hash")),
                        "文件存在": "是" if row.get("exists") else "否",
                    }
                    for row in archives[:20]
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        verify_cols = st.columns([2, 2, 5])
        with verify_cols[0]:
            first = archives[0]
            if st.button(
                "🔍 校验最新归档",
                key="settings_archive_verify_btn",
                width="stretch",
                disabled=not AA.archive_verify_ready(),
            ):
                _verify_archive_record(
                    str(first.get("id") or first.get("path") or ""),
                    f"{first.get('period')} · {first.get('kind')}",
                )
        with verify_cols[1]:
            st.caption(f"共 {len(archives)} 份归档记录")
        with verify_cols[2]:
            if not AA.archive_verify_ready():
                st.caption("归档校验模块尚未就绪。")
        _render_archive_verify()


def _render_sample_actions() -> None:
    """示例数据 / 清空台账 / CSV 快速副本。"""
    cols = st.columns(3)
    with cols[0]:
        if st.button("🧪 加载示例数据", key="settings_seed", width="stretch"):
            try:
                LA.seed_demo_data(reset=False)
            except Exception as exc:  # noqa: BLE001
                ui.failure("加载示例数据失败。", exc)
            else:
                cache.clear()
                _flash("示例数据已加载。")
                st.rerun()
    with cols[1]:
        if st.button("♻️ 重新生成示例数据", key="settings_seed_reset", width="stretch"):
            try:
                LA.seed_demo_data(reset=True)
            except Exception as exc:  # noqa: BLE001
                ui.failure("重新生成示例数据失败。", exc)
            else:
                cache.clear()
                _flash("示例数据已重新生成（原有台账已清空）。")
                st.rerun()
    with cols[2]:
        with st.expander("📄 CSV 快速副本（演示用）", expanded=False):
            if st.button("复制全部台账 CSV", key="settings_csv_copy", width="stretch"):
                try:
                    backups = LA.backup_all("manual")
                except Exception as exc:  # noqa: BLE001
                    ui.failure("备份失败。", exc)
                else:
                    st.success(f"已复制 {len(backups)} 个 CSV 到 {config.BACKUP_DIR}")

    with st.container(border=True):
        st.markdown("**清空全部台账**")
        confirm = st.checkbox(
            "我确认要清空全部台账数据（收入 / 费用 / 研发 / 合同 / 申报记录，不含日志与期间状态）",
            key="settings_clear_confirm",
        )
        if st.button("🗑️ 清空全部台账", key="settings_clear", disabled=not confirm, width="content"):
            try:
                LA.clear_all()
            except Exception as exc:  # noqa: BLE001
                ui.failure("清空台账失败。", exc)
            else:
                cache.clear()
                _flash("全部台账已清空。", "warning")
                st.rerun()


def _render_handover_section() -> None:
    """公司账套交接/迁移包：给公司或新代账的可读导出。"""
    st.markdown("---")
    ui.section(
        "📦 公司账套交接 / 迁移包",
        "与“数据备份”不同：交接包是给公司/新代账的通用数据包，含原始 CSV、可读 Excel、归档文件与清单哈希，不含 API Key。",
    )
    st.info(
        "适用场景：公司更换代账、迁移到其他系统、股东/税务检查需要完整账套、公司注销前留档。"
        "建议交接双方核对 manifest.json 中的记录数与 SHA-256，并签字确认。",
        icon="📦",
    )
    cols = st.columns(2)
    with cols[0]:
        include_archives = st.checkbox("包含申报归档文件（PDF/Excel）", value=True, key="handover_archives")
    with cols[1]:
        include_uploads = st.checkbox("包含原始附件（发票/截图，可能较大且含敏感信息）", value=False, key="handover_uploads")
    if st.button("📦 生成公司交接包", key="handover_build", type="primary", width="stretch"):
        try:
            from archiving.handover import build_handover_package

            with st.spinner("正在打包当前公司账套…"):
                result = build_handover_package(
                    include_archives=include_archives,
                    include_uploads=include_uploads,
                )
            st.session_state["_handover_result"] = result
            st.success("交接包已生成。")
        except Exception as exc:  # noqa: BLE001
            st.error(f"生成失败：{exc}")
    result = st.session_state.get("_handover_result")
    if result:
        path = Path(result["path"])
        cols = st.columns(3)
        with cols[0]:
            st.metric("文件大小", f"{result['size'] / 1024:.1f} KB")
        with cols[1]:
            st.metric("数据表", f"{len(result.get('tables') or {})} 张")
        with cols[2]:
            st.metric("记录总数", f"{sum((result.get('tables') or {}).values())} 条")
        st.caption(f"生成路径：{path}")
        st.caption(f"SHA-256：{result.get('sha256')}")
        if path.exists():
            st.download_button(
                "⬇️ 下载公司交接包",
                data=path.read_bytes(),
                file_name=path.name,
                mime="application/zip",
                key="handover_download",
                width="stretch",
            )
        st.caption("交接包不含 API Key、全局设置与审计日志；原始附件按需勾选，请妥善保管。")


def _render_data_tab() -> None:
    """数据管理：存储统计、备份 / 恢复、归档包、示例数据与清空。"""
    ui.section(
        "💾 数据管理",
        "所有台账与设置均保存在本地；备份为含 manifest 哈希的 zip，恢复前自动创建安全备份。",
    )
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
        "invoices": "发票池",
        "bank_transactions": "银行流水池",
        "matches": "匹配建议",
        "periods": "会计期间",
        "archives": "归档包",
        "backups": "数据备份",
    }
    if stats:
        st.dataframe(
            pd.DataFrame(
                [{"数据表": labels.get(name, name), "记录数": count} for name, count in stats.items()]
            ),
            hide_index=True,
            width="stretch",
        )

    st.markdown("---")
    ui.section("🗄️ 备份与恢复", "备份可随时校验 / 删除；恢复前自动安全备份，可演练。")
    _render_backup_section()
    st.markdown("")
    _render_restore_section()

    st.markdown("---")
    ui.section("🗄️ 归档包", "申报包归档记录与哈希校验。")
    _render_archive_section()

    _render_handover_section()

    st.markdown("---")
    ui.section("🧪 示例数据", "演示与回归测试用；重新生成会清空原有台账。")
    _render_sample_actions()

    st.caption(
        f"数据目录：{config.DATA_DIR} · 导出目录：{config.EXPORT_DIR} · "
        f"备份目录：{AA.backup_dir()} · 归档目录：{AA.archive_dir()} · 日志目录：{config.LOGS_DIR}"
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
    try:
        from ui.components import branding

        branding.about_block()
    except Exception:  # noqa: BLE001
        pass
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
