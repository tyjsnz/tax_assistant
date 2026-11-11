"""申报表核对：把 OCR / 手工栏次值与台账标准答案逐栏比对。

流程（对应项目开发规范（本地）Module 4）：

```
截图 → 本地 OCR（ocr.extractor）→ 栏次映射（ocr.field_mapper）
     → 台账标准答案（优先 filing.validator.validate_against_fields）
     → 逐栏比对 → 差异报告 {"status","details","summary","disclaimer"}
```

说明
----
* 标准答案由 **filing 模块**（确定性重算）提供；若 filing 尚未就绪，降级为
  「收入台账 + engine.calc_vat」自行推算，并在结果中标注 ``validator="local_fallback"``；
* DeepSeek 官方 API 无视觉能力：截图文字必须先经本地 OCR，DeepSeek 只参与文字理解 /
  栏次兜底映射；本模块不调用任何在线 OCR 服务；
* 输出状态：``PASS``（一致）/ ``WARNING``（需复核）/ ``ERROR``（存在差异或无法识别）。
"""

from __future__ import annotations

from typing import Any

import config
import engine
from engine.money import round2, yuan
from ocr import extractor, field_mapper

DISCLAIMER = config.DISCLAIMER

#: 季度免税额度（元，不含税）
_EXEMPTION_LIMIT = float(config.POLICY.get("vat_exemption_limit_quarter") or 300_000.0)
#: 金额比对容差（元）
_TOLERANCE = 0.01

_STATUS_RANK = {"PASS": 0, "WARNING": 1, "ERROR": 2}


# =============================================================================
# 基础工具
# =============================================================================
def _label(key: str) -> str:
    for definition in field_mapper.VAT_FIELD_DEFS:
        if definition["key"] == key:
            return f"第{definition['line_no']}栏 {definition['name']}"
    return str(key)


def _normalize_period(period: Any) -> str:
    text = str(period or "").strip()
    normalized = text
    try:
        from ledger.models import normalize_period

        normalized = normalize_period(text, default="") or text
    except Exception:  # noqa: BLE001
        normalized = text
    try:
        engine.period_range(normalized)
        return normalized
    except ValueError:
        return engine.current_quarter()


def _normalize_fields(fields: Any) -> dict[str, float]:
    """把外部传入的栏次字典规范化为 ``{标准 key: float}``。"""
    if not isinstance(fields, dict):
        return {}
    normalized: dict[str, float] = {}
    for key, value in fields.items():
        canonical = field_mapper.canonical_field_key(key)
        if not canonical:
            continue
        if value is None or value == "":
            continue
        text = str(value).strip().replace(",", "").replace("¥", "").replace("￥", "").replace("元", "")
        text = text.replace("（", "(").replace("）", ")")
        try:
            normalized[canonical] = round2(float(text))
        except (TypeError, ValueError):
            continue
    return normalized


def _expected_from_ledger(period: str) -> dict[str, float] | None:
    """降级方案：用收入台账 + engine.calc_vat 推算各栏次标准值。"""
    total_inclusive = 0.0
    special_inclusive = 0.0
    try:
        from ledger.income_ledger import period_totals

        totals = period_totals(period)
        total_inclusive = float(totals.get("total_inclusive") or 0)
        special_inclusive = float(totals.get("special_inclusive") or 0)
    except Exception:  # noqa: BLE001
        try:
            from ledger import store

            start, end = engine.period_range(period)
            rows = store.table("income").filter(start=start.isoformat(), end=end.isoformat())
            total_inclusive = round2(sum(row.get("inclusive_amount", 0) for row in rows))
            special_inclusive = round2(
                sum(row.get("inclusive_amount", 0) for row in rows if str(row.get("invoice_type")) == "专票")
            )
        except Exception:  # noqa: BLE001
            return None
    try:
        calc = engine.calc_vat(total_inclusive, special_inclusive, period)
    except Exception:  # noqa: BLE001
        return None
    lines = calc.get("return_lines") or {}
    exempt_sales = float(lines.get("line_10_exempt_sales") or 0)
    return {
        "line_1": float(lines.get("line_1_taxable_sales") or 0),
        "line_2": 0.0,
        "line_10": exempt_sales,
        "line_11": exempt_sales if calc.get("within_exemption_limit") else 0.0,
        "line_18": float(lines.get("line_18_tax_amount") or 0),
        "line_19": float(lines.get("line_19_tax_exempt_amount") or 0),
        "line_22": float(lines.get("line_22_reduction") or 0),
        "line_24": float(lines.get("line_24_payable") or 0),
        "surtax_total": float(calc.get("surtax") or 0),
        "total_payable": round2(
            float(lines.get("line_24_payable") or 0) + float(calc.get("surtax") or 0)
        ),
    }


