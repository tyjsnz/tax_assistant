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
    auto_cols = st.columns(3)
    with auto_cols[0]:
        auto_scan = st.checkbox("打开系统时自动扫描文件夹", value=bool(settings.get("auto_scan_on_start")), key="collector_auto_scan")
    with auto_cols[1]:
        auto_email = st.checkbox("自动扫描时同时拉取邮箱", value=bool(settings.get("auto_scan_email")), key="collector_auto_email")
    with auto_cols[2]:
        interval = st.number_input("最短间隔（小时）", min_value=1, max_value=168, value=int(settings.get("auto_scan_interval_hours") or 6), key="collector_auto_interval")
    st.caption(f"上次自动扫描：{settings.get('last_scan_at') or '尚未扫描'}")
    cols = st.columns(2)
    with cols[0]:
        if st.button("💾 保存文件夹设置", key="collector_folder_save", width="stretch"):
            collector.save_settings({
                **settings, "folder_path": folder, "auto_match": auto_match,
                "auto_scan_on_start": auto_scan, "auto_scan_email": auto_email,
                "auto_scan_interval_hours": int(interval),
            })
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


def _render_review() -> None:
    """发票查验辅助：二维码解码 + 官方入口 + 复核/查验状态。"""
    from ingest import invoice_review

    st.caption(
        "扫码/上传二维码只是辅助打开查验入口，不代表系统自动认定真伪；最终以全国增值税发票查验平台结果为准。"
    )
    tab_qr, tab_review = st.tabs(["📷 二维码辅助", "🧾 逐张复核"])
    with tab_qr:
        uploaded = st.file_uploader(
            "上传发票图片/PDF（PNG/JPG/PDF）",
            type=["png", "jpg", "jpeg", "pdf"],
            key="invoice_qr_upload",
        )
        if uploaded is not None:
            result = invoice_review.decode_qr_file(uploaded.getvalue(), uploaded.name)
            if result.get("ok"):
                info = invoice_review.inspect_payload(str(result.get("payload") or ""))
                st.code(info["payload"])
                if info.get("url"):
                    if info.get("is_official"):
                        st.success("识别到官方域名链接，可打开官方查验。")
                    else:
                        st.warning(info.get("warning") or "非官方链接，请勿输入账号信息。")
                    st.link_button("打开二维码链接", info["url"])
                else:
                    st.info(info.get("warning") or "二维码不是网址，请到官方平台手工查验。")
            else:
                for error in result.get("errors") or []:
                    st.warning(error)
        st.markdown("官方查验入口（需登录/验证码）：")
        st.link_button("全国增值税发票查验平台", "https://inv-veri.chinatax.gov.cn")
        st.caption(invoice_review.REVIEW_DISCLAIMER)

    with tab_review:
        try:
            data = invoice_review.summary()
        except Exception as exc:  # noqa: BLE001
            st.warning(f"复核数据不可用：{exc}")
            return
        cols = st.columns(3)
        with cols[0]:
            st.metric("发票总数", f"{data['total']} 张")
        with cols[1]:
            st.metric("待复核", f"{(data.get('review') or {}).get('待复核', 0)} 张")
        with cols[2]:
            st.metric("未查验", f"{(data.get('verify') or {}).get('未查验', 0)} 张")
        try:
            invoices = invoice_review.list_invoices(300)
        except Exception as exc:  # noqa: BLE001
            st.warning(f"发票列表不可用：{exc}")
            return
        if not invoices:
            st.info("发票池为空；请先到「📥 票据与流水」导入或到「📬 发票归集」扫描。")
            return
        labels = {
            f"{row.get('invoice_no') or row.get('id')} · {row.get('seller') or ''} · ¥{row.get('total_inclusive') or 0}（{row.get('review_status') or '待复核'}）": row
            for row in invoices
        }
        selected = st.selectbox("选择发票", list(labels), key="invoice_review_select")
        row = labels[selected]
        st.caption(f"开票日期：{row.get('invoice_date') or '—'} · 来源：{row.get('source_format') or '—'} · 当前查验：{row.get('verify_status') or '未查验'}")
        cols = st.columns(2)
        with cols[0]:
            review_status = st.selectbox(
                "复核状态", invoice_review.REVIEW_STATUSES,
                index=invoice_review.REVIEW_STATUSES.index(str(row.get("review_status") or "待复核"))
                if str(row.get("review_status") or "待复核") in invoice_review.REVIEW_STATUSES else 0,
                key="invoice_review_status",
            )
        with cols[1]:
            verify_status = st.selectbox(
                "查验状态", invoice_review.VERIFY_STATUSES,
                index=invoice_review.VERIFY_STATUSES.index(str(row.get("verify_status") or "未查验"))
                if str(row.get("verify_status") or "未查验") in invoice_review.VERIFY_STATUSES else 0,
                key="invoice_verify_status",
            )
        note = st.text_input("复核备注", value=str(row.get("review_note") or ""), key="invoice_review_note")
        if st.button("💾 保存复核结果", key="invoice_review_save", type="primary", width="stretch"):
            try:
                invoice_review.update_review(
                    str(row.get("id")), review_status=review_status,
                    verify_status=verify_status, review_note=note,
                )
                st.success("已保存。")
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(f"保存失败：{exc}")
        st.caption("建议：大额/异常发票先到官方平台查验，再标记“已查验-正常/异常”；红冲/作废需结合账务处理。")


