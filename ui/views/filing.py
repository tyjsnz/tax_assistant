"""📋 申报填报（核心页面）。

流程：选择期间 → 生成全部申报表 → 逐表展示栏次与校验结果 → 导出 Excel 申报包 →
保存草稿 / 标记已确认 / 标记已申报 → 进度条与完成清单 → 填报指引。

所有申报表数据来自 ``filing.registry.build_all(period)``，本页面不重算税额。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import pandas as pd
import streamlit as st

import config
import ledger
from engine import completion_checklist, current_quarter, format_date_cn, period_label, quarter_options
from ui.components import archive_api as AA, cache, formatting as F, runtime, theme, ui

FLASH_KEY = "_filing_flash"
STATE_BUNDLES = "filing_bundles"
STATE_CHECKS = "filing_checks"
STATE_ARCHIVE_RESULT = "filing_archive_result"
STATE_ARCHIVE_VERIFY = "filing_archive_verify_result"

TOTAL_LABELS = {
    # 增值税
    "vat_payable": "应缴增值税",
    "vat_exempt": "免征增值税",
    "vat_at_standard": "按3%征收率应纳税额",
    "vat_reduction": "应纳税额减征额",
    "surtax": "附加税费合计",
    "surtax_city": "城建税",
    "surtax_education": "教育费附加",
    "surtax_local_education": "地方教育附加",
    "total_tax": "合计税费",
    "total_inclusive": "含税销售额",
    "total_exclusive": "不含税销售额",
    "special_inclusive": "专票含税销售额",
    "special_exclusive": "专票不含税销售额",
    "ordinary_inclusive": "普票+未开票含税销售额",
    "ordinary_exclusive": "普票+未开票不含税销售额",
    "taxable_sales": "应征增值税销售额",
    "exempt_sales": "免税销售额",
    "line_18": "第18栏 应纳税额",
    "line_22": "第22栏 减征额",
    "line_24": "第24栏 应补（退）税额",
    # 企业所得税
    "prepay_payable": "本期应补（退）",
    "cit_accrued": "本期应纳所得税额",
    "cit_paid": "本年累计已预缴",
    "standard_tax": "按25%计算的税额",
    "tax_reduction": "减免所得税额",
    "rd_deduction": "研发加计扣除",
    "taxable_income": "应纳税所得额",
    "quarter_profit": "本季利润总额",
    "ytd_profit": "本年累计利润",
    "quarter_income_exclusive": "本季不含税收入",
    "quarter_expense": "本季费用",
    "effective_rate": "实际税负率",
    "standard_rate": "法定税率",
    "revenue": "营业收入",
    "cost": "营业成本",
    "profit": "利润总额",
    # 财务报表
    "net_profit": "净利润",
    "revenue_ytd": "营业收入（本年累计）",
    "profit_ytd": "利润总额（本年累计）",
    "net_profit_ytd": "净利润（本年累计）",
    "income_tax_ytd": "所得税费用（本年累计）",
    "total_assets": "资产总计",
    "total_liabilities": "负债合计",
    "total_equity": "所有者权益合计",
    "cash": "货币资金",
    # 印花税
    "total_payable": "应缴合计",
    "total_amount": "计税金额合计",
    "total_before_discount": "减免前税额",
    "discount_amount": "减免税额",
    "contract_count": "合同份数",
    "discount_ratio": "减免比例",
    "total_income": "收入合计",
    "total_expense": "费用合计",
}

#: 指标卡优先展示的字段（存在时优先）
TOTAL_PRIORITY = [
    "prepay_payable",
    "total_payable",
    "total_tax",
    "vat_payable",
    "surtax",
    "taxable_income",
    "profit",
    "net_profit",
    "total_amount",
    "revenue",
]

PERCENT_KEYS = {"effective_rate", "standard_rate", "discount_ratio"}
COUNT_KEYS = {"contract_count"}
SKIP_KEYS = {"estimate_note", "rates", "within_exemption_limit", "small_scale", "qualifies_small_low_profit"}


# =============================================================================
# 提示消息
# =============================================================================
def _flash(message: str, level: str = "success") -> None:
    st.session_state.setdefault(FLASH_KEY, []).append({"message": message, "level": level})


def _show_flash() -> None:
    for item in st.session_state.pop(FLASH_KEY, []) or []:
        if item["level"] == "error":
            st.error(item["message"])
        elif item["level"] == "warning":
            st.warning(item["message"])
        else:
            st.success(item["message"])


# =============================================================================
# filing 模块访问（含降级）
# =============================================================================
def _modules() -> dict[str, Any]:
    return {
        "registry": runtime.load("filing.registry"),
        "validator": runtime.load("filing.validator"),
        "export": runtime.load("filing.export"),
        "progress": runtime.load("filing.progress"),
        "guide": runtime.load("filing.guide"),
    }


def _fallback_save(bundle: dict, status: str) -> dict:
    """``filing.progress`` 未就绪时，直接 upsert 到 filings 表。"""
    table = ledger.table("filings")
    period = bundle.get("period") or current_quarter()
    form_id = bundle.get("form_id") or ""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for row in table.read():
        if str(row.get("period")) == str(period) and str(row.get("form_id")) == str(form_id):
            return table.update(
                row["id"],
                {"status": status, "data_json": bundle, "updated_at": now},
            )
    payload = ledger.models.normalize_filing(
        {
            "period": period,
            "form_id": form_id,
            "form_name": bundle.get("form_name", ""),
            "status": status,
            "data_json": bundle,
            "note": "UI 本地降级保存",
        }
    )
    payload["updated_at"] = now
    return table.append(payload)


def _save_filing(bundle: dict, status: str) -> dict:
    modules = _modules()
    progress = modules["progress"]
    if progress is not None and hasattr(progress, "save_filing"):
        return progress.save_filing(bundle, status=status)
    return _fallback_save(bundle, status)


def _mark_submitted(period: str, form_id: str) -> dict:
    modules = _modules()
    progress = modules["progress"]
    if progress is not None and hasattr(progress, "mark_submitted"):
        return progress.mark_submitted(period, form_id)
    bundle = _bundle_of(period, form_id)
    if bundle:
        return _fallback_save(bundle, "已申报")
    return {"ok": False}


def _progress(period: str) -> dict:
    """申报进度（优先 filing.progress.progress，降级按 filings 表统计）。"""
    modules = _modules()
    progress = modules["progress"]
    if progress is not None and hasattr(progress, "progress"):
        try:
            return dict(progress.progress(period))
        except Exception:  # noqa: BLE001
            pass
    checklist = completion_checklist(period)
    rows = []
    try:
        rows = [row for row in ledger.table("filings").read() if str(row.get("period")) == str(period)]
    except Exception:  # noqa: BLE001
        rows = []
    done_ids = {str(row.get("form_id")) for row in rows if str(row.get("status")) in {"已确认", "已申报"}}
    items = [
        {
            "form_id": item["form_id"],
            "form_name": item["item"],
            "status": next(
                (str(row.get("status")) for row in rows if str(row.get("form_id")) == item["form_id"]),
                "未开始",
            ),
            "required": item.get("required", True),
            "updated_at": next(
                (str(row.get("updated_at") or row.get("created_at") or "") for row in rows if str(row.get("form_id")) == item["form_id"]),
                "",
            ),
        }
        for item in checklist
    ]
    done = len(done_ids & {item["form_id"] for item in items})
    total = len(items) or 1
    return {
        "period": period,
        "total": len(items),
        "done": done,
        "percent": int(round(done / total * 100)),
        "items": items,
    }


def _list_filings(period: str) -> list[dict]:
    modules = _modules()
    progress = modules["progress"]
    if progress is not None and hasattr(progress, "list_filings"):
        try:
            return list(progress.list_filings(period))
        except Exception:  # noqa: BLE001
            pass
    try:
        return [row for row in ledger.table("filings").read() if str(row.get("period")) == str(period)]
    except Exception:  # noqa: BLE001
        return []


def _bundle_of(period: str, form_id: str) -> dict | None:
    for bundle in st.session_state.get(STATE_BUNDLES, {}).get(period, []) or []:
        if str(bundle.get("form_id")) == str(form_id):
            return bundle
    return None


# =============================================================================
# 生成与校验
# =============================================================================
def _generate(period: str) -> None:
    modules = _modules()
    registry = modules["registry"]
    if registry is None:
        ui.module_missing("申报表生成", "filing.registry", detail=runtime.error_of("filing.registry"))
        return
    try:
        bundles = list(registry.build_all(period))
    except Exception as exc:  # noqa: BLE001
        ui.failure("生成申报表失败，请确认台账数据完整后重试。", exc)
        return
    checks: dict[str, dict] = {}
    validator = modules["validator"]
    if validator is not None and hasattr(validator, "validate_bundle"):
        for bundle in bundles:
            try:
                checks[str(bundle.get("form_id"))] = dict(validator.validate_bundle(bundle))
            except Exception as exc:  # noqa: BLE001
                checks[str(bundle.get("form_id"))] = {
                    "status": "WARNING",
                    "summary": f"校验执行失败：{exc}",
                    "details": [],
                }
    st.session_state.setdefault(STATE_BUNDLES, {})[period] = bundles
    st.session_state.setdefault(STATE_CHECKS, {})[period] = checks
    cache.clear()
    _flash(f"已生成 {len(bundles)} 张申报表（{period_label(period)}）")
    st.rerun()


# =============================================================================
# 导出
# =============================================================================
def _fallback_export_excel(period: str, bundles: Sequence[dict]) -> Path:
    """``filing.export`` 未就绪时的兜底导出（openpyxl 简版工作簿）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    workbook = Workbook()
    summary_sheet = workbook.active
    summary_sheet.title = "申报总览"
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1E5AA8")
    summary_sheet.append(["报表编码", "报表名称", "类别", "生成时间", "状态"])
    for cell in summary_sheet[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
    for bundle in bundles:
        summary_sheet.append(
            [
                bundle.get("form_id", ""),
                bundle.get("form_name", ""),
                bundle.get("category", ""),
                bundle.get("generated_at", ""),
                bundle.get("status", "草稿"),
            ]
        )
    for bundle in bundles:
        sheet = workbook.create_sheet((bundle.get("form_id") or "报表")[:31])
        sheet.append([bundle.get("form_name", "申报表")])
        sheet["A1"].font = Font(bold=True, size=14)
        sheet.append([f"税款所属期：{bundle.get('period_label') or bundle.get('period') or ''}"])
        sheet.append([])
        for section in bundle.get("sections") or []:
            sheet.append([section.get("title", "")])
            sheet.cell(row=sheet.max_row, column=1).font = Font(bold=True, color="1E5AA8")
            sheet.append(["栏次", "名称", "金额", "来源", "口径"])
            for cell in sheet[sheet.max_row]:
                cell.font = Font(bold=True)
            for line in section.get("lines") or []:
                sheet.append(
                    [
                        line.get("line_no", ""),
                        line.get("name", ""),
                        F.to_float(line.get("value")),
                        line.get("source", ""),
                        line.get("rule", ""),
                    ]
                )
            sheet.append([])
        sheet.append(["⚠️ " + config.DISCLAIMER])
        widths = {"A": 14, "B": 46, "C": 16, "D": 24, "E": 30}
        for column, width in widths.items():
            sheet.column_dimensions[column].width = width
    target = Path(config.EXPORT_DIR) / f"申报包_{period}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(target)
    return target


def _export_package(period: str, bundles: Sequence[dict]) -> Path:
    modules = _modules()
    export = modules["export"]
    if export is not None and hasattr(export, "export_package_excel"):
        return Path(export.export_package_excel(period, bundles=list(bundles)))
    return _fallback_export_excel(period, bundles)


def _export_checklist(period: str, bundles: Sequence[dict]) -> Path:
    modules = _modules()
    export = modules["export"]
    if export is not None and hasattr(export, "export_checklist_markdown"):
        return Path(export.export_checklist_markdown(period, bundles=list(bundles)))
    lines = [f"# 申报核对清单（{period_label(period)}）", ""]
    for bundle in bundles:
        lines.append(f"## {bundle.get('form_name')}（{bundle.get('form_id')}）")
        for section in bundle.get("sections") or []:
            lines.append(f"### {section.get('title')}")
            for item in section.get("lines") or []:
                lines.append(
                    f"- 第{item.get('line_no')}栏 {item.get('name')}：{F.fmt_money(item.get('value'))}"
                )
        lines.append("")
    lines.append(config.DISCLAIMER)
    target = Path(config.EXPORT_DIR) / f"申报核对清单_{period}_{datetime.now():%Y%m%d_%H%M%S}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def _render_export(period: str, bundles: Sequence[dict]) -> None:
    ui.section("📦 导出申报包", "Excel 工作簿含各申报表与提交核对清单，便于对照电子税务局逐栏录入。")
    cols = st.columns(2)
    with cols[0]:
        if st.button("📊 生成 Excel 申报包", key="filing_export_excel", type="primary", width="stretch"):
            try:
                path = _export_package(period, bundles)
            except Exception as exc:  # noqa: BLE001
                ui.failure("导出 Excel 申报包失败。", exc)
            else:
                st.session_state["_filing_excel_path"] = str(path)
                st.success("Excel 申报包已生成")
    with cols[1]:
        if st.button("📝 生成核对清单 Markdown", key="filing_export_checklist", width="stretch"):
            try:
                path = _export_checklist(period, bundles)
            except Exception as exc:  # noqa: BLE001
                ui.failure("生成核对清单失败。", exc)
            else:
                st.session_state["_filing_checklist_path"] = str(path)
                st.success("核对清单已生成")

    download_cols = st.columns(2)
    excel_path = st.session_state.get("_filing_excel_path")
    with download_cols[0]:
        if excel_path and Path(excel_path).exists():
            file_path = Path(excel_path)
            st.download_button(
                "⬇️ 下载 Excel 申报包",
                data=file_path.read_bytes(),
                file_name=file_path.name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="filing_download_excel",
                width="stretch",
            )
    checklist_path = st.session_state.get("_filing_checklist_path")
    with download_cols[1]:
        if checklist_path and Path(checklist_path).exists():
            file_path = Path(checklist_path)
            st.download_button(
                "⬇️ 下载核对清单",
                data=file_path.read_bytes(),
                file_name=file_path.name,
                mime="text/markdown",
                key="filing_download_checklist",
                width="stretch",
            )


# =============================================================================
# 单张申报表
# =============================================================================
def _lines_frame(lines: Sequence[dict]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "栏次": ("⭐ " if line.get("highlight") else "") + str(line.get("line_no") or ""),
                "名称": line.get("name") or "",
                "金额": F.fmt_money(line.get("value")),
                "来源": line.get("source") or "",
                "口径": line.get("rule") or "",
            }
            for line in lines
        ]
    )


