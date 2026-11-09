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

config.ensure_dirs()
ledger.init_storage()

from ui.components import nav, theme  # noqa: E402
from ui.components import ui as ui_components  # noqa: E402

#: Streamlit 的控制流异常（st.rerun / st.stop）不能当成页面错误吞掉
_CONTROL_FLOW = {"RerunException", "StopException", "ScriptControlException"}


def _render_sidebar() -> str:
    """渲染侧边栏导航并返回当前页面标签。"""
    with st.sidebar:
        st.markdown(f"### 🧾 {config.APP_SHORT_NAME}")
        st.caption("小规模纳税人自主税务填报系统")
        # 必须在 radio 实例化之前消费页面内「快捷入口」的跳转请求
        nav.resolve_goto()
        page = st.radio(
            "功能导航",
            nav.PAGE_LABELS,
            key=nav.NAV_KEY,
            label_visibility="collapsed",
        )
        st.divider()
        company = config.get_company()
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
    return page or nav.PAGE_DASHBOARD


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
    page = _render_sidebar()
    _render_page(page)


main()
