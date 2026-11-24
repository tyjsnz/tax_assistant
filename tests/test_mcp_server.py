"""MCP 只读工具测试（v1.56）。"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

from mcp_server import server


def test_company_profile_has_no_secrets():
    result = server.get_company_profile()
    text = str(result)
    assert "api_key" not in text
    assert "id_card_full" not in text
    assert result["company"]["name"]


def test_draft_center_tool_read_only():
    result = server.get_draft_center()
    assert "items" in result and result["items"]
    assert "系统只准备草稿" in result["disclaimer"]


def test_employee_summary_masks_ids():
    result = server.get_employee_summary()
    assert "employees" in result
    assert "id_card_full" not in str(result)
