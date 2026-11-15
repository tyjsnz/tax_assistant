"""资料找回清单测试（v1.37）。"""

from __future__ import annotations

from recovery import checklist


def test_steps_count_and_content():
    assert checklist.TOTAL_STEPS == 5
    assert len(checklist.STEPS) == 5
    for step in checklist.STEPS:
        assert step["title"] and step["summary"] and step["bullets"]


def test_progress_roundtrip():
    checklist.save_progress([True, True, False, False, False], "test-company")
    state = checklist.load_progress("test-company")
    assert state["steps"][:2] == [True, True]
    assert state["steps"][2:] == [False, False, False]


def test_progress_default():
    state = checklist.load_progress("no-such-company")
    assert state["steps"] == [False] * checklist.TOTAL_STEPS