def _filing_validate(period: str, fields: dict[str, float]) -> dict[str, Any] | None:
    """调用 filing.validator 做标准校验；模块未就绪或异常时返回 None。"""
    try:
        from filing.validator import validate_against_fields

        result = validate_against_fields(period, fields)
        if isinstance(result, dict) and result.get("status") in ("PASS", "WARNING", "ERROR"):
            return result
    except Exception:  # noqa: BLE001 - 未就绪时降级
        return None
    return None


def _compare_details(period: str, fields: dict[str, float]) -> tuple[list[dict[str, Any]], str]:
    """本地逐栏比对，返回 ``(details, validator_name)``。"""
    expected = _expected_from_ledger(period)
    validator_name = "local_fallback" if expected is not None else "logic_only"
    details: list[dict[str, Any]] = []

    if expected is None:
        expected = {}
    for key, actual in fields.items():
        target = expected.get(key)
        if target is None:
            details.append(
                {
                    "field": _label(key),
                    "expected": None,
                    "actual": actual,
                    "status": "WARNING",
                    "message": "台账中无法取得该栏次标准值，请人工核对",
                }
            )
            continue
        diff = round2(actual - target)
        if abs(diff) <= _TOLERANCE:
            status, message = "PASS", "与台账数据一致"
        elif abs(diff) <= max(1.0, abs(target) * 0.01):
            status, message = "WARNING", f"与台账相差 {yuan(diff)}，请核对（可能为四舍五入或口径差异）"
        else:
            status, message = "ERROR", f"与台账相差 {yuan(diff)}，请核对"
        details.append(
            {
                "field": _label(key),
                "expected": round2(target),
                "actual": round2(actual),
                "status": status,
                "message": message,
            }
        )

    # 额外规则 1：小微企业免税销售额不得超过季度免税额度
    if "line_10" in fields and fields["line_10"] > _EXEMPTION_LIMIT + _TOLERANCE:
        details.append(
            {
                "field": _label("line_10"),
                "expected": _EXEMPTION_LIMIT,
                "actual": round2(fields["line_10"]),
                "status": "ERROR",
                "message": f"小微企业免税销售额超过季度免税标准 {yuan(_EXEMPTION_LIMIT)} 元，请核对是否超过 30 万元",
            }
        )

    # 额外规则 2：第 24 栏 = 第 18 栏 − 第 22 栏
    if all(key in fields for key in ("line_18", "line_22", "line_24")):
        calculated = round2(fields["line_18"] - fields["line_22"])
        if abs(calculated - fields["line_24"]) > _TOLERANCE:
            details.append(
                {
                    "field": _label("line_24"),
                    "expected": calculated,
                    "actual": round2(fields["line_24"]),
                    "status": "ERROR",
                    "message": "栏次勾稽关系不符：第24栏应等于第18栏 − 第22栏",
                }
            )

    # 额外规则 3：全普票免税场景，本期应补（退）税额应为 0
    if "line_24" in fields and "line_10" in fields and fields["line_10"] > 0 and fields.get("line_1", 0) == 0:
        if abs(fields["line_24"]) > _TOLERANCE:
            details.append(
                {
                    "field": _label("line_24"),
                    "expected": 0.0,
                    "actual": round2(fields["line_24"]),
                    "status": "ERROR",
                    "message": "免税场景（第1栏为 0）第24栏本期应补（退）税额应为 0",
                }
            )
    return details, validator_name


def _overall_status(details: list[dict[str, Any]]) -> str:
    status = "PASS"
    for detail in details:
        if _STATUS_RANK.get(str(detail.get("status")), 0) > _STATUS_RANK[status]:
            status = str(detail.get("status"))
    return status


def _summary(status: str, details: list[dict[str, Any]], validator_name: str) -> str:
    count = {level: sum(1 for item in details if item.get("status") == level) for level in ("PASS", "WARNING", "ERROR")}
    if status == "PASS":
        return f"申报表 {len(details)} 项栏次与台账数据一致，可以放心提交。"
    if status == "WARNING":
        return (
            f"申报表核对完成：{count['PASS']} 项一致，{count['WARNING']} 项需复核"
            f"（{count['ERROR']} 项差异），请人工确认后再提交。"
        )
    if validator_name == "logic_only":
        return (
            f"申报表存在 {count['ERROR']} 项差异、{count['WARNING']} 项需复核；"
            "且无法从台账取得标准值（台账为空或申报模块未就绪），请重点人工核对。"
        )
    return f"申报表存在 {count['ERROR']} 项与台账数据不一致，请核对后再提交。"


