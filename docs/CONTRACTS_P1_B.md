# P1-B 契约：个税扣缴（员工与工资）（CONTRACTS_P1_B）

> 目标：小规模企业**每月**的工资与个税扣缴闭环：
> 员工档案 → 月度工资表 → 累计预扣法算税 → 工资计提/发放凭证 → 扣缴申报辅助表（Excel）。
>
> **Lead 已完成（只读）**：
> * `ledger/models.py` 新增 `employees` / `payroll` 两张表（字段见下）；
> * `engine/iit.py`：累计预扣法、预扣率表、专项附加扣除标准、政策依据；
> * `engine/__init__.py` 导出：`calc_iit_withholding / calc_special_deduction / get_iit_rate_table /
>   get_special_deduction_standards / get_iit_sources / IIT_SOURCES / BASIC_DEDUCTION_PER_MONTH /
>   ZERO_DECLARATION_NOTE`；
> * `tests/test_iit.py`（21 项，已全绿）。
>
> **2026 年扣缴端升级要点（已写入引擎提示）**：5,000 元/月减除费用按**实际申报月份**累计，
> 未申报月份额度作废；零工资月份也应零申报。

---

## 0. 数据表（Lead 已定义，见 `ledger/models.py`）

| 表 | 关键字段 |
|---|---|
| `employees` | id, employee_no, name, id_card_masked, phone, department, position, hire_date, leave_date, status(在职/离职), social_insurance_base, housing_fund_base, special_deduction_json(dict), deduction_choice(累计预扣/一次性60000), is_deduction_qualified(bool), note |
| `payroll` | id, period(2026-11), employee_id, employee_name, gross_salary, bonus, allowance, social_insurance, housing_fund, special_deduction, other_deduction, cumulative_income, cumulative_basic_deduction, cumulative_social, cumulative_special, cumulative_other, cumulative_taxable, cumulative_tax, tax_withheld, net_pay, zero_declaration(bool), status(草稿/已确认/已申报), journal_entry_no, note |

常量：`EMPLOYEE_STATUSES`、`PAYROLL_STATUSES`、`BASIC_DEDUCTION_CHOICES`。
写库走 `ledger.store.table(...)`；金额用 `round2`；日期用 `normalize_date`；月份用 `normalize_period`（`2026-11`）。

---

## 1. `ledger/employees.py`（ledger-filing-dev）

```python
def add_employee(payload: dict) -> dict
# 必填 name；其余可选；hire_date 默认今天；status 默认在职；
# id_card_masked：只保存脱敏值（前 6 + **** + 后 4，非身份证原文）；special_deduction_json 用 dict；
# deduction_choice 枚举校验；is_deduction_qualified 默认 True；返回保存记录
def list_employees(status: str | None = "在职", *, include_left: bool = False) -> list[dict]
def get_employee(employee_id: str) -> dict | None
def update_employee(employee_id: str, patch: dict) -> dict
# 允许改除 id 外字段；status 改「离职」时自动补 leave_date（若未提供）
def delete_employee(employee_id: str) -> bool
# 有工资记录 → 抛 ValueError（应改状态为离职，不删除历史）；无记录才可删除
def mask_id_card(value: str) -> str
def summarize_employees() -> dict
# {"total","active","left","by_department":{...},"total_social_base","total_housing_fund_base"}
def special_deduction_monthly(employee: dict) -> float
# 调 engine.calc_special_deduction(employee["special_deduction_json"])["monthly_total"]
```

要求：员工姓名等必填校验中文提示；身份证号**不得**落盘原文；日期/枚举非法值抛 `ValueError`。

---

## 2. `ledger/payroll.py`（ledger-filing-dev）

