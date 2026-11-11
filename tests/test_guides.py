"""操作指引内容与 PDF 导出测试（v1.16）。"""

from __future__ import annotations

from pathlib import Path

from guidebooks import content
from guidebooks.pdf import build_guide_pdf


def test_three_workflows_content():
    workflows = content.list_workflows()
    assert [item["id"] for item in workflows] == ["vat", "iit", "correction"]
    for workflow in workflows:
        assert len(workflow["steps"]) == 5
        assert workflow["official_links"]
        for step in workflow["steps"]:
            assert step["title"] and step["menu_path"]
            assert step["fields"] and step["errors"]


def test_build_guide_pdf(tmp_path):
    path = Path(tmp_path) / "guides.pdf"
    result = build_guide_pdf(path=str(path))
    assert result.exists()
    assert result.stat().st_size > 5_000
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(result))
        assert len(reader.pages) >= 3
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        assert "增值税" in text and "离职" in text
    except ImportError:  # pragma: no cover
        pass


def test_pdf_only_selected_workflow(tmp_path):
    path = Path(tmp_path) / "iit.pdf"
    build_guide_pdf(["iit"], path=str(path))
    from pypdf import PdfReader

    text = "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    assert "个税扣缴申报" in text
    assert "增值税及附加税费申报（小规模纳税人" not in text