def _render_totals(totals: dict) -> None:
    """申报表合计指标卡（优先展示关键税种字段）。"""
    if not isinstance(totals, dict) or not totals:
        return
    ordered = [key for key in TOTAL_PRIORITY if key in totals]
    ordered += [key for key in totals if key not in ordered]
    items = []
    for key in ordered:
        value = totals.get(key)
        if key in SKIP_KEYS or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if key in PERCENT_KEYS:
            text = F.fmt_pct(value)
        elif key in COUNT_KEYS:
            text = f"{int(value)} 份"
        else:
            text = F.fmt_money(value)
        items.append({"label": TOTAL_LABELS.get(key, key), "value": text})
        if len(items) >= 6:
            break
    if items:
        ui.metric_row(items)


def _render_check(check: dict, bundle: dict) -> None:
    st.markdown("**校验结果**")
    if not check:
        st.info("未执行自动校验（校验模块未就绪），请人工核对栏次数据。")
        return
    status = check.get("status") or "PASS"
    summary = check.get("summary") or ""
    ui.status_alert(status, summary or "校验完成")
    details = check.get("details") or []
    if details:
        ui.status_table(details)
    theme.disclaimer(check.get("disclaimer"))


def _render_guide(bundle: dict) -> None:
    modules = _modules()
    guide = modules["guide"]
    steps: list[str] = []
    submission_text = ""
    if guide is not None:
        try:
            if hasattr(guide, "build_guide"):
                steps = list(guide.build_guide(bundle))
            if hasattr(guide, "export_submission_text"):
                submission_text = str(guide.export_submission_text(bundle) or "")
        except Exception as exc:  # noqa: BLE001
            st.warning(f"填报指引生成失败：{exc}")
    if not steps and not submission_text:
        steps = [
            "登录云南省电子税务局，进入「我要办税 → 税费申报及缴纳」。",
            f"选择税款所属期 {bundle.get('period_label') or bundle.get('period')}，进入对应申报表。",
            "按本页展示的栏次金额逐项录入，核对合计与本期应补（退）税额。",
            "确认无误后提交申报，并按提示完成税费缴纳（零申报也需提交）。",
        ]
    if steps:
        with st.expander("📖 填报指引", expanded=False):
            for step in steps:
                st.markdown(f"- {step}")
            st.caption("指引由系统按申报表自动生成，最终以电子税务局页面为准。")
    if submission_text:
        with st.expander("📋 逐栏录入文本（可复制）", expanded=False):
            st.code(submission_text, language="text")


