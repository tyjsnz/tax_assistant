# 跨模块接口契约（CONTRACTS）

> 本文件是并行开发期间的**唯一接口约定**。所有模块必须严格按此实现；若必须变更接口，
> 先通知 Lead，由 Lead 统一更新本文件后再改代码。
>
> 项目：某科技有限公司 · 小规模纳税人自主税务填报系统（税小智）
> Python 3.11 + Streamlit 1.67 + DeepSeek（唯一 AI 模型）

---

## 0. 运行命令

```bash
cd /Users/shanengzhi/www/2026/prj/tax
.venv/bin/python -m pytest tests/ -q          # 全部测试
.venv/bin/python -m pytest tests/test_ledger.py -q
.venv/bin/streamlit run ui/streamlit_app.py --server.port 8502 --server.headless true
```

依赖已安装在项目内 `.venv`（streamlit 1.67 / pandas / openpyxl / plotly / requests / pytest）。
**不要**使用系统 anaconda python（其 pydantic 已损坏）。

---

## 1. 目录、写入范围与依赖方向

```
config.py               Lead（实现完毕，只读）
engine/                 Lead（实现完毕，只读）
ledger/models.py        Lead（实现完毕，只读）
ledger/store.py         Lead（实现完毕，只读）
tests/conftest.py       Lead（实现完毕，只读）

ledger/{income_ledger,expense_ledger,rd_auxiliary,journal,reports,audit,seed}.py   ledger-filing-dev
filing/{__init__,registry,vat_return,cit_prepay,stamp_return,fin_report,
        validator,export,progress,guide}.py                                      ledger-filing-dev
tests/test_ledger.py tests/test_filing.py                                        ledger-filing-dev

agent/{__init__,llm_client,prompt_templates,intent_router,tools,orchestrator}.py  agent-knowledge-dev
knowledge/{__init__,retriever,update_checker}.py + knowledge/policy_files/**.md   agent-knowledge-dev
ocr/{__init__,extractor,field_mapper,validator}.py                               agent-knowledge-dev
tests/test_agent.py tests/test_knowledge.py tests/test_ocr.py                    agent-knowledge-dev

ui/**.py  .streamlit/config.toml  tests/test_ui_smoke.py                          ui-dev
```

依赖方向（禁止反向 import）：

```
engine  ←  ledger  ←  filing  ←  agent  ←  ui
engine  ←  knowledge / ocr（可用 engine 的常量与工具）
```

* 不得修改 `config.py`、`engine/**`、`ledger/models.py`、`ledger/store.py`、`tests/conftest.py`、
  `docs/CONTRACTS.md`。如接口不足，发消息给 Lead。
* 所有数据表结构定义只在 `ledger/models.py` 中，不要另建 schema。

---

## 2. 已完成基础模块（只读，直接用）

### 2.1 `config.py`

```python
BASE_DIR, DATA_DIR, BACKUP_DIR, UPLOAD_DIR, EXPORT_DIR, LOGS_DIR, POLICY_DIR, SETTINGS_FILE
APP_NAME, APP_VERSION, DISCLAIMER          # DISCLAIMER = "⚠️ 仅供参考，以主管税务机关核定为准"
COMPANY: dict                              # name / short_name / taxpayer_type / industry / tax_authority ...
POLICY: dict                               # vat_levy_rate=0.01、vat_exemption_limit_quarter=300000、
                                           # surtax_rate=0.06、surtax_rate_full=0.12、liutax_liangfei_discount=0.5、
                                           # small_low_profit_rate=0.05、rd_deduction_ratio=1.0、stamp_tax_rates ...
FILING_CALENDAR: dict                      # vat_quarterly / corp_tax_quarterly / personal_tax_monthly / ...
LLM_CONFIG: dict                           # provider="deepseek", base_url="https://api.deepseek.com",
                                           # model="deepseek-chat", reasoner_model="deepseek-reasoner"
ensure_dirs() -> None
load_user_settings() -> dict               # 含 api_key/model/base_url/temperature/max_tokens/company/privacy_mode
save_user_settings(patch: dict) -> dict    # 原子写 + chmod 600
get_api_key() -> str                       # settings > DEEPSEEK_API_KEY > .env
set_api_key(key: str) -> dict
mask_key(key=None) -> str
is_ai_configured() -> bool
get_llm_config() -> dict                   # 默认配置 + 用户设置，含 api_key
get_company() -> dict
get_policy() -> dict
today() -> date
is_privacy_mode() -> bool
settings_snapshot() -> dict
```

### 2.2 `engine` 包（确定性算税，**任何情况下不得用 LLM 计算税额**）

```python
from engine import (
    calc_vat, calc_corp_tax, calc_cit_prepay, calc_rd_deduction,
    aggregate_rd_expenses, calc_stamp_tax,
    get_params, get_rate_table, describe_policy, get_version, list_versions,
    policy_basis_text, sources_for, vat_basis_text,
    quarter_of, quarter_start, quarter_end, quarter_range, period_range, period_label,
    current_quarter, current_month, prev_quarter, quarter_options, month_options,
    filing_deadlines, upcoming_deadlines, next_deadline, reminders, reminder_text,
    deadline_status, format_date_cn, shift_to_workday, completion_checklist,
    dec, round2, yuan, yuan_cn, pct, safe_div, clamp, sum_money,
    RD_ITEMS, RD_ITEM_LABELS, RD_CATEGORY_MAP, RD_EXCLUDED_CATEGORIES,
)
```

