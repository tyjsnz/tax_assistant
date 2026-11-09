"""申报表 OCR 核对：本地截图文字识别 + 栏次映射 + 与台账逐栏比对。

**重要**：DeepSeek 官方 API 没有视觉能力，截图文字识别必须走**本地 OCR**
（macOS Vision 优先、pytesseract 兜底、手动粘贴文本作为最终兜底）；
DeepSeek 只参与文字理解与栏次兜底映射。

对外常用入口：

* :func:`is_available` / :func:`extract_text` / :func:`ocr_image` / :func:`build_vision_binary`：OCR；
* :data:`VAT_FIELD_DEFS` / :func:`map_fields` / :func:`regex_extract` / :func:`llm_extract` /
  :func:`parse_pasted_table`：申报表栏次映射；
* :func:`check_vat_return` / :func:`check_from_image` / :func:`check_from_text`：申报表核对。
"""

from __future__ import annotations

from ocr.extractor import (
    OCRAvailableError,
    OCRError,
    availability_text,
    build_vision_binary,
    extract_text,
    is_available,
    last_build_error,
    ocr_image,
)
from ocr.field_mapper import (
    VAT_FIELD_DEFS,
    canonical_field_key,
    llm_extract,
    map_fields,
    parse_pasted_table,
    regex_extract,
)
from ocr.validator import check_from_image, check_from_text, check_vat_return

__all__ = [
    # 文字识别
    "OCRError",
    "OCRAvailableError",
    "is_available",
    "availability_text",
    "extract_text",
    "ocr_image",
    "build_vision_binary",
    "last_build_error",
    # 栏次映射
    "VAT_FIELD_DEFS",
    "regex_extract",
    "parse_pasted_table",
    "llm_extract",
    "map_fields",
    "canonical_field_key",
    # 核对
    "check_vat_return",
    "check_from_image",
    "check_from_text",
]
