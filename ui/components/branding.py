"""官方发行标识（仅发行包展示；按用户要求不纳入本次 git commit）。

作用：让用户从下载包/应用内清楚识别官方发布渠道、开发方与联系方式，
避免从非官方渠道拿到被篡改的版本。
"""

from __future__ import annotations

import streamlit as st

import config

OFFICIAL = {
    "edition": "官方发行版",
    "site": "https://tax.flacn.net",
    "repo": "https://github.com/tyjsnz/tax_assistant",
    "developer": "snz",
    "email": "274043505@qq.com",
    "notice": "官方下载与演示仅通过 tax.flacn.net / GitHub 发布；其他渠道请核对版本与 SHA-256。",
}


def sidebar_badge() -> None:
    st.markdown(
        f"<div style='margin-top:6px;padding:6px 8px;border-radius:8px;"
        f"background:rgba(55,230,176,.10);border:1px solid rgba(55,230,176,.35);"
        f"font-size:11.5px;line-height:1.6;color:#0f766e'>"
        f"✅ <b>{OFFICIAL['edition']}</b><br/>"
        f"官网：{OFFICIAL['site']}<br/>"
        f"开发方：{OFFICIAL['developer']}</div>",
        unsafe_allow_html=True,
    )


def footer() -> None:
    st.markdown("---")
    st.markdown(
        f"✅ **{OFFICIAL['edition']}** · 官网 [{OFFICIAL['site']}]({OFFICIAL['site']}) · "
        f"开发方 **{OFFICIAL['developer']}** · 联系 [{OFFICIAL['email']}](mailto:{OFFICIAL['email']}) · "
        f"[GitHub]({OFFICIAL['repo']})"
    )
    st.caption(OFFICIAL["notice"])


def about_block() -> None:
    st.markdown(
        f"""
<div style="padding:14px 16px;border-radius:12px;border:1px solid rgba(55,230,176,.45);
background:linear-gradient(135deg,rgba(55,230,176,.10),rgba(91,157,255,.08));margin-bottom:12px">
  <div style="font-size:18px;font-weight:800;color:#0f766e">✅ {OFFICIAL['edition']} · {config.APP_NAME}</div>
  <div style="margin-top:6px;font-size:13px;line-height:1.9">
    官方下载/演示：<a href="{OFFICIAL['site']}" target="_blank">{OFFICIAL['site']}</a><br/>
    开源仓库：<a href="{OFFICIAL['repo']}" target="_blank">{OFFICIAL['repo']}</a><br/>
    开发方：<b>{OFFICIAL['developer']}</b> · 联系邮箱：<a href="mailto:{OFFICIAL['email']}">{OFFICIAL['email']}</a>
  </div>
  <div style="margin-top:6px;font-size:12px;color:#64748b">{OFFICIAL['notice']}</div>
</div>
""",
        unsafe_allow_html=True,
    )


__all__ = ["OFFICIAL", "sidebar_badge", "footer", "about_block"]