关键返回字段（**UI 与 Agent 按此取值**）：

```python
calc_vat(quarterly_total_inclusive, special_invoice_inclusive=0.0, period="", *,
         levy_rate=None, exemption_limit=None, surtax_rate=None, liutax_discount=None,
         limit_basis="total") -> dict
# 规范字段：quarter period total_inclusive total_exclusive ordinary_exclusive special_exclusive
#           vat_payable vat_exempt surtax within_exemption_limit note disclaimer
# 扩展字段：special_inclusive ordinary_inclusive exemption_limit limit_basis levy_rate standard_rate
#           vat_at_standard vat_reduction vat_exempt_at_standard surtax_full surtax_discount
#           total_tax tax_burden warnings breakdown
# 政策依据（随期间版本自动切换）：
#           policy_version policy_version_id policy_sources policy_basis vat_policy_source
#           vat_reduction_scope vat_reduction_exclusions vat_threshold_per_transaction
#           superseded_documents
#           return_lines = {line_1_taxable_sales, line_18_tax_amount, line_19_tax_exempt_amount,
#                           line_22_reduction, line_24_payable, line_10_exempt_sales,
#                           surtax_city, surtax_education, surtax_local_education}

calc_corp_tax(annual_profit=None, rd_cost=0.0, *, annual_revenue_exclusive=None, annual_cost=None,
              employees=None, total_assets=None, is_restricted_industry=False,
              is_small_low_profit=None, rd_formed_intangible=False, period="") -> dict
# annual_profit annual_revenue_exclusive annual_cost rd_cost rd_deduction rd_note taxable_income
# qualifies_small_low_profit small_low_profit_check{qualifies,income_ok,employees_ok,assets_ok,industry_ok,reasons,thresholds}
# standard_rate effective_rate corp_tax gross_tax tax_reduction note warnings

calc_cit_prepay(quarter_profit, ytd_profit, ytd_cit_paid=0.0, *, period="", rd_cost_ytd=0.0,
                employees=None, total_assets=None, is_restricted_industry=False,
                is_small_low_profit=None) -> dict
# quarter_profit ytd_profit rd_cost_ytd rd_deduction taxable_income_ytd effective_rate
# cit_accrued cit_paid prepay_payable note warnings ...

calc_rd_deduction(personnel_cost=0, material_cost=0, depreciation=0, amortization=0,
                  design_cost=0, other_cost=0, *, formed_intangible=False,
                  amortization_years=None, period="", project_name="") -> dict
# items{中文标签:金额} items_detail[{item,amount}] five_total other_cost_claimed other_cost_limit
# other_cost_allowed total_collectible deduction_ratio deduction_amount formed_intangible
# intangible_cost amortization_years annual_amortization note warnings

aggregate_rd_expenses(expenses, *, project_name="研发项目", period="", formed_intangible=False,
                      include_unmarked=False) -> dict     # 上述字段 + detail[] skipped[] source_count

calc_stamp_tax(contracts, *, small_scale=True, liutax_discount=None, period="") -> dict
# items[{contract_type,contract_no,counterparty,date,amount,rate,rate_text,
#        tax_before_discount,discount_amount,tax_payable}]
# total_amount total_before_discount discount_amount total_payable discount_ratio rates note warnings
```

金额一律四舍五入 2 位小数（`engine.money.round2`，Decimal + ROUND_HALF_UP）。

**政策版本化**（`engine/policy_params.py`）：

* `POLICY_VERSIONS`：按期间维护政策版本（`id/label/effective_from/to/params/sources/notes/
  superseded_documents`），**历史版本不可改，只追加新版本**；
* `get_params(period)` 返回参数 + `policy_sources`（含文号/条款/生效日/失效日/原文链接/税种）；
* `policy_basis_text(period)` / `vat_basis_text(period)` 供 Agent 与界面引用；
* 2023—2025 年依据财政部 税务总局公告2023年第19号；**2026 年起依据《增值税法》+
  财政部 税务总局公告2026年第10号**（19 号公告自 2026-01-01 停止执行）；
* 申报表 bundle 由 `filing.registry.build_form()` 自动补 `policy_version` /
  `policy_sources` / `policy_basis_text`，并追加到 `guide`。

### 2.3 `ledger/models.py`

```python
INVOICE_TYPES = ("普票","专票","未开票")
INCOME_CATEGORIES = ("技术服务","软件开发","运维服务","其他")
EXPENSE_CATEGORIES = ("工资","社保","房租","办公","差旅","软件订阅","研发材料","折旧","其他")
CONTRACT_TYPES = ("买卖合同","技术合同","建设工程合同","运输合同","借款合同","租赁合同","产权转移书据","营业账簿")
FILING_STATUSES = ("草稿","已确认","已申报")
TRUE_VALUES/FALSE_VALUES, VAT_LEVY_RATE=0.01
parse_bool(v, default=False) -> bool
bool_text(v) -> "是"/"否"
money(v) -> float
normalize_date(v, default=None) -> "YYYY-MM-DD"
normalize_period(v, default=None) -> "2026Q1" / "2026-01"
derive_exclusive(inclusive, levy_rate=0.01) -> (exclusive, vat)
mask_client(client, index=None) -> str
normalize_income/expense/rd/contract/journal_entry/journal_line/filing(payload) -> dict  # 带校验，非法抛 ValueError
TABLE_DEFS: dict[str, dict]      # income / expense / rd / journal_entries / journal_lines / contracts / filings
```

