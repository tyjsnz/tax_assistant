"""📬 发票归集：本地文件夹 + 专用邮箱 IMAP（v1.25）。"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ingest import collector
from ui.components import theme, ui


def _render_folder() -> None:
    st.caption(
        "把电子税务局导出的 PDF/OFD/XML 发票放进一个文件夹，系统扫描后自动解析、去重、入发票池，并可自动生成匹配建议。"
    )
    settings = collector.load_settings()
    folder = st.text_input(
        "发票文件夹路径",
        value=str(settings.get("folder_path") or ""),
        key="collector_folder_path",
        placeholder="例如：/Users/你的名字/Documents/发票导出",
        help="需要先把电子税务局「全量发票查询」导出的文件放到这个文件夹",
    )
    auto_match = st.checkbox("导入后自动生成匹配建议", value=bool(settings.get("auto_match", True)), key="collector_folder_auto_match")
    cols = st.columns(2)
    with cols[0]:
        if st.button("💾 保存文件夹设置", key="collector_folder_save", width="stretch"):
            collector.save_settings({**settings, "folder_path": folder, "auto_match": auto_match})
            st.success("已保存到本地。")
    with cols[1]:
        if st.button("📥 立即扫描并导入", key="collector_folder_scan", type="primary", width="stretch"):
            try:
                result = collector.collect_folder(folder, auto_match=auto_match)
                st.success(f"扫描 {result['scanned']} 个文件，导入 {result['imported']} 张，跳过 {result['skipped']} 个。")
                for error in result.get("errors") or []:
                    st.warning(error)
            except Exception as exc:  # noqa: BLE001
                st.error(f"扫描失败：{exc}")
    st.info(
        "电子税务局导出路径（各省略有差异）：我要办税 → 发票业务 → 全量发票查询 / 税务数字账户 → "
        "选择取得发票 → 按期间查询 → 导出 PDF/OFD/XML。把导出目录填到上面即可。",
        icon="🧭",
    )


def _render_email() -> None:
    st.caption(
        "用专用收票邮箱自动拉取附件：只读 IMAP、不删除邮件、不标记已读；需要邮箱服务商开启 IMAP 并使用「授权码」。"
    )
    settings = collector.load_settings()
    cols = st.columns(3)
    with cols[0]:
        host = st.text_input("IMAP 服务器", value=str(settings.get("email_host") or ""), key="collector_email_host", placeholder="imap.qq.com / imap.163.com")
    with cols[1]:
        port = st.number_input("端口", min_value=1, max_value=65535, value=int(settings.get("email_port") or 993), key="collector_email_port")
    with cols[2]:
        user = st.text_input("邮箱账号", value=str(settings.get("email_user") or ""), key="collector_email_user", placeholder="fapiao@example.com")
    cols = st.columns(3)
    with cols[0]:
        password = st.text_input("IMAP 授权码", value=str(settings.get("email_password") or ""), type="password", key="collector_email_password")
    with cols[1]:
        mailbox = st.text_input("邮箱目录", value=str(settings.get("email_mailbox") or "INBOX"), key="collector_email_mailbox")
    with cols[2]:
        days = st.number_input("拉取最近天数", min_value=1, max_value=365, value=int(settings.get("email_days") or 30), key="collector_email_days")
    auto_match = st.checkbox("导入后自动生成匹配建议", value=bool(settings.get("auto_match", True)), key="collector_email_auto_match")
    st.caption("授权码只保存在本机数据目录（文件权限 600），不上传；建议使用专用收票邮箱。")
    cols = st.columns(3)
    with cols[0]:
        if st.button("💾 保存邮箱设置", key="collector_email_save", width="stretch"):
            collector.save_settings({
                **settings, "email_host": host.strip(), "email_port": int(port),
                "email_user": user.strip(), "email_password": password,
                "email_mailbox": mailbox.strip() or "INBOX", "email_days": int(days), "auto_match": auto_match,
            })
            st.success("已保存到本地。")
    with cols[1]:
        if st.button("🔌 测试连接", key="collector_email_test", width="stretch"):
            try:
                result = collector.test_email({**settings, "email_host": host.strip(), "email_port": int(port),
                                               "email_user": user.strip(), "email_password": password})
                st.success(result.get("message") or "连接成功")
            except Exception as exc:  # noqa: BLE001
                st.error(f"连接失败：{exc}")
    with cols[2]:
        if st.button("📥 立即拉取附件", key="collector_email_fetch", type="primary", width="stretch"):
            try:
                result = collector.collect_email({**settings, "email_host": host.strip(), "email_port": int(port),
                                                  "email_user": user.strip(), "email_password": password,
                                                  "email_mailbox": mailbox.strip() or "INBOX", "email_days": int(days),
                                                  "auto_match": auto_match})
                st.success(
                    f"邮件 {result['messages']} 封，附件 {result['attachments']} 个，导入 {result['imported']} 张，跳过 {result['skipped']} 个。"
                )
                for error in result.get("errors") or []:
                    st.warning(error)
            except Exception as exc:  # noqa: BLE001
                st.error(f"拉取失败：{exc}")


def _render_logs() -> None:
    try:
        rows = collector.recent_logs(100)
    except Exception as exc:  # noqa: BLE001
        st.warning(f"归集记录不可用：{exc}")
        return
    if not rows:
        st.info("还没有归集记录。先扫描文件夹或拉取邮箱附件。")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "时间": row.get("created_at"), "来源": row.get("kind"), "文件": row.get("file_name"),
                    "导入": row.get("imported"), "跳过": row.get("skipped"), "状态": row.get("status"), "说明": row.get("message"),
                }
                for row in rows
            ]
        ),
        width="stretch", hide_index=True,
    )


def render() -> None:
    """渲染发票归集页。"""
    ui.page_header(
        "发票归集",
        "把电子税务局导出的发票，或专用邮箱收到的发票附件，自动解析、去重、入发票池并生成匹配建议。",
        icon="📬",
    )
    st.info(
        "为什么不能直接登录电子税务局自动下载：电子税务局没有面向本地工具的公开 API，自动登录涉及实名/验证码/账号安全。"
        "本页采用两条合规路径：**手工导出到文件夹** + **专用邮箱 IMAP 只读拉取**。",
        icon="🔐",
    )
    tabs = st.tabs(["📁 本地文件夹", "📧 邮箱归集", "📋 归集记录"])
    with tabs[0]:
        _render_folder()
    with tabs[1]:
        _render_email()
    with tabs[2]:
        _render_logs()
    st.caption("导入后请到「📥 票据与流水」核对；高置信匹配可到「⚡ AI 代办」一键入账。")
    theme.disclaimer()
