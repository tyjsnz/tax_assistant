# P0-2 契约：票据与银行流水导入 + 自动匹配（CONTRACTS_INGEST）

> 本文件是 P0-2 模块的并行开发接口约定，与 `docs/CONTRACTS.md` 配套使用。
> **数据表已由 Lead 写入 `ledger/models.py`（只读）**，后端实现不得修改该文件。
>
> 目标：把「电子发票 + 银行流水」变成结构化数据，自动匹配后一键生成收入/费用台账与凭证，
> 让用户从「手工抄数」变成「导入→确认→入账」。

---

## 0. 环境与命令

```bash
cd /Users/shanengzhi/www/2026/prj/tax
.venv/bin/python -m pytest tests/test_ingest.py -q
.venv/bin/python -m pytest tests/test_ui_smoke.py -q
```
已安装：`pypdf`（PDF 文本解析，可选降级）、`openpyxl`（xlsx）、标准库 `xml/zipfile/csv/difflib`。

---

## 1. 数据表（Lead 已定义，见 `ledger/models.py`）

| 表 | 关键字段 |
|---|---|
| `invoices` | invoice_no, invoice_code, invoice_date, invoice_type(数电票/专票/普票/其他), direction(销项/进项), seller, buyer, amount_exclusive, tax_amount, total_inclusive, status(待匹配/已匹配/已入账/已忽略), source_file, source_format(XML/OFD/PDF/手动), raw_json, note |
| `bank_transactions` | transaction_date, direction(收入/支出), amount(正数), counterparty, account_no, summary, transaction_no, balance, status(待匹配/已匹配/已入账/已忽略), source_file, source_format(CSV/XLSX), note |
| `matches` | invoice_id, invoice_no, transaction_id, transaction_no, score(0-100), match_type("发票-流水"), status(建议/已确认/已忽略), amount_diff, date_diff, note, confirmed_at |

常量（`ledger.models`）：`INVOICE_DIRECTIONS`、`INVOICE_KINDS`、`INGEST_STATUSES`、`MATCH_STATUSES`。
统一通过 `ledger.store.table("invoices"/"bank_transactions"/"matches")` 读写；日期用 `ledger.models.normalize_date`，金额用 `round2`。

---

## 2. 发票解析 `ingest/invoice_parser.py`

```python
def parse_invoice_file(source, filename: str | None = None) -> dict
# source: 文件路径（str/Path）或 bytes；filename 用于判断扩展名
# 返回：
# {"ok": bool, "format": "XML"|"OFD"|"PDF"|"UNKNOWN", "invoices": [normalized_invoice, ...],
#  "text": str, "errors": [str]}

def parse_invoice_bytes(data: bytes, filename: str) -> dict
def parse_xml_invoice(data) -> dict            # 单张 XML → {"ok","invoice": dict,"text","errors"}
def parse_ofd_invoice(data: bytes) -> dict     # zip 内查找含发票字段的 XML（支持 OriginalInvoice.xml）
def parse_pdf_invoice(data: bytes) -> dict     # pypdf 提取文本 + 正则解析；无 pypdf 时返回 ok=False + 中文提示

def normalize_invoice(raw: dict, *, source_format="XML") -> dict
# 统一字段：invoice_no/invoice_code/invoice_date/invoice_type/direction/seller/buyer/
# amount_exclusive/tax_amount/total_inclusive/status="待匹配"/source_format/raw_json

def save_invoices(invoices: list[dict], source_file: str = "") -> dict
# 去重键：invoice_no + invoice_date（无号码时用 seller+total+date 哈希）
# 返回 {"saved": [记录...], "skipped": [{"invoice":..., "reason":"重复"}]}
```

解析要求：

1. **XML 兼容性**：支持带命名空间（`EInvoice`/`Invoice`/任意前缀）与中文标签
   （`发票号码`、`开票日期`、`销售方名称`、`购买方名称`、`合计金额`、`合计税额`、`价税合计`）。
   建议把 XML 扁平化为 `{本地标签名: 文本}` 再用别名表取值。
2. **字段别名**：号码 `InvoiceNumber/发票号码`；日期 `IssuTime/IssueDate/开票日期`；销售方
   `SellerName/销售方名称`；购买方 `BuyerName/购买方名称`；价税合计 `TotalTax-includedAmount/价税合计`；
   不含税 `TotalAmWithoutTax/合计金额`；税额 `TotalTaxAm/合计税额`。