表字段（CSV 列，`read()` 返回已转换类型的 dict）：

| 表 | 字段 |
|---|---|
| `income` | id,date,client,description,category,invoice_type,invoice_no,inclusive_amount,exclusive_amount,vat_amount,is_rd_related,note,created_at |
| `expense` | id,date,description,category,amount,is_rd_related,vendor,receipt_no,note,created_at |
| `rd` | id,project_name,period,personnel_cost,material_cost,depreciation,amortization,design_cost,other_cost,total,deduction_amount,note,created_at |
| `journal_entries` | id,date,entry_no,description,total_amount,source_ref,created_at |
| `journal_lines` | id,entry_no,date,description,account,direction("借"/"贷"),amount,line_no,created_at |
| `contracts` | id,date,contract_no,counterparty,contract_type,amount,is_rd_related,note,created_at |
| `filings` | id,period,form_id,form_name,status,data_json(dict),created_at,updated_at,note |

### 2.4 `ledger/store.py`

```python
table(name) -> CSVTable
CSVTable.ensure/read()/write(rows)/append(row)/append_many(rows)/get(id)/update(id,patch)/
         delete(id)/replace_all(rows)/all()/filter(start=None,end=None,**equals)/
         sorted_by_date(descending=True)/count()/backup(tag="")/next_id()
init_storage() -> None
reset_cache() -> None
backup_all(tag="") -> list[Path]
storage_stats() -> dict[str,int]
append_log(name, row: dict) -> None          # 自动加 timestamp
read_log(name, limit=None) -> list[dict]     # 新的在前
LOG_DEFS = {calculation_log, journal_log, policy_check_log, user_action_log, filing_log}
```

审计日志列（`append_log` 会自动补 timestamp，多余键忽略）：

| 日志 | 列 |
|---|---|
| calculation_log | timestamp, calc_type, period, inputs, result, operator |
| journal_log | timestamp, entry_no, description, debit, credit, amount, source |
| policy_check_log | timestamp, checked_total, stale_count, latest_verified, detail |
| user_action_log | timestamp, action, detail |
| filing_log | timestamp, period, form_id, form_name, action, status |

---

## 3. `ledger/` 待实现接口（ledger-filing-dev）

所有函数返回普通 dict / list[dict]，金额 float 2 位小数，日期字符串。新增记录须写审计日志
（算税→calculation_log，分录→journal_log，申报→filing_log，关键操作→user_action_log）。

### 3.1 `ledger/income_ledger.py`

```python
add_income(date=None, client="", inclusive_amount=0.0, *, invoice_type="普票",
           category="技术服务", description="", invoice_no="", is_rd_related=False,
           note="", exclusive_amount=None, vat_amount=None) -> dict
list_income(start=None, end=None, **filters) -> list[dict]        # 按日期升序
get_income(income_id) -> dict | None
update_income(income_id, patch: dict) -> dict
delete_income(income_id) -> bool
import_income_csv(path) -> int                                     # 返回导入条数，兼容字段中英文别名
export_income_csv(path=None) -> Path
summarize_income(start=None, end=None) -> dict
# {"count":n,"total_inclusive":x,"total_exclusive":x,"vat_amount":x,
#  "by_invoice_type":{"普票":{"count":n,"inclusive":x,"exclusive":x}, "专票":{...}, "未开票":{...}},
#  "by_category":{"技术服务":{"inclusive":x,"exclusive":x}, ...},
#  "rd_inclusive":x,"max_single":x}
period_totals(period) -> dict
# {"period":"2026Q1","start":"2026-01-01","end":"2026-03-31",
#  "total_inclusive":x,"special_inclusive":x,"ordinary_inclusive":x,
#  "total_exclusive":x,"special_exclusive":x,"ordinary_exclusive":x,
#  "count":n,"by_invoice_type":{...}}
```

### 3.2 `ledger/expense_ledger.py`

```python
add_expense(date=None, description="", amount=0.0, *, category="办公", is_rd_related=False,
            vendor="", receipt_no="", note="") -> dict
list_expenses(start=None, end=None, **filters) -> list[dict]
get_expense/update_expense/delete_expense
import_expense_csv(path) -> int
export_expense_csv(path=None) -> Path
summarize_expenses(start=None, end=None) -> dict
# {"count":n,"total":x,"rd_total":x,"by_category":{类别:{"amount":x,"count":n}}}
period_totals(period) -> dict   # {"period","start","end","total","rd_total","by_category","count"}
```

### 3.3 `ledger/rd_auxiliary.py`

