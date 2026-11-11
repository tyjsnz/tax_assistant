"""税小智 · 小规模纳税人自主税务填报系统（Streamlit 单入口）。

启动方式
--------
```bash
.venv/bin/streamlit run ui/streamlit_app.py
```

设计约定
--------
* 侧边栏使用 ``st.radio`` 做页面导航（而非 ``st.navigation``），便于 AppTest 冒烟测试；
* 每个页面是 ``ui/views/<name>.py`` 中的 ``render()`` 函数；
* 任何页面异常都被捕获并转成中文错误提示，不允许整页白屏/崩溃。
"""

from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

import config  # noqa: E402
import ledger  # noqa: E402
import workspace  # noqa: E402

config.ensure_dirs()
workspace.ensure_default()   # 多公司：确保至少一家公司并激活（首次升级自动迁移旧数据）
ledger.init_storage()

from ui.components import company_api, nav, theme  # noqa: E402
from ui.components import ui as ui_components  # noqa: E402

#: Streamlit 的控制流异常（st.rerun / st.stop）不能当成页面错误吞掉
_CONTROL_FLOW = {"RerunException", "StopException", "ScriptControlException"}


PENDING_COMPANY_KEY = "_pending_company"


def _consume_pending_company() -> None:
    """消费其他页面请求的公司切换（必须在 selectbox 实例化之前调用）。

    设置页「切换 / 新建并切换」会写入 ``_pending_company``，这里统一激活并同步
    ``company_switch`` 的 widget 状态，避免侧边栏旧值把公司又切回去。
    """
    pending = st.session_state.pop(PENDING_COMPANY_KEY, None)
    if not pending:
        return
    try:
        company_api.switch_company(str(pending))
    except Exception as exc:  # noqa: BLE001 - 切换失败中文提示，不崩溃
        st.caption(f"切换公司失败：{exc}")
        return
    st.session_state["company_switch"] = str(pending)


def _render_company_switcher() -> None:
    """侧边栏公司切换器 + 当前地区 / 城建税税率（多公司，P1-A）。"""
    _consume_pending_company()
    companies = company_api.list_companies()
    active_id = company_api.active_company_id()
    ids = [str(item.get("id") or "") for item in companies if item.get("id")]
    labels = {str(item.get("id")): company_api.company_label(item) for item in companies}
    if not ids:
        st.caption("🏢 尚未初始化公司，请到「设置 → 企业信息」新建。")
        return
    index = ids.index(active_id) if active_id in ids else 0
    # 统一由 session_state 提供初始值（避免 index 默认值与 session_state 冲突告警）
    if st.session_state.get("company_switch") not in ids:
        st.session_state["company_switch"] = ids[index]
    selected = st.selectbox(
        "当前公司",
        ids,
        format_func=lambda company_id: labels.get(str(company_id), str(company_id)),
        key="company_switch",
        disabled=len(ids) <= 1,
        help="切换公司会切换到该公司独立的账套数据（台账 / 归档 / 备份互不影响）",
    )
    if selected and str(selected) != str(active_id):
        try:
            company_api.switch_company(str(selected))
        except Exception as exc:  # noqa: BLE001 - 切换失败不崩溃，中文提示
            st.error(f"切换公司失败：{exc}")
        else:
            st.rerun()
    region = company_api.region()
    try:
        rate = float(region.get("city_maintenance_tax_rate") or 0.07)
    except (TypeError, ValueError):
        rate = 0.07
    st.caption(f"📍 {region.get('label') or '未指定地区'} · 城建税 {rate * 100:.0f}%")


