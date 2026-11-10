# P1-C 契约：企业所得税汇算清缴辅助（CONTRACTS_P1_C）

> 目标：年度汇算的**数据准备 + 纳税调整测算 + 亏损弥补 + 年度申报表辅助 + 检查清单**。
> 系统**不代替**电子税务局年度申报，最终以 A 类年度申报表与主管税务机关口径为准。
>
> **Lead 已完成（只读）**：
> * `ledger/models.py` 新增 `tax_adjustments` / `loss_carryforwards` / `annual_settlements` 三张表；
> * `engine/cit_settlement.py`：限额类纳税调整、亏损弥补、汇算结果测算（22 项测试已全绿）；
> * `engine/__init__.py` 导出：`calc_settlement / calc_adjustment_limits / calc_loss_carryforward /
>   get_settlement_sources / SETTLEMENT_SOURCES / LOSS_CARRYFORWARD_YEARS`；
> * `tests/test_cit_settlement.py`。

---

## 0. 数据表

| 表 | 字段 |
|---|---|
| `tax_adjustments` | id, year, item_type, direction(调增/调减), accounting_amount, tax_amount, adjustment_amount, basis, source, note, created_at |
| `loss_carryforwards` | id, year(亏损年度), loss_amount, used_amount, remaining, expire_year, note, created_at, updated_at |
| `annual_settlements` | id, year, data_json(dict：限额类实际发生额/人数/资产/预缴税额/会计利润覆盖等), status(草稿/已确认), note, created_at, updated_at |

常量：`ADJUSTMENT_DIRECTIONS`、`SETTLEMENT_STATUSES`。

---

## 1. `ledger/settlement.py`（ledger-filing-dev）

```python
def annual_ledger_data(year: str | int) -> dict
# 从台账汇总全年数据：
# {"year","start","end","revenue_exclusive","revenue_inclusive","expense_total","profit",
#  "expense_by_category":{类别:金额},"salary_total"(工资类别全年合计),
#  "rd_cost"(研发相关费用合计),"rd_deduction"(engine 归集加计),"prepaid_tax"(季度预缴记录 CIT_PREPAY 合计),
#  "counts":{"income","expense","journal_entries","invoices","transactions"},
#  "monthly":[{"month":"2026-01","income_exclusive":...,"expense":...}],
#  "warnings":[...]}
# 预缴税额优先从 filings 表（form_id=CIT_PREPAY, period 为该年度季度）读取；无记录时回退 annual_settlements.data_json.prepaid_tax

def get_annual_inputs(year) -> dict          # 读取 annual_settlements.data_json（不存在返回默认空）
def save_annual_inputs(year, data: dict, *, note="") -> dict
# upsert by year；只允许白名单键：entertainment_actual/advertising_actual/welfare_actual/
# education_actual/union_actual/employees/total_assets/prepaid_tax/accounting_profit/rd_deduction/
# is_restricted_industry/note；金额 round2；状态默认草稿
def mark_settlement_status(year, status) -> dict   # 草稿/已确认

def list_adjustments(year) -> list[dict]
def add_adjustment(year, item_type, direction, adjustment_amount, *, accounting_amount=0,
                   tax_amount=0, basis="", source="", note="") -> dict
def update_adjustment(adjustment_id, patch) -> dict
def delete_adjustment(adjustment_id) -> bool
def list_losses(year=None) -> list[dict]
def add_loss(year, loss_amount, *, used_amount=0, note="") -> dict
def update_loss(loss_id, patch) -> dict      # 可更新 used_amount/note（重算 remaining）
def delete_loss(loss_id) -> bool

def compute_settlement(year) -> dict
# 汇总 annual_ledger_data + annual_inputs + adjustments + losses，调用 engine.calc_settlement：
#   revenue=annual.revenue_exclusive, cost=annual.expense_total（或 accounting_profit 覆盖）,
#   salary_total=annual.salary_total, rd_deduction=annual.rd_deduction（或输入覆盖）,
#   entertainment/advertising/welfare/education/union 来自 annual_inputs,
#   extra_adjustments=纳税调整清单（direction/amount/basis/source）,
#   losses=亏损台账（year/loss_amount/used_amount）,
#   prepaid_tax=annual.prepaid_tax（输入覆盖优先）, employees/total_assets/is_restricted_industry
# 返回 engine.calc_settlement 结果 + {"annual": 年度汇总, "inputs": 输入, "adjustments": 清单, "losses": 台账}
def settlement_checklist(year, settlement) -> list[dict]
# 汇算检查清单：[{"item","status":"PASS/WARNING/ERROR","message","action"}]
# 至少覆盖：收入与台账一致、费用凭证、研发辅助账、亏损弥补台账、预缴核对、小微条件、资料留存、
# 申报期限（次年 5 月 31 日）、纳税调整项是否遗漏（有业务招待费发生额但无调整记录等）
```

