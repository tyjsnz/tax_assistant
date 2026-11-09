"""主题与少量自定义 CSS 注入。

配色以 ``.streamlit/config.toml`` 为准（税务蓝 / 浅灰白底）；这里只补充
原生主题无法表达的部分：渐变标题栏、企业名片、空态与免责声明块的视觉处理。
不修改 Streamlit 内部类名，也不做花哨动画。
"""

from __future__ import annotations

import config
import streamlit as st

_CSS = """
<style>
  /* 内容区最大宽度：在宽屏上保持专业后台的阅读宽度 */
  .block-container { padding-bottom: 2.5rem; }

  /* 渐变标题栏 */
  .tx-hero {
    background: linear-gradient(115deg, #164B8F 0%, #1E5AA8 45%, #2F79C7 100%);
    border-radius: 14px;
    padding: 1.35rem 1.6rem;
    margin: 0 0 1.1rem 0;
    color: #FFFFFF;
    box-shadow: 0 6px 20px rgba(30, 90, 168, 0.18);
  }
  .tx-hero h1, .tx-hero h2, .tx-hero h3 { color: #FFFFFF !important; margin: 0; }
  .tx-hero .tx-hero-title { font-size: 1.42rem; font-weight: 700; letter-spacing: .02em; }
  .tx-hero .tx-hero-sub { margin-top: .45rem; font-size: .86rem; color: rgba(255,255,255,.86); }
  .tx-hero .tx-hero-right { text-align: right; font-size: .82rem; color: rgba(255,255,255,.9); }

  /* 企业名片 */
  .tx-company {
    background: #FFFFFF;
    border: 1px solid #E3E9F2;
    border-left: 4px solid #1E5AA8;
    border-radius: 12px;
    padding: 1rem 1.2rem;
    box-shadow: 0 2px 10px rgba(16, 38, 70, 0.05);
  }
  .tx-company .tx-company-name { font-size: 1.06rem; font-weight: 700; color: #1D2129; }
  .tx-company .tx-company-meta { margin-top: .35rem; font-size: .82rem; color: #5A6B85; line-height: 1.7; }
  .tx-company .tx-company-tag {
    display: inline-block; margin-right: .45rem; padding: .1rem .5rem;
    background: #EDF3FC; color: #1E5AA8; border-radius: 999px; font-size: .74rem;
  }

  /* 空态卡片 */
  .tx-empty {
    border: 1px dashed #C9D6E6;
    border-radius: 12px;
    background: #FBFCFE;
    padding: 1.6rem 1rem;
    text-align: center;
  }
  .tx-empty .tx-empty-icon { font-size: 1.8rem; }
  .tx-empty .tx-empty-title { margin-top: .45rem; font-size: .98rem; font-weight: 600; color: #1D2129; }
  .tx-empty .tx-empty-desc { margin-top: .3rem; font-size: .82rem; color: #6B7C93; }

  /* 免责声明块 */
  .tx-disclaimer {
    margin-top: .9rem;
    padding: .55rem .8rem;
    background: #FFF9EC;
    border: 1px solid #F3E1B8;
    border-radius: 8px;
    color: #8A6412;
    font-size: .78rem;
    line-height: 1.6;
  }

  /* 小节标题 */
  .tx-section-title { margin: .2rem 0 .15rem 0; font-size: 1.02rem; font-weight: 650; color: #1D2129; }
  .tx-section-desc { margin: 0 0 .5rem 0; font-size: .8rem; color: #6B7C93; }

  /* 指标卡数值强化 */
  [data-testid="stMetricValue"] { color: #16365F; }

  /* 侧边栏导航更紧凑 */
  [data-testid="stSidebar"] .stRadio > div { gap: .15rem; }

  /* 表格更紧凑 */
  [data-testid="stDataFrame"] div[role="grid"] { font-size: .82rem; }
</style>
"""


def apply() -> None:
    """注入全局样式（每页渲染一次即可）。"""
    try:
        st.html(_CSS)
    except Exception:  # noqa: BLE001 - 样式注入失败不应影响页面功能
        pass


def hero(title: str, subtitle: str = "", right_text: str = "") -> None:
    """渐变标题栏。"""
    left = f'<div class="tx-hero-title">{title}</div>'
    if subtitle:
        left += f'<div class="tx-hero-sub">{subtitle}</div>'
    right = f'<div class="tx-hero-right">{right_text}</div>' if right_text else ""
    st.html(f'<div class="tx-hero">{left}{right}</div>')


def company_card(company: dict) -> None:
    """企业名片（名称 / 纳税人类型 / 统一社会信用代码 / 主管税务机关）。"""
    company = company or {}
    name = company.get("name") or config.COMPANY["name"]
    tags = [
        company.get("taxpayer_type") or config.COMPANY["taxpayer_type"],
        company.get("accounting_standard") or config.COMPANY["accounting_standard"],
        company.get("vat_levy_method") or config.COMPANY["vat_levy_method"],
    ]
    tag_html = "".join(f'<span class="tx-company-tag">{tag}</span>' for tag in tags if tag)
    st.html(
        f"""
        <div class="tx-company">
          <div class="tx-company-name">{name}</div>
          <div class="tx-company-meta">
            <div style="margin-top:.5rem">{tag_html}</div>
            <div>统一社会信用代码：{company.get('unified_social_credit_code') or config.COMPANY['unified_social_credit_code']}</div>
            <div>主管税务机关：{company.get('tax_authority') or config.COMPANY['tax_authority']}</div>
            <div>所属行业：{company.get('industry') or config.COMPANY['industry']}</div>
          </div>
        </div>
        """
    )


def disclaimer(text: str | None = None) -> None:
    """免责声明块（所有算税 / 申报 / 政策结果下方必须调用）。"""
    message = text or config.DISCLAIMER
    st.html(f'<div class="tx-disclaimer">{message}</div>')