def _render_sidebar() -> str:
    """渲染侧边栏导航并返回当前页面标签。"""
    with st.sidebar:
        st.markdown(f"### 🧾 {config.APP_SHORT_NAME}")
        st.caption("小规模纳税人自主税务填报系统")
        # 必须在导航渲染之前消费页面内「快捷入口」的跳转请求
        nav.resolve_goto()
        current = str(st.session_state.get(nav.NAV_KEY) or nav.PAGE_DASHBOARD)
        if current not in nav.PAGE_MODULES:
            current = nav.PAGE_DASHBOARD
        # 按「开始 → 日常 → 必做 → 季度/年度 → 异常 → 学习 → 系统」分组，
        # 避免 15 个平铺菜单给新手造成"不知道从哪下手"。
        for group_label, group_pages in nav.NAV_GROUPS:
            st.caption(group_label)
            for page_label in group_pages:
                if st.button(
                    page_label,
                    key=f"nav_btn_{page_label}",
                    width="stretch",
                    type="primary" if page_label == current else "secondary",
                ):
                    st.session_state[nav.NAV_KEY] = page_label
                    st.rerun()
        st.divider()
        try:
            _render_company_switcher()
        except Exception as exc:  # noqa: BLE001 - 切换器故障不影响整页
            st.caption(f"公司切换器暂不可用：{exc}")
        company = company_api.active_company() or config.get_company()
        st.caption(f"🏢 {company.get('short_name') or company.get('name') or '企业'}")
        if config.is_ai_configured():
            st.caption(f"🤖 AI 已配置 · {config.mask_key()}")
        else:
            st.caption("🤖 AI 未配置（可在「设置」填写 Key）")
        try:
            from engine import current_quarter, period_label

            quarter_text = period_label(current_quarter())
        except Exception:  # noqa: BLE001 - 侧边栏信息降级不影响主流程
            quarter_text = f"{config.today().year} 年"
        st.caption(f"📅 当前季度：{quarter_text}")
        st.caption(f"版本 v{config.APP_VERSION} · 数据仅存本地")
    return current or nav.PAGE_DASHBOARD


def _render_page(page: str) -> None:
    """按导航标签调度页面模块；异常时展示中文错误而不是崩溃。"""
    module_name = nav.PAGE_MODULES.get(page)
    if module_name is None:
        st.warning("未知页面，已回到首页。")
        module_name = nav.PAGE_MODULES[nav.PAGE_DASHBOARD]
    try:
        ui_components.show_flash()
        module = importlib.import_module(module_name)
        module.render()
    except Exception as exc:  # noqa: BLE001 - 页面级兜底，防止整页崩溃
        if type(exc).__name__ in _CONTROL_FLOW:
            raise
        st.error(
            f"页面「{page}」渲染失败：{type(exc).__name__}。"
            "请刷新页面重试；其他页面功能不受影响。"
        )
        with st.expander("错误详情（可复制给开发者）", expanded=False):
            st.code(traceback.format_exc(), language="text")

    # 全局浮动 AI 助手：任何页面常驻右下角，点击弹出对话框
    try:
        from ui.components import floating_ai

        floating_ai.render()
    except Exception as exc:  # noqa: BLE001 - 悬浮助手失败不影响当前页面
        if type(exc).__name__ not in _CONTROL_FLOW:
            st.caption(f"浮动 AI 助手暂不可用：{exc}")


def main() -> None:
    """应用入口。"""
    st.set_page_config(
        page_title=config.APP_NAME,
        page_icon="🧾",
        layout="wide",
        initial_sidebar_state="expanded",
        menu_items={
            "Get help": None,
            "Report a bug": None,
            "About": f"{config.APP_NAME}\n版本 {config.APP_VERSION}\n{config.DISCLAIMER}",
        },
    )
    theme.apply()
    # 发票归集：打开系统时自动扫描一次（默认关闭，按设置节流）
    if not st.session_state.get("_invoice_auto_scan_done"):
        st.session_state["_invoice_auto_scan_done"] = True
        try:
            from ingest import collector

            result = collector.maybe_auto_scan()
            if result and not result.get("skipped"):
                imported = int((result.get("folder") or {}).get("imported") or 0) + int((result.get("email") or {}).get("imported") or 0)
                if imported:
                    st.toast(f"📬 自动归集发票：新导入 {imported} 张", icon="📬")
        except Exception:  # noqa: BLE001 - 自动归集失败不影响使用
            pass
    page = _render_sidebar()
    _render_page(page)


main()
