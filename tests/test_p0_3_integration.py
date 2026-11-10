"""P0-3 端到端集成测试：结账 → 期间锁 → 归档 → 备份 → 恢复（Lead 维护）。"""

from __future__ import annotations

from pathlib import Path

import pytest

import config
from ledger import income_ledger, journal, store
from ledger import locks


def _add_jan_income() -> dict:
    return income_ledger.add_income("2026-01-20", "集成测试客户", 10100, invoice_type="普票", description="集成测试")


class TestCloseAndLock:
    def test_close_lock_archive_backup_restore_flow(self, demo_data, tmp_path):
        from archiving import backup, filing_archive, restore
        from closing import checks, close, snapshot

        # 1) 结账前检查（1 月为首月，上月未结为 WARNING，允许 force）
        report = checks.pre_close_checks("2026-01")
        assert report["period"] == "2026-01"
        assert report["checks"]
        assert report["level"] in {"PASS", "WARNING", "ERROR"}
        assert all({"key", "name", "status", "message"} <= set(check) for check in report["checks"])

        # 2) 结账：生成损益结转凭证、写快照、锁定期间
        closed = close.close_period("2026-01", operator="test", force=True)
        assert closed["ok"] is True
        assert closed["status"] == "已结账"
        assert closed["snapshot_hash"]
        assert locks.is_locked("2026-01-15") is True
        assert locks.status_of("2026-01-15") == "已结账"
        assert journal.trial_balance()["balanced"] is True

        # 3) 期间锁：台账/凭证写入被拒绝
        with pytest.raises(locks.PeriodLockedError):
            income_ledger.add_income("2026-01-21", "锁定测试", 100)
        with pytest.raises(locks.PeriodLockedError):
            journal.post_entry(
                {
                    "date": "2026-01-21",
                    "description": "锁定测试",
                    "lines": [
                        {"account": "银行存款", "direction": "借", "amount": 100},
                        {"account": "主营业务收入", "direction": "贷", "amount": 100},
                    ],
                }
            )
        # 未结账期间仍可记账
        feb = income_ledger.add_income("2026-02-20", "二月客户", 10100, invoice_type="普票", description="二月收入")
        assert feb["id"]

        # 4) 快照校验：正常通过；篡改台账（绕过锁直接写表）后可发现
        assert snapshot.verify_snapshot("2026-01")["ok"] is True
        store.table("income").append(
            {
                "date": "2026-01-31",
                "client": "篡改数据",
                "inclusive_amount": 999,
                "exclusive_amount": 989.11,
                "vat_amount": 9.89,
                "invoice_type": "普票",
                "category": "技术服务",
            }
        )
        tampered = snapshot.verify_snapshot("2026-01")
        assert tampered["ok"] is False
        assert tampered["changes"]

        # 5) 归档：Excel + PDF + manifest 哈希；篡改检测
        archived = filing_archive.archive_filing_package("2026Q1", include_pdf=True)
        assert archived["ok"] is True
        files = {item["name"]: item for item in archived["files"]}
        assert files and any(name.endswith(".xlsx") for name in files)
        manifest_path = Path(archived["manifest_path"])
        assert manifest_path.exists() and manifest_path.name == "manifest.json"
        assert archived["manifest_hash"]
        verify = filing_archive.verify_archive(archived["manifest_path"])
        assert verify["ok"] is True
        xlsx = next(Path(item["path"]) for item in archived["files"] if item["name"].endswith(".xlsx"))
        original = xlsx.read_bytes()
        xlsx.write_bytes(original + b"tamper")
        try:
            tampered_archive = filing_archive.verify_archive(archived["manifest_path"])
            assert tampered_archive["ok"] is False
            assert tampered_archive["tampered"]
        finally:
            xlsx.write_bytes(original)

        # 6) 备份 → 改数据 → 恢复：恢复后回到备份时刻的数据
        created = backup.create_backup(tag="integration", note="集成测试")
        assert Path(created["path"]).exists()
        assert created["file_hash"]
        assert all("user_settings" not in str(item.get("name")) for item in created["files"])
        before_count = store.table("income").count()
        extra = store.table("income").append(
            {
                "date": "2026-03-01",
                "client": "恢复前新增",
                "inclusive_amount": 500,
                "exclusive_amount": 495.05,
                "vat_amount": 4.95,
                "invoice_type": "普票",
                "category": "技术服务",
            }
        )
        assert store.table("income").count() == before_count + 1

        preview = restore.inspect_backup(created["path"])
        assert preview["ok"] is True
        assert preview["tables"].get("收入台账") == before_count
        dry = restore.restore_backup(created["path"], dry_run=True)
        assert dry["dry_run"] is True and dry["restored"] == []
        assert store.table("income").count() == before_count + 1  # dry-run 不写盘

        restored = restore.restore_backup(created["path"], dry_run=False, safety_backup=True)
        assert restored["ok"] is True
        assert store.table("income").count() == before_count
        assert store.table("income").get(extra["id"]) is None
        assert restored["safety_backup"] and Path(restored["safety_backup"]).exists()

        # 7) 反结账：恢复最近结账期间并删除结转凭证、允许再记账
        reopened = close.reopen_period("2026-01", operator="test", reason="集成测试反结账")
        assert reopened["ok"] is True
        assert locks.is_locked("2026-01-15") is False
        assert income_ledger.add_income("2026-01-22", "反结账后可记", 100, invoice_type="普票")["id"]


class TestCLI:
    def test_periods_and_backups_commands(self, demo_data, capsys):
        import main

        assert main.main(["periods", "--limit", "3"]) == 0
        assert "会计期间状态" in capsys.readouterr().out

        assert main.main(["backups"]) == 0
        assert "数据备份" in capsys.readouterr().out