```python
add_rd_record(project_name="研发项目", period=None, *, personnel_cost=0.0, material_cost=0.0,
              depreciation=0.0, amortization=0.0, design_cost=0.0, other_cost=0.0,
              note="") -> dict
list_rd_records(period=None, project=None) -> list[dict]
update_rd_record(rd_id, patch) / delete_rd_record(rd_id) -> bool
summarize_rd(period) -> dict
# 以费用台账中 is_rd_related=True 的记录自动归集（engine.aggregate_rd_expenses），
# 再叠加 rd 表手工记录，返回 engine.calc_rd_deduction 结果 + {"period","records_detail",
# "from_expenses":{...},"from_manual":{...},"by_project":[...]}
collect_from_expenses(period) -> dict
export_rd_csv(path=None) -> Path
```

### 3.4 `ledger/journal.py`（凭证：一借多贷）

```python
VOUCHER_TYPES = ("收入","费用","工资","计提","结转","其他")
build_income_entry(income_record) -> dict      # 借 银行存款/应收账款；贷 主营业务收入 + 应交税费—应交增值税
build_expense_entry(expense_record, pay_method="bank") -> dict
build_salary_entry(amount, date=None, description="计提工资", personal_tax=0.0, social_insurance=0.0) -> dict
build_vat_exemption_entry(period) -> dict | None   # 免征增值税转其他收益；无免征返回 None
build_vat_payment_entry(period) -> dict | None     # 缴纳增值税及附加税费
next_entry_no(date=None) -> str                    # 记-202601-0001
post_entry(entry: dict) -> dict                    # 写 journal_entries + journal_lines，分配 entry_no
post_entries(entries: list[dict]) -> list[dict]
list_entries(start=None, end=None) -> list[dict]   # [{header..., lines:[{account,direction,amount,line_no}]}]
get_entry(entry_no) -> dict | None
trial_balance(start=None, end=None) -> dict
# {"accounts":[{"account":..,"debit":x,"credit":x,"balance":x}],"total_debit":x,"total_credit":x,"balanced":bool}
```

entry 结构（`build_*` 返回、`post_entry` 接受）：

```python
{"date":"2026-01-05","description":"收到云图科技技术服务费","source_ref":"INC000001",
 "voucher_type":"收入",
 "lines":[{"account":"银行存款","direction":"借","amount":20600.00},
          {"account":"主营业务收入","direction":"贷","amount":20396.04},
          {"account":"应交税费—应交增值税","direction":"贷","amount":203.96}]}
```

### 3.5 `ledger/reports.py`

```python
monthly_summary(year, months=None) -> list[dict]
# [{"month":"2026-01","income_inclusive":x,"income_exclusive":x,"expense":x,"profit":x,"tax":x}]
quarterly_summary(period) -> dict
# {"period","label","income_inclusive","income_exclusive","special_inclusive","expense",
#  "profit","vat","surtax","rd_cost","rd_deduction"}
income_expense_report(period) -> dict
# 上述季度字段 + {"start","end","by_category_income","by_category_expense","count_income","count_expense"}
profit_statement(period) -> list[dict]
# 利润表行次 [{"row":"一、营业收入","amount":x},{"row":"减：营业成本","amount":x},
#             {"row":"税金及附加",...},{"row":"销售费用",...},{"row":"管理费用",...},
#             {"row":"财务费用",...},{"row":"二、营业利润",...},{"row":"三、利润总额",...},
#             {"row":"四、净利润",...}]（用台账口径估算：营业成本=费用中工资/房租/材料等，
#             税金及附加=算出的增值税附加，研发费用单列）
dashboard() -> dict
# {"period":"2026Q4","quarter_income_inclusive":x,"quarter_income_exclusive":x,
#  "quarter_expense":x,"quarter_profit":x,"vat_payable":x,"surtax":x,"total_tax":x,
#  "ytd_income":x,"ytd_expense":x,"ytd_profit":x,"counts":{"income":n,"expense":n,"rd":n,"contracts":n},
#  "next_deadline":{...engine.upcoming_deadlines 首项...},"reminders":[...],
#  "recent_income":[最近5条],"recent_expense":[最近5条],"updated_at":str}
```

### 3.6 `ledger/audit.py`

```python
log_calculation(calc_type, period, inputs: dict, result: dict, operator="system") -> None
log_journal(entries: list[dict], source="") -> None
log_policy_check(checked_total, stale_count, latest_verified, detail="") -> None
log_user_action(action, detail="") -> None
log_filing(period, form_id, form_name, action, status) -> None
recent_logs(limit=20) -> dict[str, list[dict]]     # {"calculation_log":[...],...}
```

### 3.7 `ledger/seed.py`（示例数据，供 UI「加载示例数据」与集成演示）

```python
def seed_demo_data(reset: bool = False) -> dict:
    """生成 2026 年 Q1—Q4 示例数据（企业：禄劝某科技网络科技）。返回各表生成条数。
    reset=True 时先清空全部台账。
    要求：2026Q1 全普票约 20 万含税；2026Q2 含 1 张专票（如 10.1 万）合计约 28 万；
    2026Q3 含税约 25 万；2026Q4 至今约 12 万。每月 1-3 笔收入、2-4 笔费用（含研发相关），
    研发费用每月工资 2 万 + 材料/软件订阅；合同 3-5 份（技术合同为主）。
    """
def is_empty() -> bool       # 所有台账均为空
def clear_all() -> None      # 清空全部台账（不含日志）
```

