"""端到端集成测试：示例数据 → 台账取数 → 申报表生成 → 校验 → 导出 → 进度 → 离线问答 → 知识库 → OCR。

这些用例横跨全部模块，是发布前的最后一道防线；不访问真实 DeepSeek API。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import config
from engine import calc_vat, current_quarter
from ledger import income_ledger, journal, models, seed, store
from filing import export, progress, registry, validator


# =============================================================================
# 示例数据与台账取数
# =============================================================================
class TestSeedAndLedger:
    def test_seed_demo_data(self, demo_data):
        stats = store.storage_stats()
        assert stats["income"] >= 20
        assert stats["expense"] >= 30
        assert stats["rd"] >= 4
        assert stats["contracts"] >= 3
        assert stats["journal_entries"] >= 40

    def test_quarter_totals(self, demo_data):
        q1 = income_ledger.period_totals("2026Q1")
        assert q1["total_inclusive"] == pytest.approx(200000.0)
        assert q1["special_inclusive"] == pytest.approx(0.0)

        q2 = income_ledger.period_totals("2026Q2")
        assert q2["special_inclusive"] == pytest.approx(101000.0)
        assert q2["total_inclusive"] == pytest.approx(280000.0)

    def test_vat_from_ledger_matches_engine(self, demo_data):
        """台账取数 → 引擎算税：Q2 专票 10 万应缴 1,000，普票部分免征。"""
        totals = income_ledger.period_totals("2026Q2")
        result = calc_vat(totals["total_inclusive"], totals["special_inclusive"], "2026Q2")
        assert result["within_exemption_limit"] is True
        assert result["vat_payable"] == pytest.approx(1000.0)
        assert result["surtax"] == pytest.approx(60.0)
        assert result["total_tax"] == pytest.approx(1060.0)

    def test_journal_balanced(self, demo_data):
        balance = journal.trial_balance()
        assert balance["balanced"] is True
        assert balance["total_debit"] == pytest.approx(balance["total_credit"])


# =============================================================================
# 申报表：生成 → 校验 → 导出 → 进度
# =============================================================================
REQUIRED_BUNDLE_KEYS = {
    "form_id",
    "form_name",
    "category",
    "period",
    "period_label",
    "generated_at",
    "status",
    "sections",
    "totals",
    "calc",
    "checks",
    "summary",
    "guide",
    "disclaimer",
}


class TestFilingPipeline:
    def test_build_all(self, demo_data):
        bundles = registry.build_all("2026Q2")
        assert len(bundles) == 4
        form_ids = {bundle["form_id"] for bundle in bundles}
        assert form_ids == {"VAT_QUARTERLY", "CIT_PREPAY", "FIN_REPORT", "STAMP_QUARTERLY"}
        for bundle in bundles:
            assert REQUIRED_BUNDLE_KEYS <= set(bundle)
            assert bundle["period"] == "2026Q2"
            assert bundle["disclaimer"] == config.DISCLAIMER
            assert bundle["sections"] and bundle["sections"][0]["lines"]

    def test_vat_return_lines(self, demo_data):
        bundle = registry.build_form("VAT_QUARTERLY", "2026Q2")
        main_section = next(section for section in bundle["sections"] if "主表" in section["title"])
        lines = {line["line_no"]: line for line in main_section["lines"]}
        assert lines["1"]["value"] == pytest.approx(100000.0)
        assert lines["2"]["value"] == pytest.approx(100000.0)
        assert lines["10"]["value"] == pytest.approx(177227.72)
        assert lines["18"]["value"] == pytest.approx(3000.0)
        assert lines["22"]["value"] == pytest.approx(2000.0)
        assert lines["24"]["value"] == pytest.approx(1000.0)
        assert lines["18"]["value"] - lines["22"]["value"] == pytest.approx(lines["24"]["value"])

    def test_validation_pass(self, demo_data):
        bundle = registry.build_form("VAT_QUARTERLY", "2026Q2")
        report = validator.validate_bundle(bundle)
        assert report["status"] == "PASS"
        assert all(detail["status"] != "ERROR" for detail in report["details"])

    def test_ledger_consistency(self, demo_data):
        report = validator.check_ledger_consistency("2026Q2")
        assert report["status"] in {"PASS", "WARNING"}

    def test_export_package(self, demo_data, tmp_path):
        import openpyxl

        bundles = registry.build_all("2026Q2")
        path = export.export_package_excel("2026Q2", bundles, tmp_path / "申报包_2026Q2.xlsx")
        assert path.exists() and path.stat().st_size > 10_000
        workbook = openpyxl.load_workbook(path)
        assert len(workbook.sheetnames) >= 4
        # 至少一个 sheet 含「栏次」表头
        header_texts = []
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows(max_row=5, values_only=True):
                header_texts.extend(str(cell) for cell in row if cell)
        assert any("栏次" in text for text in header_texts)

    def test_progress_flow(self, demo_data):
        bundles = registry.build_all("2026Q1")
        for bundle in bundles:
            progress.save_filing(bundle, status="草稿")
        state = progress.progress("2026Q1")
        assert state["total"] == 5
        assert state["done"] == 0

        for bundle in bundles:
            progress.save_filing(bundle, status="已确认")
        state = progress.progress("2026Q1")
        assert state["done"] == 4
        assert state["percent"] == pytest.approx(80.0)

        progress.mark_submitted("2026Q1", "VAT_QUARTERLY")
        state = progress.progress("2026Q1")
        assert state["submitted"] >= 1

    def test_filing_log_written(self, demo_data):
        bundle = registry.build_form("VAT_QUARTERLY", "2026Q1")
        progress.save_filing(bundle)
        logs = store.read_log("filing_log")
        assert logs and any("VAT_QUARTERLY" in str(row) for row in logs)


# =============================================================================
# 离线 Agent / 知识库 / OCR
# =============================================================================
class TestAgentOffline:
    def test_run_turn_without_key(self, demo_data, no_api_key):
        from agent import orchestrator

        result = orchestrator.run_turn("我这个季度开了 8 万普票，增值税怎么算？")
        assert result["used_llm"] is False
        reply = result["reply"]
        assert "79,207.92" in reply or "792.08" in reply
        assert config.DISCLAIMER in reply
        assert result["intent"] == "calc_vat"
        assert any(call["name"] == "calc_vat" for call in result["tool_calls"])

    def test_local_reply_calendar(self, demo_data, no_api_key):
        from agent import orchestrator

        result = orchestrator.local_reply("下个月有什么需要申报的？")
        assert result["intent"] == "calendar"
        assert "申报" in result["reply"]

    def test_tools_registry(self, demo_data):
        from agent import tools

        assert len(tools.TOOL_NAMES) >= 13
        specs = tools.tool_specs()
        assert all("function" in spec for spec in specs)

        result = tools.call_tool("calc_vat", {"quarterly_total_inclusive": 280000, "special_invoice_inclusive": 101000, "period": "2026Q2"})
        assert result["ok"] is True
        assert result["data"]["vat_payable"] == pytest.approx(1000.0)

        result = tools.call_tool("query_ledger", {"period": "2026Q2"})
        assert result["ok"] is True

    def test_tool_error_handled(self):
        from agent import tools

        result = tools.call_tool("not_exist_tool", {})
        assert result["ok"] is False and result["error"]


class TestKnowledge:
    def test_policies_loaded(self):
        from knowledge import retriever

        policies = retriever.list_policies()
        assert len(policies) >= 10
        for policy in policies:
            assert policy["title"]
            assert policy["source"]
            assert policy["effective_date"]
            assert policy["last_verified"]

    def test_search_hits(self):
        from knowledge import retriever

        hits = retriever.search("研发费用加计扣除比例", top_k=3)
        assert hits
        assert any("研发" in hit["title"] for hit in hits)

    def test_answer_offline(self):
        from knowledge import retriever

        result = retriever.answer("小规模纳税人季度销售额多少可以免征增值税？")
        assert result["used_llm"] is False
        assert result["citations"]
        assert "300000" in result["answer"] or "30 万" in result["answer"] or "30万" in result["answer"]

    def test_stale_check(self):
        from knowledge import update_checker

        report = update_checker.check_policies()
        assert report["checked_total"] >= 10
        assert "checked_at" in report


class TestOCR:
    FIXTURE = Path(__file__).parent / "fixtures" / "return_form.png"

    def test_extract_fixture(self):
        from ocr import extractor

        if not self.FIXTURE.exists():
            pytest.skip("缺少 OCR 测试图片")
        result = extractor.extract_text(self.FIXTURE)
        if not result.get("ok"):
            pytest.skip(f"本机无可用 OCR 后端：{result.get('message')}")
        assert result["backend"] in {"macos_vision", "tesseract"}
        assert "增值税" in result["text"]

    def test_field_mapping_and_check(self, demo_data):
        from ocr import extractor, field_mapper

        if not self.FIXTURE.exists():
            pytest.skip("缺少 OCR 测试图片")
        result = extractor.extract_text(self.FIXTURE)
        if not result.get("ok"):
            pytest.skip("本机无可用 OCR 后端")
        mapped = field_mapper.map_fields(result["text"], period="2026Q1")
        assert mapped["fields"]
        assert mapped["fields"].get("line_10") == pytest.approx(101980.20)
        assert mapped["fields"].get("line_24") == pytest.approx(0.0)

        report = field_mapper is not None  # 保持引用，便于调试
        from ocr import validator as ocr_validator

        check = ocr_validator.check_vat_return(mapped["fields"], "2026Q1")
        assert check["status"] in {"PASS", "WARNING", "ERROR"}
        assert check["details"]
        assert check["disclaimer"] == config.DISCLAIMER


# =============================================================================
# 设置与安全
# =============================================================================
class TestSettings:
    def test_api_key_local_only_and_masked(self):
        config.set_api_key("sk-test-1234567890abcdef")
        try:
            assert config.get_api_key() == "sk-test-1234567890abcdef"
            masked = config.mask_key()
            assert "sk-test-1234567890abcdef" not in masked
            assert masked.startswith("sk-tes") and masked.endswith("cdef")
            assert config.is_ai_configured() is True
        finally:
            config.set_api_key("")

    def test_user_settings_file_permissions(self):
        config.set_api_key("sk-test")
        try:
            mode = config.SETTINGS_FILE.stat().st_mode & 0o777
            assert mode == 0o600
        finally:
            config.set_api_key("")

    def test_company_overrides(self):
        config.save_user_settings({"company": {"short_name": "测试公司"}})
        try:
            assert config.get_company()["short_name"] == "测试公司"
            assert config.get_company()["name"]  # 默认企业名仍在
        finally:
            config.save_user_settings({"company": {}})


# =============================================================================
# 命令行
# =============================================================================
class TestCLI:
    def test_calc_vat_command(self, capsys):
        import main

        assert main.main(["calc-vat", "--total", "103000", "--period", "2026Q1"]) == 0
        output = capsys.readouterr().out
        assert "101,980.20" in output
        assert "仅供参考" in output

    def test_file_command(self, demo_data, tmp_path):
        import main

        assert main.main(["file", "--period", "2026Q1", "--out", str(tmp_path)]) == 0
        exported = list(tmp_path.glob("申报包_2026Q1.xlsx"))
        assert exported