# =============================================================================
# 对外接口
# =============================================================================
def check_vat_return(fields: dict[str, Any], period: str) -> dict[str, Any]:
    """核对增值税申报表栏次与台账数据（复用 ``filing.validator.validate_against_fields``）。

    参数
    ----
    fields : 栏次字典，键可为 ``line_10`` / ``第10栏`` / ``10`` 等；值单位：元
    period : 所属季度，如 ``2026Q1``

    返回
    ----
    ``{"status", "details": [{"field", "expected", "actual", "status", "message"}],
       "summary", "disclaimer", "period", "fields", "validator"}``
    """
    normalized_period = _normalize_period(period)
    normalized_fields = _normalize_fields(fields)
    if not normalized_fields:
        return {
            "status": "ERROR",
            "details": [],
            "summary": "未识别到任何申报表栏次金额：请重新上传清晰截图，或按「第10栏 101980.20」每行一条手动粘贴。",
            "disclaimer": DISCLAIMER,
            "period": normalized_period,
            "fields": {},
            "validator": "none",
        }

    filing_result = _filing_validate(normalized_period, normalized_fields)
    if filing_result is not None:
        result = dict(filing_result)
        result["validator"] = "filing.validator"
        details = list(result.get("details") or [])
        # 兼容 filing 返回的 details 字段命名
        for detail in details:
            detail.setdefault("message", "")
        result["details"] = details
        result["status"] = result.get("status") or _overall_status(details)
        result.setdefault("summary", _summary(result["status"], details, "filing.validator"))
    else:
        details, validator_name = _compare_details(normalized_period, normalized_fields)
        status = _overall_status(details)
        result = {
            "status": status,
            "details": details,
            "summary": _summary(status, details, validator_name),
            "validator": validator_name,
        }

    result.setdefault("disclaimer", DISCLAIMER)
    result["period"] = normalized_period
    result["fields"] = normalized_fields
    return result


def check_from_text(text: str, period: str) -> dict[str, Any]:
    """从粘贴 / OCR 文本核对申报表：文本 → 栏次映射 → 逐栏比对。"""
    normalized_period = _normalize_period(period)
    mapped = field_mapper.map_fields(str(text or ""), period=normalized_period)
    result = check_vat_return(mapped.get("fields") or {}, normalized_period)
    result["mapping"] = {
        "method": mapped.get("method"),
        "unmapped": mapped.get("unmapped") or [],
        "llm_error": mapped.get("llm_error") or "",
    }
    result["raw_text"] = str(text or "")[:2000]
    if not mapped.get("fields"):
        result["status"] = "ERROR"
        result["summary"] = (
            "未能从文本中识别出申报表栏次金额。请按月/栏粘贴，例如：\n"
            "第10栏 小微企业免税销售额 101980.20\n第24栏 本期应补（退）税额 0.00"
        )
    return result


def check_from_image(image_path: str, period: str) -> dict[str, Any]:
    """从申报表截图核对：本地 OCR → 栏次映射 → 逐栏比对。

    注意：DeepSeek 无视觉能力，截图必须先经本地 OCR（macOS Vision / tesseract）；
    OCR 不可用时返回中文提示，引导用户使用「手动粘贴文本」入口。
    """
    normalized_period = _normalize_period(period)
    extracted = extractor.extract_text(image_path)
    ocr_info = {
        "backend": extracted.get("backend"),
        "ok": bool(extracted.get("ok")),
        "message": extracted.get("message") or "",
    }
    if not extracted.get("ok") or not str(extracted.get("text") or "").strip():
        return {
            "status": "ERROR",
            "details": [],
            "summary": extracted.get("message")
            or "OCR 未能识别图片文字，请改用「手动粘贴文本」入口录入申报表栏次。",
            "disclaimer": DISCLAIMER,
            "period": normalized_period,
            "fields": {},
            "ocr": ocr_info,
            "validator": "none",
        }
    result = check_from_text(str(extracted.get("text") or ""), normalized_period)
    result["ocr"] = ocr_info
    return result