### 3.8 `filing/` 申报填报

统一「申报表 bundle」结构（**UI 与导出、校验、Agent 都依赖此结构**）：

```python
{
  "form_id": "VAT_QUARTERLY",
  "form_name": "增值税及附加税费申报表（小规模纳税人适用）",
  "category": "增值税",
  "period": "2026Q1",
  "period_label": "2026年第一季度",
  "frequency": "quarterly",
  "generated_at": "2026-11-09 23:30:00",
  "status": "草稿",                       # 草稿 / 已确认 / 已申报
  "sections": [                           # 表格分区，按顺序展示
    {"title": "主表", "lines": [
        {"line_no": "1", "name": "应征增值税不含税销售额（3%征收率）",
         "value": 0.0, "value_text": "0.00", "source": "收入台账（专票）",
         "rule": "专票不含税销售额", "highlight": False},
        ...]},
    {"title": "附加税费", "lines": [...]}
  ],
  "totals": {"vat_payable": 0.0, "surtax": 0.0, "total_tax": 0.0, ...},
  "calc": {...engine.calc_vat 结果...},     # cit/stamp 表放对应 engine 结果
  "checks": [{"field": "第24栏", "status": "PASS|WARNING|ERROR", "message": "..."}],
  "summary": {"level": "PASS|WARNING|ERROR", "text": "..."},
  "guide": ["1. ...", "2. ..."],
  "data": {"income_totals": {...}, ...},   # 生成依据的原始数据快照，便于审计
  "disclaimer": "⚠️ 仅供参考，以主管税务机关核定为准"
}
```

```python
# filing/registry.py
FORMS: list[dict]     # [{"form_id","form_name","category","frequency","period_kind","description","builder"}]
list_forms() -> list[dict]
get_form(form_id) -> dict            # 不存在抛 KeyError
build_form(form_id, period, **overrides) -> dict
build_all(period) -> list[dict]      # 全部报表（含财务报表与印花税）

# filing/vat_return.py
build_vat_return(period, *, income_totals=None) -> dict
# 必填主表栏次（与规范文档 Module 4 一致，术语用现行申报表）：
#  第1栏 应征增值税不含税销售额(3%征收率) = calc.return_lines.line_1_taxable_sales
#  第2栏 税务机关代开增值税专用发票销售额
#  第10栏 小微企业免税销售额 = ordinary_exclusive（免税时）
#  第11栏 未达起征点销售额（季度销售额≤30万时为免税销售额，按口径填）
#  第18栏 应纳税额；第19栏 本期免税额；第22栏 应纳税额减征额；第24栏 本期应补（退）税额
#  附加税费：城建税、教育费附加、地方教育附加（rate 与减免后金额）
#  另出「附列资料」说明：专票/普票/未开票销售额拆分

# filing/cit_prepay.py
build_cit_prepay(period) -> dict
# 行次：1 营业收入、2 营业成本、3 利润总额、4 加：特定业务应纳税所得额、
# 5 减：不征税收入、6 减：免税收入、7 减：减计收入、8 减：加计扣除（研发）、
# 9 实际利润额、10 税率(25%)、11 应纳所得税额、12 减免所得税额、13 实际应纳所得税额、
# 14 本年累计已预缴税额、15 本期应补（退）税额。
# 使用 engine.calc_cit_prepay 计算（本年累计口径），rd_cost_ytd 取研发辅助账。

# filing/stamp_return.py
build_stamp_return(period) -> dict
# 合同台账→engine.calc_stamp_tax；行次：按合同类型的计税金额、税率、应纳税额、减免额、应缴税额。

# filing/fin_report.py
build_fin_report(period) -> dict
# 财务报表（小企业会计准则简化版）：资产负债表（期末/年初）+ 利润表（本期/本年累计）。
# 以台账推算：货币资金≈累计收入-累计费用，应收/应付为 0；必须标注「按台账估算，需与账面核对」。

# filing/validator.py
validate_bundle(bundle) -> dict
# {"status":"PASS|WARNING|ERROR","details":[{"field","expected","actual","status","message"}],
#  "summary":"...","disclaimer":...}
validate_vat_return(period, bundle=None) -> dict     # 重算并与 bundle 比对
check_ledger_consistency(period) -> dict             # 台账自检：专票+普票+未开票=合计、季度归属、金额异常
validate_against_fields(period, fields: dict) -> dict
# 与 OCR/手工填报的栏次值比对。fields 键：line_1、line_10、line_18、line_19、line_22、line_24、
# surtax_total、total_payable 等；值 float。返回同上结构，标准答案来自 build_vat_return。

# filing/export.py
export_bundle_excel(bundle, path=None) -> Path      # 单表 xlsx（含「填报指引」「校验结果」sheet）
export_package_excel(period, bundles=None, path=None) -> Path   # 多表一个工作簿，文件名含期间
export_bundle_json(bundle, path=None) -> Path
export_checklist_markdown(period, bundles=None, path=None) -> Path   # 申报核对清单（md）
EXPORT_DIR 默认 config.EXPORT_DIR

# filing/progress.py
save_filing(bundle, status="草稿") -> dict          # upsert 到 filings 表（period+form_id 唯一）
list_filings(period=None) -> list[dict]
update_status(filing_id, status) -> dict            # 状态：草稿/已确认/已申报
progress(period) -> dict
# {"period","total":5,"done":2,"percent":40,"items":[{"form_id","form_name","status","required","updated_at"}]}
mark_submitted(period, form_id) -> dict

# filing/guide.py
build_guide(bundle) -> list[str]                   # 填报步骤清单（中文、可操作）
export_submission_text(bundle) -> str              # 逐栏复制文本（便于电子税务局对照录入）
```

