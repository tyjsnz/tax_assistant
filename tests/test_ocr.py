"""OCR 申报核对测试：本地识别后端、栏次正则映射、跨行排版、与 filing.validator 联调。

说明
----
* 截图识别使用本机 **macOS Vision**（编译缓存到 ``ocr/bin/macos_ocr``）；若本机无任何 OCR
  后端则相关联网用例 ``skip``。纯规则映射与 mock 核对用例不依赖 OCR 后端，始终执行。
* 不访问真实 DeepSeek API：LLM 兜底用 fake client 模拟。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import config
import engine
from ocr import extractor, field_mapper, validator
from ocr.extractor import OCRAvailableError, OCRError
from ocr.field_mapper import (
    VAT_FIELD_DEFS,
    llm_extract,
    map_fields,
    parse_pasted_table,
    regex_extract,
)

FIXTURE_IMAGE = Path(__file__).parent / "fixtures" / "return_form.png"

#: 与 tests/fixtures/return_form.png 内容一致（2026Q1 全普票免税场景）
FIXTURE_TEXT = (
    "增值税及附加税费申报表（小规模纳税人适用）\n"
    "税款所属期：2026年01月01日 至 2026年03月31日\n"
    "1 应征增值税不含税销售额（3%征收率）\n"
    "0.00\n"
    "10小微企业免税销售额\n"
    "101980.20\n"
    "18应纳税额\n"
    "0.00\n"
    "19 本期免税\n"
    "3059.41\n"
    "22应纳税额减征额\n"
    "0.00\n"
    "24本期应补（退）税额\n"
    "0.00\n"
)


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    try:
        config.save_user_settings({"api_key": ""})
    except OSError:  # pragma: no cover
        pass
    yield


@pytest.fixture
def q1_fixture_ledger():
    """写入与截图一致的 2026Q1 台账：含税 103,000 → 不含税 101,980.20。"""
    pytest.importorskip("ledger.income_ledger")
    from ledger.income_ledger import add_income

    add_income(
        date="2026-02-01",
        client="测试客户",
        inclusive_amount=103000,
        invoice_type="普票",
        category="技术服务",
        description="技术服务费（与 OCR 样例图一致）",
    )
    return True


# =============================================================================
# 字段定义与正则映射
# =============================================================================
def test_vat_field_defs():
    keys = [definition["key"] for definition in VAT_FIELD_DEFS]
    for key in (
        "line_1",
        "line_2",
        "line_10",
        "line_11",
        "line_18",
        "line_19",
        "line_22",
        "line_24",
        "surtax_total",
        "total_payable",
    ):
        assert key in keys
    for definition in VAT_FIELD_DEFS:
        assert definition["name"]
        assert definition["aliases"]
        assert definition["line_no"]


def test_regex_extract_same_line():
    text = (
        "第1栏 应征增值税不含税销售额（3%征收率） 0.00\n"
        "第10栏 小微企业免税销售额 101,980.20\n"
        "第18栏 应纳税额 0.00\n"
        "第19栏 本期免税额 3,059.41\n"
        "第22栏 应纳税额减征额 0.00\n"
        "第24栏 本期应补（退）税额 0.00"
    )
    result = regex_extract(text)
    fields = result["fields"]
    assert fields["line_1"] == 0.0
    assert fields["line_10"] == 101980.20
    assert fields["line_18"] == 0.0
    assert fields["line_19"] == 3059.41
    assert fields["line_22"] == 0.0
    assert fields["line_24"] == 0.0
    # 栏次说明里的「3%」不能被识别成金额
    assert 3.0 not in fields.values()


def test_regex_extract_cross_line():
    """macOS Vision 会把栏次说明与金额识别成不同行，正则需支持跨行取金额。"""
    result = regex_extract(FIXTURE_TEXT)
    fields = result["fields"]
    assert fields["line_1"] == 0.0
    assert fields["line_10"] == 101980.20
    assert fields["line_18"] == 0.0
    assert fields["line_19"] == 3059.41
    assert fields["line_22"] == 0.0
    assert fields["line_24"] == 0.0


def test_regex_extract_short_forms():
    result = regex_extract("1. 123456.78\n10：101980.20\n第24栏 本期应补（退）税额 -1,234.56")
    assert result["fields"]["line_1"] == 123456.78
    assert result["fields"]["line_10"] == 101980.20
    assert result["fields"]["line_24"] == -1234.56


def test_regex_extract_surtax_and_names_only():
    text = "附加税费合计 6.00\n城市维护建设税 4.20\n教育费附加 1.80"
    fields = regex_extract(text)["fields"]
    assert fields["surtax_total"] == 6.0


def test_parse_pasted_table():
    tabbed = parse_pasted_table("栏次\t金额\n10\t101980.20\n24\t0.00")["fields"]
    assert tabbed["line_10"] == 101980.20
    assert tabbed["line_24"] == 0.0

    spaced = parse_pasted_table("10 101980.20\n24 0.00")["fields"]
    assert spaced["line_10"] == 101980.20

    csv_like = parse_pasted_table("10,101980.20")["fields"]
    assert csv_like["line_10"] == 101980.20


def test_canonical_field_key():
    assert field_mapper.canonical_field_key("line_10") == "line_10"
    assert field_mapper.canonical_field_key("第10栏") == "line_10"
    assert field_mapper.canonical_field_key("10") == "line_10"
    assert field_mapper.canonical_field_key("本期应补（退）税额") == "line_24"
    assert field_mapper.canonical_field_key("不存在") is None


def test_map_fields_rule_no_key():
    result = map_fields("第10栏 小微企业免税销售额 101980.20\n第24栏 本期应补（退）税额 0.00")
    assert result["ok"] is True
    assert result["method"] == "rule"
    assert result["fields"]["line_10"] == 101980.20
    assert result["fields"]["line_24"] == 0.0


def test_map_fields_empty():
    result = map_fields("这张图片里没有任何栏次数据")
    assert result["ok"] is False
    assert result["fields"] == {}
    assert result["method"] == "rule"


class _FakeClient:
    configured = True

    def __init__(self, payload=None, error=None):
        self._payload = payload or {"fields": {"第10栏": "101,980.20", "第24栏": 0.0}}
        self._error = error

    def chat(self, messages, **kwargs):
        if self._error:
            raise self._error
        return {"content": json.dumps(self._payload, ensure_ascii=False)}


def test_llm_extract_without_key():
    result = llm_extract("一些文字", None)
    assert result["fields"] == {}
    assert result["used_llm"] is False
    assert "未配置" in result["error"]


def test_llm_extract_with_fake_client():
    result = llm_extract("一些文字", _FakeClient())
    assert result["used_llm"] is True
    assert result["fields"]["line_10"] == 101980.20
    assert result["fields"]["line_24"] == 0.0


def test_map_fields_uses_llm_when_rule_fails():
    """规则只识别到 1 个栏次时，若配置了 Key 则用 LLM 补齐。"""
    result = map_fields("第10栏 小微企业免税销售额 101980.20", client=_FakeClient())
    assert result["method"] == "llm"
    assert result["fields"]["line_10"] == 101980.20
    assert result["fields"]["line_24"] == 0.0


# =============================================================================
# OCR 后端
# =============================================================================
def test_is_available_shape():
    status = extractor.is_available()
    assert isinstance(status, dict)
    assert status["manual"] is True
    for key in ("macos_vision", "tesseract", "manual"):
        assert key in status


def test_extract_text_missing_file(tmp_path):
    result = extractor.extract_text(tmp_path / "not_exists.png")
    assert result["ok"] is False
    assert result["backend"] == "manual"
    assert "找不到图片" in result["message"]


def test_ocr_image_missing_file(tmp_path):
    with pytest.raises(OCRError):
        extractor.ocr_image(tmp_path / "not_exists.png")


def test_extract_text_with_mock_backend(monkeypatch):
    monkeypatch.setattr(extractor, "_ocr_with_backend", lambda path: ("识别到的文字", "macos_vision"))
    result = extractor.extract_text(FIXTURE_IMAGE)
    assert result["ok"] is True
    assert result["backend"] == "macos_vision"
    assert result["text"] == "识别到的文字"


def test_extract_text_all_backends_fail(monkeypatch):
    def raise_unavailable(path):
        raise OCRAvailableError("本地 OCR 均不可用，请手动粘贴文本。")

    monkeypatch.setattr(extractor, "_ocr_with_backend", raise_unavailable)
    result = extractor.extract_text(FIXTURE_IMAGE)
    assert result["ok"] is False
    assert result["backend"] == "manual"
    assert "手动粘贴" in result["message"]


def test_build_vision_binary_no_raise():
    binary = extractor.build_vision_binary()
    assert binary is None or Path(binary).exists()


# =============================================================================
# 申报表核对
# =============================================================================
def test_check_vat_return_zero_declaration():
    pytest.importorskip("filing.validator")
    result = validator.check_vat_return({"line_10": 0.0, "line_24": 0.0}, "2026Q1")
    assert result["status"] == "PASS"
    assert result["validator"] == "filing.validator"
    assert result["details"]
    assert result["disclaimer"] == config.DISCLAIMER
    assert result["period"] == "2026Q1"


def test_check_vat_return_normalizes_keys():
    pytest.importorskip("filing.validator")
    result = validator.check_vat_return({"第10栏": "0.00", "line_24": "0"}, "2026Q1")
    assert result["fields"]["line_10"] == 0.0
    assert "line_24" in result["fields"]


def test_check_vat_return_mismatch():
    pytest.importorskip("filing.validator")
    result = validator.check_vat_return({"line_10": 200000.0}, "2026Q1")
    assert result["status"] == "ERROR"
    error_details = [detail for detail in result["details"] if detail["status"] == "ERROR"]
    assert error_details
    assert "第10栏" in error_details[0]["field"]


def test_check_vat_return_mock_report(monkeypatch):
    fake = {
        "status": "WARNING",
        "details": [
            {
                "field": "第10栏 小微企业免税销售额",
                "expected": 101980.20,
                "actual": 101990.20,
                "status": "WARNING",
                "message": "相差 10.00 元，请核对",
            }
        ],
        "summary": "1 项需复核",
        "disclaimer": config.DISCLAIMER,
    }
    monkeypatch.setattr(validator, "_filing_validate", lambda period, fields: dict(fake))
    result = validator.check_vat_return({"line_10": 101990.20}, "2026Q1")
    assert result["status"] == "WARNING"
    assert result["summary"] == "1 项需复核"
    assert result["details"][0]["field"] == "第10栏 小微企业免税销售额"


def test_check_vat_return_fallback_without_filing(monkeypatch):
    monkeypatch.setattr(validator, "_filing_validate", lambda period, fields: None)
    result = validator.check_vat_return({"line_10": 0.0, "line_24": 0.0}, "2026Q1")
    assert result["validator"] in ("local_fallback", "logic_only")
    assert result["status"] in ("PASS", "WARNING", "ERROR")
    assert result["details"]


def test_check_vat_return_empty_fields():
    result = validator.check_vat_return({}, "2026Q1")
    assert result["status"] == "ERROR"
    assert "未识别到" in result["summary"]


def test_check_from_text_no_fields():
    result = validator.check_from_text("这张截图没有识别出任何栏次", "2026Q1")
    assert result["status"] == "ERROR"
    assert result["mapping"]["method"] == "rule"


def test_check_from_text_fixture(q1_fixture_ledger):
    pytest.importorskip("filing.validator")
    result = validator.check_from_text(FIXTURE_TEXT, "2026Q1")
    assert result["status"] == "PASS", result["summary"]
    assert result["fields"]["line_10"] == 101980.20
    assert result["ocr"] if "ocr" in result else True


def test_check_from_image_ocr_failure(tmp_path):
    result = validator.check_from_image(str(tmp_path / "missing.png"), "2026Q1")
    assert result["status"] == "ERROR"
    assert result["ocr"]["ok"] is False
    assert "找不到" in result["summary"]


def test_check_from_image_with_mocked_ocr(monkeypatch, q1_fixture_ledger):
    pytest.importorskip("filing.validator")
    monkeypatch.setattr(
        extractor, "_ocr_with_backend", lambda path: (FIXTURE_TEXT, "macos_vision")
    )
    result = validator.check_from_image(str(FIXTURE_IMAGE), "2026Q1")
    assert result["status"] == "PASS", result["summary"]
    assert result["ocr"]["backend"] == "macos_vision"
    assert result["fields"]["line_19"] == 3059.41


def _ocr_ready() -> bool:
    if extractor.build_vision_binary() is not None:
        return True
    return bool(extractor.is_available()["tesseract"])


@pytest.mark.skipif(not FIXTURE_IMAGE.exists(), reason="缺少 OCR 样例图")
def test_check_from_image_real_ocr(q1_fixture_ledger):
    """真实 macOS Vision 识别样例图并完成与 filing.validator 的联调。"""
    pytest.importorskip("filing.validator")
    if not _ocr_ready():
        pytest.skip("本机没有可用的本地 OCR 后端")
    extracted = extractor.extract_text(FIXTURE_IMAGE)
    assert extracted["ok"] is True, extracted["message"]
    assert "免税销售额" in extracted["text"]

    result = validator.check_from_image(str(FIXTURE_IMAGE), "2026Q1")
    assert result["status"] == "PASS", result["summary"]
    assert result["fields"]["line_10"] == 101980.20
    assert result["ocr"]["backend"] in ("macos_vision", "tesseract")
