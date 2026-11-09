"""pytest 公共配置：把所有数据/日志/导出目录指向临时目录，保证测试隔离。

* 不污染开发者本地的 data/、logs/、exports/。
* 每个测试前清空全部台账与日志（``clean_storage`` autouse）。
* 测试环境默认无 DeepSeek API Key；需要联网的用例必须用 ``-m live`` 显式启用并自行跳过。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="tax_agent_test_"))
os.environ["TAX_DATA_DIR"] = str(_TMP_ROOT / "data")
os.environ["TAX_LOG_DIR"] = str(_TMP_ROOT / "logs")
os.environ["TAX_EXPORT_DIR"] = str(_TMP_ROOT / "exports")
os.environ.pop("DEEPSEEK_API_KEY", None)

import config  # noqa: E402  （必须在设置环境变量之后导入）
import ledger  # noqa: E402
from ledger import store  # noqa: E402


def pytest_configure(config_obj):
    config_obj.addinivalue_line("markers", "live: 需要真实 DeepSeek API Key 的联网测试")


@pytest.fixture(scope="session", autouse=True)
def _prepare_dirs():
    config.ensure_dirs()
    ledger.init_storage()
    yield
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_storage():
    """每个测试前重置全部台账、日志与本地设置，保证用例互不影响。"""
    config.ensure_dirs()
    data_dir = Path(config.DATA_DIR)
    for pattern in ("*.csv", "user_settings.json", "*.tmp"):
        for path in data_dir.glob(pattern):
            try:
                path.unlink()
            except OSError:
                pass
    logs_dir = Path(config.LOGS_DIR)
    for path in logs_dir.glob("*.csv"):
        try:
            path.unlink()
        except OSError:
            pass
    exports_dir = Path(config.EXPORT_DIR)
    for path in exports_dir.glob("*"):
        try:
            path.unlink()
        except OSError:
            pass
    store.reset_cache()
    ledger.init_storage()
    yield


@pytest.fixture
def data_dir() -> Path:
    """当前测试使用的临时数据目录。"""
    return Path(config.DATA_DIR)


@pytest.fixture
def demo_data():
    """加载示例数据（需 ledger.seed 已实现）。"""
    from ledger import seed

    seed.seed_demo_data(reset=True)
    return seed


@pytest.fixture
def no_api_key(monkeypatch):
    """确保测试环境没有 DeepSeek API Key。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    try:
        config.save_user_settings({"api_key": ""})
    except OSError:
        pass
    return True
