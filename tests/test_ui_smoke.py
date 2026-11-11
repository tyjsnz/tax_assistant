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

import config

APP_PATH = str(Path(__file__).resolve().parents[1] / "ui" / "streamlit_app.py")

PAGES = [
    "🏠 首页驾驶舱",
    "📒 台账管理",
    "👥 员工与工资",
    "🧯 离职与纠错",
    "📥 票据与流水",
    "🧮 算税工作台",
    "📋 申报填报",
    "📊 汇算清缴",
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
    """入口脚本可以正常渲染，侧边栏包含全部 12 个页面。"""
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
    assert "仅供参考" in html_text


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


def test_sidebar_company_switcher_renders() -> None:
    """侧边栏存在公司切换器，并展示当前公司地区与城建税税率。"""
    import workspace

    at = _new_app()
    _assert_clean(at, "首页（公司切换器）")
    switcher = at.selectbox(key="company_switch")
    assert switcher is not None, "侧边栏应有「当前公司」切换器"
    assert str(switcher.value) == str(workspace.active_company_id())
    captions = "\n".join(caption.value for caption in at.caption)
    assert "城建税" in captions, "侧边栏应展示城建税税率"
    assert "地区" in captions, "侧边栏应展示公司地区"


def test_settings_company_tab_renders() -> None:
    """设置页「企业信息」tab：当前公司表单、地区预设、公司列表、新建公司、工作区信息均渲染。"""
    at = _new_app()
    at.radio(key="nav").set_value("⚙️ 设置").run()
    _assert_clean(at, "设置（企业信息）")

    assert at.text_input(key="settings_company_name") is not None
    assert at.selectbox(key="settings_company_region") is not None, "应有地区预设选择框"
    labels = [button.label for button in at.button]
    assert any("保存当前公司信息" in label for label in labels)
    assert any("新建公司" in label for label in labels)
    assert any("切换" in label for label in labels)
    assert any("删除" in label for label in labels)
    captions = "\n".join(caption.value for caption in at.caption)
    assert "当前地区" in captions
    assert "城建税" in captions
    assert "工作区" in "\n".join(markdown.value for markdown in at.markdown)


def test_switch_company_via_sidebar() -> None:
    """预置第二家公司后通过侧边栏切换：active_company_id 改变、数据目录隔离、各页正常渲染。"""
    import workspace

    workspace.ensure_default()
    record = workspace.add_company(
        {"name": "第二家测试公司", "short_name": "测试二", "region_id": "yunnan-county-town"},
        activate_new=False,
    )
    second_id = str(record["id"])
    first_id = workspace.active_company_id()
    assert first_id != second_id

    at = _new_app()
    switcher = at.selectbox(key="company_switch")
    assert len(switcher.options) >= 2

    at.selectbox(key="company_switch").set_value(second_id).run()
    _assert_clean(at, "切换公司后")

    assert workspace.active_company_id() == second_id, "切换后 active_company_id 应更新"
    assert str(config.DATA_DIR).endswith(second_id), "DATA_DIR 应切到新公司账套目录"
    captions = "\n".join(caption.value for caption in at.caption)
    assert "县城" in captions or "5%" in captions, "侧边栏地区应随公司切换"

    for page in ("🏠 首页驾驶舱", "📒 台账管理", "🧮 算税工作台", "⚙️ 设置"):
        at.radio(key="nav").set_value(page).run()
        _assert_clean(at, f"切换公司后渲染{page}")


def test_settings_create_company_via_form() -> None:
    """设置页新建公司表单：填写名称提交后公司列表增加，且不出现页面错误。"""
    import workspace

    workspace.ensure_default()
    before = len(workspace.list_companies())
    at = _new_app()
    at.radio(key="nav").set_value("⚙️ 设置").run()
    _assert_clean(at, "设置（新建公司前）")

    at.text_input(key="settings_new_company_name").set_value("UI 新建测试公司")
    at.text_input(key="settings_new_company_short").set_value("UI测试")
    at.selectbox(key="settings_new_company_region").set_value("generic-county-town")
    [button for button in at.button if button.label == "新建公司"][0].click().run()
    _assert_clean(at, "设置（新建公司后）")

    companies = workspace.list_companies()
    assert len(companies) == before + 1, "新建公司后公司数量应增加"
    created = next((item for item in companies if item.get("name") == "UI 新建测试公司"), None)
    assert created is not None
    assert created.get("region_id") == "generic-county-town"


def test_payroll_page_renders_empty() -> None:
    """员工与工资：空数据下三个 tab 与关键按钮正常渲染。"""
    at = _new_app()
    at.radio(key="nav").set_value("👥 员工与工资").run()
    _assert_clean(at, "员工与工资（空数据）")

    tab_labels = [tab.label for tab in at.tabs]
    assert any("员工管理" in label for label in tab_labels)
    assert any("月度工资表" in label for label in tab_labels)
    assert any("扣缴申报辅助表" in label for label in tab_labels)

    labels = [button.label for button in at.button]
    assert any("保存员工" in label for label in labels), "应有新增员工表单"
    assert any("生成工资表" in label for label in labels), "应有「生成工资表」按钮"
    assert any("重新计算个税" in label for label in labels), "应有「重新计算个税」按钮"
    assert any("确认并生成凭证" in label for label in labels), "应有「确认并生成凭证」按钮"
    assert any("本期零申报" in label for label in labels), "应有「本期零申报」按钮"
    assert any("标记已申报" in label for label in labels), "应有「标记已申报」按钮"
    assert any("生成辅助表" in label for label in labels), "应有「生成辅助表」按钮"
    assert any("生成 Excel" in label for label in labels), "应有「生成 Excel」按钮"
    html_text = "\n".join(element.value for element in at.html)
    assert "仅供参考" in html_text, "页面底部必须展示免责声明"


def test_payroll_employee_create_and_payroll_flow() -> None:
    """员工建档 → 生成工资表 → 重算个税 → 确认凭证 → 标记已申报。"""
    pytest.importorskip("ledger.employees")
    pytest.importorskip("ledger.payroll")
    import ledger
    from ui.components import payroll_api as PA

    at = _new_app()
    at.radio(key="nav").set_value("👥 员工与工资").run()
    _assert_clean(at, "员工与工资（建档前）")

    at.text_input(key="payroll_new_name").set_value("张三")
    at.text_input(key="payroll_new_no").set_value("E001")
    at.text_input(key="payroll_new_department").set_value("技术部")
    at.number_input(key="payroll_new_social").set_value(8000.0)
    at.number_input(key="payroll_new_fund").set_value(8000.0)
    [button for button in at.button if button.label == "保存员工"][0].click().run()
    _assert_clean(at, "员工与工资（建档后）")
    assert ledger.table("employees").count() == 1, "员工建档后应有 1 条员工记录"

    month = PA.current_month_key()
    build_buttons = [button for button in at.button if button.label == "🧮 生成工资表"]
    if build_buttons and not build_buttons[0].disabled:
        build_buttons[0].click().run()
        _assert_clean(at, "员工与工资（生成工资表后）")
    assert ledger.table("payroll").count() >= 1, "生成工资表后应有工资记录"

    # 数据编辑器无法被 AppTest 驱动：用后端接口准备非零工资数据后再走 UI 确认流程
    from ui.components import payroll_api as payroll_helper

    for row in ledger.table("payroll").read():
        payroll_helper.update_payroll_row(
            row["id"],
            {
                "gross_salary": 12000.0,
                "social_insurance": 800.0,
                "housing_fund": 800.0,
                "special_deduction": 0.0,
                "other_deduction": 0.0,
            },
        )
    payroll_helper.recalc_period(month)
    at.run()
    _assert_clean(at, "员工与工资（准备工资数据后）")

    recalc = [button for button in at.button if "重新计算个税" in button.label]
    if recalc and not recalc[0].disabled:
        recalc[0].click().run()
        _assert_clean(at, "员工与工资（重算个税后）")

    confirm = [button for button in at.button if "确认并生成凭证" in button.label]
    if confirm and not confirm[0].disabled:
        confirm[0].click().run()
        _assert_clean(at, "员工与工资（确认工资表后）")
        assert ledger.table("journal_entries").count() >= 1, "确认工资表后应生成凭证"

    declare = [button for button in at.button if "标记已申报" in button.label]
    if declare and not declare[0].disabled:
        declare[0].click().run()
        _assert_clean(at, "员工与工资（标记已申报后）")
        payroll_rows = ledger.table("payroll").read()
        assert payroll_rows and all(str(row.get("status")) == "已申报" for row in payroll_rows)


def test_payroll_zero_declaration() -> None:
    """无工资月份点击「本期零申报」不崩溃，并插入零申报记录。"""
    pytest.importorskip("ledger.payroll")
    import ledger

    at = _new_app()
    at.radio(key="nav").set_value("👥 员工与工资").run()
    zero_buttons = [button for button in at.button if "本期零申报" in button.label]
    assert zero_buttons, "应提供「本期零申报」按钮"
    if zero_buttons[0].disabled:
        pytest.skip("零申报后端未就绪（按钮禁用）")
    zero_buttons[0].click().run()
    _assert_clean(at, "员工与工资（零申报后）")
    rows = ledger.table("payroll").read()
    assert any(str(row.get("employee_id")) == "ZERO" for row in rows), "零申报应写入 ZERO 记录"


def test_payroll_iit_return_and_export() -> None:
    """扣缴申报辅助表：生成辅助表与导出 Excel 按钮可点击且不异常。"""
    pytest.importorskip("filing.iit_return")
    from ui.components import payroll_api as PA

    at = _new_app()
    at.radio(key="nav").set_value("👥 员工与工资").run()
    build = [button for button in at.button if button.label == "📋 生成辅助表"]
    assert build, "应有「生成辅助表」按钮"
    if build[0].disabled:
        pytest.skip("辅助表后端未就绪（按钮禁用）")
    build[0].click().run()
    _assert_clean(at, "员工与工资（生成辅助表后）")

    export = [button for button in at.button if button.label == "📊 生成 Excel"]
    assert export, "应有「生成 Excel」按钮"
    if not export[0].disabled:
        export[0].click().run()
        _assert_clean(at, "员工与工资（导出 Excel 后）")
        assert at.download_button, "导出后应出现下载按钮"


def _settlement_year() -> str:
    """页面默认汇算年度（与 settlement_api.current_year 一致）。"""
    from ui.components import settlement_api

    return settlement_api.current_year()


def test_settlement_page_renders_empty() -> None:
    """汇算清缴：空数据下五个 tab 与关键按钮正常渲染。"""
    at = _new_app()
    at.radio(key="nav").set_value("📊 汇算清缴").run()
    _assert_clean(at, "汇算清缴（空数据）")

    tab_labels = [tab.label for tab in at.tabs]
    assert any("年度数据" in label for label in tab_labels)
    assert any("纳税调整" in label for label in tab_labels)
    assert any("亏损弥补" in label for label in tab_labels)
    assert any("申报辅助表" in label for label in tab_labels)
    assert any("检查清单" in label for label in tab_labels)

    labels = [button.label for button in at.button]
    assert any("保存年度输入" in label for label in labels), "应有「保存年度输入」按钮"
    assert any("计算汇算结果" in label for label in labels), "应有「计算汇算结果」按钮"
    assert any("添加调整项" in label for label in labels), "应有「添加调整项」按钮"
    assert any("添加亏损记录" in label for label in labels), "应有「添加亏损记录」按钮"
    assert any("生成辅助表" in label for label in labels), "应有「生成辅助表」按钮"
    assert any("生成 Excel" in label for label in labels), "应有「生成 Excel」按钮"
    assert at.selectbox(key="settlement_year") is not None
    html_text = "\n".join(element.value for element in at.html)
    assert "仅供参考" in html_text, "页面底部必须展示免责声明"


def test_settlement_save_inputs_and_compute() -> None:
    """保存年度补充输入 → 计算汇算结果 → 添加手工纳税调整。"""
    pytest.importorskip("ledger.settlement")
    import ledger

    year = _settlement_year()
    at = _new_app()
    at.radio(key="nav").set_value("📊 汇算清缴").run()
    _assert_clean(at, "汇算清缴（输入前）")

    at.number_input(key=f"settlement_ent_{year}").set_value(10000.0)
    at.number_input(key=f"settlement_prepaid_{year}").set_value(5000.0)
    [button for button in at.button if button.label == "💾 保存年度输入"][0].click().run()
    _assert_clean(at, "汇算清缴（保存输入后）")
    assert ledger.table("annual_settlements").count() >= 1, "保存年度输入后应有 annual_settlements 记录"

    compute_buttons = [button for button in at.button if button.label == "🧮 计算汇算结果"]
    assert compute_buttons, "应有「计算汇算结果」按钮"
    if compute_buttons[0].disabled:
        pytest.skip("汇算计算后端未就绪（按钮禁用）")
    compute_buttons[0].click().run()
    _assert_clean(at, "汇算清缴（计算后）")
    metric_labels = [metric.label for metric in at.metric]
    assert any("应纳税所得额" in label for label in metric_labels)
    assert any("应纳所得税额" in label for label in metric_labels)

    # 添加手工纳税调整
    at.number_input(key=f"settlement_adj_amount_{year}").set_value(5000.0)
    add_buttons = [button for button in at.button if button.label == "添加调整项"]
    assert add_buttons
    add_buttons[0].click().run()
    _assert_clean(at, "汇算清缴（添加纳税调整后）")
    assert ledger.table("tax_adjustments").count() >= 1, "添加后应有纳税调整记录"


def test_settlement_annual_return_and_export() -> None:
    """年度申报辅助表：生成辅助表与导出 Excel 按钮可点击且不异常。"""
    pytest.importorskip("filing.cit_annual")
    year = _settlement_year()
    at = _new_app()
    at.radio(key="nav").set_value("📊 汇算清缴").run()
    build = [button for button in at.button if button.label == "📋 生成辅助表"]
    assert build, "应有「生成辅助表」按钮"
    if build[0].disabled:
        pytest.skip("年度申报辅助表后端未就绪（按钮禁用）")
    build[0].click().run()
    _assert_clean(at, "汇算清缴（生成辅助表后）")

    export = [button for button in at.button if button.label == "📊 生成 Excel"]
    assert export, "应有「生成 Excel」按钮"
    if not export[0].disabled:
        export[0].click().run()
        _assert_clean(at, "汇算清缴（导出 Excel 后）")
        assert at.download_button, "导出后应出现下载按钮"


def test_correction_page_renders_empty() -> None:
    """离职与纠错：空数据下三个子标签正常渲染。"""
    at = _new_app()
    at.radio(key="nav").set_value("🧯 离职与纠错").run()
    _assert_clean(at, "离职与纠错（空数据）")

    tab_labels = [tab.label for tab in at.tabs]
    assert any("扫描与更正" in label for label in tab_labels)
    assert any("离职补偿金" in label for label in tab_labels)
    assert any("异议申诉台账" in label for label in tab_labels)
    html_text = "\n".join(element.value for element in at.html)
    assert "仅供参考" in html_text
    warnings = "\n".join(str(item.value) for item in at.warning)
    assert "申诉台账不可用" not in warnings
    infos = "\n".join(str(item.value) for item in at.info)
    assert "暂无申诉记录" in infos


def test_floating_ai_button_on_all_pages() -> None:
    """全局浮动 AI 助手按钮应出现在任意页面。"""
    at = _new_app()
    assert any(getattr(button, "key", "") == "floating_ai_open" for button in at.button)
    at.radio(key="nav").set_value("📒 台账管理").run()
    _assert_clean(at, "台账管理（浮动 AI）")
    assert any(getattr(button, "key", "") == "floating_ai_open" for button in at.button)