3. **方向**：`config.get_company()["name"]` 出现在 `seller` → 销项；出现在 `buyer` → 进项；
   都对不上 → 按 seller/buyer 与公司名相似度择一，无法判断则 `direction="销项"` 并在 note 标注待确认。
4. **发票类型**：XML/PDF 文本含「专用发票」→ `专票`；含「普通发票」→ `普票`；
   仅有「数电票/电子发票」→ `数电票`；否则 `其他`。
5. **日期**：支持 `YYYY-MM-DD`、`YYYY-MM-DD HH:MM:SS`、`YYYY年MM月DD日`、`YYYYMMDD`。
6. **金额**：去千分位与「¥/￥」；`total_inclusive` 缺失时用 `amount_exclusive + tax_amount` 补齐；
   允许 1 分钱误差，差额回填让三者勾稽一致。
7. 任一字段缺失不得崩溃：`ok=True` 但 `errors` 里给出提示，缺失字段留空/0。
8. PDF：pypdf 提取文本后用中文字段正则（`发票号码[:：]?\s*(\d+)`、`价税合计.*?(\d+\.\d{2})` 等）；
   纯图片 PDF 无法识别时返回 `ok=False` 并提示「请使用 XML/OFD 或手动录入」。
9. OFD：`zipfile` 扫描所有 `*.xml`，优先找含 `InvoiceNumber/发票号码` 的条目；拼接文本供展示。

`ingest/importer.py` 中的手动录入：

```python
def add_manual_invoice(payload: dict) -> dict
# payload: invoice_no/invoice_date/total_inclusive/amount_exclusive?/tax_amount?/seller/buyer/
#          invoice_type/direction/note；返回保存后的记录
```

---

## 3. 银行流水解析 `ingest/bank_parser.py`

```python
BANK_COLUMN_ALIASES: dict[str, list[str]]
# date: ["交易日期","日期","记账日期","交易时间"]
# amount: ["交易金额","金额","发生额","收支金额"]
# income: ["收入","收入金额","贷方发生额","收入金额(元)","贷方金额"]
# expense: ["支出","支出金额","借方发生额","支出金额(元)","借方金额"]
# direction: ["收支方向","方向","借贷标志","收付标志","交易类型"]
# counterparty: ["对方户名","对方账户名","交易对方","对方单位","收/付款方"]
# summary: ["摘要","交易摘要","用途","备注","交易说明"]
# transaction_no: ["交易流水号","流水号","交易序号","凭证号"]
# balance: ["余额","账户余额","交易后余额"]

def detect_columns(headers: list[str]) -> dict           # canonical_key → 原始列名（可为空）
def parse_bank_file(source, filename=None, *, mapping=None, account_no="") -> dict
# {"ok","format":"CSV"|"XLSX"|"UNKNOWN","transactions":[normalized_transaction],
#  "columns":{...},"text":"","errors":[]}
def parse_bank_csv(data: bytes, mapping=None) -> dict    # 编码自动尝试 utf-8-sig / gbk / gb18030
def parse_bank_excel(data: bytes, mapping=None) -> dict  # openpyxl：自动找表头行（前 5 行内）
def normalize_transaction(raw: dict, mapping: dict, *, account_no="") -> dict

def save_transactions(transactions: list[dict], source_file="") -> dict
# 去重键：transaction_no；无流水号时用 (date,direction,amount,counterparty,summary) 哈希
# 返回 {"saved":[...], "skipped":[{"transaction":..., "reason":"重复"}]}
```

解析要求：

1. **列映射**：先按别名自动识别；识别不到时允许调用方传 `mapping`（UI 提供高级映射）。
2. **方向判定**：
   - 有独立收入/支出列：非空列为方向；两列都空跳过；都为非空取绝对值较大者并告警；
   - 单金额列 + 方向列：方向值含「收入/收/贷/进/入账」→ 收入；「支出/付/借/出/出账」→ 支出；
   - 单金额列无方向列：按金额正负判断（负→支出），金额取绝对值；
   - 无法判断 → 跳过该行并记 errors（附行号）。
3. **金额**：去 `¥/￥/逗号/空格`；正数；为 0 的行跳过。
4. **日期**：复用 `ledger.models.normalize_date`；Excel 日期单元格（datetime）直接转 `YYYY-MM-DD`。
5. **去重**：重复导入同一文件不得产生重复流水。
6. 支持表头前有标题行（银行导出常有 1-3 行说明）：在前 5 行内找包含「日期/金额/摘要」等关键词的行作为表头。

---

## 4. 自动匹配 `ingest/matcher.py`