def _render_bundle_actions(bundle: dict, period: str) -> None:
    form_id = str(bundle.get("form_id") or "")
    status = str(bundle.get("status") or "草稿")
    st.caption(f"当前状态：{F.status_icon(status)} {status}")
    cols = st.columns(3)
    actions = [
        ("💾 保存草稿", "草稿", cols[0], f"filing_save_{form_id}"),
        ("✅ 标记已确认", "已确认", cols[1], f"filing_confirm_{form_id}"),
        ("📤 标记已申报", "已申报", cols[2], f"filing_submit_{form_id}"),
    ]
    for label, target_status, column, key in actions:
        with column:
            if st.button(label, key=key, width="stretch"):
                try:
                    if target_status == "已申报":
                        _mark_submitted(period, form_id)
                    else:
                        _save_filing(bundle, target_status)
                except Exception as exc:  # noqa: BLE001
                    ui.failure(f"更新申报状态失败：{label}", exc)
                else:
                    bundle["status"] = target_status
                    stored = st.session_state.get(STATE_BUNDLES, {}).get(period) or []
                    for item in stored:
                        if str(item.get("form_id")) == form_id:
                            item["status"] = target_status
                    cache.clear()
                    _flash(f"{bundle.get('form_name')} 状态已更新为「{target_status}」")
                    st.rerun()