要求：期间锁不适用于年度汇算（年度数据汇总只读），但`annual_settlements` 写操作需审计日志（`ledger.audit.log_user_action`）；金额 2 位；年度参数 `"2026"`。

---

## 2. `filing/cit_annual.py`（ledger-filing-dev）

```python
FORM_ID = "CIT_ANNUAL"
FORM_NAME = "企业所得税年度纳税申报辅助表（A 类）"

def build_cit_annual_return(year: str | int, *, settlement: dict | None = None) -> dict
# 标准 bundle 结构：
# sections:
#   - "主表（A 类关键行次）"：利润总额、纳税调整增加额、纳税调整减少额、研发加计扣除、
#     弥补以前年度亏损、应纳税所得额、适用税率、应纳所得税额、减免所得税额、
#     实际应纳所得税额、本年累计已预缴、应补（退）税额
#   - "纳税调整明细"：逐项（限额类 + 手工调整）会计金额/税法金额/调整额/依据
#   - "亏损弥补明细"：亏损年度/可弥补额/本年弥补/剩余/到期年度/状态
#   - "政策依据与资料留存"：engine.get_settlement_sources + 汇算资料清单
# checks：预缴与应纳差异、亏损超期、调整项平衡（清单合计=主表调整额）、利润与台账一致性、
#         小微条件、研发加计资料、零申报提示；summary 汇总 PASS/WARNING/ERROR
# guide：电子税务局 A 类年度申报步骤（含附表填写顺序、5/31 截止、留存备查资料、更正申报）
# totals：{"accounting_profit","adjustment_add","adjustment_reduce","rd_deduction","loss_used",
#          "taxable_income","corporate_tax","prepaid_tax","payable","refund"}
def export_cit_annual_excel(bundle, path=None) -> Path
# Excel：主表、纳税调整明细、亏损弥补明细、政策依据与操作步骤（4 个 sheet）
```

要求：不声称代替申报；列次/行次用中文口径；`checks` 明细可执行；Excel 可读。

---

## 3. `closing/checks.py` 增量（ledger-filing-dev，可选但建议）

新增年度检查 key **`annual_settlement`**（仅当结账期间为 12 月时触发）：
12 月结账时提示「次年度 5 月 31 日前需完成汇算清缴」，不阻塞结账（WARNING/PASS），
`fix_hint` 指向「📊 汇算清缴」页面。不改变现有 key 语义。

---

## 4. UI（ui-dev）：新增第 11 页「📊 汇算清缴」

只改：`ui/components/nav.py`、新增 `ui/views/settlement.py`、可新增 `ui/components/settlement_api.py`、
`ui/components/__init__.py`、`tests/test_ui_smoke.py`。

页面 `st.tabs`：