**申报表填报流程要求**（`filing` + `ui` 配合）：选期间 → `build_all` 生成 → 页面展示栏次 →
`validate_bundle` 校验 → `save_filing` 存草稿 → `export_package_excel` 导出 → `mark_submitted`
标记已申报；期间进度用 `progress(period)`。

---

## 4. `agent/` 待实现接口（agent-knowledge-dev）

### 4.1 `agent/llm_client.py`

```python
class DeepSeekError(Exception): ...
class DeepSeekClient:
    def __init__(self, api_key=None, base_url=None, model=None, temperature=None,
                 max_tokens=None, timeout=None)
    @property configured -> bool
    def chat(self, messages: list[dict], *, tools=None, temperature=None, max_tokens=None,
             response_format=None) -> dict
    # {"content": str, "reasoning_content": str,
    #  "tool_calls": [{"id": str|None, "name": str, "arguments": dict}],
    #  "usage": {...}, "model": str, "raw": dict}
    def stream_chat(self, messages, *, tools=None, **kwargs) -> Iterator[str]   # 逐段文本
    def complete(self, prompt: str, system: str | None = None, **kwargs) -> str
    def test_connection(self) -> dict
    # {"ok": bool, "model": str, "latency_ms": int, "message": str, "reply": str}
def get_client(api_key=None, model=None) -> DeepSeekClient
def parse_json(text: str) -> dict          # 容错解析（去 ```json 包裹、截取首个 {...}）
```

要求：`requests` 直连 `{base_url}/chat/completions`，OpenAI 兼容协议；网络/鉴权错误翻译成中文
`DeepSeekError`；不打印/记录 API Key；超时默认 90s；工具调用参数 JSON 解析失败时返回空 dict 并告警。

### 4.2 `agent/prompt_templates.py`

```python
DISCLAIMER, PERSONA
def build_system_prompt(company=None, policy=None, extra=None) -> str
# 采用规范文档 2.2 System Prompt：角色「税小智」、企业画像、行为准则、回复风格、
# 硬性约束（算税必须调用工具、政策标注来源与生效日期、末尾免责声明、禁止避税建议）
SYSTEM_PROMPT: str                       # 默认实例
TAX_CALC_PROMPT / INCOME_PROMPT / POLICY_PROMPT / FILING_PROMPT / OCR_PROMPT / SUMMARY_PROMPT: str
INTENT_PROMPT: str                       # 要求输出 JSON {intent, entities, confidence}
```

### 4.3 `agent/tools.py`

```python
TOOL_NAMES: list[str]
def tool_specs() -> list[dict]           # OpenAI function-calling schema
def call_tool(name: str, arguments: dict) -> dict
# 统一返回 {"ok": True/False, "data": 任意, "display": str(中文摘要), "error": str|None}
def describe_tools() -> list[dict]       # 供 UI 展示：名称/用途/参数
```

必须包含的工具（名称固定）：

| 名称 | 作用 | 主要参数 |
|---|---|---|
| `calc_vat` | 增值税及附加计算 | quarterly_total_inclusive, special_invoice_inclusive, period |
| `calc_corp_tax` | 企业所得税年度测算 | annual_profit, rd_cost, employees, total_assets, period |
| `calc_cit_prepay` | 所得税季度预缴 | quarter_profit, ytd_profit, ytd_cit_paid, rd_cost_ytd, period |
| `calc_rd_deduction` | 研发加计扣除 | period, formed_intangible |
| `calc_stamp_tax` | 印花税 | period |
| `add_income` | 记一笔收入 | client, inclusive_amount, invoice_type, category, date, description |
| `add_expense` | 记一笔费用 | description, amount, category, is_rd_related, date |
| `query_ledger` | 台账汇总 | period, kind(income/expense/rd/contracts) |
| `build_filing` | 生成申报表 | form_id, period |
| `check_filing` | 申报核对（文本/截图字段） | period, fields 或 text |
| `get_filing_calendar` | 申报日历 | days |
| `search_policy` | 政策检索 | query |
| `export_filing` | 导出申报包 Excel | period |

工具实现直接调用 engine/ledger/filing，**不得让模型自行计算**；所有结果带 `disclaimer`。

### 4.4 `agent/intent_router.py`

```python
INTENTS: dict[str, str]   # calc_vat/calc_corp_tax/calc_rd/income/expense/journal/policy/filing_check/calendar/red_bill/chat
def detect_intent(text: str, *, client=None) -> dict
# {"intent": str, "confidence": float, "entities": dict, "method": "rule"|"llm", "raw": str}
def extract_entities(text: str) -> dict
# {"amounts":[float],"date":"YYYY-MM-DD","period":"2026Q1","client":str,
#  "invoice_type":str,"category":str,"is_rd_related":bool,"keywords":[...]}
def route(text: str) -> dict
# {"intent","tool","arguments","reply_hint"}
```

意图必须覆盖规范文档 2.1 的 8 类（算税/记账/分录/政策/申报核对/研发归集/日历/红利账单），
规则优先（正则+关键词，覆盖常见中文表述与金额单位「万/元」），LLM 只作兜底。

### 4.5 `agent/orchestrator.py`

```python
def build_context() -> str
def local_reply(text: str) -> dict
# 无 API Key 时的确定性回复：{"reply","intent","tool_calls":[{"name","arguments","result"}],
#  "used_llm":False,"error":None,"disclaimer":...}
def run_turn(user_text: str, history: list[dict] | None = None, *, api_key=None, model=None,
             allow_tools=True, max_rounds=4) -> dict