def _render_bundle(bundle: dict, check: dict, period: str, index: int) -> None:
    level = (check or {}).get("status") or (bundle.get("summary") or {}).get("level") or "PASS"
    form_id = str(bundle.get("form_id") or f"FORM{index}")
    form_name = bundle.get("form_name") or form_id
    status = str(bundle.get("status") or "草稿")
    label = f"{F.status_icon(level)} {form_name} · {status}"
    with st.expander(label, expanded=(index == 0)):
        st.caption(
            f"报表编码：{form_id} · 类别：{bundle.get('category') or '—'} · "
            f"频率：{bundle.get('frequency') or '—'} · 生成时间：{bundle.get('generated_at') or '—'}"
        )
        summary = bundle.get("summary") or {}
        ui.status_alert(summary.get("level") or level, summary.get("text") or "申报表已生成，请逐栏核对。")

        for section in bundle.get("sections") or []:
            st.markdown(f"**{section.get('title') or '申报表'}**")
            lines = section.get("lines") or []
            if lines:
                st.dataframe(_lines_frame(lines), hide_index=True, width="stretch")
            else:
                st.caption("本分区无栏次数据")

        _render_totals(bundle.get("totals") or {})
        _render_check(check, bundle)
        _render_bundle_actions(bundle, period)
        _render_guide(bundle)

        with st.expander("🧾 生成依据数据快照（审计用）", expanded=False):
            data = bundle.get("data") or {}
            if data:
                st.json(data, expanded=False)
            else:
                st.caption("无数据快照")
        theme.disclaimer(bundle.get("disclaimer"))