```python
def build_payroll(period: str, *, employee_ids: list[str] | None = None) -> dict
# 为当前在职员工生成当月工资表草稿（入职日期 ≤ 月末，离职日期为空或 > 月初）；
# 已存在同员工同期间的草稿/确认记录则跳过；返回 {"period","created":n,"skipped":[...],"rows":[...]}
def list_payroll(period: str | None = None) -> list[dict]
def get_payroll_row(payroll_id: str) -> dict | None
def update_payroll_row(payroll_id: str, patch: dict) -> dict
# 可改 gross_salary/bonus/allowance/social_insurance/housing_fund/special_deduction/other_deduction/note；
# 已确认/已申报的记录禁止修改（ValueError）；日期所在期间受 ledger.locks 期间锁保护
def delete_payroll_row(payroll_id: str) -> bool          # 已确认禁止删除
def cumulative_for_employee(employee_id: str, period: str) -> dict
# 本年累计（截至目标月份，含目标月当前记录）：
# {"months_declared": 已申报月份数, "cumulative_income":..., "cumulative_social":...,
#  "cumulative_housing_fund":..., "cumulative_special_additional":...,
#  "cumulative_other":..., "cumulative_tax_withheld":...}
# 口径：历史月份只统计 status in ("已确认","已申报") 的记录；目标月使用当前记录（若存在）
# 「已申报月份数」= 该员工本年 status=="已申报" 的月份数（2026 规则：按实际申报月份累计减除费用）
def recalc_period(period: str) -> dict
# 逐行按 engine.calc_iit_withholding 重算累计字段、tax_withheld、net_pay（只处理草稿行，已确认行跳过并报告）；
# 参数：months_declared/social/housing/special/other/tax_withheld 来自 cumulative_for_employee，
# one_time_basic_deduction = employee.deduction_choice == "一次性60000"；
# 返回 {"period","rows":[row...],"total_tax":...,"warnings":[...]}
def mark_zero_declaration(period: str) -> dict
# 无工资月份零申报：插入/更新一条 employee_id="ZERO"、employee_name="（零申报）"、zero_declaration=True、
# 收入与扣除全 0、status="已申报" 的记录；返回该记录
def confirm_period(period: str, *, post_journal: bool = True, operator: str = "system") -> dict
# 确认本期：逐行生成/更新「工资计提 + 发放代扣」凭证（见 §2.1），状态→已确认，写 journal_entry_no、审计日志；
# 已确认/已申报行跳过；金额为 0 的行不入账；返回 {"period","rows","entries":[...],"totals":{...}}
def mark_period_declared(period: str) -> dict
# 用户在自然人电子税务局扣缴端完成申报后标记：status 已确认 → 已申报；写审计；返回 {"period","count":n}
def summarize_period(period: str) -> dict
# {"period","headcount","total_gross","total_social","total_housing_fund","total_special",
#  "total_taxable","total_tax","total_net","declared":bool,"status_counts":{...}}
def payroll_periods(limit: int = 12) -> list[str]   # 已有工资表的月份（新→旧）
```

### 2.1 工资凭证（复用 `ledger.journal`）

* **计提**（按本期应发合计）：借 `管理费用—工资薪金`（应发合计）；贷 `应付职工薪酬`（应发合计）；
  研发人员部分可选 `研发支出—人员人工`（若员工备注/部门含"研发"则归研发，余额归管理费用）。
* **发放代扣**：借 `应付职工薪酬`（应发合计）；贷 `银行存款`（实发合计）；
  贷 `其他应付款—社会保险费`（个人社保合计）；贷 `其他应付款—住房公积金`（个人公积金合计）；
  贷 `应交税费—应交个人所得税`（预扣个税合计）。
* `source_ref="PAY-{period}"`、`voucher_type="工资"`；借贷必须平衡；期间锁生效时抛 `PeriodLockedError`；
  已生成过凭证的期间重复确认不得重复入账（幂等）。
* 写 `ledger.audit.log_user_action("工资表确认"/"工资个税申报标记", ...)`。

---

## 3. `filing/iit_return.py`（ledger-filing-dev）

