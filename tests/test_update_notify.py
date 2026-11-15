"""版本检查测试（v1.36）。"""

from __future__ import annotations

from ui.components import update_notify


def test_parse_and_compare():
    assert update_notify.parse_version("v1.35.2") == (1, 35, 2)
    assert update_notify.is_newer("1.36.0", "1.35.2") is True
    assert update_notify.is_newer("1.35.2", "1.35.2") is False
    assert update_notify.is_newer("1.35.1", "1.35.2") is False


def test_fetch_disabled(monkeypatch):
    monkeypatch.setenv("TAX_DISABLE_UPDATE_CHECK", "1")
    assert update_notify.fetch_latest() is None


def test_fetch_mocked(monkeypatch):
    class FakeResponse:
        status_code = 200
        def json(self):
            return {"version": "1.36.0", "downloads": {"macos": "https://example.com/mac.zip"}}
    monkeypatch.setattr(update_notify.requests, "get", lambda *a, **k: FakeResponse())
    info = update_notify.fetch_latest()
    assert info and info["version"] == "1.36.0"
