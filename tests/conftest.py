"""pytest 公共配置：把所有数据/日志/导出目录指向临时目录，保证测试隔离。

多公司（P1-A）改造后：
* ``TAX_DATA_DIR`` 指向临时数据根目录，``workspace.reset_all()`` 每个测试重建默认公司；
* 不污染开发者本地 data/、logs/、exports/；
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
import workspace  # noqa: E402
from ledger import store  # noqa: E402


def pytest_configure(config):  # noqa: A002  — pytest 钩子要求参数名必须为 config
    config.addinivalue_line("markers", "live: 需要真实 DeepSeek API Key 的联网测试")


def _clear_runtime_dirs() -> None:
    """清空日志与导出等公司目录之外的运行产物。"""
    for pattern, target in (
        ("*.csv", Path(config.LOGS_DIR)),
        ("*", Path(config.EXPORT_DIR)),
    ):
        if not target.exists():
            continue
        for path in target.glob(pattern):
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink()
            except OSError:
                pass


@pytest.fixture(scope="session", autouse=True)
def _prepare_dirs():
    config.ensure_dirs()
    workspace.reset_all()
    ledger.init_storage()
    yield
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_storage():
    """每个测试重建「默认公司 + 空台账 + 空日志」，保证用例互不影响。"""
    _clear_runtime_dirs()
    workspace.reset_all()
    ledger.init_storage()
    _clear_runtime_dirs()
    yield


@pytest.fixture
def data_dir() -> Path:
    """当前激活公司的数据目录。"""
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