def _render_dashboard() -> None:
    """发票对账看板：归集/待匹配/重复/疑似红冲作废/跨期。"""
    from ingest import reconciliation

    try:
        data = reconciliation.build_dashboard()
    except Exception as exc:  # noqa: BLE001
        st.warning(f"对账看板不可用：{exc}")
        return
    st.caption(
        "本看板只基于**已归集到系统**的发票统计；“未归集”无法从系统外部感知，"
        "请以电子税务局全量发票查询为完整口径。"
    )
    cols = st.columns(4)
    with cols[0]:
        st.metric("已归集发票", f"{data['total']} 张")
    with cols[1]:
        st.metric(f"{data['current_month']} 新增", f"{data['this_month']} 张")
    with cols[2]:
        st.metric("待匹配", f"{data['pending']} 张")
    with cols[3]:
        st.metric("已匹配/入账", f"{data['matched']} 张")

    sections = [
        ("🔁 重复发票号", data.get("duplicate_numbers"), ["invoice_no", "count"]),
        ("⚠️ 疑似红冲/作废/负数", data.get("suspected_red"), ["invoice_no", "seller", "amount", "note"]),
        ("📅 跨期发票（早于本季度）", data.get("cross_period"), ["invoice_no", "invoice_date", "seller", "amount"]),
        ("❌ 归集失败记录", data.get("failed_logs"), ["created_at", "source", "file_name", "message"]),
    ]
    for title, rows, keys in sections:
        st.markdown(f"**{title}**")
        if not rows:
            st.caption("暂无。")
            continue
        display = []
        for row in rows:
            display.append({
                {"invoice_no": "发票号码", "count": "次数", "seller": "销售方", "amount": "金额", "note": "说明",
                 "invoice_date": "开票日期", "created_at": "时间", "source": "来源", "file_name": "文件", "message": "说明"}[key]: row.get(key)
                for key in keys
            })
        st.dataframe(display, width="stretch", hide_index=True)

    if data.get("skipped_logs"):
        st.caption(f"归集日志中另有 {data['skipped_logs']} 条重复/跳过记录（已自动去重，不会重复入账）。")
    st.info(
        "建议每月核对一次：电子税务局全量发票查询的发票总数，应 ≥ 系统已归集数；"
        "差异可能是尚未导出/未发邮件/归集失败，可在「归集记录」排查。",
        icon="🧾",
    )


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
    tabs = st.tabs(["📁 本地文件夹", "📧 邮箱归集", "🔎 查验复核", "📊 对账看板", "📋 归集记录"])
    with tabs[0]:
        _render_folder()
    with tabs[1]:
        _render_email()
    with tabs[2]:
        _render_review()
    with tabs[3]:
        _render_dashboard()
    with tabs[4]:
        _render_logs()
    st.caption("导入后请到「📥 票据与流水」核对；高置信匹配可到「⚡ AI 代办」一键入账。")
    theme.disclaimer()
