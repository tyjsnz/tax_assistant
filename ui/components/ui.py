"""通用布局与交互组件：标题栏、指标卡、卡片、空态、表格、错误提示。

页面只组合这些组件，不重复写样式与异常处理逻辑。
"""

from __future__ import annotations

import traceback
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Sequence

import pandas as pd
import streamlit as st

import config
from ui.components import cache, ledger_api, theme

# =============================================================================
# 标题与分节
# =============================================================================
def page_header(title: str, subtitle: str = "", icon: str = "", right_text: str = "") -> None:
    """渐变标题栏（每个页面顶部调用一次）。"""
    full_title = f"{icon} {title}".strip() if icon else title
    theme.hero(full_title, subtitle, right_text)


def section(title: str, description: str = "", *, icon: str = "") -> None:
    """小节标题 + 说明。"""
    label = f"{icon} {title}".strip() if icon else title
    st.markdown(f'<div class="tx-section-title">{label}</div>', unsafe_allow_html=True)
    if description:
        st.markdown(f'<div class="tx-section-desc">{description}</div>', unsafe_allow_html=True)


# =============================================================================
# 指标卡
# =============================================================================
def metric_row(items: Sequence[dict], columns: int | None = None, *, border: bool = True) -> None:
    """一排指标卡。

    ``items`` 每项支持 ``label`` / ``value`` / ``delta`` / ``help`` / ``icon``。
    传入值由调用方负责格式化（金额用 ``formatting.fmt_money``）。
    """
    if not items:
        return
    count = columns or len(items)
    count = max(1, min(count, len(items)))
    columns_ctx = st.columns(count)
    for index, item in enumerate(items):
        column = columns_ctx[index % count]
        with column:
            st.metric(
                label=item.get("label", ""),
                value=item.get("value", "—"),
                delta=item.get("delta"),
                help=item.get("help"),
                icon=item.get("icon"),
                border=border,
                width="stretch",
            )


def kpi(label: str, value: Any, delta: Any = None, help_text: str | None = None) -> None:
    """单个指标卡。"""
    st.metric(label=label, value=value, delta=delta, help=help_text, border=True, width="stretch")


# =============================================================================
# 卡片容器
# =============================================================================
@contextmanager
def card(title: str = "", description: str = "", *, key: str | None = None) -> Iterator[None]:
    """圆角细边框卡片容器（标题可选）。"""
    with st.container(border=True, key=key):
        if title:
            st.markdown(f"**{title}**")
        if description:
            st.caption(description)
        yield


# =============================================================================
# 空态与示例数据
# =============================================================================
FLASH_KEY = "_ui_flash"


def flash(message: str, level: str = "success") -> None:
    """跨 rerun 的提示消息（rerun 后仍可见）。"""
    st.session_state.setdefault(FLASH_KEY, []).append({"message": message, "level": level})


def show_flash() -> None:
    """展示并清空待显示提示。"""
    for item in st.session_state.pop(FLASH_KEY, []) or []:
        level = item.get("level", "success")
        if level == "warning":
            st.warning(item["message"])
        elif level == "error":
            st.error(item["message"])
        elif level == "info":
            st.info(item["message"])
        else:
            st.success(item["message"])


def empty_state(
    title: str,
    description: str = "",
    *,
    icon: str = "🗂️",
    seed_key: str | None = None,
    seed_label: str = "加载示例数据",
    show_seed: bool = True,
) -> bool:
    """友好空态；带「加载示例数据」按钮时返回本次是否点击。

    注意：点击后本函数内部直接加载示例数据、清缓存并 ``st.rerun()``，不会返回 True。
    """
    st.html(
        f"""
        <div class="tx-empty">
          <div class="tx-empty-icon">{icon}</div>
          <div class="tx-empty-title">{title}</div>
          <div class="tx-empty-desc">{description}</div>
        </div>
        """
    )
    if not show_seed or seed_key is None:
        return False
    _, center, _ = st.columns([1, 1, 1])
    with center:
        clicked = st.button(
            seed_label,
            key=seed_key,
            icon=":material/database_upload:",
            type="primary",
            width="stretch",
        )
    if clicked:
        seed_demo_data()
    return False


def seed_demo_data(reset: bool = False) -> None:
    """加载示例数据 → 清缓存 → rerun（失败时 st.error，不崩溃）。"""
    try:
        ledger_api.seed_demo_data(reset=reset)
    except Exception as exc:  # noqa: BLE001
        st.error(f"加载示例数据失败：{exc}")
        return
    cache.clear()
    flash("示例数据已加载")
    st.rerun()


