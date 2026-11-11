"""在 AI 对话里识别「代办」意图，返回预览/执行分派（v1.23）。

原则：只有**明确的动作请求**才触发代办，普通问答（“怎么申报”“截止日哪天”）
不会被误判成执行。
"""

from __future__ import annotations

from typing import Any, Callable

from automation import plans

ACTION_LABELS = {
    "payroll": "生成本月工资表",
    "ingest": "票据匹配并自动入账",
    "filing": "生成并归档季度申报包",
    "correction": "扫描离职异常并导出更正操作单",
    "pipeline": "一键季度流水线（票据→工资→异常→申报包）",
}

_STRONG_ACTIONS = (
    "帮我", "生成", "准备", "做一下", "处理", "一键", "代办", "自动",
    "入账", "匹配", "归档", "扫描", "导出", "执行",
)

_PREVIEWS: dict[str, Callable[..., dict[str, Any]]] = {
    "payroll": plans.preview_payroll,
    "ingest": plans.preview_ingest,
    "filing": plans.preview_filing,
    "correction": plans.preview_correction,
    "pipeline": plans.preview_pipeline,
}


def detect(text: str) -> dict[str, Any] | None:
    """识别代办请求；不确定时返回 None（走普通 AI 对话）。"""
    value = str(text or "").strip()
    if not value:
        return None
    lowered = value.lower()
    if not any(word in value for word in _STRONG_ACTIONS):
        return None
    # 纯概念/流程问答不触发执行
    if any(word in value for word in ("怎么", "如何", "为什么", "是什么", "吗？", "吗?")):
        # 但“帮我准备/帮我做/一键”仍视为明确动作
        if not any(word in value for word in ("帮我", "一键", "代办", "执行")):
            return None
    action = ""
    if ("流水线" in value or "全流程" in value) or (
        "一键" in value and "季度" in value and ("申报" in value or "代办" in value)
    ):
        action = "pipeline"
    elif ("工资" in value or "个税" in value) and any(word in value for word in ("生成", "做", "准备", "帮我", "计算")):
        action = "payroll"
    elif ("票据" in value or "发票" in value or "流水" in value) and any(
        word in value for word in ("匹配", "入账", "自动", "帮我", "处理")
    ):
        action = "ingest"
    elif ("离职" in value or "多申报" in value or "更正" in value) and any(
        word in value for word in ("扫描", "处理", "导出", "操作单", "帮我", "一键")
    ):
        action = "correction"
    elif ("申报" in value or "申报表" in value or "季度" in value) and any(
        word in value for word in ("准备", "生成", "归档", "帮我", "一键", "做")
    ):
        action = "filing"
    if not action:
        return None
    params: dict[str, Any] = {}
    if action == "payroll":
        params["period"] = plans.month_period()
    elif action in ("filing", "pipeline"):
        params["period"] = plans.quarter_period()
    return {
        "action": action,
        "label": ACTION_LABELS[action],
        "params": params,
        "request": value,
    }


def preview(intent: dict[str, Any]) -> dict[str, Any]:
    action = str(intent.get("action") or "")
    if action == "pipeline":
        return plans.preview_pipeline((intent.get("params") or {}).get("period"))
    func = _PREVIEWS.get(action)
    if func is None:
        raise ValueError(f"不支持的代办动作：{action}")
    return func(**(intent.get("params") or {}))


def execute(intent: dict[str, Any]) -> dict[str, Any]:
    action = str(intent.get("action") or "")
    if action == "payroll":
        return plans.execute_payroll(**intent.get("params", {}))
    if action == "ingest":
        return plans.execute_ingest(**intent.get("params", {}))
    if action == "filing":
        return plans.execute_filing(**intent.get("params", {}))
    if action == "correction":
        return plans.execute_correction(**intent.get("params", {}))
    if action == "pipeline":
        return plans.execute_pipeline((intent.get("params") or {}).get("period"))
    raise ValueError(f"不支持的代办动作：{action}")


def result_text(action: str, result: dict[str, Any]) -> str:
    """把执行结果转成一条适合聊天的中文摘要。"""
    if action == "payroll":
        period = result.get("period", "")
        return f"✅ 已生成 {period} 工资表草稿，并创建执行前备份。请到「👥 员工与工资」复核。"
    if action == "ingest":
        return (
            f"✅ 已完成票据匹配：自动确认入账 {result.get('confirmed_count', 0)} 条，"
            f"跳过 {len(result.get('skipped') or [])} 条；执行前已备份。"
        )
    if action == "filing":
        period = result.get("period", "")
        return (
            f"✅ 已生成并归档 {period} 申报包（Excel/PDF/哈希清单），执行前已备份。"
            "请在「📋 申报填报」核对，并到电子税务局手工提交。"
        )
    if action == "correction":
        return (
            f"✅ 已扫描离职异常并为 {len(result.get('exported') or [])} 位员工生成更正操作单，"
            "执行前已备份；请到「🧯 离职与纠错」复核并到扣缴端更正。"
        )
    if action == "pipeline":
        steps = result.get("steps") or []
        ok = sum(1 for item in steps if item.get("ok"))
        return (
            f"✅ 季度流水线执行完成：{ok}/{len(steps)} 步成功，执行前已备份。"
            "请到「👥 员工与工资」「📥 票据与流水」「📋 申报填报」逐步复核；申报仍需在电子税务局手工提交。"
        )
    return "✅ 代办执行完成。"


__all__ = ["detect", "preview", "execute", "result_text", "ACTION_LABELS"]