# =============================================================================
# 进度与清单
# =============================================================================
def _render_progress(period: str, bundles: Sequence[dict]) -> None:
    progress = _progress(period)
    percent = max(0, min(100, int(progress.get("percent") or 0)))
    done = int(progress.get("done") or 0)
    total = int(progress.get("total") or 0)
    ui.section("📈 申报进度", f"{period_label(period)} 申报事项完成情况（已确认 / 已申报计入完成）。")
    st.progress(percent / 100, text=f"完成 {done}/{total} · {percent}%")
    ui.metric_row(
        [
            {"label": "申报事项总数", "value": f"{total}"},
            {"label": "已完成", "value": f"{done}"},
            {"label": "待办理", "value": f"{max(total - done, 0)}"},
        ],
        columns=3,
    )

    checklist = completion_checklist(period)
    items = {str(item.get("form_id")): item for item in progress.get("items") or []}
    frames = []
    for item in checklist:
        form_id = item.get("form_id")
        record = items.get(str(form_id)) or {}
        frames.append(
            {
                "申报事项": item.get("item"),
                "是否必需": "必需" if item.get("required") else "视情况",
                "系统状态": record.get("status") or "未开始",
                "更新时间": record.get("updated_at") or "—",
                "说明": item.get("note") or "",
            }
        )
    with st.container(border=True):
        st.markdown("**申报完成清单**")
        st.dataframe(pd.DataFrame(frames), hide_index=True, width="stretch")
        st.caption("完成清单由 engine.completion_checklist 生成；状态来自本地申报记录。")

    filings = _list_filings(period)
    if filings:
        with st.expander("🗂️ 本地申报记录", expanded=False):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "报表编码": row.get("form_id"),
                            "报表名称": row.get("form_name"),
                            "状态": row.get("status"),
                            "更新时间": row.get("updated_at") or row.get("created_at") or "—",
                        }
                        for row in filings
                    ]
                ),
                hide_index=True,
                width="stretch",
            )


def _render_guides_summary(bundles: Sequence[dict]) -> None:
    ui.section("📖 填报指引", "按申报表生成的逐项操作步骤，可对照电子税务局逐栏录入。")
    if not bundles:
        st.caption("生成申报表后，这里会列出每张表的填报步骤。")
        return
    for bundle in bundles:
        with st.expander(f"{bundle.get('form_name') or bundle.get('form_id')} 填报指引", expanded=False):
            _render_guide(bundle)
    theme.disclaimer()


# =============================================================================
# 期间结账状态 / 申报包归档（P0-3）
# =============================================================================
def _closure_text(period: str) -> str:
    """季度结账状态文案（已结账 / 部分已结 / 未结账）。"""
    closure = AA.quarter_closure(period)
    quarter_status = str(closure.get("quarter_status") or "未结账")
    if quarter_status != "未结账":
        return quarter_status
    if closure.get("all_closed"):
        return "已结账"
    return f"未结账（{int(closure.get('closed_count') or 0)}/{int(closure.get('total') or 3)} 月已结）"


def _render_closure_badges(period: str) -> None:
    """期间结账状态 + 归档状态徽标、各月结账明细。"""
    closure = AA.quarter_closure(period)
    try:
        archived = AA.has_archive(period)
    except Exception:  # noqa: BLE001
        archived = False
    statuses = closure.get("statuses") or {}
    ui.metric_row(
        [
            {
                "label": "期间结账状态",
                "value": ("🔒 " if closure.get("all_closed") or closure.get("quarter_status") != "未结账" else "⬜ ") + _closure_text(period),
                "icon": ":material/lock:",
                "help": "结账后期间台账与凭证锁定；ERROR 检查项未通过时无法结账。",
            },
            {
                "label": "申报包归档",
                "value": "🗄️ 已归档" if archived else "⬜ 未归档",
                "icon": ":material/archive:",
                "help": f"归档目录：{AA.archive_dir()}",
            },
            {
                "label": "已结账月份",
                "value": f"{int(closure.get('closed_count') or 0)} / {int(closure.get('total') or 3)}",
                "icon": ":material/event_available:",
            },
        ],
        columns=3,
    )
    if statuses:
        month_text = " · ".join(
            f"{AA.month_label(month)} {status}" for month, status in statuses.items()
        )
        st.caption(f"各月结账状态：{month_text}（结账在「首页驾驶舱 → 期间结账」操作）")


def _run_archive(period: str, include_pdf: bool, note: str) -> None:
    """执行申报包归档。"""
    try:
        with st.spinner("正在生成并归档申报包（Excel + PDF + manifest）…"):
            result = AA.archive_filing_package(period, include_pdf=include_pdf, note=note)
    except Exception as exc:  # noqa: BLE001
        st.error(f"申报包归档失败：{exc}")
        return
    st.session_state[STATE_ARCHIVE_RESULT] = result
    st.session_state.pop(STATE_ARCHIVE_VERIFY, None)
    cache.clear()
    _flash(
        f"{period_label(period)} 申报包已归档"
        + (f"，manifest {AA.short_hash(result.get('manifest_hash'), 16)}" if result.get("manifest_hash") else "")
    )
    st.rerun()


def _run_verify_archive(target: str) -> None:
    """校验归档包哈希与完整性。"""
    if not target:
        st.warning("未找到可校验的归档记录。")
        return
    try:
        with st.spinner("正在校验归档文件哈希…"):
            result = AA.verify_archive(target)
    except Exception as exc:  # noqa: BLE001
        st.error(f"归档校验失败：{exc}")
        return
    st.session_state[STATE_ARCHIVE_VERIFY] = result
    st.rerun()