def seed_button(key: str, label: str = "加载示例数据", *, reset: bool = False, width: str = "content") -> None:
    """独立的示例数据按钮（非空态场景，如设置页）。"""
    if st.button(label, key=key, icon=":material/database_upload:", width=width):
        seed_demo_data(reset=reset)


# =============================================================================
# 提示与错误
# =============================================================================
def module_missing(feature: str, module: str, *, detail: str = "") -> None:
    """模块未就绪的友好提示（不抛出异常）。"""
    extra = f"（{detail}）" if detail else ""
    st.warning(
        f"{feature}暂不可用：`{module}` 模块尚未就绪{extra}。"
        "该模块由队友并行开发，完成后刷新页面即可使用；算税、台账、日历等本地功能不受影响。",
        icon="🧩",
    )


def failure(message: str, exc: BaseException | None = None, *, show_traceback: bool = True) -> None:
    """中文错误提示 + 可折叠异常详情（绝不整页崩溃）。"""
    st.error(message)
    if exc is not None and show_traceback:
        with st.expander("查看错误详情", expanded=False):
            st.code("".join(traceback.format_exception_only(type(exc), exc)).strip(), language="text")


def status_alert(status: str, text: str) -> None:
    """按 PASS / WARNING / ERROR 输出提示框。"""
    from ui.components import formatting

    level = formatting.status_alert_level(status)
    icon = formatting.status_icon(status)
    message = f"{icon} {text}"
    if level == "success":
        st.success(message)
    elif level == "warning":
        st.warning(message)
    elif level == "error":
        st.error(message)
    else:
        st.info(message)


# =============================================================================
# 表格
# =============================================================================
def records_table(
    rows: Iterable[dict],
    columns: Sequence[tuple[str, str]],
    *,
    key: str | None = None,
    height: int | str = "auto",
    empty_text: str = "暂无记录",
    money_fields: Iterable[str] = (),
) -> None:
    """只读记录表（列 = 字段名 / 中文表头）。

    ``money_fields`` 中的字段按 ``¥1,234.56`` 格式化，保证界面金额口径统一。
    """
    from ui.components.formatting import fmt_money

    data = list(rows)
    if not data:
        st.caption(empty_text)
        return
    money_set = set(money_fields)
    records = []
    for row in data:
        record = {}
        for field, label in columns:
            value = row.get(field)
            record[label] = fmt_money(value) if field in money_set else value
        records.append(record)
    frame = pd.DataFrame(records)
    st.dataframe(frame, hide_index=True, height=height, key=key, width="stretch")


def _fmt_cell(value: Any) -> str:
    """差异报告单元格格式化：数字统一金额展示，其余转字符串（避免 Arrow 类型混用）。"""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        from ui.components.formatting import fmt_money

        return fmt_money(value)
    return str(value)


def status_table(details: Sequence[dict]) -> None:
    """PASS / WARNING / ERROR 差异报告表（按状态着色）。"""
    if not details:
        st.caption("暂无校验明细")
        return
    frame = pd.DataFrame(
        [
            {
                "状态": _fmt_cell(detail.get("status")),
                "项目": _fmt_cell(detail.get("field")),
                "期望值": _fmt_cell(detail.get("expected")),
                "实际值": _fmt_cell(detail.get("actual")),
                "说明": _fmt_cell(detail.get("message")),
            }
            for detail in details
        ]
    )

    def _style(value: Any) -> str:
        text = str(value)
        if text == "PASS":
            return "background-color: #E8F5E9; color: #1B5E20; font-weight: 600;"
        if text == "WARNING":
            return "background-color: #FFF8E1; color: #8A6412; font-weight: 600;"
        if text == "ERROR":
            return "background-color: #FDECEA; color: #B3261E; font-weight: 600;"
        return ""

    try:
        styled = frame.style.map(_style, subset=["状态"])  # pandas >= 2.1
    except AttributeError:  # pragma: no cover - 兼容旧 pandas
        styled = frame.style.applymap(_style, subset=["状态"])
    st.dataframe(styled, hide_index=True, width="stretch")


# =============================================================================
# 小工具
# =============================================================================
def kv_line(label: str, value: str) -> None:
    """键值行（紧凑展示）。"""
    st.markdown(f"**{label}**：{value}")


def ai_status_caption() -> None:
    """AI 配置状态（脱敏）。"""
    if config.is_ai_configured():
        st.caption(f"🤖 AI 已配置：{config.mask_key()} · 模型 {config.get_llm_config().get('model')}")
    else:
        st.caption("🤖 AI 未配置：可在「⚙️ 设置」填写 DeepSeek API Key")


def company_snapshot() -> dict:
    """企业信息快照（异常时回退默认值）。"""
    try:
        return config.get_company()
    except Exception:  # noqa: BLE001
        return dict(config.COMPANY)
