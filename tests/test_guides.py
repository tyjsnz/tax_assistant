"""操作指引内容与 PDF 导出测试（v1.16）。"""

from __future__ import annotations

from pathlib import Path

from guidebooks import content
from guidebooks.pdf import build_guide_pdf


def test_six_workflows_content():
    workflows = content.list_workflows()
    assert [item["id"] for item in workflows] == [
        "vat", "iit", "cit_prepay", "settlement", "first_filing", "correction"
    ]
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


def test_local_shot_save_list_delete(tmp_path):
    import base64

    from guidebooks import store as shot_store

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    record = shot_store.save_shot("vat", 2, png, "my-vat.png", "这里填不含税销售额")
    path = shot_store.resolve_path(record)
    assert path.exists()
    shots = shot_store.list_shots("vat", 2)
    assert any(item["id"] == record["id"] for item in shots)
    shot_store.delete_shot(record["id"])
    assert not path.exists()
    assert not any(item["id"] == record["id"] for item in shot_store.list_shots("vat", 2))


def test_local_shot_rejects_bad_type_and_size():
    import pytest

    from guidebooks import store as shot_store

    with pytest.raises(ValueError):
        shot_store.save_shot("vat", 1, b"abc", "evil.exe")
    with pytest.raises(ValueError):
        shot_store.save_shot("vat", 1, b"x" * (shot_store.MAX_BYTES + 1), "big.png")


def test_pdf_only_selected_workflow(tmp_path):
    path = Path(tmp_path) / "iit.pdf"
    build_guide_pdf(["iit"], path=str(path))
    from pypdf import PdfReader

    text = "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    assert "个税扣缴申报" in text
    assert "增值税及附加税费申报（小规模纳税人" not in text