# 有 Key：system prompt + 历史 + 用户输入 → 工具循环 → 最终中文回复；
# 无 Key 或 API 失败：回退 local_reply，并在 error 字段说明原因。
def run_agent(...) 同 run_turn（兼容别名）
```

回复要求：末尾附 `config.DISCLAIMER`；引用政策时给出公告号与生效日期；数字保留 2 位小数。

---

## 5. `knowledge/` 与 `ocr/` 待实现接口（agent-knowledge-dev）

### 5.1 `knowledge/retriever.py`

```python
POLICY_DIR = config.POLICY_DIR
def load_policies(force=False) -> list[dict]
# 解析 knowledge/policy_files/**/*.md 的 YAML frontmatter：
# {id, title, category, effective_date, expiry_date, source, last_verified, tags[], path, content, summary}
def list_policies(category=None) -> list[dict]
def get_policy(policy_id) -> dict | None
def search(query, top_k=5, category=None) -> list[dict]      # BM25/关键词，返回带 score 的政策
def build_context(query, top_k=4) -> str                    # 供 LLM 的检索上下文（含来源与日期）
def answer(query, *, client=None, history=None) -> dict
# {"answer","used_llm":bool,"citations":[{"title","source","effective_date","last_verified","path"}],
#  "matched":[{"id","title","score"}],"disclaimer"}
```
无 API Key 时输出「检索到的政策摘要 + 原文片段」，有 Key 时用 DeepSeek 基于上下文作答并要求标注来源。

### 5.2 `knowledge/update_checker.py`

```python
def stale_policies(days=90, today=None) -> list[dict]
def check_policies(days=90) -> dict
# {"checked_total":n,"stale":[{"id","title","last_verified","days"}],"latest_verified":str,
#  "advice":[str],"checked_at":str}；并写 policy_check_log 审计日志
def mark_verified(policy_id, verified_date=None) -> dict     # 更新 frontmatter last_verified 并写日志
def policy_stats() -> dict                                   # {"total","categories":{...},"oldest":...}
```

### 5.3 `knowledge/policy_files/` 内容（至少 10 篇，按规范文档 5.2 格式）

```
增值税/小规模纳税人起征点与1%征收率.md
增值税/六税两费减半征收.md
增值税/技术合同免征增值税.md
企业所得税/小型微利企业优惠.md
企业所得税/研发费用加计扣除.md
其他税费/印花税.md
其他税费/个人所得税扣缴.md
申报流程/云南电子税务局_增值税申报.md
申报流程/自然人扣缴端_个税申报.md
申报流程/企业所得税_季度预缴与汇算清缴.md
```
frontmatter 必填：`title/category/effective_date/expiry_date/source/last_verified/tags`。
正文按「适用条件 / 优惠内容 / 办理方式 / 留存资料 / 注意事项」组织，务必标注政策文号与生效日期；
可用 web_search 核对关键政策（当前 2026-11），无法核实的表述要写「以主管税务机关口径为准」。

### 5.4 `ocr/`

```python
# ocr/extractor.py
def is_available() -> dict            # {"macos_vision":bool,"tesseract":bool,"manual":True}
def extract_text(image_path) -> dict
# {"text":str,"backend":"macos_vision"|"tesseract"|"manual","ok":bool,"message":str}
def ocr_image(image_path) -> str      # 优先 macOS Vision（Swift 编译缓存到 ocr/bin/macos_ocr），
                                      # 其次 pytesseract（需 chi_sim），都不可用抛 OCRAvailableError
def build_vision_binary() -> Path|None

# ocr/field_mapper.py
VAT_FIELD_DEFS: list[dict]            # [{key:"line_1", line_no:"1", name, aliases[...]}]
def map_fields(text, *, period=None, client=None) -> dict
# {"fields":{"line_1":float,...},"method":"rule"|"llm","unmapped":[str],"ok":bool,"raw":text}
def regex_extract(text) -> dict
def llm_extract(text, client) -> dict
def parse_pasted_table(text) -> dict