def _render_archive_result(result: dict) -> None:
    """展示最近一次归档结果：文件、manifest 哈希、校验结论。"""
    if not isinstance(result, dict) or not result:
        return
    files = result.get("files") or []
    checks_summary = result.get("checks_summary") or {}
    errors = int(checks_summary.get("ERROR") or 0)
    warnings = int(checks_summary.get("WARNING") or 0)
    with st.container(border=True):
        st.markdown("**最近一次归档结果**")
        ui.metric_row(
            [
                {"label": "归档文件", "value": f"{len(files)} 个"},
                {"label": "PDF", "value": "已生成" if result.get("pdf") else "未生成"},
                {"label": "申报表", "value": f"{len(result.get('forms') or [])} 张"},
                {"label": "校验告警", "value": f"{warnings} 提示 / {errors} 错误"},
            ],
            columns=4,
        )
        if files:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "文件": item.get("name"),
                            "大小": AA.fmt_size(item.get("size")),
                            "SHA-256": AA.short_hash(item.get("sha256"), 20),
                        }
                        for item in files
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
        cols = st.columns(2)
        with cols[0]:
            st.caption(f"归档目录：{result.get('dir') or AA.archive_dir()}")
            if result.get("manifest_path"):
                st.caption(
                    f"manifest：{Path(str(result['manifest_path'])).name} · "
                    f"哈希 {AA.short_hash(result.get('manifest_hash'), 20)}"
                )
        with cols[1]:
            download_targets = [
                item
                for item in files
                if str(item.get("path") or "").lower().endswith((".xlsx", ".pdf"))
            ]
            for index, item in enumerate(download_targets[:2]):
                path = Path(str(item.get("path") or ""))
                if not path.exists():
                    continue
                st.download_button(
                    f"⬇️ 下载 {path.name}",
                    data=path.read_bytes(),
                    file_name=path.name,
                    mime=(
                        "application/pdf"
                        if path.suffix.lower() == ".pdf"
                        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    ),
                    key=f"filing_archive_download_{index}_{path.stem}",
                    width="stretch",
                )
        for warning in result.get("warnings") or []:
            st.warning(str(warning))
        theme.disclaimer(result.get("disclaimer"))


def _render_archive_verify(result: dict) -> None:
    """展示归档校验结果。"""
    if not isinstance(result, dict) or not result:
        return
    ok = bool(result.get("ok"))
    with st.container(border=True):
        st.markdown("**归档校验结果**")
        ui.status_alert(
            "PASS" if ok else "ERROR",
            "归档文件哈希与 manifest 一致，未被篡改。" if ok else "归档文件校验未通过，请检查 tampered / missing 明细。",
        )
        files = result.get("files") or []
        if files:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "文件": item.get("name"),
                            "期望哈希": AA.short_hash(item.get("expected"), 16),
                            "实际哈希": AA.short_hash(item.get("actual"), 16),
                            "结果": "✅ 一致" if item.get("ok") else "❌ 不一致",
                        }
                        for item in files
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
        for name in result.get("tampered") or []:
            st.error(f"文件被篡改：{name}")
        for name in result.get("missing") or []:
            st.warning(f"文件缺失：{name}")
        st.caption(f"校验时间：{result.get('checked_at') or '—'}")
        theme.disclaimer(result.get("disclaimer"))


def _render_archive_panel(period: str) -> None:
    """申报包归档面板：归档按钮、结果、校验与历史归档列表。"""
    ui.section(
        "📦 申报包归档",
        "归档生成 Excel + PDF + SHA-256 manifest（只读快照），可随时校验是否被篡改；"
        "归档不改变期间状态，建议结账后再归档。",
    )
    ready = AA.archiving_ready()
    closure = AA.quarter_closure(period)
    cols = st.columns([1, 2, 2, 4])
    with cols[0]:
        include_pdf = st.checkbox("同时生成 PDF", value=True, key="filing_archive_pdf")
    with cols[1]:
        note = st.text_input("归档备注（可选）", key="filing_archive_note", placeholder="如：2026Q1 归档")
    with cols[2]:
        st.write("")
        if st.button(
            "📦 归档本期申报包",
            key="filing_archive_run",
            type="primary",
            width="stretch",
            disabled=not ready,
            help="生成 Excel + PDF + manifest 并复制到 data/archive/{期间}",
        ):
            _run_archive(period, include_pdf, note)
    with cols[3]:
        if not ready:
            ui.module_missing("申报包归档", "archiving.filing_archive", detail=AA.missing_detail())
        elif not closure.get("all_closed") and closure.get("quarter_status") == "未结账":
            st.caption("提示：本期尚未完成结账。归档仍可执行，但建议先结账再归档，保证归档包与账面一致。")

    result = st.session_state.get(STATE_ARCHIVE_RESULT)
    if result:
        _render_archive_result(result)

    try:
        archives = AA.list_archives()
    except Exception as exc:  # noqa: BLE001
        ui.failure("读取归档记录失败。", exc, show_traceback=False)
        archives = []
    period_archives = [row for row in archives if str(row.get("period") or "") == str(period)]
    verify_cols = st.columns([2, 2, 5])
    with verify_cols[0]:
        if st.button(
            "🔍 校验归档",
            key="filing_verify_archive",
            width="stretch",
            disabled=not (AA.archive_verify_ready() and period_archives),
            help="按 manifest 重新计算文件 SHA-256，检测篡改/缺失",
        ):
            target = period_archives[0]
            _run_verify_archive(str(target.get("id") or target.get("path") or ""))
    with verify_cols[1]:
        st.caption(f"本期归档记录：{len(period_archives)} 条")
    with verify_cols[2]:
        st.caption(f"归档目录：{AA.archive_dir()}")

    verify_result = st.session_state.get(STATE_ARCHIVE_VERIFY)
    if verify_result:
        _render_archive_verify(verify_result)

    with st.expander(f"🗂️ 历史归档列表（{len(archives)} 条）", expanded=bool(archives)):
        if not archives:
            st.info("暂无归档记录。点击上方「📦 归档本期申报包」生成第一份归档。", icon="🗄️")
            return
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "期间": row.get("period"),
                        "类型": row.get("kind"),
                        "创建时间": row.get("created_at"),
                        "文件": row.get("file_name") or Path(str(row.get("path") or "")).name,
                        "大小": row.get("size_text") or AA.fmt_size(row.get("size_bytes")),
                        "哈希摘要": row.get("file_hash_short") or AA.short_hash(row.get("file_hash")),
                        "文件存在": "是" if row.get("exists") else "否",
                    }
                    for row in archives[:20]
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        verify_missing = not AA.archive_verify_ready()
        for index, row in enumerate(archives[:5]):
            row_label = f"{row.get('period')} · {row.get('kind')} · {row.get('created_at') or '—'}"
            if st.button(
                f"🔍 校验 {row_label}",
                key=f"filing_verify_archive_row_{index}_{row.get('id') or index}",
                disabled=verify_missing,
                width="content",
            ):
                _run_verify_archive(str(row.get("id") or row.get("path") or ""))
    theme.disclaimer()


