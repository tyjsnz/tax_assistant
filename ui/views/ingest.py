"""📥 票据与流水：电子发票 / 银行流水导入 → 池管理 → 自动匹配 → 确认入账。

页面职责（不实现解析 / 匹配算法，全部调用 ``ingest.*`` 后端）：
1. 一键加载 ``examples/`` 示例票据与流水（``ingest.demo.seed_demo_pool``）；
2. 上传发票（XML/OFD/PDF，多文件）与手动录入，结果入库到发票池；
3. 上传银行流水（CSV/XLSX，支持高级列映射覆盖自动识别）；
4. 自动匹配建议表（确定性评分），勾选后批量确认入账 / 忽略，并展示台账记录与凭证号；
5. 发票池 / 流水池查看、状态筛选、修改状态与备注、删除；
6. 指标卡 + 免责声明；后端模块缺失时优雅降级，绝不白屏。
"""

from __future__ import annotations

from typing import Any, Sequence

import pandas as pd
import streamlit as st

import config
from ui.components import cache, formatting as F, ingest_api as IA, nav, theme, ui

#: 结果缓存键（跨 rerun 展示上一次导入 / 确认结果）
STATE_INVOICE_RESULT = "ingest_invoice_result"
STATE_BANK_RESULT = "ingest_bank_result"
STATE_CONFIRM_RESULTS = "ingest_confirm_results"
STATE_IGNORE_RESULT = "ingest_ignore_result"
FLASH_KEY = "_ingest_flash"

STATUS_OPTIONS = ["全部", *IA.INGEST_STATUSES]
POOL_LABELS = {"invoices": "发票池", "transactions": "银行流水池"}


# =============================================================================
# 提示
# =============================================================================
def _flash(message: str, level: str = "success") -> None:
    st.session_state.setdefault(FLASH_KEY, []).append({"message": message, "level": level})


def _show_flash() -> None:
    for item in st.session_state.pop(FLASH_KEY, []) or []:
        level = item.get("level", "success")
        if level == "warning":
            st.warning(item["message"])
        elif level == "error":
            st.error(item["message"])
        else:
            st.success(item["message"])


