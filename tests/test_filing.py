"""filing 申报填报模块测试：申报表生成、三种增值税场景、企业所得税预缴、印花税、
财务报表、规则校验、导出、进度流转与填报指引。

测试环境由 ``tests/conftest.py`` 自动隔离；示例数据通过 ``demo_data`` fixture 加载。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import load_workbook

import config
import filing
from engine import calc_vat, round2
from ledger import income_ledger
from filing import (
    build_all,
    build_cit_prepay,
    build_fin_report,
    build_form,
    build_stamp_return,
    build_vat_return,
    check_ledger_consistency,
    export_bundle_excel,
    export_bundle_json,
    export_checklist_markdown,
    export_package_excel,
    get_form,
    list_forms,
    list_filings,
    mark_submitted,
    period_progress,
    save_filing,
    update_status,
    validate_against_fields,
    validate_bundle,
    validate_vat_return,
)

_BUNDLE_KEYS = (
    "form_id",
    "form_name",
    "category",
    "period",
    "period_label",
    "frequency",
    "generated_at",
    "status",
    "sections",
    "totals",
    "calc",
    "checks",
    "summary",
    "guide",
    "data",
    "disclaimer",
)


def _line_value(bundle: dict, line_no: str) -> float:
    for section in bundle["sections"]:
        for line in section["lines"]:
            if str(line["line_no"]) == str(line_no):
                return float(line["value"])
    raise AssertionError(f"未找到第 {line_no} 栏")


def _find_line(bundle: dict, line_no: str) -> dict:
    for section in bundle["sections"]:
        for line in section["lines"]:
            if str(line["line_no"]) == str(line_no):
                return line
    raise AssertionError(f"未找到第 {line_no} 栏")


# =============================================================================
# 注册表
# =============================================================================
class TestRegistry:
    def test_list_and_get_forms(self):
        """报表注册表包含 4 张可生成报表，元数据不含构造器。"""
        forms = list_forms()
        assert [form["form_id"] for form in forms] == [
            "VAT_QUARTERLY",
            "CIT_PREPAY",
            "FIN_REPORT",
            "STAMP_QUARTERLY",
        ]
        for form in forms:
            assert set(form) == {"form_id", "form_name", "category", "frequency", "period_kind", "description"}
            assert form["frequency"] == "quarterly"
        assert get_form("VAT_QUARTERLY")["builder"] is not None
        with pytest.raises(KeyError):
            get_form("NOT_EXIST")

    def test_build_all_bundle_structure(self, demo_data):
        """build_all 生成 4 张申报表，bundle 结构字段齐全。"""
        bundles = build_all("2026Q2")
        assert len(bundles) == 4
        for bundle in bundles:
            for key in _BUNDLE_KEYS:
                assert key in bundle, f"{bundle.get('form_id')} 缺少字段 {key}"
            assert bundle["period"] == "2026Q2"
            assert bundle["period_label"] == "2026年第二季度"
            assert bundle["status"] == "草稿"
            assert isinstance(bundle["sections"], list) and bundle["sections"]
            assert isinstance(bundle["checks"], list)
            assert bundle["summary"]["level"] in ("PASS", "WARNING", "ERROR")
            assert bundle["guide"] and bundle["guide"][-1] == config.DISCLAIMER
            assert bundle["disclaimer"] == config.DISCLAIMER

    def test_build_form_accepts_month_period(self, demo_data):
        """月份期间自动换算到所属季度。"""
        bundle = build_form("VAT_QUARTERLY", "2026-05")
        assert bundle["period"] == "2026Q2"
        assert bundle["form_id"] == "VAT_QUARTERLY"


# =============================================================================
# 增值税申报表（三种场景）
# =============================================================================
class TestVatReturn:
    def test_scenario_all_ordinary_exempt(self, demo_data):
        """场景一：全普票 20 万，季度不含税销售额 ≤ 30 万 → 免征增值税。"""
        bundle = build_vat_return("2026Q1")
        calc = bundle["calc"]
        assert calc["within_exemption_limit"] is True
        assert _line_value(bundle, "1") == 0.0
        assert _line_value(bundle, "2") == 0.0
        assert _line_value(bundle, "10") == 198019.80
        assert _line_value(bundle, "11") == 0.0
        assert _line_value(bundle, "18") == 0.0
        assert _line_value(bundle, "19") == 5940.59
        assert _line_value(bundle, "22") == 0.0
        assert _line_value(bundle, "24") == 0.0
        assert bundle["totals"]["vat_payable"] == 0.0
        assert bundle["totals"]["vat_exempt"] == 1980.20
        assert bundle["totals"]["surtax"] == 0.0
        assert bundle["totals"]["total_tax"] == 0.0
        assert bundle["summary"]["level"] == "PASS"
        assert "免税" in bundle["calc"]["note"]

    def test_scenario_with_special_invoice(self, demo_data):
        """场景二：季度 28 万含 10.1 万专票 → 专票部分缴税、普票部分免税。"""
        bundle = build_vat_return("2026Q2")
        calc = bundle["calc"]
        assert calc["within_exemption_limit"] is True
        assert _line_value(bundle, "1") == 100000.0
        assert _line_value(bundle, "2") == 100000.0
        assert _line_value(bundle, "10") == 177227.72
        assert _line_value(bundle, "18") == 3000.0
        assert _line_value(bundle, "19") == 5316.83
        assert _line_value(bundle, "22") == 2000.0
        assert _line_value(bundle, "24") == 1000.0
        assert bundle["totals"]["vat_payable"] == 1000.0
        assert bundle["totals"]["surtax"] == 60.0
        assert bundle["totals"]["total_tax"] == 1060.0
        assert bundle["totals"]["surtax_city"] == 35.0
        assert bundle["totals"]["surtax_education"] == 15.0
        assert bundle["totals"]["surtax_local_education"] == 10.0
        # 附列资料：专票 + 普票 + 未开票 = 合计
        attachment = next(section for section in bundle["sections"] if "附列资料" in section["title"])
        attachment_total = attachment["lines"][-1]["value"]
        assert attachment_total == 277227.72
        assert sum(line["value"] for line in attachment["lines"][:-1]) == 277227.72
        assert bundle["summary"]["level"] == "PASS"

    def test_scenario_exceed_30w(self, demo_data):
        """场景三：季度含税 40.4 万（不含税 40 万）超过免税额度 → 全额按 1% 缴税。"""
        income_ledger.add_income("2026-02-18", "云南远卓", 204000.0, invoice_type="普票", category="技术服务")
        bundle = build_vat_return("2026Q1")
        calc = bundle["calc"]
        assert calc["within_exemption_limit"] is False
        assert calc["total_exclusive"] == 400000.0
        assert _line_value(bundle, "1") == 400000.0
        assert _line_value(bundle, "10") == 0.0
        assert _line_value(bundle, "18") == 12000.0
        assert _line_value(bundle, "22") == 8000.0
        assert _line_value(bundle, "24") == 4000.0
        assert bundle["totals"]["surtax"] == 240.0
        assert bundle["totals"]["total_tax"] == 4240.0
        assert bundle["summary"]["level"] == "PASS"

    def test_manual_income_totals_override(self, demo_data):
        """手工指定销售额时不与台账逐笔比对，但栏次勾稽仍需正确（给出 WARNING）。"""
        bundle = build_vat_return("2026Q1", income_totals={"total_inclusive": 404000.0, "special_inclusive": 0.0})
        assert bundle["totals"]["total_inclusive"] == 404000.0
        assert _line_value(bundle, "24") == 4000.0
        assert bundle["data"]["source"] == "manual"
        assert bundle["summary"]["level"] == "WARNING"
        assert any(check["field"] == "数据来源" for check in bundle["checks"])

    def test_vat_calc_matches_engine(self, demo_data):
        """申报表栏次与 engine.calc_vat 的 return_lines 严格一致。"""
        bundle = build_vat_return("2026Q2")
        expected = calc_vat(280000.0, 101000.0, period="2026Q2")["return_lines"]
        assert _line_value(bundle, "1") == expected["line_1_taxable_sales"]
        assert _line_value(bundle, "10") == expected["line_10_exempt_sales"]
        assert _line_value(bundle, "18") == expected["line_18_tax_amount"]
        assert _line_value(bundle, "19") == expected["line_19_tax_exempt_amount"]
        assert _line_value(bundle, "22") == expected["line_22_reduction"]
        assert _line_value(bundle, "24") == expected["line_24_payable"]


# =============================================================================
# 企业所得税预缴
# =============================================================================
class TestCitPrepay:
    def test_build_cit_prepay(self, demo_data):
        """A 类预缴表行次 1—15 齐全，行间勾稽正确，与 engine 结果一致。"""
        bundle = build_cit_prepay("2026Q1")
        lines = {str(index): _line_value(bundle, str(index)) for index in range(1, 16)}
        assert lines["1"] == 198019.79
        assert lines["2"] == 100530.0
        assert lines["3"] == 97489.79
        assert lines["4"] == lines["5"] == lines["6"] == lines["7"] == 0.0
        assert lines["8"] == 70480.0
        assert lines["9"] == 27009.79
        assert lines["10"] == 0.25
        assert lines["11"] == 6752.45
        assert lines["12"] == 5401.96
        assert lines["13"] == 1350.49
        assert lines["14"] == 0.0
        assert lines["15"] == 1350.49
        assert round2(lines["13"] - lines["14"]) == lines["15"]
        assert round2(lines["9"] * 0.25) == lines["11"]
        assert round2(lines["11"] - lines["12"]) == lines["13"]
        assert bundle["totals"]["prepay_payable"] == 1350.49
        assert bundle["totals"]["qualifies_small_low_profit"] is True
        assert bundle["calc"]["effective_rate"] == 0.05
        assert bundle["summary"]["level"] != "ERROR"

    def test_cit_prepay_ytd_and_paid(self, demo_data):
        """Q2 累计口径：本年累计利润 = Q1 + Q2 利润，本年累计已预缴 = Q1 预缴额。"""
        q1 = build_cit_prepay("2026Q1")
        q2 = build_cit_prepay("2026Q2")
        assert q2["data"]["ytd_profit"] == round2(q1["totals"]["profit"] + q2["totals"]["quarter_profit"])
        assert q2["totals"]["cit_paid"] == q1["totals"]["prepay_payable"]
        assert q2["data"]["quarters"] == ["2026Q1", "2026Q2"]
        assert q2["summary"]["level"] != "ERROR"


# =============================================================================
# 印花税申报表
# =============================================================================
class TestStampReturn:
    def test_build_stamp_return(self, demo_data):
        """Q1 合同 2 份（技术 18 万 + 买卖 1.8 万），六税两费减半后应缴 29.70 元。"""
        bundle = build_stamp_return("2026Q1")
        assert bundle["totals"]["contract_count"] == 2
        assert bundle["totals"]["total_amount"] == 198000.0
        assert bundle["totals"]["total_before_discount"] == 59.4
        assert bundle["totals"]["discount_amount"] == 29.7
        assert bundle["totals"]["total_payable"] == 29.7
        assert bundle["calc"]["small_scale"] is True
        assert bundle["summary"]["level"] == "PASS"
        assert len(bundle["sections"]) == 3

    def test_stamp_return_q3(self, demo_data):
        """Q3 技术合同 9 万 → 减免前 27 元、减半后 13.50 元。"""
        bundle = build_stamp_return("2026Q3")
        assert bundle["totals"]["contract_count"] == 1
        assert bundle["totals"]["total_payable"] == 13.5


# =============================================================================
# 财务报表
# =============================================================================
class TestFinReport:
    def test_build_fin_report(self, demo_data):
        """财务报表包含资产负债表（期末/年初）与利润表（本期/本年累计）。"""
        bundle = build_fin_report("2026Q1")
        titles = [section["title"] for section in bundle["sections"]]
        assert any("资产负债表（期末数" in title for title in titles)
        assert any("资产负债表（年初数" in title for title in titles)
        assert any("利润表（本期数" in title for title in titles)
        assert any("利润表（本年累计数" in title for title in titles)
        assert all("按台账估算" in title for title in titles)
        totals = bundle["totals"]
        assert totals["total_assets"] == 97460.09
        assert totals["total_liabilities"] == 0.0
        assert totals["total_equity"] == 97460.09
        assert totals["revenue"] == 198019.79
        assert totals["net_profit"] == 92587.09
        assert "按台账估算" in bundle["calc"]["note"]
        # 资产负债表平衡
        balance = next(section for section in bundle["sections"] if "期末数" in section["title"])
        rows = {line["line_no"]: line["value"] for line in balance["lines"]}
        assert rows["5"] == rows["12"] == rows["8"] + rows["11"]
        assert bundle["summary"]["level"] == "WARNING"  # 台账估算口径提示


# =============================================================================
# 规则校验
# =============================================================================
class TestValidator:
    def test_validate_bundle_pass(self, demo_data):
        """标准申报表校验通过。"""
        bundle = build_vat_return("2026Q2")
        report = validate_bundle(bundle)
        assert report["status"] == "PASS"
        assert report["disclaimer"] == config.DISCLAIMER
        assert all(detail["status"] == "PASS" for detail in report["details"])

    def test_validate_bundle_detects_tamper(self, demo_data):
        """篡改第 24 栏后校验报 ERROR。"""
        bundle = build_vat_return("2026Q2")
        _find_line(bundle, "24")["value"] = 999.0
        report = validate_bundle(bundle)
        assert report["status"] == "ERROR"
        assert any(detail["status"] == "ERROR" for detail in report["details"])

    def test_validate_other_forms(self, demo_data):
        """企业所得税、印花税、财务报表 bundle 均可校验且无 ERROR。"""
        for form_id in ("CIT_PREPAY", "STAMP_QUARTERLY", "FIN_REPORT"):
            bundle = build_form(form_id, "2026Q2")
            report = validate_bundle(bundle)
            assert report["status"] in ("PASS", "WARNING"), f"{form_id} 校验异常：{report['summary']}"
            assert report["details"]

    def test_validate_vat_return(self, demo_data):
        """重算增值税申报表并比对：标准 bundle 通过。"""
        bundle = build_vat_return("2026Q2")
        report = validate_vat_return("2026Q2", bundle)
        assert report["status"] == "PASS"
        report_rebuilt = validate_vat_return("2026Q2")
        assert report_rebuilt["status"] == "PASS"

    def test_check_ledger_consistency(self, demo_data):
        """台账自检：发票拆分、逐笔金额勾稽、研发归集全部通过。"""
        report = check_ledger_consistency("2026Q2")
        assert report["status"] == "PASS", [detail for detail in report["details"] if detail["status"] != "PASS"]
        fields = {detail["field"] for detail in report["details"]}
        assert "发票类型拆分：含税合计" in fields
        assert "逐笔金额勾稽" in fields

    def test_check_ledger_consistency_flags_bad_data(self, demo_data):
        """篡改一笔收入的不含税金额后，台账自检报 ERROR。"""
        record = income_ledger.add_income("2026-04-15", "测试客户", 10100.0)
        income_ledger.update_income(record["id"], {"exclusive_amount": 9000.0, "vat_amount": 100.0})
        report = check_ledger_consistency("2026Q2")
        assert report["status"] == "ERROR"

    def test_validate_against_fields(self, demo_data):
        """与 OCR/手工栏次值比对：正确值 PASS，错误值 ERROR。"""
        fields = {
            "line_1": 100000.0,
            "line_2": 100000.0,
            "line_10": 177227.72,
            "line_11": 0.0,
            "line_18": 3000.0,
            "line_19": 5316.83,
            "line_22": 2000.0,
            "line_24": 1000.0,
            "surtax_city": 35.0,
            "surtax_education": 15.0,
            "surtax_local_education": 10.0,
            "surtax_total": 60.0,
            "total_payable": 1060.0,
        }
        report = validate_against_fields("2026Q2", fields)
        assert report["status"] == "PASS", [detail for detail in report["details"] if detail["status"] != "PASS"]

        fields["line_24"] = 0.0
        bad = validate_against_fields("2026Q2", fields)
        assert bad["status"] == "ERROR"
        assert any(detail["field"] == "第24栏 本期应补（退）税额" for detail in bad["details"] if detail["status"] == "ERROR")

    def test_validate_against_fields_rejects_empty(self, demo_data):
        """未提供栏次数据时抛 ValueError。"""
        with pytest.raises(ValueError):
            validate_against_fields("2026Q2", {})


# =============================================================================
# 导出
# =============================================================================
class TestExport:
    def test_export_bundle_excel(self, demo_data):
        """单表 Excel 导出：文件存在、可被 openpyxl 读取、含 3 个 sheet。"""
        bundle = build_vat_return("2026Q2")
        path = export_bundle_excel(bundle)
        assert isinstance(path, Path) and path.exists() and path.suffix == ".xlsx"
        workbook = load_workbook(path)
        assert workbook.sheetnames == ["申报表", "校验结果", "填报指引"]
        sheet = workbook["申报表"]
        assert "增值税及附加税费申报表" in str(sheet["A1"].value)
        assert sheet.freeze_panes == "A6"

    def test_export_package_excel(self, demo_data):
        """整包 Excel 导出：申报清单 + 4 张报表 sheet，文件名含期间。"""
        path = export_package_excel("2026Q2")
        assert path.exists() and "2026Q2" in path.name
        workbook = load_workbook(path)
        assert workbook.sheetnames[0] == "申报清单"
        assert len(workbook.sheetnames) == 5

    def test_export_bundle_json(self, demo_data):
        """JSON 导出内容可解析且字段完整。"""
        bundle = build_vat_return("2026Q2")
        path = export_bundle_json(bundle)
        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["form_id"] == "VAT_QUARTERLY"
        assert payload["totals"]["vat_payable"] == 1000.0
        assert payload["summary"]["level"] == "PASS"

    def test_export_checklist_markdown(self, demo_data):
        """Markdown 核对清单包含报表概览、逐表核对与免责声明。"""
        path = export_checklist_markdown("2026Q2")
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "# 申报核对清单（2026Q2）" in content
        assert "申报表概览" in content
        assert "填报步骤" in content
        assert config.DISCLAIMER in content


# =============================================================================
# 申报进度
# =============================================================================
class TestProgress:
    def test_save_and_upsert(self, demo_data):
        """同一期间 + 报表编码重复保存只更新，不新增记录。"""
        bundle = build_vat_return("2026Q2")
        first = save_filing(bundle, status="草稿")
        assert first["status"] == "草稿"
        assert first["data_json"]["totals"]["vat_payable"] == 1000.0
        second = save_filing(bundle, status="草稿")
        assert second["id"] == first["id"]
        assert len(list_filings("2026Q2")) == 1

    def test_status_progression(self, demo_data):
        """状态按 草稿→已确认→已申报 推进，禁止回退。"""
        record = save_filing(build_vat_return("2026Q2"))
        confirmed = update_status(record["id"], "已确认")
        assert confirmed["status"] == "已确认"
        submitted = update_status(record["id"], "已申报")
        assert submitted["status"] == "已申报"
        with pytest.raises(ValueError):
            update_status(record["id"], "草稿")
        with pytest.raises(ValueError):
            update_status(record["id"], "非法状态")
        with pytest.raises(KeyError):
            update_status("FIL999999", "已确认")

    def test_save_filing_validation(self, demo_data):
        """缺少期间/编码或状态非法时抛 ValueError。"""
        with pytest.raises(ValueError):
            save_filing({"form_id": "VAT_QUARTERLY", "period": ""})
        with pytest.raises(ValueError):
            save_filing({"form_id": "", "period": "2026Q2"})
        with pytest.raises(ValueError):
            save_filing(build_vat_return("2026Q2"), status="未知")

    def test_progress_and_mark_submitted(self, demo_data):
        """完成度按引擎申报清单的 5 项统计；标记已申报后进度推进。"""
        empty = period_progress("2026Q2")
        assert empty["total"] == 5
        assert empty["done"] == 0 and empty["percent"] == 0.0
        assert all(item["status"] in ("未生成", "扣缴端申报") for item in empty["items"])
        # 个税在自然人电子税务局扣缴端按月申报，不在本系统生成
        iit = next(item for item in empty["items"] if item["form_id"] == "IIT_MONTHLY")
        assert iit["external"] is True and iit["status"] == "扣缴端申报"

        save_filing(build_vat_return("2026Q2"))
        after_save = period_progress("2026Q2")
        assert after_save["done"] == 0

        mark_submitted("2026Q2", "VAT_QUARTERLY")
        updated = period_progress("2026Q2")
        assert updated["done"] == 1
        assert updated["percent"] == 20.0
        assert updated["submitted"] == 1
        vat_item = next(item for item in updated["items"] if item["form_id"] == "VAT_QUARTERLY")
        assert vat_item["status"] == "已申报" and vat_item["required"] is True

        mark_submitted("2026Q2", "IIT_MONTHLY")
        iit_item = next(item for item in period_progress("2026Q2")["items"] if item["form_id"] == "IIT_MONTHLY")
        assert iit_item["status"] == "已申报"
        assert "个人所得税" in iit_item["form_name"]

    def test_build_form_reflects_saved_status(self, demo_data):
        """已保存/已申报的报表重新生成时沿用记录状态。"""
        bundle = build_vat_return("2026Q2")
        record = save_filing(bundle)
        update_status(record["id"], "已确认")
        rebuilt = build_form("VAT_QUARTERLY", "2026Q2")
        assert rebuilt["status"] == "已确认"


# =============================================================================
# 填报指引
# =============================================================================
class TestGuide:
    def test_build_guide(self, demo_data):
        """填报步骤为可操作中文清单，末尾附免责声明。"""
        bundle = build_vat_return("2026Q2")
        steps = bundle["guide"]
        assert len(steps) >= 5
        assert any("电子税务局" in step for step in steps)
        assert any("第 24 栏" in step or "第24栏" in step for step in steps)
        assert steps[-1] == config.DISCLAIMER

    def test_export_submission_text(self, demo_data):
        """逐栏复制文本包含栏次、金额与免责声明。"""
        bundle = build_vat_return("2026Q2")
        text = filing.export_submission_text(bundle)
        assert "增值税及附加税费申报表" in text
        assert "第1栏" in text and "第24栏" in text
        assert "1,000.00" in text
        assert config.DISCLAIMER in text
