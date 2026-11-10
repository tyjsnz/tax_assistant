"""P0-3 归档与备份恢复测试（tests/test_archiving.py）。

覆盖契约 3.6：SHA-256 / canonical_json 稳定性、manifest 校验、归档产物与哈希、
PDF 中文文本、篡改/缺失检测、重复归档历史、备份不含 API Key、备份校验 /
列表 / 删除、恢复预览与演练、实际恢复与安全备份、损坏 zip 与 zip-slip 拒绝。
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest

import config
from ledger import audit, store
from ledger.seed import seed_demo_data

from archiving import backup, filing_archive, hash as hash_tools, manifest as manifest_tools
from archiving import pdf as pdf_module, restore


# =============================================================================
# 测试隔离与工具
# =============================================================================
@pytest.fixture(autouse=True)
def _isolate_archive_dirs():
    """conftest 只清理 data/*.csv，这里额外清理归档与备份目录，保证用例隔离。"""
    data_dir = Path(config.DATA_DIR)
    for sub in ("archive", "backups"):
        shutil.rmtree(data_dir / sub, ignore_errors=True)
    yield
    for sub in ("archive", "backups"):
        shutil.rmtree(data_dir / sub, ignore_errors=True)


@pytest.fixture
def seeded():
    """加载 2026 年示例数据（供归档与备份用例使用）。"""
    result = seed_demo_data(reset=True)
    return result


def _actions(action: str) -> list[dict]:
    return [row for row in audit.read_log("user_action_log") if row.get("action") == action]


def _zip_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return archive.namelist()


def _zip_manifest(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        return json.loads(archive.read("manifest.json").decode("utf-8"))


def _zip_read(path: Path, name: str) -> bytes:
    with zipfile.ZipFile(path) as archive:
        return archive.read(name)


def _rewrite_zip(path: Path, replace: dict[str, bytes] | None = None, extra: dict[str, bytes] | None = None) -> Path:
    """重写备份 zip（替换/追加条目），用于模拟篡改与未知文件。"""
    replace = replace or {}
    extra = extra or {}
    tmp = path.with_name(path.name + ".rewrite.tmp")
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as target:
        for name in source.namelist():
            data = replace.get(name, source.read(name))
            target.writestr(name, data)
        for name, data in extra.items():
            target.writestr(name, data)
    tmp.replace(path)
    return path


def _write_zip(path: Path, members: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def _minimal_manifest(files: list[dict], **overrides) -> dict:
    manifest = {
        "format_version": "1.0",
        "kind": "数据备份",
        "created_at": config.now_text(),
        "app_version": config.APP_VERSION,
        "files": files,
        "tables": {},
        "include_logs": False,
        "disclaimer": config.DISCLAIMER,
    }
    manifest.update(overrides)
    return manifest




# =============================================================================
# 1. 哈希与 manifest 工具
# =============================================================================
def test_sha256_bytes_and_file_stable(tmp_path):
    """sha256_bytes 与 sha256_file 结果一致且可复现。"""
    target = tmp_path / "a.txt"
    target.write_bytes(b"abc")
    assert hash_tools.sha256_bytes(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    assert hash_tools.sha256_file(target) == hash_tools.sha256_bytes(target.read_bytes())
    assert hash_tools.sha256_file(target) == hash_tools.sha256_file(target)
    target.write_bytes(b"abcd")
    assert hash_tools.sha256_file(target) != hash_tools.sha256_bytes(b"abc")


def test_canonical_json_and_hash_json_stable():
    """canonical_json 键排序稳定、中文不转义，hash_json 可复现。"""
    left = {"b": 2, "a": "中文", "c": [1, 2]}
    right = {"c": [1, 2], "a": "中文", "b": 2}
    assert hash_tools.canonical_json(left) == hash_tools.canonical_json(right)
    assert "中文" in hash_tools.canonical_json(left)
    assert " " not in hash_tools.canonical_json(left)
    assert hash_tools.hash_json(left) == hash_tools.hash_json(right)
    assert len(hash_tools.hash_json(left)) == 64


def test_manifest_build_and_validate(tmp_path):
    """manifest 可构造、写入、读取，结构校验能发现缺字段。"""
    file_path = tmp_path / "x.csv"
    file_path.write_bytes(b"id,name\n1,a\n")
    entry = manifest_tools.file_entry(file_path)
    manifest = manifest_tools.build_manifest(
        kind="数据备份",
        files=[entry],
        tables={"收入台账": 1},
        policy_version="v1",
        policy_basis_text="政策依据",
        note="测试",
    )
    target = manifest_tools.write_manifest(tmp_path / "manifest.json", manifest)
    loaded = manifest_tools.read_manifest(target)
    assert loaded["format_version"] == manifest_tools.FORMAT_VERSION
    assert loaded["files"][0]["sha256"] == entry["sha256"]
    assert manifest_tools.validate_manifest(loaded, required=manifest_tools.BACKUP_FIELDS)["ok"]
    broken = manifest_tools.validate_manifest({"kind": "数据备份"})
    assert not broken["ok"]
    assert "format_version" in broken["missing"]
    bad_version = dict(loaded, format_version="9.9")
    assert not manifest_tools.validate_manifest(bad_version)["ok"]
    with pytest.raises(ValueError):
        manifest_tools.read_manifest(tmp_path / "缺失.json")


# =============================================================================
# 2. 申报包归档
# =============================================================================
def test_archive_creates_files_and_db_record(seeded):
    """归档产物齐全（xlsx/pdf/manifest），并写入 archives 表与审计日志。"""
    result = filing_archive.archive_filing_package("2026Q1", note="单元测试")
    assert result["ok"] is True
    target_dir = Path(result["dir"])
    assert target_dir == filing_archive.archive_dir("2026Q1")
    assert target_dir == Path(config.DATA_DIR) / "archive" / "2026Q1"
    for name in (f"申报包_2026Q1.xlsx", f"申报表_2026Q1.pdf", "manifest.json"):
        assert (target_dir / name).is_file(), name
    names = {item["name"] for item in result["files"]}
    assert names == {"申报包_2026Q1.xlsx", "申报表_2026Q1.pdf", "manifest.json"}
    manifest_item = next(item for item in result["files"] if item["name"] == "manifest.json")
    assert manifest_item["sha256"] == result["manifest_hash"]
    assert result["pdf"] is True
    assert result["manifest_hash"] == hash_tools.sha256_file(Path(result["manifest_path"]))
    rows = store.table("archives").read()
    assert len(rows) == 1
    assert rows[0]["period"] == "2026Q1"
    assert rows[0]["kind"] == "申报包"
    assert rows[0]["path"] == result["manifest_path"]
    assert rows[0]["file_hash"] == result["manifest_hash"]
    assert rows[0]["size_bytes"] == result["size_bytes"]
    assert _actions("申报包归档")


def test_archive_manifest_hash_matches_files(seeded):
    """manifest 字段完整，逐文件 SHA-256 与磁盘一致。"""
    result = filing_archive.archive_filing_package("2026Q1")
    target_dir = Path(result["dir"])
    manifest = manifest_tools.read_manifest(target_dir / "manifest.json")
    check = manifest_tools.validate_manifest(manifest, required=manifest_tools.ARCHIVE_FIELDS)
    assert check["ok"], check
    assert manifest["period"] == "2026Q1"
    assert manifest["kind"] == "申报包"
    assert manifest["app_version"] == config.APP_VERSION
    assert manifest["disclaimer"] == config.DISCLAIMER
    assert manifest["policy_basis_text"]
    assert manifest["policy_version"]
    assert len(manifest["forms"]) == 4
    assert manifest["checks"]["summary"].keys() >= {"PASS", "WARNING", "ERROR"}
    for entry in manifest["files"]:
        assert entry["sha256"] == hash_tools.sha256_file(target_dir / entry["name"])
        assert entry["size"] == (target_dir / entry["name"]).stat().st_size


def test_archive_pdf_text_chinese(seeded):
    """PDF 可用 pypdf 提取中文（「申报表」「免责声明」「仅供参考」不乱码）。"""
    pytest.importorskip("pypdf")
    from pypdf import PdfReader

    result = filing_archive.archive_filing_package("2026Q1")
    pdf_path = Path(result["dir"]) / "申报表_2026Q1.pdf"
    reader = PdfReader(str(pdf_path))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    for keyword in ("申报表", "免责声明", "仅供参考", "校验结果汇总", "2026年第一季度"):
        assert keyword in text, keyword
    assert config.get_company()["name"] in text
    assert "第 1 页" in text
    assert "�" not in text
    assert len(reader.pages) >= 3


def test_archive_returns_forms_and_checks(seeded):
    """归档返回报表编码、校验汇总与文件角色。"""
    result = filing_archive.archive_filing_package("2026Q1")
    assert set(result["forms"]) == {
        "VAT_QUARTERLY",
        "CIT_PREPAY",
        "FIN_REPORT",
        "STAMP_QUARTERLY",
    }
    summary = result["checks_summary"]
    assert set(summary) == {"PASS", "WARNING", "ERROR"}
    assert sum(summary.values()) == 4
    assert all(item["status"] in summary for item in result["checks"])
    roles = {item.get("role") for item in result["files"]}
    assert roles == {"excel", "pdf", "manifest"}


def test_archive_unclosed_period_warning(seeded):
    """期间未结账时归档成功但给出「建议先结账再归档」提示，且不改变期间状态。"""
    assert store.table("periods").count() == 0
    result = filing_archive.archive_filing_package("2026Q1")
    assert result["ok"] is True
    assert result["not_closed_warning"] is True
    assert any("建议先结账再归档" in item for item in result["warnings"])
    assert store.table("periods").count() == 0
    from ledger import locks

    assert locks.status_of("2026Q1") == "未结账"


def test_archive_without_pdf(seeded):
    """include_pdf=False 时只产出 Excel 与 manifest。"""
    result = filing_archive.archive_filing_package("2026Q1", include_pdf=False)
    assert result["ok"] is True
    assert result["pdf"] is False
    target_dir = Path(result["dir"])
    assert (target_dir / "申报包_2026Q1.xlsx").is_file()
    assert not (target_dir / "申报表_2026Q1.pdf").exists()
    manifest = manifest_tools.read_manifest(target_dir / "manifest.json")
    assert [entry["name"] for entry in manifest["files"]] == ["申报包_2026Q1.xlsx"]
    assert manifest["include_pdf"] is False


def test_archive_pdf_unavailable_degrades(seeded, monkeypatch):
    """reportlab 不可用时降级为「Excel + manifest」并返回 warnings。"""
    def _raise(*args, **kwargs):
        raise pdf_module.PdfUnavailableError("模拟 reportlab 缺失")

    monkeypatch.setattr(pdf_module, "build_filing_pdf", _raise)
    result = filing_archive.archive_filing_package("2026Q1")
    assert result["ok"] is True
    assert result["pdf"] is False
    assert any("降级" in item for item in result["warnings"])
    assert (Path(result["dir"]) / "申报包_2026Q1.xlsx").is_file()
    assert not (Path(result["dir"]) / "申报表_2026Q1.pdf").exists()


def test_archive_tamper_detected(seeded):
    """归档文件被追加内容后 verify_archive 报 tampered。"""
    result = filing_archive.archive_filing_package("2026Q1")
    xlsx = Path(result["dir"]) / "申报包_2026Q1.xlsx"
    with xlsx.open("ab") as handle:
        handle.write(b"tampered")
    verify = filing_archive.verify_archive(result["id"])
    assert verify["ok"] is False
    assert "申报包_2026Q1.xlsx" in verify["tampered"]
    assert any(item["name"] == "申报包_2026Q1.xlsx" and not item["ok"] for item in verify["files"])
    # 目录路径校验同样生效
    assert filing_archive.verify_archive(str(result["dir"]))["ok"] is False


def test_archive_missing_file_detected(seeded):
    """归档 PDF 被删除后 verify_archive 报 missing。"""
    result = filing_archive.archive_filing_package("2026Q1")
    (Path(result["dir"]) / "申报表_2026Q1.pdf").unlink()
    verify = filing_archive.verify_archive(result["id"])
    assert verify["ok"] is False
    assert "申报表_2026Q1.pdf" in verify["missing"]
    assert verify["tampered"] == []


def test_archive_repeat_keeps_history(seeded):
    """重复归档同期间：新增记录、保留旧 manifest 历史，不删除历史记录。"""
    first = filing_archive.archive_filing_package("2026Q1", note="第一次")
    second = filing_archive.archive_filing_package("2026Q1", note="第二次", include_pdf=False)
    assert second["history_manifest"]
    target_dir = Path(second["dir"])
    history_files = sorted(target_dir.glob("manifest_*.json"))
    assert history_files
    old_manifest = json.loads(history_files[0].read_text(encoding="utf-8"))
    assert old_manifest["kind"] == "申报包"
    assert old_manifest["note"] == "第一次"
    rows = store.table("archives").read()
    assert len(rows) == 2
    assert len(filing_archive.list_archives()) == 2
    assert first["id"] != second["id"]


def test_list_archives_filter_period(seeded):
    """list_archives 支持按期间过滤，按时间倒序。"""
    filing_archive.archive_filing_package("2026Q1", include_pdf=False)
    filing_archive.archive_filing_package("2026Q2", include_pdf=False)
    all_rows = filing_archive.list_archives()
    assert len(all_rows) == 2
    q1_rows = filing_archive.list_archives("2026Q1")
    assert len(q1_rows) == 1
    assert q1_rows[0]["period"] == "2026Q1"
    assert q1_rows[0]["verified"] is True
    assert q1_rows[0]["exists"] is True
    assert q1_rows[0]["pdf"] is False
    assert set(q1_rows[0]["forms"]) == {
        "VAT_QUARTERLY",
        "CIT_PREPAY",
        "FIN_REPORT",
        "STAMP_QUARTERLY",
    }


def test_verify_archive_by_path_and_bad_ref(seeded):
    """verify_archive 支持归档目录/manifest 路径；不存在的编号抛中文 ValueError。"""
    result = filing_archive.archive_filing_package("2026Q1", include_pdf=False)
    assert filing_archive.verify_archive(str(result["dir"]))["ok"] is True
    assert filing_archive.verify_archive(result["manifest_path"])["ok"] is True
    with pytest.raises(ValueError, match="未找到归档"):
        filing_archive.verify_archive("ARC999999")
    with pytest.raises(ValueError, match="未找到归档"):
        filing_archive.verify_archive(str(Path(config.DATA_DIR) / "不存在"))


# =============================================================================
# 3. 数据备份
# =============================================================================
def test_create_backup_excludes_sensitive_files(seeded):
    """备份绝不包含 user_settings.json / uploads / backups / archive。"""
    data_dir = Path(config.DATA_DIR)
    config.save_user_settings({"api_key": "sk-secret-should-not-backup"})
    (data_dir / "uploads").mkdir(parents=True, exist_ok=True)
    (data_dir / "uploads" / "发票.xml").write_bytes(b"<xml/>")
    (data_dir / "archive" / "2026Q1").mkdir(parents=True, exist_ok=True)
    (data_dir / "archive" / "2026Q1" / "x.txt").write_text("archive", encoding="utf-8")

    result = backup.create_backup(tag="secure", note="不含敏感信息")
    zip_path = Path(result["path"])
    assert zip_path.is_file()
    assert "secure" in zip_path.name
    names = _zip_names(zip_path)
    assert "manifest.json" in names
    assert "README.txt" in names
    assert not any("user_settings" in name for name in names)
    assert not any(name.startswith("uploads/") for name in names)
    assert not any(name.startswith("backups/") for name in names)
    assert not any(name.startswith("archive/") for name in names)
    assert not any(name.endswith(".tmp") for name in names)
    data_entries = [name for name in names if name.startswith("data/")]
    assert data_entries and all(name.endswith(".csv") for name in data_entries)
    manifest = _zip_manifest(zip_path)
    assert "user_settings.json" in manifest["excluded"]
    assert not any("user_settings" in str(entry["name"]) for entry in manifest["files"])
    assert "sk-secret-should-not-backup" not in _zip_read(zip_path, "README.txt").decode("utf-8", "ignore")


def test_backup_manifest_tables_and_hashes(seeded):
    """备份 manifest 的文件哈希、各表条数与实际一致。"""
    audit.log_user_action("测试准备", "写一条日志，验证备份包含日志")
    result = backup.create_backup(note="manifest 校验")
    manifest = result["manifest"]
    check = manifest_tools.validate_manifest(manifest, required=manifest_tools.BACKUP_FIELDS)
    assert check["ok"], check
    assert manifest["kind"] == "数据备份"
    assert manifest["disclaimer"] == config.DISCLAIMER
    zip_path = Path(result["path"])
    for entry in manifest["files"]:
        assert hash_tools.sha256_bytes(_zip_read(zip_path, entry["name"])) == entry["sha256"]
        assert _zip_read(zip_path, entry["name"]).__len__() == entry["size"]
    assert manifest["tables"]["收入台账"] == store.table("income").count()
    assert manifest["tables"]["费用台账"] == store.table("expense").count()
    assert manifest["tables"]["分录行"] == store.table("journal_lines").count()
    assert manifest["table_keys"]["income"] == store.table("income").count()
    assert manifest["table_labels"]["income"] == "收入台账"
    assert manifest["include_logs"] is True
    assert any(entry["name"].startswith("logs/") for entry in manifest["files"])
    without_logs = backup.create_backup(include_logs=False)
    assert not any(entry["name"].startswith("logs/") for entry in without_logs["manifest"]["files"])
    assert without_logs["include_logs"] is False


def test_backup_list_verify_and_audit(seeded):
    """list_backups / verify_backup 正常，创建与删除均写审计日志。"""
    result = backup.create_backup(tag="list", note="列表校验")
    rows = backup.list_backups()
    assert len(rows) == 1
    assert rows[0]["id"] == result["id"]
    assert rows[0]["exists"] is True
    assert rows[0]["file_count"] == result["file_count"]
    assert rows[0]["tables"]["收入台账"] == store.table("income").count()
    verify = backup.verify_backup(result["id"])
    assert verify["ok"] is True
    assert verify["tampered"] == [] and verify["missing"] == []
    assert verify["file_hash"] == result["file_hash"]
    assert all(item["ok"] for item in verify["files"])
    assert _actions("数据备份")


def test_delete_backup(seeded):
    """delete_backup 删除文件与记录并写审计；重复删除抛 ValueError。"""
    result = backup.create_backup(tag="delete")
    zip_path = Path(result["path"])
    assert backup.delete_backup(result["id"]) is True
    assert not zip_path.exists()
    assert backup.list_backups() == []
    assert _actions("数据备份删除")
    with pytest.raises(ValueError, match="未找到备份"):
        backup.delete_backup(result["id"])


def test_backup_tamper_detected(seeded):
    """备份内的台账被替换后 verify_backup 报 tampered 且整体哈希不一致。"""
    result = backup.create_backup()
    zip_path = Path(result["path"])
    _rewrite_zip(zip_path, replace={"data/income_ledger.csv": b"id,date\nHACK,2026-01-01\n"})
    verify = backup.verify_backup(result["id"])
    assert verify["ok"] is False
    assert "data/income_ledger.csv" in verify["tampered"]
    assert verify["file_hash_ok"] is False


def test_inspect_backup_no_write(seeded):
    """inspect_backup 预览 manifest/条数/校验，不写盘、不创建安全备份。"""
    result = backup.create_backup(tag="inspect")
    data_dir = Path(config.DATA_DIR)
    before = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in data_dir.glob("*.csv")
    }
    backup_count = len(list((data_dir / "backups").glob("*.zip")))
    preview = restore.inspect_backup(result["id"])
    assert preview["ok"] is True
    assert preview["created_at"]
    assert preview["app_version"] == config.APP_VERSION
    assert preview["files"]
    assert preview["tables"]["收入台账"] == store.table("income").count()
    assert preview["verify"]["ok"] is True
    after = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in data_dir.glob("*.csv")
    }
    assert before == after
    assert len(list((data_dir / "backups").glob("*.zip"))) == backup_count
    assert not _actions("数据恢复")


# =============================================================================
# 4. 恢复
# =============================================================================
def test_restore_dry_run_no_write(seeded):
    """restore_backup(dry_run=True) 只校验预览：不写台账、不建安全备份、不写恢复审计。"""
    result = backup.create_backup(tag="dry")
    store.table("income").append(
        {"date": "2026-02-15", "customer": "演练客户", "amount_inclusive": 11300}
    )
    before_rows = {name: store.table(name).count() for name in ("income", "expense")}
    backups_before = len(list((Path(config.DATA_DIR) / "backups").glob("*.zip")))
    dry = restore.restore_backup(result["id"], dry_run=True)
    assert dry["ok"] is True
    assert dry["dry_run"] is True
    assert dry["restored"] == []
    assert dry["safety_backup"] == ""
    after_rows = {name: store.table(name).count() for name in ("income", "expense")}
    assert before_rows == after_rows
    assert len(list((Path(config.DATA_DIR) / "backups").glob("*.zip"))) == backups_before
    assert any("演练" in item for item in dry["warnings"])
    assert not _actions("数据恢复")


def test_restore_actual_roundtrip_and_safety_backup(seeded):
    """实际恢复：台账恢复为备份时点数据，自动安全备份存在，写「数据恢复」审计。"""
    result = backup.create_backup(tag="before_restore")
    zip_path = Path(result["path"])
    original_income = store.table("income").count()
    original_expense = store.table("expense").count()
    original_income_sha = hash_tools.sha256_file(Path(config.DATA_DIR) / "income_ledger.csv")
    # 改数据：新增收入与费用
    store.table("income").append(
        {"date": "2026-02-20", "customer": "恢复测试客户", "amount_inclusive": 50000}
    )
    store.table("expense").append(
        {"date": "2026-02-21", "category": "办公费", "amount": 800}
    )
    assert store.table("income").count() == original_income + 1
    restored = restore.restore_backup(result["id"], dry_run=False, safety_backup=True)
    assert restored["ok"] is True
    assert restored["dry_run"] is False
    assert restored["safety_backup"]
    safety_path = Path(restored["safety_backup"])
    assert safety_path.is_file()
    assert "restore_safety" in safety_path.name
    assert store.table("income").count() == original_income
    assert store.table("expense").count() == original_expense
    assert hash_tools.sha256_file(Path(config.DATA_DIR) / "income_ledger.csv") == original_income_sha
    assert hash_tools.sha256_bytes(_zip_read(zip_path, "data/income_ledger.csv")) == original_income_sha
    restored_tables = {item["table"]: item["rows"] for item in restored["restored"]}
    assert restored_tables["收入台账"] == original_income
    assert restored_tables["费用台账"] == original_expense
    assert _actions("数据恢复")


def test_restore_corrupt_zip_rejected(seeded):
    """损坏 zip 拒绝恢复，且不会创建安全备份。"""
    result = backup.create_backup(tag="corrupt")
    zip_path = Path(result["path"])
    zip_path.write_bytes(b"this is not a zip file")
    backups_before = len(list((Path(config.DATA_DIR) / "backups").glob("*.zip")))
    with pytest.raises(ValueError, match="损坏|无法读取|哈希"):
        restore.restore_backup(result["id"], dry_run=False)
    assert len(list((Path(config.DATA_DIR) / "backups").glob("*.zip"))) == backups_before
    assert not _actions("数据恢复")


def test_restore_zip_slip_rejected(seeded, tmp_path):
    """含 ../ 或绝对路径条目的备份整体拒绝（zip-slip）。"""
    payload = b"id,date\nHACK,2026-01-01\n"
    entry = {"name": "../evil.csv", "sha256": hash_tools.sha256_bytes(payload), "size": len(payload)}
    manifest = _minimal_manifest([entry])
    relative_zip = _write_zip(
        tmp_path / "evil_relative.zip",
        {"manifest.json": json.dumps(manifest, ensure_ascii=False).encode("utf-8"), "../evil.csv": payload},
    )
    with pytest.raises(ValueError, match="拒绝|zip-slip|越界"):
        restore.restore_backup(relative_zip, dry_run=True)
    assert not (Path(config.DATA_DIR).parent / "evil.csv").exists()

    absolute_name = "/tmp/tax_evil.csv"
    absolute_entry = {
        "name": absolute_name,
        "sha256": hash_tools.sha256_bytes(payload),
        "size": len(payload),
    }
    absolute_manifest = _minimal_manifest([absolute_entry])
    absolute_zip = _write_zip(
        tmp_path / "evil_absolute.zip",
        {"manifest.json": json.dumps(absolute_manifest, ensure_ascii=False).encode("utf-8"), absolute_name: payload},
    )
    with pytest.raises(ValueError, match="拒绝|绝对路径|zip-slip"):
        restore.restore_backup(absolute_zip, dry_run=True)
    assert not Path(absolute_name).exists()


def test_restore_unknown_member_rejected(seeded, tmp_path):
    """备份含清单外文件时整体拒绝。"""
    result = backup.create_backup(tag="unknown")
    copied = tmp_path / "unknown_member.zip"
    shutil.copy2(result["path"], copied)
    _rewrite_zip(copied, extra={"evil.csv": b"id\n1\n"})
    with pytest.raises(ValueError, match="未在清单|白名单"):
        restore.restore_backup(copied, dry_run=False)
    assert not _actions("数据恢复")


def test_restore_missing_manifest_rejected(seeded, tmp_path):
    """缺少 manifest.json 的 zip 拒绝恢复。"""
    zip_path = _write_zip(
        tmp_path / "no_manifest.zip",
        {"README.txt": "这是一个没有 manifest 的备份".encode("utf-8")},
    )
    with pytest.raises(ValueError, match="manifest"):
        restore.restore_backup(zip_path, dry_run=True)


def test_restore_hash_mismatch_rejected(seeded, tmp_path):
    """备份内文件哈希与 manifest 不一致时拒绝恢复。"""
    result = backup.create_backup(tag="hash")
    copied = tmp_path / "copied_backup.zip"
    shutil.copy2(result["path"], copied)
    _rewrite_zip(copied, replace={"data/income_ledger.csv": b"id,date\nHACK,2026-01-01\n"})
    with pytest.raises(ValueError, match="校验未通过|哈希"):
        restore.restore_backup(copied, dry_run=True)
    assert not _actions("数据恢复")
    # 原台账未被修改
    assert "HACK" not in (Path(config.DATA_DIR) / "income_ledger.csv").read_text(encoding="utf-8")
