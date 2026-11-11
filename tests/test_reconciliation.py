"""发票对账看板 / 自动扫描测试（v1.26）。"""

from __future__ import annotations

import shutil
from pathlib import Path

from ingest import collector, reconciliation
from ingest.importer import import_invoice_files

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ingest" / "invoice_purchase.xml"


def test_reconciliation_counts_imported_invoice():
    import_invoice_files([FIXTURE], auto_match=False)
    data = reconciliation.build_dashboard()
    assert data["total"] >= 1
    assert data["pending"] >= 1
    assert data["current_month"]


def test_auto_scan_disabled_by_default():
    collector.save_settings({"auto_scan_on_start": False})
    result = collector.maybe_auto_scan()
    assert result.get("skipped") is True


def test_auto_scan_force_scans_folder(tmp_path):
    folder = tmp_path / "invoices"
    folder.mkdir()
    shutil.copy(FIXTURE, folder / "invoice_purchase.xml")
    collector.save_settings({
        "folder_path": str(folder), "auto_scan_on_start": True,
        "auto_scan_interval_hours": 6, "auto_match": False,
    })
    result = collector.maybe_auto_scan(force=True)
    assert result.get("folder", {}).get("scanned") == 1
