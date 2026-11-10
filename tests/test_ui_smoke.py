"""UI 冒烟测试：入口渲染、逐页切换与关键交互不崩溃。

约束（与 docs/CONTRACTS.md 第 6 节一致）：
* 使用 ``streamlit.testing.v1.AppTest`` 在进程内运行真实入口脚本；
* 测试环境无 DeepSeek API Key、台账初始为空，页面不得崩溃；
* 页面级兜底会把异常转成 ``st.error``，因此除断言无异常外，还要断言不出现页面错误提示。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP_PATH = str(Path(__file__).resolve().parents[1] / "ui" / "streamlit_app.py")

PAGES = [
    "🏠 首页驾驶舱",
    "📒 台账管理",
    "📥 票据与流水",
    "🧮 算税工作台",
    "📋 申报填报",
    "🔍 申报核对",
    "📚 政策知识库",
    "🤖 AI 助手",
    "⚙️ 设置",
]

TIMEOUT = 180


def _new_app() -> AppTest:
    """创建并首次运行应用。"""
    at = AppTest.from_file(APP_PATH, default_timeout=TIMEOUT)
    at.run()
    return at


def _assert_clean(at: AppTest, context: str = "") -> None:
    """断言：无未捕获异常、无页面级错误提示。"""
    exceptions = [f"{item.type}: {item.value}" for item in at.exception]
    errors = [item.value for item in at.error]
    assert not exceptions, f"{context} 出现未捕获异常：{exceptions}"
    assert not errors, f"{context} 出现页面错误提示：{errors}"


def test_app_renders() -> None:
    """入口脚本可以正常渲染，侧边栏包含全部 9 个页面。"""
    at = _new_app()
    _assert_clean(at, "首页")
    radio = at.radio(key="nav")
    assert radio is not None
    assert list(radio.options) == PAGES


def test_switch_all_pages() -> None:
    """逐个切换侧边栏页面，均不出现异常或页面级错误。"""
    at = _new_app()
    for page in PAGES:
        at.radio(key="nav").set_value(page).run()
        _assert_clean(at, page)


def test_calculator_shows_disclaimer_and_results() -> None:
    """算税工作台：输入金额后出结果，且结果下方有免责声明。"""
    at = _new_app()
    at.radio(key="nav").set_value("🧮 算税工作台").run()
    _assert_clean(at, "算税工作台")

    at.number_input(key="calc_vat_total").set_value(20600.0).run()
    _assert_clean(at, "算税工作台（输入金额后）")
    metric_values = [metric.value for metric in at.metric]
    assert any("¥" in str(value) for value in metric_values), "应展示带 ¥ 的算税指标"
    html_text = "\n".join(element.value for element in at.html)
    assert "仅供参考" in html_text, "算税结果下方必须展示免责声明"


def test_ledger_page_has_four_tabs_and_editor() -> None:
    """台账管理：四个 tab 均在，空数据时有友好空态。"""
    at = _new_app()
    at.radio(key="nav").set_value("📒 台账管理").run()
    _assert_clean(at, "台账管理")
    labels = [tab.label for tab in at.tabs]
    assert any("收入" in label for label in labels)
    assert any("费用" in label for label in labels)
    assert any("研发" in label for label in labels)
    assert any("合同" in label for label in labels)
    # 空数据时页面展示「加载示例数据」按钮
    seed_buttons = [button for button in at.button if "示例数据" in button.label]
    assert seed_buttons, "空数据时必须提供「加载示例数据」按钮"


def test_load_demo_data_from_dashboard() -> None:
    """点击「加载示例数据」后页面不崩溃，并出现台账数据。"""
    pytest.importorskip("ledger.seed")
    at = _new_app()
    seed_buttons = [button for button in at.button if "示例数据" in button.label]
    assert seed_buttons, "首页空态应提供加载示例数据按钮"
    seed_buttons[0].click().run()
    _assert_clean(at, "加载示例数据后")

    at.radio(key="nav").set_value("📒 台账管理").run()
    _assert_clean(at, "加载示例数据后进入台账管理")
    assert at.data_editor, "加载示例数据后台账页应展示可编辑表格"


def test_filing_generate_bundle() -> None:
    """申报填报：生成申报表后逐表展示且无错误（依赖 filing.registry）。"""
    pytest.importorskip("filing.registry")
    at = _new_app()
    at.radio(key="nav").set_value("📋 申报填报").run()
    _assert_clean(at, "申报填报")
    generate = [button for button in at.button if "生成申报表" in button.label]
    assert generate, "申报填报页应有「生成申报表」按钮"
    generate[0].click().run()
    _assert_clean(at, "生成申报表后")
    assert at.expander, "生成后应逐表展示申报表 expander"


def test_assistant_offline_reply() -> None:
    """AI 助手：无 API Key 时给出离线模式提示，发送消息不崩溃。"""
    at = _new_app()
    at.radio(key="nav").set_value("🤖 AI 助手").run()
    _assert_clean(at, "AI 助手")
    infos = "\n".join(item.value for item in at.info)
    assert "离线模式" in infos, "无 API Key 时应提示离线模式"
    assert at.chat_input, "AI 助手页应有对话输入框"
    at.chat_input[0].set_value("这个季度开了 8 万普票，增值税怎么算？").run()
    _assert_clean(at, "AI 助手发送消息后")


def test_settings_masks_api_key() -> None:
    """设置页：展示脱敏状态，且 API Key 通过密码输入框填写。"""
    at = _new_app()
    at.radio(key="nav").set_value("⚙️ 设置").run()
    _assert_clean(at, "设置")
    captions = "\n".join(item.value for item in at.caption)
    assert "未配置" in captions or "已配置" in captions
    api_input = at.text_input(key="settings_api_key_value")
    assert api_input is not None, "设置页应提供 API Key 输入框"
    assert api_input.value == "", "API Key 输入框不应回显明文"
    assert not at.error


def test_ingest_page_renders_empty() -> None:
    """票据与流水：空数据下正常渲染上传区、匹配区、池 tabs 与免责声明。"""
    at = _new_app()
    at.radio(key="nav").set_value("📥 票据与流水").run()
    _assert_clean(at, "票据与流水（空数据）")

    labels = [button.label for button in at.button]
    assert any("一键加载示例" in label for label in labels), "应提供「一键加载示例票据/流水」按钮"
    assert any("自动匹配" in label for label in labels), "应提供「自动匹配」按钮"
    assert any("解析并入库" in label for label in labels), "应提供「解析并入库」按钮"
    assert any("导入流水" in label for label in labels), "应提供「导入流水」按钮"
    assert len(at.file_uploader) >= 2, "应提供发票与银行流水两个上传控件"
    tab_labels = [tab.label for tab in at.tabs]
    assert any("发票池" in label for label in tab_labels)
    assert any("银行流水池" in label for label in tab_labels)
    html_text = "\n".join(element.value for element in at.html)
    assert "仅供参考" in html_text, "票据页底部必须展示免责声明"


def test_ingest_load_demo_pool() -> None:
    """票据与流水：点击「一键加载示例」后票据池/流水池有数据、生成匹配建议，且无异常。"""
    pytest.importorskip("ingest.demo")
    import ledger

    at = _new_app()
    at.radio(key="nav").set_value("📥 票据与流水").run()
    _assert_clean(at, "票据与流水（加载示例前）")
    demo_buttons = [button for button in at.button if "一键加载示例" in button.label]
    assert demo_buttons, "应提供「一键加载示例票据/流水」按钮"
    demo_buttons[0].click().run()
    _assert_clean(at, "票据与流水（加载示例后）")

    assert ledger.table("invoices").count() >= 2, "示例发票应已入库"
    assert ledger.table("bank_transactions").count() >= 3, "示例流水应已入库"
    assert ledger.table("matches").count() >= 1, "示例数据应生成匹配建议"
    assert at.data_editor, "加载示例后应展示匹配建议/票据池表格"

    # 再次点击自动匹配不崩溃（已存在的组合会跳过）
    match_buttons = [button for button in at.button if "自动匹配" in button.label]
    assert match_buttons
    match_buttons[0].click().run()
    _assert_clean(at, "票据与流水（自动匹配后）")


def test_ingest_confirm_all_generates_ledger() -> None:
    """票据与流水：加载示例 → 确认全部建议入账 → 台账出现收入/费用记录与凭证。"""
    pytest.importorskip("ingest.demo")
    pytest.importorskip("ingest.matcher")
    import ledger

    at = _new_app()
    at.radio(key="nav").set_value("📥 票据与流水").run()
    demo_buttons = [button for button in at.button if "一键加载示例" in button.label]
    assert demo_buttons
    demo_buttons[0].click().run()
    _assert_clean(at, "票据与流水（确认前）")

    before = ledger.table("income").count() + ledger.table("expense").count()
    confirm_buttons = [button for button in at.button if "确认全部建议" in button.label]
    assert confirm_buttons, "应提供「确认全部建议」批量入账按钮"
    confirm_buttons[0].click().run()
    _assert_clean(at, "票据与流水（确认入账后）")

    after = ledger.table("income").count() + ledger.table("expense").count()
    assert after > before, "确认匹配合成后台账应出现收入/费用记录"
    booked = [
        row
        for row in ledger.table("invoices").read()
        if str(row.get("status")) == "已入账"
    ]
    assert booked, "确认后发票状态应更新为「已入账」"
    assert ledger.table("journal_entries").count() > 0, "确认后应生成记账凭证"
    captions = "\n".join(caption.value for caption in at.caption)
    assert "台账与凭证" in captions, "确认后应展示生成的台账与凭证号提示"


def test_dashboard_closing_card_renders_empty() -> None:
    """首页驾驶舱：空数据下「期间结账」卡片正常渲染（当前/上月状态、结账/反结账入口）。"""
    at = _new_app()
    at.radio(key="nav").set_value("🏠 首页驾驶舱").run()
    _assert_clean(at, "首页（结账卡片）")

    labels = [button.label for button in at.button]
    assert any("立即结账" in label for label in labels), "首页应有「立即结账」按钮"
    assert any("反结账" in label for label in labels), "首页应有「反结账」按钮"
    metric_labels = [metric.label for metric in at.metric]
    assert any("当前月" in label for label in metric_labels), "结账卡片应展示当前月状态"
    assert any("上月" in label for label in metric_labels), "结账卡片应展示上月状态"
    assert any("最近已结账" in label for label in metric_labels), "结账卡片应展示最近已结账月份"


def test_settings_data_management_renders() -> None:
    """设置页数据管理：备份 / 恢复 / 归档区域渲染正常，无错误提示。"""
    at = _new_app()
    at.radio(key="nav").set_value("⚙️ 设置").run()
    _assert_clean(at, "设置（数据管理）")

    labels = [button.label for button in at.button]
    assert any("创建备份" in label for label in labels), "数据管理应有「创建备份」按钮"
    assert any("预览备份内容" in label for label in labels), "数据管理应有备份预览按钮"
    assert any("清空全部台账" in label for label in labels), "数据管理应有清空台账入口"
    captions = "\n".join(caption.value for caption in at.caption)
    assert "备份" in captions or "归档" in captions
    html_text = "\n".join(element.value for element in at.html)
    assert "仅供参考" in html_text, "设置页应展示免责声明"

    # 后端就绪时：创建备份按钮可点击且不崩溃（临时数据目录）
    create_buttons = [button for button in at.button if button.label == "🗄️ 创建备份"]
    if create_buttons and not create_buttons[0].disabled:
        create_buttons[0].click().run()
        _assert_clean(at, "设置（创建备份后）")


def test_filing_archive_panel_renders_and_click_safe() -> None:
    """申报填报：归档按钮与状态徽标渲染正常；按钮可点击时点击不异常。"""
    at = _new_app()
    at.radio(key="nav").set_value("📋 申报填报").run()
    _assert_clean(at, "申报填报（归档面板）")

    labels = [button.label for button in at.button]
    assert any("归档本期申报包" in label for label in labels), "申报页应有「归档本期申报包」按钮"
    assert any("校验归档" in label for label in labels), "申报页应有「校验归档」按钮"
    metric_labels = [metric.label for metric in at.metric]
    assert any("期间结账状态" in label for label in metric_labels), "申报页应展示期间结账状态"
    assert any("申报包归档" in label for label in metric_labels), "申报页应展示归档状态"

    archive_buttons = [button for button in at.button if "归档本期申报包" in button.label]
    if archive_buttons and not archive_buttons[0].disabled:
        archive_buttons[0].click().run()
        _assert_clean(at, "申报填报（归档后）")