# =============================================================================
# 数据读取（统一容错）
# =============================================================================
def _safe(func, default, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        ui.failure("票据/流水数据读取失败，请稍后重试。", exc, show_traceback=False)
        return default


def _overview() -> dict:
    return _safe(IA.overview, {})


def _invoices_by_id() -> dict[str, dict]:
    return {str(row.get("id")): row for row in _safe(IA.invoice_rows, [])}


def _transactions_by_id() -> dict[str, dict]:
    return {str(row.get("id")): row for row in _safe(IA.transaction_rows, [])}


def _pick(row: dict, *keys: str, default: Any = "") -> Any:
    """从多个可能字段名中取第一个非空值（后端摘要字段命名不完全一致）。"""
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return default


# =============================================================================
# 指标卡
# =============================================================================
def _render_metrics(overview: dict) -> None:
    ui.metric_row(
        [
            {
                "label": "待匹配发票",
                "value": f"{int(overview.get('invoices_pending') or 0)} 张",
                "icon": ":material/receipt_long:",
                "help": f"发票池共 {int(overview.get('invoices_total') or 0)} 张",
            },
            {
                "label": "待匹配流水",
                "value": f"{int(overview.get('transactions_pending') or 0)} 笔",
                "icon": ":material/account_balance:",
                "help": f"流水池共 {int(overview.get('transactions_total') or 0)} 笔",
            },
            {
                "label": "待确认建议",
                "value": f"{int(overview.get('suggestions') or 0)} 条",
                "icon": ":material/link:",
                "help": f"已确认 {int(overview.get('confirmed') or 0)} 条 · 已忽略 {int(overview.get('ignored') or 0)} 条",
            },
            {
                "label": "已入账金额",
                "value": F.fmt_money(overview.get("booked_total")),
                "icon": ":material/savings:",
                "help": f"收入 {F.fmt_money(overview.get('matched_income'))} · 支出 {F.fmt_money(overview.get('matched_expense'))}",
            },
            {
                "label": "已入账票据",
                "value": f"{int(overview.get('invoices_booked') or 0)} / {int(overview.get('transactions_booked') or 0)}",
                "icon": ":material/checklist:",
                "help": "发票已入账张数 / 流水已入账笔数",
            },
        ]
    )
    last_import = overview.get("last_import_at") or "—"
    st.caption(f"数据全部保存在本地 · 最近导入时间：{last_import}")


# =============================================================================
# 一键示例 / 清空
# =============================================================================
def _load_demo(reset: bool) -> None:
    try:
        result = IA.seed_demo_pool(reset=reset)
    except Exception as exc:  # noqa: BLE001
        ui.failure("加载示例票据/流水失败。", exc)
        return
    cache.clear()
    _flash(
        "已加载示例："
        f"发票 {int(result.get('invoices') or 0)} 张、"
        f"流水 {int(result.get('transactions') or 0)} 笔、"
        f"匹配建议 {int(result.get('suggestions') or 0)} 条"
    )
    st.rerun()


def _reset_pool() -> None:
    try:
        IA.reset_pool()
    except Exception as exc:  # noqa: BLE001
        ui.failure("清空票据池失败。", exc)
        return
    cache.clear()
    _flash("票据池、流水池与匹配建议已清空。", "warning")
    st.rerun()


def _render_demo_actions() -> None:
    with st.container(border=True):
        st.markdown("**快速开始**")
        cols = st.columns([2, 2, 2, 3])
        with cols[0]:
            if st.button(
                "🧪 一键加载示例票据/流水",
                key="ingest_load_demo",
                type="primary",
                width="stretch",
                disabled=not IA.demo_ready(),
                help="导入 examples/ 下 2 张示例发票 + 1 个银行流水 CSV，并自动生成匹配建议",
            ):
                _load_demo(bool(st.session_state.get("ingest_demo_reset")))
        with cols[1]:
            st.checkbox("加载前清空现有票据池", key="ingest_demo_reset")
            if st.button(
                "🗑️ 清空票据池",
                key="ingest_reset_pool",
                width="stretch",
            ):
                _reset_pool()
        with cols[2]:
            st.caption("示例文件位于项目 `examples/` 目录，均为虚构数据。")
        with cols[3]:
            st.caption(
                "流程：加载示例 / 上传文件 → 自动匹配 → 勾选确认入账 → 到「台账管理」核对收入、费用与凭证。"
            )


# =============================================================================
# 导入结果展示
# =============================================================================
def _render_import_result(result: dict, *, kind: str) -> None:
    """展示最近一次导入的 saved / skipped / errors 与明细。"""
    if not isinstance(result, dict) or not result:
        return
    saved = result.get("saved")
    saved_count = len(saved) if isinstance(saved, (list, tuple)) else int(saved or 0)
    if not saved_count and isinstance(result.get("invoices"), (int, float)) and kind == "invoice":
        saved_count = int(result.get("invoices") or 0)
    if not saved_count and isinstance(result.get("transactions"), (int, float)) and kind == "bank":
        saved_count = int(result.get("transactions") or 0)
    parsed = result.get("parsed")
    parsed_count = len(parsed) if isinstance(parsed, (list, tuple)) else int(parsed or saved_count or 0)
    skipped = result.get("skipped")
    skipped_count = len(skipped) if isinstance(skipped, (list, tuple)) else int(skipped or 0)
    error_items = result.get("errors") or []
    errors_count = len(error_items) if isinstance(error_items, (list, tuple)) else int(error_items or 0)

    with st.container(border=True):
        st.markdown(f"**最近一次{'发票' if kind == 'invoice' else '流水'}导入结果**")
        ui.metric_row(
            [
                {"label": "解析记录", "value": f"{parsed_count}"},
                {"label": "成功入库", "value": f"{saved_count}"},
                {"label": "重复跳过", "value": f"{skipped_count}"},
                {"label": "文件错误", "value": f"{errors_count}"},
            ],
            columns=4,
        )
        by_format = result.get("by_format") or {}
        if by_format:
            st.caption("按来源格式：" + " · ".join(f"{key} {value}" for key, value in by_format.items()))

        if isinstance(saved, (list, tuple)) and saved:
            rows = []
            for record in saved:
                if not isinstance(record, dict):
                    continue
                rows.append(
                    {
                        "发票号码" if kind == "invoice" else "交易流水号":
                            record.get("invoice_no") if kind == "invoice" else record.get("transaction_no"),
                        "日期": record.get("invoice_date") if kind == "invoice" else record.get("transaction_date"),
                        "方向": record.get("direction"),
                        "类型" if kind == "invoice" else "摘要":
                            record.get("invoice_type") if kind == "invoice" else record.get("summary"),
                        "对方": (record.get("seller") or "") if kind == "invoice" else record.get("counterparty"),
                        "金额": F.fmt_money(
                            record.get("total_inclusive") if kind == "invoice" else record.get("amount")
                        ),
                        "状态": record.get("status"),
                    }
                )
            if rows:
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

        if isinstance(skipped, (list, tuple)) and skipped:
            with st.expander(f"跳过的记录（{len(skipped)} 条）", expanded=False):
                for item in skipped[:30]:
                    if isinstance(item, dict):
                        st.caption(f"{item.get('reason') or '重复'}：{item.get('invoice') or item.get('transaction')}")
                    else:
                        st.caption(str(item))

        if error_items:
            with st.expander(f"文件错误 / 提示（{errors_count} 条）", expanded=True):
                for item in error_items[:30] if isinstance(error_items, (list, tuple)) else []:
                    if isinstance(item, dict):
                        st.warning(f"{item.get('file') or '文件'}：{item.get('error') or '解析失败'}")
                    else:
                        st.warning(str(item))


# =============================================================================
# 发票上传 / 手动录入
# =============================================================================
def _import_invoices(uploaded: list[Any]) -> None:
    if not uploaded:
        st.warning("请先选择发票文件。")
        return
    try:
        with st.spinner("正在解析发票文件…"):
            result = IA.import_invoice_files(uploaded, auto_match=False)
    except Exception as exc:  # noqa: BLE001
        ui.failure("发票解析入库失败，请检查文件格式（XML/OFD/PDF）。", exc)
        return
    st.session_state[STATE_INVOICE_RESULT] = result
    cache.clear()
    saved = result.get("saved")
    saved_count = len(saved) if isinstance(saved, (list, tuple)) else int(saved or 0)
    _flash(f"发票解析完成：入库 {saved_count} 张；可点击「🔎 自动匹配」生成匹配建议。")
    st.rerun()


def _manual_invoice_form() -> None:
    """手动录入发票表单。"""
    with st.form("ingest_manual_invoice_form", clear_on_submit=True):
        st.caption(
            "OCR/PDF 无法识别时可手动录入；小规模纳税人进项发票不抵扣，仅作费用凭证。"
            "不含税金额与税额可留空（0），匹配只使用价税合计与票面信息。"
        )
        cols = st.columns(3)
        with cols[0]:
            invoice_no = st.text_input("发票号码", key="ingest_manual_no", placeholder="如 26530100000012345678")
            invoice_date = st.date_input("开票日期", value=config.today(), key="ingest_manual_date")
            invoice_type = st.selectbox("发票类型", IA.INVOICE_KINDS, index=1, key="ingest_manual_type")
        with cols[1]:
            total_inclusive = st.number_input(
                "价税合计（元）", min_value=0.0, step=100.0, format="%.2f", key="ingest_manual_total"
            )
            amount_exclusive = st.number_input(
                "不含税金额（元，可留 0 自动推算）", min_value=0.0, step=100.0, format="%.2f",
                key="ingest_manual_exclusive",
            )
            tax_amount = st.number_input(
                "税额（元，可留 0 自动推算）", min_value=0.0, step=10.0, format="%.2f", key="ingest_manual_tax"
            )
        with cols[2]:
            direction = st.selectbox("发票方向", IA.INVOICE_DIRECTIONS, index=0, key="ingest_manual_direction")
            seller = st.text_input("销售方", key="ingest_manual_seller")
            buyer = st.text_input("购买方", key="ingest_manual_buyer")
        note = st.text_input("备注", key="ingest_manual_note")
        submitted = st.form_submit_button("保存发票到发票池", type="primary", width="stretch")
    if not submitted:
        return
    if not IA.manual_invoice_ready():
        st.warning("后端 ingest 模块尚未就绪，暂时无法保存手动录入的发票。")
        return
    if total_inclusive <= 0 and amount_exclusive <= 0:
        st.error("请填写价税合计或不含税金额（必须大于 0）。")
        return
    payload = {
        "invoice_no": invoice_no.strip(),
        "invoice_date": invoice_date.isoformat(),
        "invoice_type": invoice_type,
        "direction": direction,
        "seller": seller.strip(),
        "buyer": buyer.strip(),
        "total_inclusive": float(total_inclusive),
        "amount_exclusive": float(amount_exclusive),
        "tax_amount": float(tax_amount),
        "note": note.strip(),
        "source_format": "手动",
    }
    try:
        record = IA.add_manual_invoice(payload)
    except Exception as exc:  # noqa: BLE001
        ui.failure("保存发票失败，请检查填写内容。", exc)
        return
    cache.clear()
    _flash(f"已手动录入发票 {record.get('invoice_no') or record.get('id') or ''}，状态「待匹配」。")
    st.rerun()


def _render_invoice_upload() -> None:
    ui.section(
        "🧾 上传发票",
        "支持数电票 XML / OFD / PDF，多文件一次导入；解析后进入发票池，状态为「待匹配」。",
    )
    uploaded = st.file_uploader(
        "上传发票文件（可多选）",
        type=["xml", "ofd", "pdf"],
        accept_multiple_files=True,
        key="ingest_invoice_upload",
        label_visibility="collapsed",
        help="数电票 XML 识别最准确；PDF 需要本地可提取文本，图片型 PDF 建议手动录入。",
    )
    cols = st.columns([2, 2, 4])
    with cols[0]:
        if st.button(
            "📥 解析并入库",
            key="ingest_invoice_import",
            type="primary",
            width="stretch",
            disabled=not uploaded or not IA.invoice_import_ready(),
        ):
            if uploaded:
                _import_invoices(list(uploaded))
    with cols[1]:
        st.caption(f"已选择 {len(uploaded)} 个文件" if uploaded else "尚未选择文件")
    with cols[2]:
        if not IA.invoice_import_ready():
            st.caption("后端 ingest 模块就绪后可解析入库。")

    result = st.session_state.get(STATE_INVOICE_RESULT)
    if result:
        _render_import_result(result, kind="invoice")

    with st.expander("➕ 手动录入发票", expanded=False):
        _manual_invoice_form()


# =============================================================================
# 银行流水上传
# =============================================================================
def _import_bank(uploaded, mapping: dict[str, str] | None, account_no: str) -> None:
    if uploaded is None:
        st.warning("请先选择银行流水文件。")
        return
    try:
        with st.spinner("正在解析银行流水…"):
            result = IA.import_bank_files(
                [(uploaded.name, uploaded.getvalue())],
                mapping=mapping,
                account_no=account_no.strip(),
                auto_match=False,
            )
    except Exception as exc:  # noqa: BLE001
        ui.failure("银行流水导入失败，请检查文件格式或列映射。", exc)
        return
    st.session_state[STATE_BANK_RESULT] = result
    cache.clear()
    saved = result.get("saved")
    saved_count = len(saved) if isinstance(saved, (list, tuple)) else int(saved or 0)
    if not saved_count and isinstance(result.get("transactions"), (int, float)):
        saved_count = int(result.get("transactions") or 0)
    _flash(f"银行流水导入完成：入库 {saved_count} 笔；可点击「🔎 自动匹配」。")
    st.rerun()


def _render_bank_upload() -> None:
    ui.section(
        "🏦 上传银行流水",
        "支持 CSV / XLSX；自动识别表头与列（日期 / 金额 / 收入 / 支出 / 对方 / 摘要）。"
        "识别不准时可在「高级列映射」中手动指定。",
    )
    uploaded = st.file_uploader(
        "上传银行流水文件（CSV / XLSX）",
        type=["csv", "xlsx", "xls"],
        key="ingest_bank_upload",
        label_visibility="collapsed",
    )
    account_no = st.text_input(
        "银行账号（可选）",
        key="ingest_bank_account",
        placeholder="用于标记流水归属，如 6222 **** 1234",
    )

    mapping: dict[str, str] | None = None
    if uploaded is not None:
        preview = _safe(IA.bank_preview, {"ok": False, "headers": [], "columns": {}, "errors": ["预览失败"]}, uploaded.getvalue(), uploaded.name) or {}
        headers = preview.get("headers") or []
        detected = preview.get("columns") or {}
        for message in preview.get("errors") or []:
            st.warning(message)
        if headers:
            st.caption("识别到表头：" + " ｜ ".join(str(header) for header in headers[:12]))
            detected_frame = pd.DataFrame(
                [
                    {
                        "标准列": IA.BANK_COLUMN_LABELS.get(key, key),
                        "自动识别的原始列": detected.get(key) or "（未识别）",
                    }
                    for key in IA.BANK_COLUMN_LABELS
                ]
            )
            with st.expander("🔧 高级列映射", expanded=not detected.get("date")):
                use_custom = st.toggle(
                    "使用自定义列映射覆盖自动识别",
                    value=False,
                    key="ingest_bank_custom",
                    help="一般无需开启；当自动识别错误或为空时，逐列选择原始表头。",
                )
                if use_custom:
                    selected: dict[str, str] = {}
                    header_options = ["（不映射）", *headers]
                    map_cols = st.columns(3)
                    for index, (key, label) in enumerate(IA.BANK_COLUMN_LABELS.items()):
                        with map_cols[index % 3]:
                            default = detected.get(key) or "（不映射）"
                            default_index = header_options.index(default) if default in header_options else 0
                            chosen = st.selectbox(
                                f"{label}（{key}）",
                                header_options,
                                index=default_index,
                                key=f"ingest_map_{key}",
                            )
                            if chosen != "（不映射）":
                                selected[key] = chosen
                    mapping = selected
                else:
                    st.dataframe(detected_frame, hide_index=True, width="stretch")

    cols = st.columns([2, 2, 5])
    with cols[0]:
        if st.button(
            "📥 导入流水",
            key="ingest_bank_import",
            type="primary",
            width="stretch",
            disabled=uploaded is None or not IA.bank_import_ready(),
        ):
            _import_bank(uploaded, mapping, account_no)
    with cols[1]:
        st.caption("导入后进入流水池「待匹配」。")

    result = st.session_state.get(STATE_BANK_RESULT)
    if result:
        _render_import_result(result, kind="bank")


# =============================================================================
# 自动匹配
# =============================================================================
def _match_view(match: dict, invoice_map: dict[str, dict], transaction_map: dict[str, dict]) -> dict:
    """把匹配记录整理成建议表一行（兼容后端摘要字段命名差异）。"""
    invoice = invoice_map.get(str(match.get("invoice_id"))) or {}
    transaction = transaction_map.get(str(match.get("transaction_id"))) or {}
    direction = _pick(invoice, "direction") or _pick(match, "invoice_direction", "direction")
    if direction == "销项":
        invoice_counterparty = _pick(invoice, "buyer") or _pick(match, "buyer", "seller")
    else:
        invoice_counterparty = _pick(invoice, "seller") or _pick(match, "seller", "buyer")
    return {
        "匹配编号": str(match.get("id") or ""),
        "匹配分": round(float(match.get("score") or 0), 1),
        "方向": direction or "—",
        "发票号码": _pick(invoice, "invoice_no") or _pick(match, "invoice_no"),
        "发票对方": invoice_counterparty or "—",
        "发票日期": _pick(invoice, "invoice_date") or _pick(match, "invoice_date"),
        "发票类型": _pick(invoice, "invoice_type") or _pick(match, "invoice_type"),
        "发票价税合计": F.to_float(_pick(invoice, "total_inclusive") or _pick(match, "total_inclusive", "invoice_total")),
        "流水日期": _pick(transaction, "transaction_date") or _pick(match, "transaction_date", "date"),
        "流水对方": _pick(transaction, "counterparty") or _pick(match, "counterparty"),
        "流水金额": F.to_float(_pick(transaction, "amount") or _pick(match, "transaction_amount", "amount")),
        "金额差": F.to_float(match.get("amount_diff")),
        "日期差(天)": int(match.get("date_diff") or 0),
        "摘要": _pick(transaction, "summary") or _pick(match, "summary"),
    }


def _run_suggest_matches(date_window: int, min_score: float) -> None:
    try:
        with st.spinner("正在计算匹配建议…"):
            result = IA.suggest_matches(date_window_days=int(date_window), min_score=float(min_score))
    except Exception as exc:  # noqa: BLE001
        ui.failure("自动匹配失败，请确认已导入发票与流水。", exc)
        return
    cache.clear()
    created = result.get("created")
    created_count = len(created) if isinstance(created, (list, tuple)) else int(result.get("count") or 0)
    _flash(f"自动匹配完成：生成 {created_count} 条建议（候选 {int(result.get('candidates') or 0)} 对）。")
    st.rerun()


def _confirm_selected(match_ids: Sequence[str]) -> None:
    outcomes: list[dict] = []
    for match_id in match_ids:
        try:
            outcome = IA.confirm_match(str(match_id))
        except Exception as exc:  # noqa: BLE001
            outcomes.append({"ok": False, "match_id": match_id, "error": str(exc)})
            continue
        outcomes.append({"ok": bool(outcome.get("ok", True)), **outcome, "match_id": match_id})
    st.session_state[STATE_CONFIRM_RESULTS] = outcomes
    cache.clear()
    success = sum(1 for outcome in outcomes if outcome.get("ok"))
    failed = len(outcomes) - success
    if failed:
        _flash(f"确认入账完成：成功 {success} 条，失败 {failed} 条（详见结果表）。", "warning")
    else:
        _flash(f"确认入账完成：{success} 条匹配已生成台账与凭证，请到「台账管理」核对。")
    st.rerun()


def _ignore_selected(match_ids: Sequence[str]) -> None:
    ignored = 0
    errors: list[str] = []
    for match_id in match_ids:
        try:
            IA.ignore_match(str(match_id))
            ignored += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{match_id}: {exc}")
    st.session_state[STATE_IGNORE_RESULT] = {"ignored": ignored, "errors": errors}
    cache.clear()
    if errors:
        _flash(f"已忽略 {ignored} 条，失败 {len(errors)} 条。", "warning")
    else:
        _flash(f"已忽略 {ignored} 条匹配建议（不修改发票/流水状态与台账）。")
    st.rerun()


def _render_confirm_results() -> None:
    results = st.session_state.get(STATE_CONFIRM_RESULTS)
    if not results:
        return
    with st.container(border=True):
        st.markdown("**最近一次确认入账结果**")
        rows = []
        for outcome in results:
            ledger_info = outcome.get("ledger") or {}
            record = ledger_info.get("record") or {}
            journal = outcome.get("journal") or {}
            kind = ledger_info.get("kind") or ""
            rows.append(
                {
                    "结果": "✅ 成功" if outcome.get("ok") else "❌ 失败",
                    "台账类型": "收入" if kind == "income" else ("费用" if kind == "expense" else "—"),
                    "台账编号": record.get("id") or "—",
                    "凭证号": journal.get("entry_no") or "—",
                    "匹配编号": outcome.get("match_id") or "",
                    "提示": "；".join(outcome.get("warnings") or []) or outcome.get("error") or "",
                }
            )
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        success = sum(1 for outcome in results if outcome.get("ok"))
        st.caption(f"成功 {success} 条 · 失败 {len(results) - success} 条 · 台账与凭证可在「台账管理」页查看。")
        cols = st.columns([1, 1, 3])
        with cols[0]:
            if st.button("📒 去台账管理查看", key="ingest_goto_ledger", width="stretch"):
                nav.goto(nav.PAGE_LEDGER)
        with cols[1]:
            if st.button("清除结果", key="ingest_clear_confirm", width="stretch"):
                st.session_state.pop(STATE_CONFIRM_RESULTS, None)
                st.rerun()

    ignore_result = st.session_state.get(STATE_IGNORE_RESULT)
    if ignore_result:
        st.caption(
            f"最近忽略：{int(ignore_result.get('ignored') or 0)} 条"
            + (f"（失败 {len(ignore_result.get('errors') or [])} 条）" if ignore_result.get("errors") else "")
        )


def _render_matching() -> None:
    ui.section(
        "🔗 自动匹配",
        "按「同方向 + 金额 + 日期 + 对方名称」确定性评分（0-100），贪心一一分配；"
        "确认后自动生成收入/费用台账与记账凭证。",
    )
    control = st.columns([2, 2, 2, 4])
    with control[0]:
        date_window = st.number_input(
            "日期窗口（天）", min_value=0, max_value=90, value=IA.DEFAULT_DATE_WINDOW, step=1,
            key="ingest_match_days",
        )
    with control[1]:
        min_score = st.number_input(
            "最低匹配分", min_value=0.0, max_value=100.0, value=IA.DEFAULT_MIN_SCORE, step=5.0,
            key="ingest_match_min",
        )
    with control[2]:
        if st.button(
            "🔎 自动匹配",
            key="ingest_run_match",
            type="primary",
            width="stretch",
            disabled=not IA.match_ready(),
        ):
            _run_suggest_matches(date_window, min_score)
    with control[3]:
        st.caption("已存在建议/已确认/已忽略的发票-流水组合不会被重复生成。")

    try:
        suggestions = IA.list_matches(status="建议")
    except Exception as exc:  # noqa: BLE001
        ui.failure("读取匹配建议失败。", exc, show_traceback=False)
        suggestions = []

    ui.metric_row(
        [
            {"label": "待确认建议", "value": f"{len(suggestions)} 条"},
            {"label": "已确认", "value": f"{len(_safe(IA.list_matches, [], status='已确认') or [])} 条"},
            {"label": "已忽略", "value": f"{len(_safe(IA.list_matches, [], status='已忽略') or [])} 条"},
        ],
        columns=3,
    )

    if not suggestions:
        st.info(
            "暂无待确认的匹配建议。请先导入票据与流水（或点击上方「一键加载示例」），再点击「🔎 自动匹配」。",
            icon="🔗",
        )
        _render_confirm_results()
        return

    invoice_map = _invoices_by_id()
    transaction_map = _transactions_by_id()
    views = [_match_view(match, invoice_map, transaction_map) for match in suggestions]
    frame = pd.DataFrame(views)
    frame.insert(0, "确认", False)
    column_config: dict[str, Any] = {
        "确认": st.column_config.CheckboxColumn("确认", help="勾选后点击下方「确认勾选并入账」", width="small"),
        "匹配编号": st.column_config.TextColumn("匹配编号", width="small"),
        "匹配分": st.column_config.NumberColumn("匹配分", format="%.1f", width="small"),
        "方向": st.column_config.TextColumn("方向", width="small"),
        "发票价税合计": st.column_config.NumberColumn("发票价税合计", format="¥%.2f"),
        "流水金额": st.column_config.NumberColumn("流水金额", format="¥%.2f"),
        "金额差": st.column_config.NumberColumn("金额差", format="¥%.2f", width="small"),
        "日期差(天)": st.column_config.NumberColumn("日期差(天)", format="%.0f", width="small"),
    }
    disabled = [column for column in frame.columns if column != "确认"]
    edited = st.data_editor(
        frame,
        column_config=column_config,
        disabled=disabled,
        hide_index=True,
        num_rows="fixed",
        key="ingest_suggestion_editor",
        width="stretch",
        height=min(440, 90 + 36 * max(len(frame), 1)),
    )
    selected_ids = [
        str(row.get("匹配编号"))
        for _, row in edited.iterrows()
        if bool(row.get("确认")) and str(row.get("匹配编号") or "")
    ]
    action_cols = st.columns([2, 2, 2, 4])
    with action_cols[0]:
        if st.button(
            "✅ 确认勾选并入账",
            key="ingest_confirm_selected",
            type="primary",
            width="stretch",
            disabled=not selected_ids or not IA.match_ready(),
            help="逐条确认匹配：生成收入/费用台账与凭证，并把发票/流水状态更新为「已入账」",
        ):
            _confirm_selected(selected_ids)
    with action_cols[1]:
        if st.button(
            "🚫 忽略勾选",
            key="ingest_ignore_selected",
            width="stretch",
            disabled=not selected_ids or not IA.match_ready(),
        ):
            _ignore_selected(selected_ids)
    with action_cols[2]:
        st.caption(f"已勾选 {len(selected_ids)} 条")
    with action_cols[3]:
        st.caption("金额差 > 0.01 元时按发票金额入账并给出提示；重复确认会被拒绝（幂等保护）。")

    bulk_cols = st.columns([2, 2, 2, 4])
    with bulk_cols[0]:
        if st.button(
            f"✅ 确认全部建议（{len(suggestions)}）",
            key="ingest_confirm_all",
            width="stretch",
            disabled=not IA.match_ready(),
            help="一次处理当前建议列表中的全部匹配（逐条生成台账与凭证）",
        ):
            _confirm_selected([str(match.get("id")) for match in suggestions if match.get("id")])
    with bulk_cols[1]:
        if st.button(
            f"🚫 忽略全部（{len(suggestions)}）",
            key="ingest_ignore_all",
            width="stretch",
            disabled=not IA.match_ready(),
        ):
            _ignore_selected([str(match.get("id")) for match in suggestions if match.get("id")])
    with bulk_cols[2]:
        st.caption("批量操作不可撤销：确认后会生成台账与凭证（可在台账页删除）。")

    _render_confirm_results()


# =============================================================================
# 票据池 / 流水池
# =============================================================================
INVOICE_POOL_COLUMNS: list[tuple[str, str, str]] = [
    ("invoice_date", "开票日期", "text"),
    ("invoice_type", "类型", "text"),
    ("direction", "方向", "text"),
    ("seller", "销售方", "text"),
    ("buyer", "购买方", "text"),
    ("amount_exclusive", "不含税金额", "money"),
    ("tax_amount", "税额", "money"),
    ("total_inclusive", "价税合计", "money"),
    ("source_file", "来源文件", "text"),
]

TRANSACTION_POOL_COLUMNS: list[tuple[str, str, str]] = [
    ("transaction_date", "交易日期", "text"),
    ("direction", "收支方向", "text"),
    ("amount", "金额", "money"),
    ("counterparty", "对方户名", "text"),
    ("summary", "摘要", "text"),
    ("transaction_no", "交易流水号", "text"),
    ("account_no", "账号", "text"),
    ("source_file", "来源文件", "text"),
]


def _pool_frame(rows: Sequence[dict], columns: Sequence[tuple[str, str, str]]) -> pd.DataFrame:
    """池记录 → 可编辑数据框（中文表头，金额为数值列）。"""
    data: dict[str, list[Any]] = {
        "选中": pd.Series([False] * len(rows), dtype="bool"),
        "编号": pd.Series([str(row.get("id") or "") for row in rows], dtype="object"),
    }
    for field, label, kind in columns:
        values = []
        for row in rows:
            value = row.get(field)
            if kind == "money":
                values.append(F.to_float(value))
            else:
                values.append("" if value in (None, "") else str(value))
        dtype = "float64" if kind == "money" else "object"
        data[label] = pd.Series(values, dtype=dtype)
    data["状态"] = pd.Series([str(row.get("status") or "待匹配") for row in rows], dtype="object")
    data["备注"] = pd.Series([str(row.get("note") or "") for row in rows], dtype="object")
    return pd.DataFrame(data)


def _pool_column_config(columns: Sequence[tuple[str, str, str]]) -> dict[str, Any]:
    config_map: dict[str, Any] = {
        "选中": st.column_config.CheckboxColumn("选中", help="勾选后点击「删除选中」", width="small"),
        "编号": st.column_config.TextColumn("编号", width="small"),
        "状态": st.column_config.SelectboxColumn("状态", options=list(IA.INGEST_STATUSES), width="small"),
        "备注": st.column_config.TextColumn("备注"),
    }
    for _, label, kind in columns:
        if kind == "money":
            config_map[label] = st.column_config.NumberColumn(label, format="¥%.2f")
        else:
            config_map[label] = st.column_config.TextColumn(label)
    return config_map


def _save_pool_edits(kind: str, rows: Sequence[dict], edited: pd.DataFrame) -> None:
    originals = {str(row.get("id")): row for row in rows}
    updated = 0
    errors: list[str] = []
    for _, record in edited.iterrows():
        row_id = str(record.get("编号") or "").strip()
        if not row_id or row_id not in originals:
            continue
        original = originals[row_id]
        patch: dict[str, Any] = {}
        status = str(record.get("状态") or "").strip()
        if status and status != str(original.get("status") or "") and status in IA.INGEST_STATUSES:
            patch["status"] = status
        note = "" if pd.isna(record.get("备注")) else str(record.get("备注")).strip()
        if note != str(original.get("note") or ""):
            patch["note"] = note
        if not patch:
            continue
        try:
            IA.update_pool_row(kind, row_id, patch)
            updated += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{row_id}: {exc}")
    for message in errors:
        st.warning(message)
    if updated:
        cache.clear()
        _flash(f"已更新 {updated} 条{POOL_LABELS.get(kind, '')}记录。")
        st.rerun()
    elif not errors:
        st.info("没有检测到状态/备注修改。")


def _delete_selected(kind: str, edited: pd.DataFrame) -> None:
    selected = [
        str(record.get("编号") or "")
        for _, record in edited.iterrows()
        if bool(record.get("选中")) and str(record.get("编号") or "")
    ]
    if not selected:
        st.warning("请先在表格中勾选「选中」要删除的记录。")
        return
    result = _safe(IA.delete_pool_rows, {"deleted": 0, "errors": []}, kind, selected) or {}
    for message in result.get("errors") or []:
        st.warning(message)
    deleted = int(result.get("deleted") or 0)
    if deleted:
        cache.clear()
        _flash(f"已删除 {deleted} 条{POOL_LABELS.get(kind, '')}记录。", "warning")
        st.rerun()
    else:
        st.error("没有删除任何记录。")


def _render_pool(kind: str) -> None:
    """渲染一个池（invoices / transactions）。"""
    label = POOL_LABELS.get(kind, kind)
    status = st.selectbox(
        "状态筛选",
        STATUS_OPTIONS,
        index=0,
        key=f"ingest_{kind}_status",
    )
    try:
        rows = IA.invoice_rows(status) if kind == "invoices" else IA.transaction_rows(status)
    except Exception as exc:  # noqa: BLE001
        ui.failure(f"读取{label}失败。", exc, show_traceback=False)
        rows = []
    all_rows = _safe(IA.invoice_rows, []) if kind == "invoices" else _safe(IA.transaction_rows, [])
    by_status = {
        item: sum(1 for row in all_rows if str(row.get("status")) == item) for item in IA.INGEST_STATUSES
    }
    st.caption(
        f"共 {len(all_rows)} 条 · "
        + " · ".join(f"{item} {count}" for item, count in by_status.items() if count)
        + (f" · 当前筛选：{status} {len(rows)} 条" if status != "全部" else "")
    )
    if not rows:
        st.info(
            f"{label}暂无{'符合筛选条件的' if status != '全部' else ''}记录。"
            "可上传文件或点击上方「一键加载示例票据/流水」。",
            icon="🗂️",
        )
        return

    columns = INVOICE_POOL_COLUMNS if kind == "invoices" else TRANSACTION_POOL_COLUMNS
    frame = _pool_frame(rows, columns)
    edited = st.data_editor(
        frame,
        column_config=_pool_column_config(columns),
        disabled=[column for column in frame.columns if column not in {"选中", "状态", "备注"}],
        hide_index=True,
        num_rows="fixed",
        key=f"ingest_{kind}_editor",
        width="stretch",
        height=min(460, 90 + 36 * max(len(frame), 1)),
    )
    action_cols = st.columns([2, 2, 5])
    with action_cols[0]:
        if st.button("💾 保存状态/备注", key=f"ingest_{kind}_save", type="primary", width="stretch"):
            _save_pool_edits(kind, rows, edited)
    with action_cols[1]:
        if st.button("🗑️ 删除选中", key=f"ingest_{kind}_delete", width="stretch"):
            _delete_selected(kind, edited)
    with action_cols[2]:
        st.caption("可直接修改「状态」与「备注」；「选中」勾选后删除，删除不可恢复。")


def _render_pools() -> None:
    ui.section(
        "🗂️ 票据池 / 流水池",
        "查看已导入的发票与流水，按状态筛选、修改状态/备注或删除误导入记录。",
    )
    invoice_tab, transaction_tab = st.tabs(["🧾 发票池", "🏦 银行流水池"])
    with invoice_tab:
        _render_pool("invoices")
    with transaction_tab:
        _render_pool("transactions")


# =============================================================================
# 页面入口
# =============================================================================
def render() -> None:
    """渲染「📥 票据与流水」页面。"""
    ui.page_header(
        "票据与流水",
        "电子发票 + 银行流水 → 结构化票据池 → 自动匹配 → 一键生成台账与凭证。",
        icon="📥",
        right_text=f"今天：{config.today().isoformat()}",
    )
    _show_flash()

    backend_ok = IA.available()
    if not backend_ok:
        ui.module_missing("票据与流水导入", "ingest", detail=IA.missing_detail())

    overview = _overview()
    _render_metrics(overview)
    _render_demo_actions()
    _render_invoice_upload()
    _render_bank_upload()
    _render_matching()
    _render_pools()
    theme.disclaimer()