```python
FORM_ID = "IIT_WITHHOLDING"
FORM_NAME = "个人所得税扣缴申报辅助表（工资薪金）"

def build_iit_return(period: str, *, rows: list[dict] | None = None) -> dict
# 复用「申报表 bundle」结构（见 docs/CONTRACTS.md 3.8）：
# sections:
#   - "主表（本期汇总）"：行次 1 本期收入合计、2 本期免税收入、3 减除费用合计、
#     4 专项扣除（社保+公积金）、5 专项附加扣除、6 其他扣除、7 应纳税所得额、
#     8 应预扣税额、9 已预扣税额、10 本期应补（退）税额、11 申报人数
#   - "员工明细"：每员工一行（姓名/收入/社保/公积金/专项附加/其他/累计应纳税所得/税率/税额/实发）
#   - "政策依据"：引用 engine.get_iit_sources() 与累计预扣率表
# checks：社保+公积金 ≤ 收入、专项附加 ≤ 收入、已确认/已申报状态、零申报提示、无员工、
#         2026 扣缴端减除费用规则提示
# guide：扣缴端操作步骤（人员信息采集→综合所得申报→正常工资薪金→税款计算→申报→缴款→标记已申报）
# 返回标准 bundle 字段 + totals + calc（engine 汇总结果）+ disclaimer + policy_sources
def export_iit_excel(bundle: dict, path=None) -> Path
# Excel：主表、员工明细、政策依据与操作步骤 3 个 sheet；路径默认 config.EXPORT_DIR
```

要求：金额 2 位；栏次中文口径清晰；不声称"代替申报"（系统只生成辅助表，实际申报在扣缴端）。

---

## 4. `closing/checks.py` 增量（ledger-filing-dev）

新增检查 key **`payroll_status`**：

* 期间无工资表且公司无在职员工 → PASS；
* 期间无工资表但有在职员工 → WARNING（提示补充工资表或零申报）；
* 有工资表但存在草稿行 → WARNING（提示确认后再结账）；
* 全部已确认/已申报 → PASS。
检查文案含 `fix_hint`，不改变现有 9 项 key 的顺序与语义；`pre_close_checks` 返回结构不变。

---

## 5. Agent / 知识库（agent-knowledge-dev）

1. `agent/tools.py` 新增工具 **`calc_iit`**（OpenAI function schema）：
   ```python
   args: {cumulative_income, months_declared, cumulative_social_insurance=0,
          cumulative_housing_fund=0, cumulative_special_additional=0,
          cumulative_other_deduction=0, cumulative_tax_withheld=0,
          one_time_basic_deduction=False, period=""}
   ```
   调用 `engine.calc_iit_withholding`；`display` 中文摘要（含本期应预扣、政策依据、免责声明）；
   返回 `{"ok","data","display","error"}` 风格与现有工具一致。
2. `agent/intent_router.py`：新增意图 **`iit`**，关键词覆盖「个税 / 工资 / 薪金 / 专项附加扣除 /
   减除费用 / 预扣 / 五险一金 / 个税怎么算」；`route` 映射到 `calc_iit`；与现有 `calc_vat` 等不冲突
   （"增值税"仍走 calc_vat）。
3. `agent/orchestrator.py` `_compose_local` 增加 `iit` 分支：自然语言解释（本期应预扣、累计口径、
   专项附加扣除、2026 扣缴端零申报提醒），末尾免责声明。
4. `knowledge/policy_files/其他税费/个人所得税扣缴.md`：更新为现行口径——累计预扣法/预扣率表/
   5,000 元减除费用、专项附加扣除标准（2023 提高后）、2026 扣缴端「按实际申报月份累计」与零申报提醒、
   资料留存；frontmatter `source/last_verified: 2026-11-10`；文末附政策沿革与「以扣缴端口径为准」。
5. 测试：`tests/test_agent.py` 新增 `calc_iit` 工具与意图/本地回复用例；`tests/test_knowledge.py`
   更新个税文档断言（若涉及）。不访问真实 API。

