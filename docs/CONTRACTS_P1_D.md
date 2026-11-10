# P1-D 契约：其他申报提醒与清单（工商年报 / 残保金 / 社保）（CONTRACTS_P1_D）

> 目标：非税/低频申报的**提醒 + 数据准备清单 + 测算辅助**，不做系统直连、不代替申报。
>
> **Lead 已完成（只读）**：
> * `engine/disability_fund.py`：残保金测算（1.5% 比例、分档减缴 50%/90%、30 人以下免征、
>   年平均工资 2 倍封顶提示），9 项测试已全绿；
> * `config.FILING_CALENDAR` 新增 `social_insurance_monthly`（每月 25 日提醒）、
>   `disability_fund_annual`（9 月 30 日提醒），并扩展 `engine/tax_calendar` 期间推算；
> * `engine/__init__.py` 导出 `calc_disability_fund / get_disability_sources / ...`。

---

## 1. `filing/annual_report.py`（ledger-filing-dev）

```python
FORM_ID = "ANNUAL_REPORT"
FORM_NAME = "工商年报数据准备表"

def build_annual_report(year: str | int) -> dict
# 标准 bundle（sections/totals/calc/checks/summary/guide/disclaimer）
# sections：
#   - "企业基本信息"：名称/统一社会信用代码/纳税人类型/主管税务机关/经营状态（存续）/从业人数/
#     社保参保人数/联系电话（提示到设置页维护公司信息）
#   - "资产状况（按台账估算，需与财务报表核对）"：资产总额、负债总额、所有者权益、营业收入、
#     利润总额、纳税总额（增值税+附加+所得税口径汇总）、研发费用
#   - "股东及出资信息（需人工补充）"：占位行 + 提示从公司章程/工商登记获取
#   - "网站 / 网店、对外投资（需人工补充）"：占位提示
# checks：从业人数与工资表/社保人数一致性、收入/资产与台账一致、6 月 30 日截止、
#         公示后修改限制、对外投资/股东信息需人工核对
# guide：国家企业信用信息公示系统（https://www.gsxt.gov.cn）操作步骤、所需资料、逾期后果
# data：annual_ledger_data(year) 快照；totals：资产/负债/所有者权益/收入/利润/纳税/人数
def export_annual_report_excel(bundle, path=None) -> Path   # 至少 2  sheet：主表 + 校验/操作步骤
```

数据取数：`ledger.reports.profit_statement` / `ledger.settlement.annual_ledger_data` /
`ledger.employees.summarize_employees` / `config.get_company()`；资产估算：货币资金≈累计收入-累计费用（标注估算）。

## 2. `filing/disability_fund.py`（ledger-filing-dev）

```python
FORM_ID = "DISABILITY_FUND"
FORM_NAME = "残疾人就业保障金测算/申报辅助表"

def build_disability_fund(
    year: str | int,
    *,
    employees: int | None = None,
    average_wage: float | None = None,
    disabled_employed: float = 0.0,
    local_average_wage: float | None = None,
) -> dict
# 默认取数：employees 默认=上年员工数（employees 表在职+离职）；average_wage 默认=工资费用合计/人数
# （无数据时提示手工录入）；其余调 engine.calc_disability_fund
# sections：
#   - "主表"：在职职工人数、实际安排残疾人就业人数、安排比例、法定比例 1.5%、差额人数、
#     上年在职职工年平均工资、应缴基数、分档减缴比例、应减缴额、本期应缴额
#   - "政策依据与说明"：分档减缴、30 人以下免征、平均工资 2 倍封顶、申报期限（以当地残联/税务公告为准）
# checks：30 人免征/达标/未达标分档、平均工资与工资表一致性、人数与个税工资表一致性、
#         9 月 30 日申报提醒（各地差异）、需向残联申报审核的提示
# guide：残联按比例安排残疾人就业审核 → 税务申报缴纳流程
def export_disability_fund_excel(bundle, path=None) -> Path
```

## 3. UI（ui-dev）

只改：`ui/views/filing.py`、可新增 `ui/components/annual_api.py`、`tests/test_ui_smoke.py`。
在「📋 申报填报」页面底部新增折叠区 **「📄 工商年报 / 残保金」**：

* 年度选择 → 「生成工商年报数据准备表」（`build_annual_report`）→ 展示基本信息/资产状况/校验/操作步骤，
  「导出 Excel」下载；
* 残保金：人数/平均工资/安排残疾人数/当地社平工资输入 → 「测算残保金」（`build_disability_fund`）→
  展示应缴/减缴/免征、校验与申报步骤，「导出 Excel」；
* 明确提示：工商年报 6 月 30 日、残保金以当地残联/税务公告为准；系统只做数据准备，不代替申报。
* `tests/test_ui_smoke.py` 保持 11 页，新增：折叠区渲染、生成/测算/导出按钮存在且点击不异常。

## 4. Agent / 知识库（agent-knowledge-dev）

1. `agent/tools.py` 新增工具 **`calc_disability_fund`**（参数 employees/average_wage/disabled_employed/
   local_average_wage；调 engine；display 含应缴/减缴/免征/政策依据/免责）；
2. `agent/intent_router.py` 新增意图 **`disability_fund`**（残保金/残疾人就业保障金/按比例安排就业），
   route → calc_disability_fund；「工商年报」问答走 policy 或 calendar，不强行工具；
3. 知识库新增/更新：`knowledge/policy_files/其他税费/残疾人就业保障金.md`（比例 1.5%、分档减缴、
   30 人以下免征、平均工资 2 倍封顶、申报流程、留存资料）、`knowledge/policy_files/申报流程/工商年报.md`
   （6 月 30 日、公示系统步骤、填报内容、逾期后果、更正）；frontmatter `last_verified: 2026-11-10`；
4. `tests/test_agent.py` / `test_knowledge.py` 新增工具/意图/回复/文档断言；不访问真实 API。

## 5. 测试要求

| 文件 | 覆盖 |
|---|---|
| `tests/test_annual_report.py`（ledger-filing-dev） | bundle 结构/基本信息/资产估算/checks/guide/Excel 导出 |
| `tests/test_disability_fund_bundle.py`（ledger-filing-dev） | 默认取数（人数/平均工资）、传入覆盖、30 人免征/分档/达标、checks/guide/Excel |
| `tests/test_agent.py` / `test_knowledge.py`（agent-knowledge-dev） | 工具/意图/回复/知识库文档 |
| `tests/test_ui_smoke.py`（ui-dev） | 11 页 + 折叠区渲染/按钮不异常 |
| Lead | `tests/test_p1_d_integration.py`：日历提醒包含社保/残保金、工商年报 bundle、残保金测算→bundle→Excel、CLI |

## 6. 完成定义（DoD）

1. 各自测试全绿；全量 `pytest tests/` 通过；
2. 不改 `engine/**`（已实现）、`config.py`（日历已加）、`ledger/models.py`、`tests/conftest.py`、`docs/`；
3. 中文 docstring、无 TODO 占位；
4. 完成后 `send_message` 给 Lead：文件清单、接口、测试结果、遗留问题（<400 字）。
