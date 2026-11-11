"""操作指引 PDF 导出（自绘内容，不包含电子税务局截图）。

复用 reportlab + ``STSong-Light`` 中文字体；reportlab 缺失时抛
:class:`GuidePdfUnavailableError`，由 UI 降级提示。
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import config
from guidebooks.content import GUIDE_DISCLAIMER

try:  # pragma: no cover - 依赖安装情况由测试覆盖
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    REPORTLAB_AVAILABLE = True
except Exception:  # noqa: BLE001
    REPORTLAB_AVAILABLE = False
    colors = None  # type: ignore[assignment]
    TA_CENTER = TA_LEFT = 0  # type: ignore[assignment]
    A4 = (595.27, 841.89)  # type: ignore[assignment]
    ParagraphStyle = getSampleStyleSheet = None  # type: ignore[assignment]
    mm = 72 / 25.4  # type: ignore[assignment]
    pdfmetrics = UnicodeCIDFont = None  # type: ignore[assignment]
    PageBreak = Paragraph = SimpleDocTemplate = Spacer = Table = TableStyle = None  # type: ignore[assignment]

FONT_NAME = "STSong-Light"
_font_ready = False


class GuidePdfUnavailableError(RuntimeError):
    """reportlab 缺失或中文字体加载失败。"""


def _ensure_font() -> str:
    global _font_ready
    if not REPORTLAB_AVAILABLE:
        raise GuidePdfUnavailableError("未安装 reportlab，无法导出操作指引 PDF。")
    if not _font_ready:
        try:
            pdfmetrics.registerFont(UnicodeCIDFont(FONT_NAME))
            _font_ready = True
        except Exception as exc:  # noqa: BLE001
            raise GuidePdfUnavailableError(f"操作指引 PDF 中文字体加载失败：{exc}") from exc
    return FONT_NAME


def _safe(value: Any) -> str:
    text = "" if value is None else str(value)
    for source, target in (("⚠️", "【提示】"), ("⚠", "【提示】"), ("\ufe0f", "")):
        text = text.replace(source, target)
    return "".join(char for char in text if ord(char) <= 0xFFFF)


def _styles() -> dict[str, Any]:
    font = _ensure_font()
    sample = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "GuideTitle", parent=sample["Title"], fontName=font,
            fontSize=20, leading=26, alignment=TA_CENTER, spaceAfter=8,
        ),
        "subtitle": ParagraphStyle(
            "GuideSub", parent=sample["Normal"], fontName=font,
            fontSize=10.5, leading=16, textColor=colors.HexColor("#4b5563"),
        ),
        "h2": ParagraphStyle(
            "GuideH2", parent=sample["Heading2"], fontName=font,
            fontSize=15, leading=21, textColor=colors.HexColor("#0f766e"), spaceBefore=10, spaceAfter=6,
        ),
        "h3": ParagraphStyle(
            "GuideH3", parent=sample["Heading3"], fontName=font,
            fontSize=12.5, leading=18, textColor=colors.HexColor("#111827"), spaceBefore=8, spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "GuideBody", parent=sample["Normal"], fontName=font,
            fontSize=10, leading=15, alignment=TA_LEFT,
        ),
        "small": ParagraphStyle(
            "GuideSmall", parent=sample["Normal"], fontName=font,
            fontSize=8.8, leading=13, textColor=colors.HexColor("#6b7280"),
        ),
        "cell": ParagraphStyle(
            "GuideCell", parent=sample["Normal"], fontName=font,
            fontSize=9.2, leading=13.5,
        ),
    }


def _footer(canvas: Any, doc: Any) -> None:
    canvas.saveState()
    canvas.setFont(FONT_NAME, 8)
    canvas.setFillColor(colors.HexColor("#9ca3af"))
    canvas.drawString(18 * mm, 12 * mm, f"{config.APP_NAME} · v{config.APP_VERSION} · 自绘操作指引")
    canvas.drawRightString(A4[0] - 18 * mm, 12 * mm, f"第 {doc.page} 页")
    canvas.restoreState()


def _link_text(links: list[dict[str, str]]) -> str:
    return "；".join(f"{item.get('label')}：{item.get('url')}" for item in links if item.get("url"))


def build_guide_pdf(workflow_ids: list[str] | None = None, path: str | Path | None = None) -> Path:
    """导出操作指引 PDF；默认导出全部 3 个流程。"""
    from guidebooks.content import get_workflow, list_workflows

    _ensure_font()
    ids = list(workflow_ids or [item["id"] for item in list_workflows()])
    workflows = [get_workflow(key) for key in ids]
    workflows = [item for item in workflows if item]
    if not workflows:
        raise ValueError("没有可导出的操作指引")
    target = Path(path) if path else Path(tempfile.gettempdir()) / f"操作指引_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
    styles = _styles()
    doc = SimpleDocTemplate(
        str(target), pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title="智小税 · 新手操作指引", author=config.APP_NAME,
    )
    story: list[Any] = [
        Paragraph(_safe("智小税 · 新手操作指引"), styles["title"]),
        Paragraph(_safe(f"{config.APP_NAME} · v{config.APP_VERSION} · 生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M')}"), styles["small"]),
        Spacer(1, 6),
        Paragraph(
            _safe("本 PDF 为自绘操作地图与字段说明，不是电子税务局官方截图；不同省份界面可能不同，"
                  "请以当地电子税务局实际页面为准。"),
            styles["subtitle"],
        ),
        Spacer(1, 10),
    ]
    for workflow_index, workflow in enumerate(workflows):
        if workflow_index:
            story.append(PageBreak())
        story.append(Paragraph(_safe(f"{workflow.get('icon', '')} {workflow.get('title', '')}"), styles["h2"]))
        story.append(Paragraph(_safe(workflow.get("subtitle", "")), styles["subtitle"]))
        links = _link_text(workflow.get("official_links") or [])
        if links:
            story.append(Spacer(1, 3))
            story.append(Paragraph(_safe("官方入口：" + links), styles["small"]))
        for index, step in enumerate(workflow.get("steps") or [], start=1):
            story.append(Spacer(1, 6))
            story.append(Paragraph(_safe(f"第 {index} 步 · {step.get('title', '')}"), styles["h3"]))
            story.append(Paragraph(_safe("菜单路径：" + str(step.get("menu_path") or "")), styles["body"]))
            fields = step.get("fields") or []
            if fields:
                rows = [[Paragraph("字段/项目", styles["cell"]), Paragraph("怎么填/注意什么", styles["cell"])]]
                for name, note in fields:
                    rows.append([Paragraph(_safe(name), styles["cell"]), Paragraph(_safe(note), styles["cell"])])
                table = Table(rows, colWidths=[48 * mm, 122 * mm], repeatRows=1)
                table.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6fffb")),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]))
                story.append(Spacer(1, 3))
                story.append(table)
            errors = step.get("errors") or []
            if errors:
                story.append(Spacer(1, 4))
                story.append(Paragraph(_safe("常见错误：" + "；".join(str(item) for item in errors)), styles["body"]))
            tip = str(step.get("tips") or "").strip()
            if tip:
                story.append(Spacer(1, 2))
                story.append(Paragraph(_safe("小贴士：" + tip), styles["small"]))
        story.append(Spacer(1, 8))
        story.append(Paragraph(_safe(GUIDE_DISCLAIMER), styles["small"]))
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return target


__all__ = ["build_guide_pdf", "GuidePdfUnavailableError", "REPORTLAB_AVAILABLE"]