---

## 6. UI（ui-dev）：新增第 10 页「👥 员工与工资」

只改：`ui/components/nav.py`、新增 `ui/views/payroll.py`、可新增 `ui/components/payroll_api.py`、
`ui/views/dashboard.py`（可选：工资提醒卡片）、`tests/test_ui_smoke.py`。

页面结构（`st.tabs`）：

1. **员工管理**：新增/编辑员工表单（姓名、工号、部门、岗位、入职日期、状态、社保基数、公积金基数、
   减除费用方式、专项附加扣除明细：子女数/婴幼儿数/继续教育/住房贷款或租金档/赡养老人/备注）→
   `ledger.employees`；员工表格（在职/离职筛选）+ 删除/离职操作；指标卡（在职人数、社保基数合计等）。
2. **月度工资表**：月份选择（`engine.month_options`）→「生成工资表」（`ledger.payroll.build_payroll`）→
   `st.data_editor` 可编辑应发/奖金/补贴/社保/公积金/专项附加/其他 →「重新计算个税」
   （`recalc_period`，展示累计应纳税所得、税率、本期应预扣、实发）→「确认并生成凭证」
   （`confirm_period`，展示凭证号）→「标记已申报」（`mark_period_declared`）；无工资月份提供
   「本期零申报」按钮（`mark_zero_declaration`）；指标卡（人数/应发/个税/实发）。
3. **扣缴申报辅助表**：`filing.iit_return.build_iit_return(period)` 展示主表/员工明细/校验/操作步骤，
   「导出 Excel」（`export_iit_excel` + `st.download_button`），页面说明「实际申报在自然人电子税务局扣缴端完成」。

导航：`ui/components/nav.py` 增加 `PAGE_PAYROLL = "👥 员工与工资"`（放在「📒 台账管理」之后、
「📥 票据与流水」之前或之后均可，保持顺序自然）；`tests/test_ui_smoke.py` PAGES 同步为 10 页并新增：
员工与工资页空数据渲染、生成工资表/零申报/导出按钮存在且点击不异常（可用临时数据）。

---

## 7. 测试要求

| 测试文件 | 覆盖 |
|---|---|
| `tests/test_payroll.py`（ledger-filing-dev） | 员工 CRUD/脱敏/离职保护；工资表生成/编辑/删除/期间锁；累计口径（已申报月份、跨月收入、已预扣）；recalc 多员工、税率跳档、专项附加；确认生成凭证借贷平衡且幂等；标记已申报；零申报；汇总；**累计预扣法与 engine 计算结果一致** |
| `tests/test_iit_return.py`（ledger-filing-dev） | bundle 结构/主表行次/员工明细/checks（社保>收入、无员工、草稿状态）/guide 口径/Excel 导出可读 |
| `tests/test_closing.py`（追加） | `payroll_status` 检查的 PASS/WARNING 分支 |
| `tests/test_agent.py` / `test_knowledge.py`（agent-knowledge-dev） | calc_iit 工具、iit 意图、本地回复、个税知识库 frontmatter/正文 |
| `tests/test_ui_smoke.py`（ui-dev） | 10 页、新页面渲染与关键按钮不异常 |
| Lead | 新增 `tests/test_p1_b_integration.py`：员工→工资→算税→确认→凭证→扣缴辅助表→结账检查→期间锁 端到端 |

---

## 8. 完成定义（DoD）

1. 各自测试全绿；全量 `pytest tests/` 通过；
2. 不改 `engine/**`（已实现）、`ledger/models.py`、`ledger/locks.py`、`config.py`、`workspace.py`、`tests/conftest.py`、`docs/`；
3. 中文 docstring、无 TODO 占位、错误提示可操作；
4. 完成后 `send_message` 给 Lead：文件清单、接口、测试结果、遗留问题（<400 字）。