# ocr/validator.py
def check_vat_return(fields: dict, period: str) -> dict
# 复用 filing.validator.validate_against_fields，输出差异报告：
# {"status":"PASS/WARNING/ERROR","details":[{"field","expected","actual","status","message"}],
#  "summary","disclaimer"}
def check_from_image(image_path, period) -> dict      # extract → map → check
def check_from_text(text, period) -> dict
```

**注意**：DeepSeek 官方 API 无视觉能力，截图识别必须走本地 OCR（macOS Vision / tesseract），
文字理解与栏次映射可用 DeepSeek。UI 同时提供「手动粘贴文本」入口作为兜底。

---

## 6. UI 规范（ui-dev）

* 入口：`ui/streamlit_app.py`（单文件入口 + `ui/views/*.py` 页面函数；侧边栏 radio 导航，
  **不要用 st.navigation**，以便 `streamlit.testing.v1.AppTest` 可靠测试）。
* 启动引导：脚本顶部 `sys.path.insert(0, str(Path(__file__).resolve().parents[1]))`；
  调用 `config.ensure_dirs()`、`ledger.init_storage()`。
* 主题：`.streamlit/config.toml`（primaryColor 深蓝/税务蓝，backgroundColor 浅灰白，字体思源/系统中文），
  可用少量自定义 CSS（卡片、渐变标题、指标卡片），风格专业克制。
* 页面（侧边栏）：
  1. 🏠 首页驾驶舱：企业名片、本季度收入/应纳税费/申报倒计时、提醒、收入趋势图（plotly）、台账笔数、快捷入口
  2. 📒 台账管理：收入/费用/研发/合同 四个 tab，新增表单、`st.data_editor` 表格、CSV 导入导出、删除、汇总指标
  3. 🧮 算税工作台：增值税/企业所得税/研发加计/印花税 计算器，「从台账取数」按钮，结果 + 分录 + 解释
  4. 📋 申报填报：期间选择 → 生成全部申报表 → 逐表展示栏次（可核对）→ 校验结果 → 导出 Excel/JSON → 标记已申报 → 进度条
  5. 🔍 申报核对：上传截图/粘贴文本 → OCR → 字段映射 → 差异报告
  6. 📚 政策知识库：搜索、分类筛选、政策详情（来源/生效日期）、检查更新
  7. 🤖 AI 助手：聊天（`st.chat_message`/`st.chat_input`），无 Key 时提示并可继续离线使用；模型切换
  8. ⚙️ 设置：API Key 填写/测试连接/清除（`config.set_api_key`）、模型选择、企业信息、隐私模式、数据备份、示例数据、关于与免责
* 所有算税/申报结果下方必须显示 `config.DISCLAIMER`；金额格式 `¥1,234.56`；
  空数据有友好空态与「加载示例数据」按钮（`ledger.seed.seed_demo_data`）。
* 不要把 API Key 写进日志或 st.write 明文（用 `config.mask_key`）。
* 冒烟测试 `tests/test_ui_smoke.py`：

```python
from streamlit.testing.v1 import AppTest
def test_app_renders():
    at = AppTest.from_file("ui/streamlit_app.py", default_timeout=60)
    at.run()
    assert not at.exception
```
必要时切换各页面 radio 后 `at.run()` 再断言。测试环境无 API Key，页面不得崩溃。

---

## 7. 通用约定

1. **语言**：面向用户的文字、注释、docstring 全部中文；变量/函数名英文 snake_case。
2. **金额**：统一 `engine.money.round2`（Decimal ROUND_HALF_UP），输出 float；界面千分位。
3. **免责声明**：所有算税/申报/政策输出必须带 `config.DISCLAIMER` 或 `config.POLICY` 来源。
4. **禁止**：LLM 算税、编造政策、建议逃税/避税、主动建议转账、把财务数据传出本地
   （DeepSeek 调用仅发送必要文本片段，如用户问题、申报表文字、台账汇总，不含客户名时可脱敏）。
5. **审计**：算税、分录、申报、关键操作写 `ledger/audit.py` 日志；异常不得静默吞掉关键错误，
   但台账写入失败时可降级（日志失败不影响主流程）。
6. **测试**：不访问真实 DeepSeek API（有 Key 的联网测试用 `pytest.mark.skipif` + `-m live` 排除）；
   测试只写临时目录（`tests/conftest.py` 已把 `TAX_DATA_DIR/TAX_LOG_DIR/TAX_EXPORT_DIR` 指向临时目录）；
   每个测试用 `clean_storage` fixture（autouse）保证隔离。
7. **导入**：包内相对导入用绝对形式（`from ledger.store import table`），禁止 `from . import` 混用。
8. **错误**：对外函数遇到用户输入错误抛 `ValueError`（中文消息）；UI 层 `try/except` 后 `st.error`；
   Agent 工具调用错误转为 `{"ok": False, "error": "..."}`。
9. **性能**：CSV 全量读写可接受（数据量小），但页面内同一数据不要重复读多次（用 `st.cache_data(ttl=5)` 或局部变量）。

---

## 8. 完成定义（DoD）

1. `python -m pytest tests/` 全绿（各自范围内）。
2. 模块公开接口与本文档一致；不修改不属于自己范围的文件。
3. 关键路径有中文 docstring；无 TODO 占位实现。
4. 与相邻模块的联调自测通过（如 ledger-filing-dev 用真实 engine 计算；ui-dev 用真实 ledger/filing）。
5. 完成后用 `send_message` 向 Lead 汇报：改动文件清单、公开接口、测试结果、遗留问题。