```python
def score_pair(invoice: dict, transaction: dict, *, date_window_days=15) -> dict
# {"eligible": bool, "score": float(0-100), "amount_diff": float, "date_diff": int,
#  "name_similarity": float(0-1), "reasons": [str]}
def suggest_matches(*, date_window_days=15, min_score=60.0) -> dict
# {"created":[match,...], "count":n, "candidates":m, "summary":{...}}
def list_matches(status: str | None = None) -> list[dict]
# 每条 = match 字段 + invoice 摘要（seller/buyer/invoice_date/invoice_type/total_inclusive）
#         + transaction 摘要（date/amount/counterparty/summary）
def confirm_match(match_id: str, *, post_journal: bool = True) -> dict
# {"ok":True, "match":{...}, "ledger":{"kind":"income"|"expense","record":{...}},
#  "journal":{"entry_no":..., "lines":[...]}|None, "invoice_status":"已入账",
#  "transaction_status":"已入账", "warnings":[...]}
def ignore_match(match_id: str) -> dict
def match_summary() -> dict
# {"invoices_pending","invoices_matched","transactions_pending","suggestions","confirmed",
#  "ignored","matched_income","matched_expense","pool_total":{...}}
```

评分规则（**必须确定性实现，便于测试**）：

* 仅匹配同方向：销项 ↔ 收入；进项 ↔ 支出；方向不符直接 `eligible=False`。
* 金额：`amount_diff = abs(invoice.total_inclusive - transaction.amount)`
  - ≤0.01 → +45；≤0.5% → +35；≤1% → +30；≤3% → +18；≤5% → +8；>5% → 不可匹配
* 日期：`date_diff = abs(invoice_date - transaction_date).days`
  - 0 天 → +30；≤3 → +25；≤7 → +18；≤15 → +10；>window → 不可匹配
* 对方名称：把发票对方（销项取 buyer，进项取 seller）与流水 counterparty 归一化
  （去空格/全角、去「有限公司/有限责任公司/科技」等后缀）后用 `difflib.SequenceMatcher`，
  `name_similarity × 25` 计入。
* 总分四舍五入 1 位；`min_score` 默认 60。
* 建议生成：候选对按分数降序贪心分配，**一张发票/一笔流水只匹配一次**；
  已存在 `建议` 或 `已确认`/`已忽略` 的组合跳过。
* `confirm_match`：
  - 校验 match 状态必须为 `建议`，否则抛 `ValueError`（幂等保护）；
  - 销项 → `ledger.income_ledger.add_income(...)`（client=buyer，含税=total_inclusive，
    invoice_type 专票→"专票"，其余→"普票"，category 按摘要关键词猜测：软件开发/运维/技术服务，
    invoice_no、note="票据流水匹配入账"）；
  - 进项 → `ledger.expense_ledger.add_expense(...)`（category 猜测：办公用品/服务/差旅/其他，
    vendor=seller，receipt_no=invoice_no，note="进项发票入账（小规模不抵扣）"）；
  - `ledger.journal.build_income_entry/build_expense_entry` 生成 entry，`post_journal=True` 时
    `journal.post_entry(entry)` 入账；
  - 更新 match=已确认+confirmed_at、invoice/transaction=已入账；
  - 写 `ledger.audit.log_user_action("票据匹配入账", detail)`；
  - 金额差异 >0.01 时在 `warnings` 提示（按发票金额入账）。
* `ignore_match`：match=已忽略，不动发票/流水状态；写审计日志。

---

## 5. 编排与演示 `ingest/importer.py` / `ingest/demo.py`

```python
# ingest/importer.py
def import_invoice_files(files, *, auto_match=False) -> dict
# files: [(filename, bytes)] 或带 .name/.getvalue() 的上传对象
# {"parsed":n,"saved":n,"skipped":n,"errors":[{"file","error"}],"by_format":{...},
#  "saved_records":[...],"match":{...}|None}
def import_bank_files(files, *, mapping=None, account_no="", auto_match=False) -> dict
def import_files(files, *, auto_match=True) -> dict      # 按扩展名分发（xml/ofd/pdf → 发票；csv/xlsx → 流水）
def pool_summary() -> dict
# {"invoices":{count,by_status,by_direction,by_type,amount},"transactions":{...},
#  "matches":{count,by_status},"last_import_at":str}
def reset_pool() -> dict                                  # 清空三张池表（测试/演示用，写审计日志）

# ingest/demo.py
def seed_demo_pool(reset: bool = False) -> dict
# 读取项目 examples/ 下 2 张 XML 发票 + 1 个银行流水 CSV，导入并自动匹配；
# 返回 {"invoices":n,"transactions":n,"suggestions":n,"files":[...]}
```

