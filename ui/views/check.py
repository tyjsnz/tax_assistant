"""🔍 申报核对：上传增值税申报表截图 / 粘贴文本 → OCR → 栏次映射 → 差异报告。

说明：DeepSeek 官方 API **没有视觉能力**，截图识别必须走本地 OCR（macOS Vision / Tesseract）；
若本机 OCR 不可用，可把申报表栏次文字粘贴到文本框，同样能完成核对。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

import config
from engine import current_quarter, quarter_options
from ui.components import cache, formatting as F, runtime, theme, ui

STATE_FIELDS = "ocr_fields"
STATE_LABELS = "ocr_field_labels"
STATE_REPORT = "ocr_report"


# =============================================================================
# 文本状态管理
# =============================================================================
def _apply_pending_text() -> None:
    """把 OCR / 粘贴得到的文本写入可编辑文本框（必须在 text_area 实例化之前调用）。"""
    pending = st.session_state.pop("_ocr_pending_text", None)
    if pending is not None:
        st.session_state["ocr_text_edit"] = pending


def _current_text() -> str:
    return str(st.session_state.get("ocr_text_edit") or st.session_state.get("ocr_text") or "")


def _set_text(text: str) -> None:
    st.session_state["ocr_text"] = text
    st.session_state["_ocr_pending_text"] = text


# =============================================================================
# 各步骤
# =============================================================================
def _extract_image(uploaded, period: str) -> None:
    extractor = runtime.load("ocr.extractor")
    if extractor is None:
        ui.module_missing("截图 OCR 识别", "ocr.extractor", detail=runtime.error_of("ocr.extractor"))
        return
    target = Path(config.UPLOAD_DIR) / f"ocr_{datetime.now():%Y%m%d%H%M%S}_{uploaded.name}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(uploaded.getvalue())
        with st.spinner("正在识别截图文字…"):
            result = extractor.extract_text(target)
    except Exception as exc:  # noqa: BLE001
        ui.failure("截图识别失败，请改用「手动粘贴文本」。", exc)
        return
    text = str(result.get("text") or "")
    backend = result.get("backend") or "unknown"
    if not result.get("ok") or not text.strip():
        st.warning(
            f"OCR 未识别出有效文字（后端：{backend}）：{result.get('message') or '无可用本地 OCR'}。"
            "请改用「手动粘贴文本」入口。"
        )
        return
    _set_text(text)
    st.session_state["_ocr_backend"] = backend
    st.success(f"已识别 {len(text)} 个字符（后端：{backend}），可在下方文本框中修正后核对。")
    st.rerun()


def _map_fields(text: str, period: str) -> dict:
    mapper = runtime.load("ocr.field_mapper")
    if mapper is None or not hasattr(mapper, "map_fields"):
        ui.module_missing("栏次映射", "ocr.field_mapper", detail=runtime.error_of("ocr.field_mapper"))
        return {}
    with st.spinner("正在映射申报表栏次…"):
        result = mapper.map_fields(text, period=period)
    fields = dict(result.get("fields") or {})
    labels: dict[str, str] = {}
    defs = getattr(mapper, "VAT_FIELD_DEFS", None) or []
    for definition in defs:
        key = definition.get("key")
        if key:
            labels[key] = f"第{definition.get('line_no')}栏 {definition.get('name')}"
    st.session_state[STATE_FIELDS] = fields
    st.session_state[STATE_LABELS] = labels
    st.session_state.pop(STATE_REPORT, None)
    if not fields:
        st.warning(
            "未能识别出标准栏次金额。请检查文本格式（如「第24栏 本期应补（退）税额：0.00」），"
            "或手动在下方表格录入栏次金额。"
        )
    else:
        st.success(f"已映射 {len(fields)} 个栏次（方法：{result.get('method') or 'rule'}）。")
    if result.get("unmapped"):
        with st.expander("未映射的文本行", expanded=False):
            for line in result["unmapped"]:
                st.text(line)
    return fields


def _check_fields(fields: dict, period: str) -> dict:
    validator = runtime.load("ocr.validator")
    if validator is not None and hasattr(validator, "check_vat_return"):
        with st.spinner("正在比对台账数据…"):
            report = validator.check_vat_return(fields, period)
        return dict(report)
    filing_validator = runtime.load("filing.validator")
    if filing_validator is not None and hasattr(filing_validator, "validate_against_fields"):
        report = filing_validator.validate_against_fields(period, fields)
        return dict(report)
    ui.module_missing("申报核对", "ocr.validator", detail=runtime.error_of("ocr.validator"))
    return {}


def _run_text_check(text: str, period: str) -> None:
    validator = runtime.load("ocr.validator")
    if validator is not None and hasattr(validator, "check_from_text"):
        try:
            with st.spinner("正在识别并核对…"):
                report = dict(validator.check_from_text(text, period))
        except Exception as exc:  # noqa: BLE001
            ui.failure("申报核对失败。", exc)
            return
        st.session_state[STATE_REPORT] = report
        st.session_state.pop(STATE_FIELDS, None)
        st.session_state.pop(STATE_LABELS, None)
        st.success("核对完成")
        return
    fields = _map_fields(text, period)
    if not fields:
        return
    report = _check_fields(fields, period)
    if report:
        st.session_state[STATE_REPORT] = report
        st.success("核对完成")


# =============================================================================
# 结果展示
# =============================================================================
def _render_report(report: dict) -> None:
    if not report:
        return
    status = report.get("status") or "PASS"
    summary = report.get("summary") or ""
    ui.section("📊 差异报告", "逐栏比对台账重算结果与申报表填报值。")
    ui.status_alert(status, summary or "核对完成")
    details = report.get("details") or []
    if details:
        ui.status_table(details)
        counts = {level: sum(1 for d in details if str(d.get("status")) == level) for level in ("PASS", "WARNING", "ERROR")}
        st.caption(f"核对结果：通过 {counts['PASS']} 项 · 警告 {counts['WARNING']} 项 · 错误 {counts['ERROR']} 项")
    with st.expander("核对建议", expanded=False):
        st.markdown(
            "- 如果申报表与台账不一致，请先检查台账数据是否完整（收入是否漏记、专票金额是否登记）。\n"
            "- 修订申报表前，建议在电子税务局核对应纳税额与免税额口径。\n"
            "- 核对结果仅供内部检查，最终以主管税务机关核定为准。"
        )
    theme.disclaimer(report.get("disclaimer"))


def _render_fields_editor(period: str) -> None:
    fields = dict(st.session_state.get(STATE_FIELDS) or {})
    labels = dict(st.session_state.get(STATE_LABELS) or {})
    st.markdown("**识别到的栏次金额**（可直接修正，修正后重新核对）")
    if not fields:
        st.caption("尚未映射栏次。可先点击上方「映射栏次」，或手动追加栏次金额。")
        manual_keys = st.text_input(
            "手动追加栏次（格式：line_24=0 或 第24栏=0）",
            key="ocr_manual_field",
            placeholder="例如：line_1=101980.20",
        )
        if st.button("追加该栏次", key="ocr_add_field") and manual_keys:
            key, _, raw = manual_keys.partition("=")
            try:
                value = float(raw.strip() or 0)
            except ValueError:
                st.error("金额格式不正确")
                return
            key = key.strip()
            alias = {"第1栏": "line_1", "第10栏": "line_10", "第18栏": "line_18", "第19栏": "line_19",
                     "第22栏": "line_22", "第24栏": "line_24", "附加税合计": "surtax_total", "合计应补": "total_payable"}
            fields[alias.get(key, key)] = value
            st.session_state[STATE_FIELDS] = fields
            st.rerun()
        return
    rows = [
        {
            "栏次": labels.get(key, key),
            "字段键": key,
            "金额": float(value) if isinstance(value, (int, float)) else 0.0,
        }
        for key, value in fields.items()
    ]
    edited = st.data_editor(
        pd.DataFrame(rows),
        column_config={
            "栏次": st.column_config.TextColumn("栏次", disabled=True),
            "字段键": st.column_config.TextColumn("字段键", disabled=True),
            "金额": st.column_config.NumberColumn("金额", format="¥%.2f", step=0.01),
        },
        hide_index=True,
        key="ocr_fields_editor",
        width="stretch",
        num_rows="fixed",
    )
    if st.button("🔁 按修正后栏次重新核对", key="ocr_recheck", type="primary", width="stretch"):
        corrected = {
            str(row["字段键"]): F.to_float(row["金额"]) for _, row in edited.iterrows()
        }
        st.session_state[STATE_FIELDS] = corrected
        report = _check_fields(corrected, period)
        if report:
            st.session_state[STATE_REPORT] = report
            st.success("核对完成")


# =============================================================================
# 页面入口
# =============================================================================
def render() -> None:
    """渲染申报核对页。"""
    ui.page_header(
        "申报核对",
        "上传增值税申报表截图或粘贴栏次文本，自动与台账重算结果比对，输出差异报告。",
        icon="🔍",
        right_text=f"当前季度：{current_quarter()}",
    )
    _apply_pending_text()

    extractor = runtime.load("ocr.extractor")
    capabilities: dict = {}
    if extractor is not None and hasattr(extractor, "is_available"):
        try:
            capabilities = dict(extractor.is_available())
        except Exception:  # noqa: BLE001
            capabilities = {}
    st.info(
        "DeepSeek 官方 API 不具备视觉能力，截图识别由**本地 OCR** 完成（macOS Vision / Tesseract），"
        "文字理解与栏次映射才使用 DeepSeek。若本地 OCR 不可用，请使用「手动粘贴文本」入口。",
        icon="🔍",
    )
    if capabilities:
        st.caption(
            "本地 OCR 能力："
            + " · ".join(
                f"{name} {'可用' if available else '不可用'}"
                for name, available in capabilities.items()
            )
        )

    period = st.selectbox("核对所属期间", list(quarter_options(count=8)), index=0, key="ocr_period")

    upload_tab, paste_tab = st.tabs(["📷 上传截图识别", "📝 手动粘贴文本"])
    with upload_tab:
        uploaded = st.file_uploader(
            "上传增值税申报表截图（png / jpg / jpeg）",
            type=["png", "jpg", "jpeg"],
            key="ocr_uploader",
            label_visibility="collapsed",
        )
        upload_cols = st.columns(2)
        with upload_cols[0]:
            if st.button("🔎 识别截图文字", key="ocr_extract", disabled=uploaded is None, type="primary", width="stretch"):
                if uploaded is not None:
                    _extract_image(uploaded, period)
        with upload_cols[1]:
            if st.button("⚡ 一键识别并核对", key="ocr_one_click", disabled=uploaded is None, width="stretch"):
                if uploaded is not None:
                    extractor_module = runtime.load("ocr.extractor")
                    if extractor_module is not None and hasattr(extractor_module, "extract_text"):
                        target = Path(config.UPLOAD_DIR) / f"ocr_{datetime.now():%Y%m%d%H%M%S}_{uploaded.name}"
                        try:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(uploaded.getvalue())
                            result = extractor_module.extract_text(target)
                            text = str(result.get("text") or "")
                        except Exception as exc:  # noqa: BLE001
                            ui.failure("截图识别失败，请改用手动粘贴。", exc)
                        else:
                            if text.strip():
                                _set_text(text)
                                _run_text_check(text, period)
                                st.rerun()
                            else:
                                st.warning("OCR 未识别出文字，请改用手动粘贴文本。")
                    else:
                        ui.module_missing("截图 OCR", "ocr.extractor", detail=runtime.error_of("ocr.extractor"))
    with paste_tab:
        manual_text = st.text_area(
            "粘贴申报表栏次文字（可包含「第1栏」「第10栏」「第24栏」等）",
            height=200,
            key="ocr_manual_text",
            placeholder="示例：\n第1栏 应征增值税不含税销售额：0.00\n第10栏 小微企业免税销售额：101,980.20\n第24栏 本期应补（退）税额：0.00",
        )
        paste_cols = st.columns(2)
        with paste_cols[0]:
            if st.button("📋 使用粘贴文本", key="ocr_use_manual", type="primary", width="stretch"):
                if manual_text.strip():
                    _set_text(manual_text.strip())
                    st.rerun()
                else:
                    st.warning("请先粘贴文本")
        with paste_cols[1]:
            if st.button("⚡ 一键映射并核对", key="ocr_manual_check", width="stretch"):
                if manual_text.strip():
                    _set_text(manual_text.strip())
                    _run_text_check(manual_text.strip(), period)
                else:
                    st.warning("请先粘贴文本")

    st.markdown("---")
    ui.section("📄 识别文本（可编辑修正）", "识别结果可能有误差，请对照截图修正后再进行栏次映射。")
    if "ocr_text_edit" not in st.session_state:
        st.session_state["ocr_text_edit"] = st.session_state.get("ocr_text", "")
    text = st.text_area(
        "识别文本",
        height=220,
        key="ocr_text_edit",
        label_visibility="collapsed",
        placeholder="OCR 识别文本将显示在这里；也可以直接粘贴申报表文字。",
    )
    st.session_state["ocr_text"] = text

    action_cols = st.columns([1, 1, 1])
    with action_cols[0]:
        if st.button("🧭 映射栏次", key="ocr_map", type="primary", width="stretch"):
            if not text.strip():
                st.warning("请先识别或粘贴申报表文字。")
            else:
                _map_fields(text, period)
    with action_cols[1]:
        if st.button("🔍 核对差异", key="ocr_check", width="stretch"):
            if not text.strip():
                st.warning("请先识别或粘贴申报表文字。")
            else:
                _run_text_check(text, period)
    with action_cols[2]:
        st.caption("核对逻辑：台账重算 → 标准栏次 → 与申报表填报值逐栏比对。")

    _render_fields_editor(period)

    report = st.session_state.get(STATE_REPORT)
    if report:
        _render_report(report)
    theme.disclaimer()