1. **年度数据**：年度选择（如 2026）→ 展示 `annual_ledger_data`（全年收入/费用/利润/工资总额/研发费用/预缴税额、月度明细表）→ 录入补充数据表单（业务招待费/广告费/福利费/教育经费/工会经费/从业人数/资产总额/预缴税额/会计利润覆盖/研发加计覆盖/限制行业）→「保存年度输入」（`save_annual_inputs`）→「计算汇算结果」（`compute_settlement`）→ 指标卡（会计利润、纳税调整、应纳税所得额、应纳所得税、应补/退）与结果明细。
2. **纳税调整**：限额类自动项只读展示（业务招待费/广告费/三项经费限额与调增）；手工调整清单表格 + 新增表单（项目/方向/金额/会计金额/税法金额/依据/备注）+ 删除。
3. **亏损弥补**：亏损台账（年度/亏损额/已弥补/剩余/到期年度）+ 新增/编辑（已弥补金额）/删除；展示本年度可弥补测算。
4. **申报辅助表**：`build_cit_annual_return` 展示主表/调整明细/亏损明细/校验/操作步骤 → 「导出 Excel」下载 → 明确「实际申报在电子税务局完成」。
5. **检查清单**：`settlement_checklist` 列表（PASS/WARNING/ERROR 着色）+ 资料留存清单。

导航顺序：放在「📋 申报填报」之后（第 5 页左右），共 **11 页**；`tests/test_ui_smoke.py` 同步并新增：
页面空数据渲染、保存年度输入/计算/导出按钮存在且点击不异常（可用临时数据）。

---

## 5. Agent / 知识库（agent-knowledge-dev）

1. `agent/tools.py` 新增工具 **`calc_cit_settlement`**（参数：year, revenue_exclusive, cost_total,
   salary_total, entertainment/advertising/welfare/education/union_actual, rd_deduction,
   extra_adjustments(list), losses(list), prepaid_tax, employees, total_assets；调用
   `engine.calc_settlement`；display 中文摘要含应纳税所得额/应纳所得税/应补退/政策依据/免责）。
2. `agent/intent_router.py` 新增意图 **`cit_settlement`**（关键词：汇算清缴/年度所得税/企业所得税年度/
   纳税调整/亏损弥补/业务招待费/广告费限额），route → `calc_cit_settlement`，不与其他意图冲突。
3. `agent/orchestrator.py` 本地回复分支：年度汇算口径解释（调增/调减、亏损弥补 5 年、小微 5%、
   5 月 31 日前申报）。
4. `knowledge/policy_files/企业所得税/汇算清缴.md`（可新增）或更新现有申报流程文档：年度 A 类申报要点、
   纳税调整常见项目与限额、亏损弥补、小微优惠、资料留存、申报期限；frontmatter source/last_verified=2026-11-10。
5. 测试：`tests/test_agent.py` / `test_knowledge.py` 新增工具、意图、本地回复、文档断言；不访问真实 API。

---

## 6. 测试要求

| 文件 | 覆盖 |
|---|---|
| `tests/test_settlement.py`（ledger-filing-dev） | annual_ledger_data 汇总口径（收入/费用/工资/研发/预缴）、annual inputs 白名单与 upsert、调整 CRUD、亏损 CRUD（remaining 重算）、compute_settlement 与 engine 一致、checklist 分支、审计日志 |
| `tests/test_cit_annual.py`（ledger-filing-dev） | bundle 结构/主表行次/调整与亏损明细/checks/guide/Excel 4 sheet |
| `tests/test_closing.py`（追加） | 12 月结账 annual_settlement 提示（WARNING/PASS） |
| `tests/test_agent.py` / `test_knowledge.py`（agent-knowledge-dev） | calc_cit_settlement 工具、cit_settlement 意图、本地回复、知识库文档 |
| `tests/test_ui_smoke.py`（ui-dev） | 11 页、新页面渲染与关键按钮不异常 |
| Lead | `tests/test_p1_c_integration.py`：年度数据 → 输入/调整/亏损 → 汇算计算 → 辅助表导出 → 检查清单 端到端 |

---

## 7. 完成定义（DoD）

1. 各自测试全绿；全量 `pytest tests/` 通过；
2. 不改 `engine/**`、`ledger/models.py`、`config.py`、`workspace.py`、`tests/conftest.py`、`docs/`；
3. 中文 docstring、无 TODO 占位、错误提示可操作；
4. 完成后 `send_message` 给 Lead：文件清单、接口、测试结果、遗留问题（<400 字）。
