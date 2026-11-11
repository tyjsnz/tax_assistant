"""AI 对话代办意图识别测试（v1.23）。"""

from __future__ import annotations

from automation import intents


def test_detect_filing_action():
    intent = intents.detect("帮我准备本季度申报")
    assert intent is not None
    assert intent["action"] == "filing"
    assert intent["label"]


def test_detect_payroll_action():
    intent = intents.detect("帮我生成本月工资表")
    assert intent is not None
    assert intent["action"] == "payroll"


def test_question_does_not_trigger():
    assert intents.detect("申报截止日是哪天？") is None
    assert intents.detect("增值税怎么算？") is None
    assert intents.detect("离职后还在申报怎么办？") is None


def test_ingest_and_correction():
    assert intents.detect("帮我把票据匹配入账")["action"] == "ingest"
    assert intents.detect("帮我扫描离职多申报并导出操作单")["action"] == "correction"


def test_preview_for_intent():
    intent = {"action": "payroll", "params": {"period": "2026-11"}}
    preview = intents.preview(intent)
    assert preview["action"] == "payroll"


def test_detect_pipeline():
    assert intents.detect("帮我跑一键季度流水线")["action"] == "pipeline"
    assert intents.detect("从票据到申报全流程处理一下")["action"] == "pipeline"


def test_pipeline_preview():
    preview = intents.preview({"action": "pipeline", "params": {"period": "2026Q4"}})
    assert preview["action"] == "pipeline"
    assert len(preview["steps"]) == 4