# =============================================================================
# 页面入口
# =============================================================================
def render() -> None:
    """渲染申报填报页。"""
    from ui.components import explainers

    header_period = str(st.session_state.get("filing_period") or current_quarter())
    try:
        archived_hint = "已归档" if AA.has_archive(header_period) else "未归档"
    except Exception:  # noqa: BLE001
        archived_hint = "未归档"
    ui.page_header(
        "申报填报",
        "生成、校验、导出并跟踪季度申报表；申报数据全部来自本地台账与确定性算税引擎。",
        icon="📋",
        right_text=(
            f"当前季度：{period_label(header_period)} · 结账：{_closure_text(header_period)} · 归档：{archived_hint}"
        ),
    )
    explainers.basics()
    _show_flash()

    modules = _modules()
    if modules["registry"] is None:
        ui.module_missing("申报表生成", "filing.registry", detail=runtime.error_of("filing.registry"))

    periods = list(quarter_options(count=8))
    default_index = periods.index(header_period) if header_period in periods else 0
    cols = st.columns([2, 2, 2])
    with cols[0]:
        period = st.selectbox("税款所属期", periods, index=default_index, key="filing_period")
    with cols[1]:
        st.write("")
        if st.button("🔄 生成申报表", key="filing_generate", type="primary", width="stretch"):
            _generate(period)
    with cols[2]:
        st.write("")
        st.caption("生成后请逐表核对栏次，再导出 / 保存草稿 / 标记已申报。")

    _render_closure_badges(period)

    bundles = list(st.session_state.get(STATE_BUNDLES, {}).get(period, []) or [])
    checks = st.session_state.get(STATE_CHECKS, {}).get(period, {}) or {}
    if not bundles:
        st.info(
            "尚未生成本期间的申报表。点击「生成申报表」后，将自动汇总收入台账、费用台账、研发辅助账与合同台账，"
            "生成增值税、企业所得税预缴、财务报表与印花税申报表。",
            icon="📋",
        )
    else:
        ui.metric_row(
            [
                {"label": "申报表数量", "value": f"{len(bundles)}"},
                {"label": "校验通过", "value": f"{sum(1 for c in checks.values() if str(c.get('status')) == 'PASS')}"},
                {"label": "需关注", "value": f"{sum(1 for c in checks.values() if str(c.get('status')) in {'WARNING', 'ERROR'})}"},
            ],
            columns=3,
        )
        for index, bundle in enumerate(bundles):
            form_id = str(bundle.get("form_id"))
            _render_bundle(bundle, checks.get(form_id) or {}, period, index)
        _render_export(period, bundles)

    _render_archive_panel(period)
    _render_progress(period, bundles)
    _render_guides_summary(bundles)
    _render_annual_other_panel()
    theme.disclaimer()


