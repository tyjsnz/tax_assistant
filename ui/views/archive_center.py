"""🗂️ 资料中心：按公司/年/月/类别查找历史导出资料（v1.54）。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

import config
from storage import export_index
from ui.components import downloads, theme, ui


def _format_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _year_month(entry: dict) -> tuple[str, str]:
    rel = str(entry.get("relative_path") or "")
    parts = rel.replace("\\", "/").split("/")
    if len(parts) >= 4:
        return parts[1], parts[2]
    return "", ""


def render() -> None:
    ui.page_header(
        "资料中心",
        "按年/月/类别查找历史导出文件；文件按“所属期 → 年 → 月 → 类别”自动归档。",
        icon="🗂️",
    )
    entries = export_index.scan()
    if not entries:
        st.info(
            "还没有历史导出资料。到任意页面生成/导出文件后，会自动归档到：\n\n"
            f"`{Path(config.EXPORT_DIR)}/{{公司}}/{{年}}/{{月}}/{{类别}}/`"
        )
        st.caption("原始台账仍集中保存在 companies/{公司}/ 下，不按月拆分。")
        theme.disclaimer()
        return

    for entry in entries:
        year, month = _year_month(entry)
        entry["_year"], entry["_month"] = year, month
    years = sorted({entry["_year"] for entry in entries if entry["_year"]}, reverse=True)
    categories = sorted({str(entry.get("category") or "") for entry in entries if entry.get("category")})
    filters = st.columns([1, 1, 1, 2])
    with filters[0]:
        year = st.selectbox("年份", ["全部"] + years, key="archive_filter_year")
    with filters[1]:
        month_options = ["全部"] + sorted({entry["_month"] for entry in entries if entry["_month"]})
        month = st.selectbox("月份 / 季度", month_options, key="archive_filter_month")
    with filters[2]:
        category = st.selectbox("类别", ["全部"] + categories, key="archive_filter_category")
    with filters[3]:
        keyword = st.text_input("搜索文件名 / 所属期", key="archive_filter_keyword", placeholder="如：申报包、2026-10")

    filtered = []
    for entry in entries:
        if year != "全部" and entry["_year"] != year:
            continue
        if month != "全部" and entry["_month"] != month:
            continue
        if category != "全部" and str(entry.get("category")) != category:
            continue
        haystack = f"{entry.get('file_name')} {entry.get('period')} {entry.get('category')}"
        if keyword and keyword not in haystack:
            continue
        filtered.append(entry)

    total_size = sum(int(entry.get("size") or 0) for entry in filtered)
    ui.metric_row(
        [
            {"label": "资料文件", "value": f"{len(filtered)} 个"},
            {"label": "占用空间", "value": _format_size(total_size)},
            {"label": "最新导出", "value": str(filtered[0].get("exported_at") or "—") if filtered else "—"},
        ],
        columns=3,
    )
    if not filtered:
        st.warning("没有匹配的资料，请调整筛选条件。")
        theme.disclaimer()
        return

    frame = pd.DataFrame([
        {
            "导出时间": entry.get("exported_at"),
            "所属期": entry.get("period") or f"{entry['_year']}-{entry['_month']}",
            "类别": entry.get("category"),
            "文件": entry.get("file_name"),
            "大小": _format_size(int(entry.get("size") or 0)),
            "SHA-256": str(entry.get("sha256") or "")[:12],
        }
        for entry in filtered
    ])
    st.dataframe(frame, hide_index=True, width="stretch", alt="历史导出资料列表")

    options = {
        f"{entry.get('exported_at')} · {entry.get('category')} · {entry.get('file_name')}": entry
        for entry in filtered
    }
    with st.container(border=True):
        selected_label = st.selectbox("选择要取用的资料", list(options), key="archive_selected_file")
        selected = options[selected_label]
        path = Path(config.EXPORT_DIR) / str(selected.get("relative_path") or "")
        st.caption(f"文件位置：{path}")
        if path.exists():
            downloads.download_or_save(
                "取用这份资料",
                file_path=path,
                file_name=path.name,
                mime="application/octet-stream",
                key="archive_center_take",
            )
        else:
            st.warning("文件不存在，可能已被移动或删除。可重新导出。")
    st.caption(
        "原始台账在 companies/{公司}/ 下集中保存；导出文件按所属期归档。"
        "旧的平铺导出文件可用 `scripts/migrate_exports.py` 迁移到 `_legacy` 或按年月整理。"
    )
    theme.disclaimer()
