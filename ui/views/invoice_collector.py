"""📬 发票归集：本地文件夹 + 专用邮箱 IMAP（v1.25）。"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ingest import collector, invoice_reconcile, invoice_tasks, matcher
from ui.components import downloads
from ui.components import nav, theme, ui


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


def _render_ocr() -> None:
    """DeepSeek OCR：识别图片/扫描 PDF 发票，预览后入池。"""
    from integrations import deepseek_tools, invoice_ocr

    st.caption(
        "用 DeepSeek OCR 识别拍照/扫描发票的号码、日期、金额、购销方；只做预览，"
        "确认后才入发票池，不直接入账、不替代查验。"
    )
    if not deepseek_tools.is_configured():
        st.warning("未配置 DeepSeek API Key，OCR 不可用；请先到「设置 → API 与模型」填写。")
        if st.button("⚙️ 去设置", key="collector_ocr_goto_settings"):
            from ui.components import nav

            nav.goto(nav.PAGE_SETTINGS)
        return
    st.info(
        "隐私提示：上传的发票图片/PDF 会发送到 DeepSeek OCR 服务；请确认你同意后再使用。"
        "处理完成后系统不保留上传原文件（仅保留发票字段与 OCR 文本摘要）。",
        icon="🔐",
    )
    consent = st.checkbox("我同意将本次发票文件发送到 DeepSeek OCR", key="collector_ocr_consent")
    uploaded = st.file_uploader(
        "选择发票图片/PDF（PNG/JPG/PDF，≤100MB）",
        type=["png", "jpg", "jpeg", "pdf"],
        key="collector_ocr_upload",
    )
    if uploaded is not None and st.button("📄 OCR 识别", key="collector_ocr_run", type="primary", width="stretch", disabled=not consent):
        try:
            with st.spinner("正在调用 DeepSeek OCR…"):
                result = invoice_ocr.recognize_invoice(uploaded.getvalue(), uploaded.name)
            st.session_state["_ocr_result"] = result
        except Exception as exc:  # noqa: BLE001
            st.error(f"OCR 识别失败：{exc}")
    result = st.session_state.get("_ocr_result")
    if not result:
        st.caption("先勾选同意，再选择文件识别。")
        return
    if not result.get("ok"):
        for error in result.get("errors") or []:
            st.warning(error)
        with st.expander("OCR 原始文本", expanded=False):
            st.text_area("OCR 文本", value=str(result.get("text") or "")[:12000], height=220, key="ocr_raw_text")
        return
    invoice = result.get("invoice") or {}
    st.success("OCR 识别完成，请核对以下字段后入池。")
    st.dataframe(
        [{
            "发票号码": invoice.get("invoice_no"),
            "开票日期": invoice.get("invoice_date"),
            "发票类型": invoice.get("invoice_type"),
            "方向": invoice.get("direction"),
            "销售方": invoice.get("seller"),
            "购买方": invoice.get("buyer"),
            "不含税金额": invoice.get("amount_exclusive"),
            "税额": invoice.get("tax_amount"),
            "价税合计": invoice.get("total_inclusive"),
        }],
        width="stretch", hide_index=True,
    )
    for warning in result.get("warnings") or []:
        st.warning(warning)
    with st.expander("OCR 原始文本", expanded=False):
        st.text_area("OCR 文本", value=str(result.get("text") or "")[:12000], height=220, key="ocr_raw_text_2")
    if st.button("✅ 确认入池", key="collector_ocr_save", type="primary", width="stretch"):
        try:
            record = invoice_ocr.save_invoice(invoice, uploaded.name if uploaded else "OCR")
            st.success(f"已入发票池：{record.get('invoice_no') or record.get('id')}")
            st.session_state.pop("_ocr_result", None)
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(f"入池失败：{exc}")


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


def _render_reconcile() -> None:
    """发票查重与入账核对。"""
    st.caption(
        "回答两个问题：这张发票之前是不是已经做账了？补录/导入时是不是重复了？"
        "系统按发票号码、发票池状态和收入/费用台账交叉核对，不访问电子税务局。"
    )
    try:
        result = invoice_reconcile.reconcile()
    except Exception as exc:  # noqa: BLE001
        st.error(f"核对失败：{exc}")
        return
    summary = result.get("summary") or {}
    ui.metric_row(
        [
            {"label": "发票总数", "value": f"{summary.get('total', 0)}"},
            {"label": "已入账", "value": f"{summary.get('posted', 0)}"},
            {"label": "未入账", "value": f"{summary.get('unposted', 0)}"},
            {"label": "重复 / 冲突", "value": f"{summary.get('duplicate', 0)}"},
        ],
        columns=4,
    )
    if not result.get("rows"):
        st.info("发票池为空。先到「本地文件夹」/「邮箱归集」导入发票，或手工补录后再核对。")
    else:
        frame = pd.DataFrame([
            {
                "判断": row.get("verdict"), "发票号码": row.get("invoice_no") or row.get("identity"),
                "开票日期": row.get("invoice_date"), "方向": row.get("direction"),
                "销售方": row.get("seller"), "价税合计": row.get("total_inclusive"),
                "台账匹配": row.get("ledger_text"), "重复张数": row.get("duplicate_count"),
                "备注": row.get("note"),
            }
            for row in result["rows"]
        ])
        st.dataframe(
            frame, hide_index=True, alt="发票池查重与入账核对结果",
            column_config={"价税合计": st.column_config.NumberColumn("价税合计", format="¥%.2f")},
        )
        downloads.download_or_save(
            "⬇️ 下载核对结果 CSV",
            data=frame.to_csv(index=False).encode("utf-8-sig"),
            file_name="发票查重核对.csv",
            mime="text/csv",
            key="collector_reconcile_download",
            width="stretch",
        )

    tasks = invoice_reconcile.build_tasks(result)
    with st.container(border=True):
        st.markdown("**🧾 待生成凭证任务**")
        st.caption("把未入账/重复/冲突发票自动转成待办；已入账发票不会出现在这里。")
        ignored = invoice_tasks.ignored_keys()
        active_tasks = [task for task in tasks if str(task.get("identity")) not in ignored]
        ignored_tasks = [task for task in tasks if str(task.get("identity")) in ignored]
        cols = st.columns(4)
        cols[0].metric("待处理任务", len(active_tasks))
        cols[1].metric("未入账", sum(1 for task in active_tasks if task.get("verdict") == "未入账"))
        cols[2].metric("重复/冲突", sum(1 for task in active_tasks if task.get("verdict") in {"重复录入", "信息冲突"}))
        cols[3].metric("已忽略", len(ignored_tasks))
        if active_tasks:
            task_frame = pd.DataFrame([
                {
                    "发票号码": task.get("invoice_no"), "开票日期": task.get("invoice_date"),
                    "方向": task.get("direction"), "销售方": task.get("seller"),
                    "价税合计": task.get("total_inclusive"), "判断": task.get("verdict"),
                    "处理建议": task.get("action"),
                }
                for task in active_tasks
            ])
            st.dataframe(
                task_frame, hide_index=True, alt="待生成凭证的发票任务清单",
                column_config={"价税合计": st.column_config.NumberColumn("价税合计", format="¥%.2f")},
            )
            downloads.download_or_save(
                "⬇️ 下载待办任务 CSV",
                data=invoice_reconcile.export_tasks_csv(active_tasks),
                file_name="发票待入账任务.csv",
                mime="text/csv",
                key="collector_tasks_download",
                width="stretch",
            )
            labels = {
                f"{task.get('invoice_no')} · {task.get('verdict')} · ¥{float(task.get('total_inclusive') or 0):,.2f}": task
                for task in active_tasks
            }
            selected_label = st.selectbox("选择一条任务", list(labels), key="collector_task_select")
            selected_task = labels[selected_label]
            action_cols = st.columns(3)
            with action_cols[0]:
                if st.button(
                    "✅ 直接生成凭证", key="collector_task_post", type="primary", width="stretch",
                    disabled=selected_task.get("verdict") != "未入账",
                    help="把这张未入账发票直接生成收入/费用台账与凭证；重复/冲突任务请先处理。",
                ):
                    try:
                        result = matcher.post_invoice_direct(str(selected_task.get("invoice_id")))
                        st.success(f"已生成 {result['ledger']['kind']}台账 {result['ledger']['record'].get('id')}，发票号码 {selected_task.get('invoice_no')}。")
                        st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"生成失败：{exc}")
            with action_cols[1]:
                if st.button(
                    "🔗 自动匹配银行流水", key="collector_task_match", width="stretch",
                    help="用现有发票与银行流水按方向/金额/日期/对方名称自动评分，生成匹配建议。",
                ):
                    try:
                        result = matcher.suggest_matches()
                        st.success(f"已生成 {result.get('count', 0)} 条匹配建议。可到「票据与流水」确认入账。")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"匹配失败：{exc}")
            with action_cols[2]:
                if st.button("🚫 忽略任务", key="collector_task_ignore", width="stretch"):
                    invoice_tasks.ignore(str(selected_task.get("identity")), note=str(selected_task.get("verdict")))
                    st.toast("已忽略，可在下方恢复")
                    st.rerun()
        elif tasks:
            st.info("当前任务都已忽略。可在下方「已忽略任务」恢复。")
        else:
            st.success("没有待处理的发票：当前发票都已入账，或还没有导入发票。")

        if ignored_tasks:
            with st.expander(f"已忽略任务（{len(ignored_tasks)}）", expanded=False):
                for task in ignored_tasks:
                    cols = st.columns([5, 2])
                    with cols[0]:
                        st.caption(f"{task.get('invoice_no')} · {task.get('verdict')} · ¥{float(task.get('total_inclusive') or 0):,.2f}")
                    with cols[1]:
                        if st.button("恢复", key=f"collector_task_restore_{task.get('identity')}", width="stretch"):
                            invoice_tasks.restore(str(task.get("identity")))
                            st.rerun()

    with st.container(border=True):
        st.markdown("**查一张发票**")
        keyword = st.text_input("输入发票号码、发票代码或销售方", key="collector_reconcile_keyword", placeholder="例如：26312000000123456789")
        if st.button("查重并核对台账", key="collector_reconcile_check", type="primary", width="stretch"):
            try:
                found = invoice_reconcile.check_one(keyword)
                st.session_state["_collector_reconcile_found"] = found
            except Exception as exc:  # noqa: BLE001
                st.error(f"查询失败：{exc}")
        found = st.session_state.get("_collector_reconcile_found")
        if isinstance(found, dict):
            if not found.get("rows"):
                st.warning("发票池里没有找到这张发票。可能是还没导入，或号码输入有误。")
            for row in found["rows"]:
                status = "✅ 已入账" if row.get("posted") else "⚠️ 未入账"
                st.markdown(f"### {status} · {row.get('invoice_no') or '无号码'}")
                st.caption(
                    f"开票日期：{row.get('invoice_date') or '—'} · 方向：{row.get('direction') or '—'} · "
                    f"销售方：{row.get('seller') or '—'} · 价税合计：¥{float(row.get('total_inclusive') or 0):,.2f} · "
                    f"池状态：{row.get('status') or '—'}"
                )
                if row.get("ledger_matches"):
                    st.success("已在台账中找到对应记录：" + "、".join(
                        f"{m['kind']} {m['id']}（{m['date']}，¥{float(m['amount'] or 0):,.2f}）"
                        for m in row["ledger_matches"]
                    ))
                else:
                    st.warning("台账中没有按发票号码匹配到收入/费用记录；如确认未入账，请到「票据与流水」生成入账凭证。")

    with st.expander("判定规则说明", expanded=False):
        for rule in invoice_reconcile.explain_rules():
            st.markdown(f"- {rule}")
    if st.button("去「票据与流水」处理未入账发票", key="collector_reconcile_goto", width="stretch"):
        nav.goto(nav.PAGE_INGEST)


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
    tabs = st.tabs(["📁 本地文件夹", "📧 邮箱归集", "📄 发票 OCR", "🔎 查验复核", "🔍 查重核对", "📊 对账看板", "📋 归集记录"])
    with tabs[0]:
        _render_folder()
    with tabs[1]:
        _render_email()
    with tabs[2]:
        _render_ocr()
    with tabs[3]:
        _render_review()
    with tabs[4]:
        _render_reconcile()
    with tabs[5]:
        _render_dashboard()
    with tabs[6]:
        _render_logs()
    st.caption("导入后请到「📥 票据与流水」核对；高置信匹配可到「⚡ AI 代办」一键入账。")
    theme.disclaimer()