def _render_annual_other_panel() -> None:
    """年度辅助申报：工商年报数据准备、残保金测算（P1-D，可选辅助）。"""
    try:
        from filing import annual_report, disability_fund
    except Exception as exc:  # noqa: BLE001 - 模块缺失时不影响主申报流程
        st.warning(f"工商年报/残保金模块不可用：{exc}")
        return

    with st.expander("📄 工商年报 / 残保金（年度辅助，不代替申报）", expanded=False):
        st.caption(
            "工商年报：每年 1 月 1 日—6 月 30 日在国家企业信用信息公示系统报送；"
            "残保金：按年向残联/税务申报，具体期限以当地公告为准。系统只做数据准备与测算。"
        )
        now_year = datetime.now().year
        year = st.selectbox(
            "报告年度",
            [str(now_year), str(now_year - 1)],
            index=0,
            key="annual_other_year",
            format_func=lambda value: f"{value} 年度",
        )
        col_report, col_fund = st.columns(2)

        with col_report:
            ui.section("工商年报数据准备", "企业基本信息 + 资产状况估算 + 校验/操作步骤")
            if st.button("📄 生成工商年报数据准备表", key="annual_report_build", width="stretch"):
                try:
                    st.session_state["annual_report_bundle"] = annual_report.build_annual_report(year)
                except Exception as exc:  # noqa: BLE001
                    st.error(f"生成失败：{exc}")
            bundle = st.session_state.get("annual_report_bundle")
            if bundle:
                totals = bundle.get("totals") or {}
                ui.metric_row(
                    [
                        {"label": "资产总额（估）", "value": F.fmt_money(totals.get("assets"))},
                        {"label": "营业总收入", "value": F.fmt_money(totals.get("revenue"))},
                        {"label": "从业人数", "value": f"{int(totals.get('employees') or 0)} 人"},
                    ],
                    columns=3,
                )
                for section in bundle.get("sections") or []:
                    with st.expander(section.get("title"), expanded=False):
                        st.dataframe(
                            pd.DataFrame(
                                [
                                    {
                                        "行次": line.get("line_no"),
                                        "项目": line.get("name"),
                                        "金额/内容": line.get("value_text"),
                                        "来源/口径": line.get("source") or line.get("rule"),
                                    }
                                    for line in section.get("lines") or []
                                ]
                            ),
                            width="stretch",
                            hide_index=True,
                        )
                for item in bundle.get("checks") or []:
                    getattr(ui, "status_alert", st.info)(item.get("status"), f"{item.get('field')}：{item.get('message')}")
                with st.expander("报送操作步骤", expanded=False):
                    for index, step in enumerate(bundle.get("guide") or [], start=1):
                        st.markdown(f"{index}. {step}")
                try:
                    path = annual_report.export_annual_report_excel(bundle)
                    st.download_button(
                        "📊 导出工商年报数据准备 Excel",
                        data=Path(path).read_bytes(),
                        file_name=Path(path).name,
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key="annual_report_download",
                        width="stretch",
                    )
                except Exception as exc:  # noqa: BLE001
                    st.warning(f"导出失败：{exc}")

        with col_fund:
            ui.section("残疾人就业保障金", "1.5% 比例 + 分档减缴（50%/90%）+ 30 人以下免征")
            fund_employees = st.number_input("上年在职职工人数", min_value=0, value=30, step=1, key="fund_employees")
            fund_wage = st.number_input("上年在职职工年平均工资（元）", min_value=0.0, value=80_000.0, step=1_000.0, key="fund_wage")
            fund_disabled = st.number_input("实际安排残疾人就业人数", min_value=0.0, value=0.0, step=0.5, key="fund_disabled")
            fund_local = st.number_input(
                "当地社会平均工资（元，可选，用于 2 倍封顶核对）", min_value=0.0, value=0.0, step=1_000.0, key="fund_local"
            )
            if st.button("🧮 测算残保金", key="fund_build", width="stretch"):
                try:
                    st.session_state["disability_fund_bundle"] = disability_fund.build_disability_fund(
                        year,
                        employees=int(fund_employees),
                        average_wage=float(fund_wage),
                        disabled_employed=float(fund_disabled),
                        local_average_wage=float(fund_local) or None,
                    )
                except Exception as exc:  # noqa: BLE001
                    st.error(f"测算失败：{exc}")
            fund_bundle = st.session_state.get("disability_fund_bundle")
            if fund_bundle:
                totals = fund_bundle.get("totals") or {}
                ui.metric_row(
                    [
                        {"label": "本期应缴", "value": F.fmt_money(totals.get("payable"))},
                        {"label": "应缴基数", "value": F.fmt_money(totals.get("base_amount"))},
                        {"label": "减缴比例", "value": f"{float(totals.get('discount_rate') or 0) * 100:.0f}%"},
                    ],
                    columns=3,
                )
                st.success((fund_bundle.get("summary") or {}).get("text") or "残保金测算完成")
                for section in fund_bundle.get("sections") or []:
                    with st.expander(section.get("title"), expanded=False):
                        st.dataframe(
                            pd.DataFrame(
                                [
                                    {
                                        "行次": line.get("line_no"),
                                        "项目": line.get("name"),
                                        "金额/内容": line.get("value_text"),
                                        "口径/依据": line.get("rule") or line.get("source"),
                                    }
                                    for line in section.get("lines") or []
                                ]
                            ),
                            width="stretch",
                            hide_index=True,
                        )
                for item in fund_bundle.get("checks") or []:
                    st.caption(f"{item.get('status')} · {item.get('field')}：{item.get('message')}")
                with st.expander("申报操作步骤", expanded=False):
                    for index, step in enumerate(fund_bundle.get("guide") or [], start=1):
                        st.markdown(f"{index}. {step}")
                try:
                    path = disability_fund.export_disability_fund_excel(fund_bundle)
                    st.download_button(
                        "📊 导出残保金测算 Excel",
                        data=Path(path).read_bytes(),
                        file_name=Path(path).name,
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key="fund_download",
                        width="stretch",
                    )
                except Exception as exc:  # noqa: BLE001
                    st.warning(f"导出失败：{exc}")
