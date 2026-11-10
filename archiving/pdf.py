"""申报表归档 PDF 生成（reportlab + ``UnicodeCIDFont("STSong-Light")`` 中文字体）。

PDF 结构
--------
1. 封面：企业名称、纳税人识别号、税款所属期、生成时间、应用版本、政策依据、
   免责声明；
2. 每张申报表一页（过长自动分页）：表名 + 栏次/名称/金额/数据来源/填报规则表格，
   并附「合计数据」表；
3. 汇总校验结果：PASS/WARNING/ERROR 条数 + 逐表校验明细；
4. 每页页脚：应用版本、免责声明、页码。

reportlab 未安装或中文字体加载失败时抛 :class:`PdfUnavailableError`，由
:func:`archiving.filing_archive.archive_filing_package` 捕获并降级为
「仅 Excel + manifest」，同时在返回的 ``warnings`` 中说明。
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import config

FONT_NAME = "STSong-Light"

try:  # pragma: no cover - 依赖是否安装由测试覆盖降级分支
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
    from reportlab.platypus import LongTable

    REPORTLAB_AVAILABLE = True
except Exception:  # noqa: BLE001 - reportlab 缺失时整体降级
    REPORTLAB_AVAILABLE = False
    colors = None  # type: ignore[assignment]
    TA_CENTER = TA_LEFT = 0  # type: ignore[assignment]
    A4 = (595.27, 841.89)  # type: ignore[assignment]
    ParagraphStyle = getSampleStyleSheet = None  # type: ignore[assignment]
    mm = 72 / 25.4  # type: ignore[assignment]
    pdfmetrics = UnicodeCIDFont = None  # type: ignore[assignment]
    PageBreak = Paragraph = SimpleDocTemplate = Spacer = Table = TableStyle = None  # type: ignore[assignment]
    LongTable = None  # type: ignore[assignment]


class PdfUnavailableError(RuntimeError):
    """reportlab 缺失或中文字体加载失败，无法生成 PDF。"""


_font_ready = False


def _ensure_font() -> str:
    """注册并返回中文字体名（幂等）；失败时抛 :class:`PdfUnavailableError`。"""
    global _font_ready
    if not REPORTLAB_AVAILABLE:
        raise PdfUnavailableError(
            "未安装 reportlab（或缺少中文字体支持），无法生成归档 PDF；请执行 pip install reportlab 后重试"
        )
    if not _font_ready:
        try:
            pdfmetrics.registerFont(UnicodeCIDFont(FONT_NAME))
            _font_ready = True
        except Exception as exc:  # noqa: BLE001 - 字体加载失败统一降级
            raise PdfUnavailableError(f"归档 PDF 中文字体（{FONT_NAME}）加载失败：{exc}") from exc
    return FONT_NAME


# =============================================================================
# 文本/金额格式化
# =============================================================================
def _safe_text(value: Any) -> str:
    """把任意值转成可被 CID 中文字体渲染的安全文本（去 emoji / 非常用符号）。"""
    text = "" if value is None else str(value)
    for source, target in (("⚠️", "【提示】"), ("⚠", "【提示】"), ("\ufe0f", "")):
        text = text.replace(source, target)
    # CID 字体无彩色 emoji 字形，剔除 BMP 之外字符，避免出现方框/乱码
    return "".join(char for char in text if ord(char) <= 0xFFFF)


def _money(value: Any) -> str:
    """金额格式化：千分位 + 2 位小数；无法转换时原样返回安全文本。"""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return _safe_text(value)


def _label_for_total(key: str) -> str:
    """合计项中文名称（复用申报 Excel 导出的口径，缺失时回退字段名）。"""
    try:
        from filing.export import _TOTAL_LABELS  # noqa: PLC0415 - 延迟导入避免循环

        return str(_TOTAL_LABELS.get(key) or key)
    except Exception:  # noqa: BLE001 - 导出模块不可用不应影响 PDF
        return str(key)


def _period_text(period: Any, bundles: list[dict[str, Any]]) -> str:
    for bundle in bundles:
        label = str(bundle.get("period_label") or "").strip()
        if label:
            return f"{label}（{period}）"
    return str(period or "")


# =============================================================================
# 样式
# =============================================================================
def _styles() -> dict[str, Any]:
    _ensure_font()
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "ArchiveTitle",
            parent=base["Title"],
            fontName=FONT_NAME,
            fontSize=20,
            leading=28,
            alignment=TA_CENTER,
            spaceAfter=8,
            textColor=colors.HexColor("#1F4E79"),
        ),
        "subtitle": ParagraphStyle(
            "ArchiveSubtitle",
            parent=base["Normal"],
            fontName=FONT_NAME,
            fontSize=11,
            leading=18,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#2E75B6"),
        ),
        "h2": ParagraphStyle(
            "ArchiveH2",
            parent=base["Heading2"],
            fontName=FONT_NAME,
            fontSize=14,
            leading=20,
            spaceBefore=6,
            spaceAfter=6,
            textColor=colors.HexColor("#1F4E79"),
        ),
        "h3": ParagraphStyle(
            "ArchiveH3",
            parent=base["Heading3"],
            fontName=FONT_NAME,
            fontSize=11,
            leading=16,
            spaceBefore=4,
            spaceAfter=4,
            textColor=colors.HexColor("#2E75B6"),
        ),
        "normal": ParagraphStyle(
            "ArchiveNormal",
            parent=base["Normal"],
            fontName=FONT_NAME,
            fontSize=9,
            leading=14,
            alignment=TA_LEFT,
        ),
        "small": ParagraphStyle(
            "ArchiveSmall",
            parent=base["Normal"],
            fontName=FONT_NAME,
            fontSize=8,
            leading=12,
        ),
        "cell": ParagraphStyle(
            "ArchiveCell",
            parent=base["Normal"],
            fontName=FONT_NAME,
            fontSize=7.5,
            leading=10,
        ),
        "cell_bold": ParagraphStyle(
            "ArchiveCellBold",
            parent=base["Normal"],
            fontName=FONT_NAME,
            fontSize=7.5,
            leading=10,
            textColor=colors.white,
        ),
        "note": ParagraphStyle(
            "ArchiveNote",
            parent=base["Normal"],
            fontName=FONT_NAME,
            fontSize=8,
            leading=12,
            textColor=colors.HexColor("#7F7F7F"),
        ),
    }


def _para(text: Any, style: Any) -> Any:
    return Paragraph(_safe_text(text), style)


# =============================================================================
# 分节构建
# =============================================================================
def _cover(period: Any, bundles: list[dict[str, Any]], styles: dict[str, Any]) -> list[Any]:
    company = config.get_company()
    policy_basis = ""
    policy_version = ""
    for bundle in bundles:
        policy_basis = policy_basis or str(bundle.get("policy_basis_text") or "")
        policy_version = policy_version or str(bundle.get("policy_version") or "")
    story: list[Any] = [
        Spacer(1, 30 * mm),
        _para("申报表归档包", styles["title"]),
        _para("税小智 · 小规模纳税人自主税务填报系统", styles["subtitle"]),
        Spacer(1, 12 * mm),
    ]
    rows = [
        ("企业名称", company.get("name") or ""),
        ("纳税人识别号", company.get("unified_social_credit_code") or ""),
        ("纳税人类型", company.get("taxpayer_type") or "小规模纳税人"),
        ("税款所属期", _period_text(period, bundles)),
        ("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("应用版本", f"税小智 {config.APP_VERSION}"),
        ("政策依据", policy_basis or "（未获取到政策依据）"),
        ("政策版本", policy_version or "—"),
        ("申报表数量", f"{len(bundles)} 张"),
        ("免责声明", config.DISCLAIMER),
    ]
    data = [
        [_para(label, styles["cell"]), _para(value, styles["cell"])]
        for label, value in rows
    ]
    table = Table(data, colWidths=[34 * mm, 130 * mm])
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), FONT_NAME),
                ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#DDEBF7")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#BFBFBF")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.extend([table, Spacer(1, 14 * mm)])
    story.append(
        _para(
            "本 PDF 由本地确定性程序生成，与「申报包」Excel、manifest.json（含逐文件 SHA-256）"
            "共同构成申报留档；正式申报以电子税务局回执与主管税务机关核定为准。",
            styles["note"],
        )
    )
    story.append(PageBreak())
    return story


def _form_story(
    index: int,
    total: int,
    bundle: dict[str, Any],
    styles: dict[str, Any],
) -> list[Any]:
    story: list[Any] = [
        _para(
            f"申报表 {index}/{total}：{bundle.get('form_name') or bundle.get('form_id') or '申报表'}",
            styles["h2"],
        ),
        _para(
            f"报表编码：{bundle.get('form_id') or ''}　所属期间："
            f"{bundle.get('period_label') or ''}（{bundle.get('period') or ''}）　"
            f"报表状态：{bundle.get('status') or '草稿'}　生成时间：{bundle.get('generated_at') or ''}",
            styles["small"],
        ),
        Spacer(1, 2 * mm),
    ]
    header = ["栏次", "项目", "金额（元）", "数据来源", "填报规则"]
    data: list[list[Any]] = [
        [_para(cell, styles["cell_bold"]) for cell in header]
    ]
    section_rows: list[int] = []
    for section in bundle.get("sections") or []:
        section_rows.append(len(data))
        data.append([_para(section.get("title") or "表格", styles["normal"]), "", "", "", ""])
        for line in section.get("lines") or []:
            data.append(
                [
                    _para(line.get("line_no") or "", styles["cell"]),
                    _para(line.get("name") or "", styles["cell"]),
                    _para(_money(line.get("value")), styles["cell"]),
                    _para(line.get("source") or "", styles["cell"]),
                    _para(line.get("rule") or "", styles["cell"]),
                ]
            )
    table = LongTable(
        data,
        colWidths=[13 * mm, 42 * mm, 24 * mm, 44 * mm, 47 * mm],
        repeatRows=1,
    )
    style_commands = [
        ("FONTNAME", (0, 0), (-1, -1), FONT_NAME),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2E75B6")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#BFBFBF")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (2, 1), (2, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]
    for row_index in section_rows:
        style_commands.extend(
            [
                ("SPAN", (0, row_index), (-1, row_index)),
                ("BACKGROUND", (0, row_index), (-1, row_index), colors.HexColor("#DDEBF7")),
            ]
        )
    table.setStyle(TableStyle(style_commands))
    story.append(table)

    totals = bundle.get("totals") or {}
    total_rows = [
        (key, value)
        for key, value in totals.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    if total_rows:
        story.extend([Spacer(1, 3 * mm), _para("合计数据", styles["h3"])])
        total_data = [
            [
                _para("合计项", styles["cell_bold"]),
                _para("金额（元）", styles["cell_bold"]),
            ]
        ]
        for key, value in total_rows:
            total_data.append(
                [
                    _para(_label_for_total(str(key)), styles["cell"]),
                    _para(_money(value), styles["cell"]),
                ]
            )
        total_table = LongTable(total_data, colWidths=[100 * mm, 40 * mm], repeatRows=1)
        total_table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), FONT_NAME),
                    ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2E75B6")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#BFBFBF")),
                    ("ALIGN", (1, 1), (1, -1), "RIGHT"),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ]
            )
        )
        story.append(total_table)
    story.append(PageBreak())
    return story


def _checks_story(checks: Any, styles: dict[str, Any]) -> list[Any]:
    reports: list[dict[str, Any]] = []
    if isinstance(checks, list):
        reports = [item for item in checks if isinstance(item, dict)]
    elif isinstance(checks, dict):
        reports = [item for item in checks.get("forms") or [] if isinstance(item, dict)]
    counts = {"PASS": 0, "WARNING": 0, "ERROR": 0}
    for report in reports:
        status = str(report.get("status") or "PASS").upper()
        counts[status if status in counts else "PASS"] += 1
    story: list[Any] = [
        _para("校验结果汇总", styles["h2"]),
        _para(
            f"PASS {counts['PASS']} 项　WARNING {counts['WARNING']} 项　ERROR {counts['ERROR']} 项"
            "（按申报表统计；ERROR 需修正后再正式申报）",
            styles["normal"],
        ),
        Spacer(1, 3 * mm),
    ]
    for index, report in enumerate(reports, start=1):
        status = str(report.get("status") or "PASS").upper()
        story.append(
            _para(
                f"{index}. {report.get('form_name') or report.get('form_id') or '申报表'}："
                f"{status} — {report.get('summary') or ''}",
                styles["h3"],
            )
        )
        details = [item for item in report.get("details") or [] if isinstance(item, dict)]
        if not details:
            story.append(_para("　（无校验明细）", styles["small"]))
            continue
        data: list[list[Any]] = [
            [
                _para("校验项目", styles["cell_bold"]),
                _para("状态", styles["cell_bold"]),
                _para("标准值", styles["cell_bold"]),
                _para("实际值", styles["cell_bold"]),
                _para("说明", styles["cell_bold"]),
            ]
        ]
        for item in details:
            data.append(
                [
                    _para(item.get("field") or "", styles["cell"]),
                    _para(item.get("status") or "", styles["cell"]),
                    _para(_money(item.get("expected")) if isinstance(item.get("expected"), (int, float)) else "", styles["cell"]),
                    _para(_money(item.get("actual")) if isinstance(item.get("actual"), (int, float)) else "", styles["cell"]),
                    _para(item.get("message") or "", styles["cell"]),
                ]
            )
        table = LongTable(data, colWidths=[28 * mm, 16 * mm, 22 * mm, 22 * mm, 82 * mm], repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), FONT_NAME),
                    ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2E75B6")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#BFBFBF")),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ]
            )
        )
        story.append(table)
        story.append(Spacer(1, 2 * mm))
    story.extend(
        [
            Spacer(1, 4 * mm),
            _para("免责声明", styles["h3"]),
            _para(config.DISCLAIMER, styles["normal"]),
        ]
    )
    return story


# =============================================================================
# 对外接口
# =============================================================================
def build_filing_pdf(
    period: Any,
    bundles: list[dict[str, Any]] | None = None,
    checks: Any = None,
    path: Any = None,
) -> Path:
    """生成申报表归档 PDF，返回文件路径。

    参数
    ----
    period : 税款所属期（如 ``2026Q1``）；
    bundles : :func:`filing.registry.build_all` 返回的申报表 bundle 列表；
    checks : 校验结果（每表 ``validate_bundle`` 报告组成的列表，或含 ``forms`` 的字典）；
    path : 输出路径，默认写入 ``config.EXPORT_DIR``。
    """
    if not REPORTLAB_AVAILABLE:
        raise PdfUnavailableError(
            "未安装 reportlab（或缺少中文字体支持），无法生成归档 PDF；请执行 pip install reportlab 后重试"
        )
    font = _ensure_font()
    styles = _styles()
    bundle_list = [item for item in (bundles or []) if isinstance(item, dict)]
    text = str(period or "")
    if not text and bundle_list:
        text = str(bundle_list[0].get("period") or "")
    if path is None:
        target = Path(config.EXPORT_DIR) / (
            f"申报表_{text or '期间'}_{datetime.now():%Y%m%d_%H%M%S}.pdf"
        )
    else:
        target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    def _footer(canvas, doc) -> None:  # noqa: ANN001 - reportlab 回调
        canvas.saveState()
        canvas.setFont(font, 8)
        canvas.setStrokeColor(colors.HexColor("#BFBFBF"))
        canvas.line(18 * mm, 16 * mm, A4[0] - 18 * mm, 16 * mm)
        canvas.drawString(
            18 * mm,
            11 * mm,
            _safe_text(f"税小智 {config.APP_VERSION} · {config.DISCLAIMER}"),
        )
        canvas.drawRightString(A4[0] - 18 * mm, 11 * mm, f"第 {doc.page} 页")
        canvas.restoreState()

    story: list[Any] = []
    story.extend(_cover(text, bundle_list, styles))
    total = len(bundle_list)
    for index, bundle in enumerate(bundle_list, start=1):
        story.extend(_form_story(index, total, bundle, styles))
    story.extend(_checks_story(checks, styles))
    story.append(Spacer(1, 8 * mm))
    story.append(_para(config.DISCLAIMER, styles["note"]))

    document = SimpleDocTemplate(
        str(target),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=22 * mm,
        title=_safe_text(f"申报表归档_{text}"),
        author=_safe_text(config.APP_SHORT_NAME),
        subject=_safe_text(f"{text} 申报表归档 PDF"),
    )
    document.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return target


def build_filing_pdf_to_temp(period: Any, bundles: list[dict[str, Any]], checks: Any = None) -> Path:
    """在临时目录生成 PDF（供归档流程复制到归档目录，避免残留临时文件）。"""
    temp_dir = Path(tempfile.mkdtemp(prefix="tax_filing_pdf_"))
    return build_filing_pdf(
        period,
        bundles,
        checks=checks,
        path=temp_dir / f"申报表_{period}.pdf",
    )


__all__ = [
    "REPORTLAB_AVAILABLE",
    "PdfUnavailableError",
    "build_filing_pdf",
    "build_filing_pdf_to_temp",
]
