"""发票自动归集测试（v1.25）。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from ingest import collector


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ingest" / "invoice_purchase.xml"


def test_settings_roundtrip(tmp_path):
    settings = collector.save_settings({
        "folder_path": str(tmp_path), "email_host": "imap.example.com",
        "email_user": "u", "email_password": "p", "auto_match": True,
    })
    assert settings["folder_path"] == str(tmp_path)
    loaded = collector.load_settings()
    assert loaded["email_host"] == "imap.example.com"
    if os.name != "nt":
        assert oct(collector.SETTINGS_PATH.stat().st_mode & 0o777) == "0o600"


def test_collect_folder_imports_and_dedupes(tmp_path):
    folder = tmp_path / "invoices"
    folder.mkdir()
    shutil.copy(FIXTURE, folder / "invoice_purchase.xml")
    result = collector.collect_folder(str(folder), auto_match=False)
    assert result["scanned"] == 1
    assert result["imported"] >= 1
    again = collector.collect_folder(str(folder), auto_match=False)
    assert again["imported"] == 0
    assert again["skipped"] >= 1
    assert collector.recent_logs(5)


def test_collect_folder_invalid_path():
    with pytest.raises(ValueError):
        collector.collect_folder("/path/does/not/exist")


def test_email_requires_settings():
    with pytest.raises(ValueError):
        collector.test_email({"email_host": "", "email_user": "", "email_password": ""})