`ingest/__init__.py` 导出：`parse_invoice_file / parse_bank_file / save_invoices /
save_transactions / suggest_matches / confirm_match / ignore_match / import_files /
pool_summary / seed_demo_pool`。

---

## 6. UI 契约 `ui/views/ingest.py`（ui-dev）

* 侧边栏新增页面 **「📥 票据与流水」**，插在「📒 台账管理」之后；更新 `ui/components/nav.py` 的
  `PAGES`、`PAGE_MODULES`，并同步 `tests/test_ui_smoke.py` 的 `PAGES` 列表（9 页）。
* 页面结构：
  1. 页头 hero（`ui` 组件）+ 一键「加载示例票据/流水」（`ingest.demo.seed_demo_pool()`，成功后 `st.rerun()`）；
  2. **上传发票**：`st.file_uploader(accept_multiple_files=True, type=["xml","ofd","pdf"])`，
     按钮「解析并入库」→ 显示 saved/skipped/errors 指标与明细；展开「手动录入发票」表单
     （发票号码/日期/价税合计/类型/方向/销售方/购买方 → `ingest.importer.add_manual_invoice`）；
  3. **上传银行流水**：`st.file_uploader(type=["csv","xlsx"])` + 账号输入 + 高级列映射 expander
     （`ingest.bank_parser.detect_columns` 失败或用户想覆盖时用 `st.selectbox` 逐列映射）→「导入流水」；
  4. **票据池 / 流水池**：`st.tabs(["发票池","银行流水池"])` + 状态筛选 + `st.data_editor`（只读或允许改
     status/note）+ 「删除选中」；列显示中文表头、金额千分位；
  5. **自动匹配**：按钮「🔎 自动匹配」→ `suggest_matches()`；建议表含 score/发票/流水/金额差/日期差/
     「确认」勾选列（`st.data_editor`）；「✅ 确认勾选并入账」（逐条 `confirm_match`，汇总成功/失败）、
     「🚫 忽略」；确认后显示生成的台账记录与凭证号；
  6. 指标卡：待匹配发票、待匹配流水、建议数、已入账金额（`pool_summary`/`match_summary`）；
  7. 免责声明 `config.DISCLAIMER`；模块缺失/异常用 `runtime.load` 降级 + `ui.failure`。
* 金额格式 `F.fmt_money`；空态 `ui.empty`；不允许整页崩溃。
* 测试：`tests/test_ui_smoke.py` 更新 9 页；新增用例：新页面渲染无异常、点击「加载示例」后无异常
  （测试环境用临时数据目录，示例文件来自 `examples/`，路径用 `config.BASE_DIR`）。

---

## 7. 测试要求（后端 `tests/test_ingest.py`）

至少覆盖：

1. 发票：示例 XML（销项/进项方向正确）、中文标签 + 命名空间 XML、OFD（zip 内 XML）、
   PDF（pypdf 缺失或纯图片时降级不崩溃）、未知扩展名 → ok=False。
2. 去重：同一发票保存两次只入库一次。
3. 流水：收入/支出分列 CSV、单金额+方向 CSV、正负金额 CSV、GBK 编码 CSV、xlsx、表头前有说明行；
   无流水号时按内容哈希去重。
4. 匹配：金额+日期+名称 → 高分；方向不符不匹配；超日期窗口不匹配；金额差 5% 以内可匹配但分数下降；
   贪心一对一（两张发票不匹配同一笔流水）。
5. 入账：`confirm_match` 生成 income/expense 记录且字段正确、journal 借贷平衡、发票/流水状态变
   「已入账」、重复确认抛 ValueError、`ignore_match` 不改变台账。
6. 汇总：`pool_summary()`/`match_summary()` 数字正确；`reset_pool()` 清空。
7. 演示数据：`seed_demo_pool()` 能导入示例文件并产生建议。

测试只写临时目录（全局 autouse fixture 已隔离），不访问网络；使用 `tests/fixtures/ingest/` 下文件。

---

## 8. 完成定义（DoD）

1. `pytest tests/test_ingest.py -q` 全绿；`pytest tests/test_ui_smoke.py -q` 全绿（9 页）。
2. 公开接口与本文档一致；不修改 `ledger/models.py`、`engine/**`、`config.py`、`tests/conftest.py`。
3. 中文 docstring、无 TODO 占位；错误信息可操作。
4. 完成后 `send_message` 给 Lead：文件清单、接口、测试结果、遗留问题。
