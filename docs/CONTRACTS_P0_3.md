# P0-3 契约：月结/季结状态机 + 申报包归档 + 备份恢复（CONTRACTS_P0_3）

> 配套 `docs/CONTRACTS.md`。目标：把「能记账、能报税」升级为「**可审计地关账、可自证地归档、可恢复地备份**」。
>
> * 期间结账后锁定写入，保证已出报表/申报表/归档包与账面一致；
> * 申报包归档产出 Excel + PDF + SHA-256 清单（manifest），可随时校验未被篡改；
> * 备份/恢复覆盖全部台账 CSV（**绝不含 API Key**），恢复前自动安全备份，可演练。
>
> Lead 已完成基础层：`ledger/models.py` 新增 `periods` / `archives` / `backups` 三张表；
> `ledger/locks.py` 期间锁模块；`requirements.txt` 增加 `reportlab`（中文 PDF 用 STSong-Light CID 字体，已实测）。

---

## 0. 命令

```bash
cd /Users/shanengzhi/www/2026/prj/tax
.venv/bin/python -m pytest tests/test_closing.py -q
.venv/bin/python -m pytest tests/test_archiving.py -q
.venv/bin/python -m pytest tests/test_ui_smoke.py -q
```

---

## 1. 基础层（Lead 已完成，只读）

### 1.1 数据表

| 表 | 字段 |
|---|---|
| `periods` | id, period(2026-01 / 2026Q1), period_type(月/季), status(未结账/已结账/已申报/已归档), closed_at, closed_by, reopened_at, snapshot_hash, snapshot_json, note, created_at, updated_at |
| `archives` | id, period, kind(申报包/结账包/其他), path, file_hash, size_bytes, manifest_json, note, created_at |
| `backups` | id, path, file_hash, size_bytes, manifest_json, note, created_at |

常量：`PERIOD_TYPES`、`PERIOD_STATUSES`、`ARCHIVE_KINDS`（`ledger.models`）。

### 1.2 `ledger/locks.py`

```python
class PeriodLockedError(ValueError)          # period/action/status 属性
month_key(value) -> "2026-01"
quarter_key(value) -> "2026Q1"
period_key(value) -> str                     # 季度输入→季度键；日期/月份→月份键
status_map() -> dict[str,str]
latest_closed_month() -> str | None
is_locked(period_or_date) -> bool            # 月份/季度已结账，或存在更晚已结账月份
status_of(period_or_date) -> str
ensure_open(period_or_date, action="记账") -> None   # 已锁定抛 PeriodLockedError
locked_periods() -> list[dict]
lock_summary() -> dict                       # latest_closed_month/current_locked/locked_periods/disclaimer
```

**锁规则**：目标月份自身或所属季度状态≠「未结账」→ 锁定；存在晚于目标月份的已结账期间 → 锁定。
`periods` 表无记录视为未锁定（兼容未启用结账的场景）。

---

## 2. `closing/` 包（ledger-filing-dev）

> 职责：期间状态机的唯一写入口；结账生成损益结转凭证；反结账撤销；快照与校验。
> 只写 `closing/**`，并**允许**在下列文件内加锁校验（`from ledger.locks import ensure_open`）：
> `ledger/income_ledger.py`、`ledger/expense_ledger.py`、`ledger/journal.py`、
> `ledger/rd_auxiliary.py`、`ingest/matcher.py`。其余文件不可改。

### 2.1 `closing/periods.py`

```python
def period_type_of(period) -> "月" | "季"
def current_period(today=None) -> str          # 当前月份键 2026-11
def list_periods(kind: str | None = None, limit: int | None = None) -> list[dict]
# 每条 = periods 行 + 派生数据：
# {"period","period_label","period_type","status","closed_at","closed_by","reopened_at",
#  "locked":bool,"income_inclusive","income_exclusive","vat","expense","profit",
#  "counts":{"income","expense","journal_entries","invoices","transactions"},"snapshot_hash"}
def get_period(period) -> dict | None
def ensure_period(period) -> dict             # 不存在则创建（status=未结账，period_type 自动判断）
def period_overview(period) -> dict           # 与 list_periods 单条同结构 + checklist 摘要
```

### 2.2 `closing/checks.py`

```python
def pre_close_checks(period) -> dict
# {"period","period_label","ok":bool,"level":"PASS|WARNING|ERROR",
#  "checks":[{"key","name","status":"PASS|WARNING|ERROR","message","fix_hint"}],
#  "blocking":[check...],"warnings":[check...],"generated_at","disclaimer"}
```

必须实现的检查（key 固定，便于 UI/测试）：

| key | 检查 | 不通过时 |
|---|---|---|
| `period_in_future` | 不允许结未来期间 | ERROR |
| `status_not_closed` | 目标期间未结账 | ERROR（已结账重复结） |
| `previous_period_closed` | 上一个月已结账（首月/首期允许） | WARNING |
| `journal_balanced` | 期间内凭证借贷合计平衡（|差|≤0.01） | ERROR |
| `has_activity` | 期间有收入/费用/凭证（否则零申报） | WARNING |
| `no_zero_vouchers` | 无 0 元凭证 | WARNING |
| `no_unmatched_ingest` | 期间内待匹配发票/流水/待确认建议 | WARNING |
| `invoice_duplicates` | 同期发票号码无重复 | ERROR |
| `policy_period` | 期间在已验证政策区间（2023-01-01~2027-12-31） | WARNING |

### 2.3 `closing/close.py`

```python
def is_closed(period) -> bool
def closing_entries(period) -> list[dict]      # 预览结转凭证（不写库）
def close_period(period, *, operator="system", force=False, note="") -> dict
def reopen_period(period, *, operator="system", reason="") -> dict
def close_quarter(period, *, operator="system", force=False, note="") -> dict
def reopen_quarter(period, *, operator="system", reason="") -> dict
```

规则：

1. `close_period`：
   - `pre_close_checks`；存在 **ERROR** → 抛 `ValueError`（`force=True` 也不放行，可附前缀「无法结账：」）；
   - 生成 **损益结转凭证**（一张多借多贷凭证，`source_ref=f"CLOSE-{period}"`，`voucher_type="结转"`）：
     * 收入类科目（`主营业务收入`、`其他业务收入`、`其他收益`）按期间内 `journal_lines` 净额
       （贷-借）→ 借 各收入科目 / 贷 `本年利润`；
     * 费用类科目（`主营业务成本`、`税金及附加`、`销售费用`、`财务费用`、
       `管理费用*`、`研发支出*`）按净额（借-贷）→ 借 `本年利润` / 贷 各费用科目；
     * 金额为 0 的科目不生成；若收入与费用均为 0，仍写期间状态但不生成凭证；
   - 生成 `closing.snapshot.period_snapshot(period)`，把 `hash` 写入 `periods.snapshot_hash`、
     完整快照写入 `snapshot_json`；
   - `periods.status="已结账"`、`closed_at`、`closed_by`；
   - 写 `ledger.audit.log_user_action("期间结账", detail)`；
   - 返回 `{"ok":True,"period","period_type","status","entries":[{entry_no,lines,total}...],"snapshot_hash","checks":{...}}`。
2. `reopen_period`：仅**最近一个已结账期间**可反结；删除 `journal_entries` 中
   `source_ref=f"CLOSE-{period}"` 的凭证头及对应 `journal_lines`；`status="未结账"`、`reopened_at`；
   写审计；返回 `{"ok":True,"period","status":"未结账","removed_entries":[entry_no...],"snapshot_hash":"","warnings":[]}`。
3. `close_quarter`：要求**季度内 3 个月均已结账**（否则 ERROR 拒绝，`force` 不放行）；生成季度快照
   （`period_snapshot` 支持季度）、`periods` 行 `period_type="季"`、status=已结账；不重复生成月度结转凭证。
4. `reopen_quarter`：仅最近已结账季度可反结；季度状态回未结账（月度状态不变）。
5. 幂等：重复结账/反结未结账期间 → `ValueError`（中文可操作提示）。

### 2.4 `closing/snapshot.py`

```python
def period_snapshot(period) -> dict
# {"period","period_type","generated_at","start","end",
#  "totals":{"income_inclusive","income_exclusive","vat","expense","profit","rd_cost","rd_deduction","surtax"},
#  "counts":{"income","expense","rd","contracts","journal_entries","invoices","transactions"},
#  "balances":{科目:净额...},
#  "hash":"sha256(canonical_json(去掉 hash/generated_at 的部分))",
#  "disclaimer":...}
def verify_snapshot(period) -> dict
# {"ok":bool,"period","expected_hash","current_hash","changes":[中文差异描述...],"disclaimer"}
```

* 数据取数使用现有 `ledger.income_ledger.period_totals`、`ledger.expense_ledger.period_totals`、
  `ledger.rd_auxiliary.summarize_rd`、`engine.calc_vat`（可选）与 `journal` 期间明细；
* 哈希需**可复现**：按 id 排序、金额统一 2 位、JSON `sort_keys=True, ensure_ascii=False`；
* `verify_snapshot` 在结账后检测台账被改动（供归档校验与 UI 展示）。

### 2.5 锁校验接入（写路径）

在以下函数写入前调用 `locks.ensure_open(<记录日期/期间>, action="...")`，并让异常自然上抛：

| 文件 | 函数 | 期间来源 |
|---|---|---|
| `ledger/income_ledger.py` | add_income / update_income / delete_income / import_income_csv | 记录 date |
| `ledger/expense_ledger.py` | add_expense / update_expense / delete_expense / import_expense_csv | 记录 date |
| `ledger/journal.py` | post_entry / post_entries | entry date |
| `ledger/rd_auxiliary.py` | add_rd_record / update_rd_record / delete_rd_record | rd 记录 period |
| `ingest/matcher.py` | confirm_match | 交易日期（或发票日期） |

注意：结账模块自身生成/删除结转凭证时**不得**被锁拦截 → 直接使用 `store.table` 写库，
或内部调用 journal 时绕过 ensure_open（例如 `journal.post_entry(entry, check_lock=False)`，
需给 `post_entry` 增加关键字参数 `check_lock: bool = True`，默认保持锁定校验）。

### 2.6 测试 `tests/test_closing.py`（≥25 用例）

覆盖：状态机（未结账→已结账→反结账→未结账）、未来期间拒绝、重复结账报错、
检查项（不平衡/重复发票/零活动/待匹配/上期未结）、结转凭证借贷平衡且科目正确、
按期间过滤（不含其他期间）、快照可复现与篡改检测、反结账删除结转凭证且不影响正常凭证、
季度结账要求 3 个月已结账、锁生效（income/expense/journal/rd/ingest.confirm_match 均抛
`PeriodLockedError`）、锁不影响未结账期间、`list_periods` 派生数据正确。

---

## 3. `archiving/` 包（archiving-dev）

> 职责：申报包归档（Excel + PDF + SHA-256 manifest）、备份/恢复、校验。
> 只写 `archiving/**` 与 `tests/test_archiving.py`。

### 3.1 `archiving/hash.py`

```python
def sha256_bytes(data: bytes) -> str
def sha256_file(path) -> str
def canonical_json(obj) -> str      # sort_keys=True, ensure_ascii=False, separators=(",",":")
def hash_json(obj) -> str
```

### 3.2 `archiving/filing_archive.py`

```python
def archive_dir(period) -> Path                       # config.DATA_DIR / "archive" / period
def archive_filing_package(period, *, include_pdf=True, note="", operator="system") -> dict
# 流程：filing.registry.build_all(period) → 逐表 filing.validator.validate_bundle
#      → filing.export.export_package_excel(...) 生成 xlsx
#      → include_pdf 时 archiving.pdf.build_filing_pdf(...) 生成 pdf
#      → 复制到 data/archive/{period}/，逐文件 sha256，写 manifest.json（UTF-8, indent=2）
#      → 写 archives 表（kind="申报包"）、audit log
# 返回 {"ok","period","dir","files":[{"name","path","sha256","size"}],
#       "manifest_path","manifest_hash","pdf":bool,"forms":[form_id...],
#       "checks_summary":{"PASS":n,"WARNING":n,"ERROR":n},"disclaimer"}
def list_archives(period=None) -> list[dict]          # 含文件存在/哈希校验概要
def verify_archive(archive_id_or_path) -> dict
# {"ok","period","manifest_path","files":[{"name","expected","actual","ok"}],
#  "tampered":[文件名...],"missing":[...],"checked_at","disclaimer"}
```
* 归档目录 `data/archive/{period}`；文件命名 `申报包_{period}.xlsx`、`申报表_{period}.pdf`、`manifest.json`；
* manifest 字段：`format_version`、`kind`、`period`、`generated_at`、`app_version`、
  `policy_version`/`policy_basis_text`、`forms`、`checks`、`files`、`disclaimer`；
* 重复归档同期间：允许覆盖同名文件（保留旧 manifest 备份为 `manifest_YYYYmmdd_HHMMSS.json`），
  在 `archives` 表新增一条记录（不删除历史）；
* 归档是**只读快照**：不改变期间状态；若期间未结账，在返回中给 WARNING 提示「建议先结账再归档」。

### 3.3 `archiving/pdf.py`

```python
REPORTLAB_AVAILABLE: bool
class PdfUnavailableError(RuntimeError)
def build_filing_pdf(period, bundles, checks=None, path=None) -> Path
```
* 用 `reportlab` + `UnicodeCIDFont("STSong-Light")`（已安装/已实测中文）；
* 内容：封面（企业名称/纳税人识别号/期间/生成时间/应用版本/政策依据/免责声明）→ 每张申报表
  （表名 + 栏次/名称/金额/来源表格）→ 汇总校验结果（PASS/WARNING/ERROR 条数 + 明细摘要）；
* 分页（`PageBreak`）、页脚页码；金额千分位 2 位；不得出现乱码；
* reportlab 不可用时抛 `PdfUnavailableError`，调用方降级为「仅 Excel + manifest」并在返回 `warnings` 说明。

### 3.4 `archiving/backup.py`

```python
def backup_dir() -> Path                              # config.DATA_DIR / "backups"
def create_backup(tag="", *, include_logs=True, note="", operator="system") -> dict
# zip 到 data/backups/backup_YYYYmmdd_HHMMSS[_tag].zip
# 内容：data/*.csv（全部台账，按文件名排序）+ manifest.json + README.txt（+ logs/*.csv 可选）
# **绝不包含**：data/user_settings.json（API Key）、data/uploads/、data/backups/、data/archive/
# manifest：{format_version, created_at, app_version, files:[{name,sha256,size}], tables:{表:条数}}
# 写 backups 表 + audit log("数据备份")
def list_backups() -> list[dict]                       # 文件存在、大小、创建时间、manifest 概要
def verify_backup(backup_id_or_path) -> dict           # {"ok","path","files":[expected/actual/ok],"missing","tampered"}
def delete_backup(backup_id_or_path) -> bool           # 删除文件 + backups 表记录 + 审计
```

### 3.5 `archiving/restore.py`

```python
def inspect_backup(backup_id_or_path) -> dict
# {"ok","path","created_at","app_version","files":[{name,size,sha256}],
#  "tables":{表:条数},"verify":{...},"warnings":[],"disclaimer"}
def restore_backup(backup_id_or_path, *, dry_run=True, safety_backup=True, operator="system") -> dict
# dry_run=True 只校验与预览，不写盘；
# 实际恢复：① verify_backup 不通过 → ValueError；② safety_backup=True 先 create_backup(tag="restore_safety")；
#          ③ 仅允许恢复 manifest 中白名单文件（data/ 下 *.csv 的 basename），逐文件校验哈希后原子替换；
#          ④ ledger.store.reset_cache() + init_storage()；⑤ 写 audit log("数据恢复")；
# 返回 {"ok","dry_run","restored":[{"table","rows","path"}],"safety_backup":str,
#       "warnings":[],"message"}
```
安全要求：**拒绝 zip-slip**（条目含 `..`/绝对路径/非白名单文件即整体拒绝并抛 `ValueError`）、
拒绝哈希不匹配、拒绝 manifest 缺失、恢复前必须安全备份（除非显式 `safety_backup=False` 且调用方为测试）。

### 3.6 测试 `tests/test_archiving.py`（≥18 用例）

覆盖：hash/canonical_json 稳定性；manifest 校验；`archive_filing_package` 产物存在、manifest 哈希正确、
PDF 文本含「申报表」「免责声明」且中文不乱码（用 pypdf 提取）；篡改文件后 `verify_archive` 报 tampered；
重复归档保留历史记录；`create_backup` 不含 user_settings.json、manifest 校验通过；
`list_backups/verify_backup/delete_backup`；`inspect_backup`；`restore_backup(dry_run)` 不写盘；
实际恢复后台账数据与备份一致（先改数据再恢复）、自动安全备份存在；损坏 zip/zip-slip 被拒绝。

---

## 4. UI 增量（ui-dev）

只写：`ui/views/settings.py`、`ui/views/filing.py`、`ui/views/dashboard.py`、`ui/components/archive_api.py`
（可新增）、`ui/components/cache.py`（如需）、`tests/test_ui_smoke.py`。不改其他页面/后端/config。

1. **⚙️ 设置 → 数据管理**（`settings.py` 现有 tab 内扩展）：
   - 备份：输入备注/tag →「创建备份」→ 显示路径、大小、SHA-256；备份列表（时间/大小/文件数/校验状态）+
     「校验」「删除」；恢复：上传备份 zip → `inspect_backup` 预览（表/条数/校验结果）→
     勾选「我已阅读并确认将覆盖当前台账」→「恢复」（内部自动安全备份）→ 结果展示；
   - 归档：归档列表（期间/类型/时间/哈希摘要）+「校验」；展示归档目录路径。
2. **📋 申报填报**（`filing.py`）：
   - 页头显示当期「结账状态」badge（未结账/已结账）与「本期是否已归档」；
   - 「📦 归档本期申报包」按钮 → `archive_filing_package(period)` → 展示 xlsx/pdf 路径、manifest 哈希、
     校验结论；「🔍 校验归档」；已归档时显示历史归档列表；
   - 归档失败（如未结账 WARNING 不影响成功）不白屏。
3. **🏠 首页驾驶舱**（`dashboard.py`）：
   - 新增「期间结账」卡片：当前月份/上一月份状态、结账截止提醒、`lock_summary` 概要；
   - 「立即结账」按钮 → 弹出 `st.dialog` 展示 `pre_close_checks` 检查清单（PASS/WARNING/ERROR 着色）→
     有 ERROR 时禁用确认并提示；确认后 `close_period`；已结账显示「反结账」按钮（二次确认，调用 `reopen_period`）；
   - 结账/反结账后 `st.rerun()` 并清理缓存。
4. 所有模块用 `ui/components/runtime.py` 的 `runtime.load` 容错；缺模块时按钮禁用 + 中文提示；
   写操作命中 `PeriodLockedError` 时用 `st.error(str(exc))` 友好展示。
5. `tests/test_ui_smoke.py` 保持 9 页并新增：设置页数据管理渲染、首页结账卡片渲染（空数据）、
   申报填报归档按钮存在且点击不异常（可用 mock 数据或临时目录）；测试环境无 API Key、数据为空/示例态。

---

## 5. CLI（Lead）

`main.py` 新增：
```bash
main.py close --period 2026-01 [--force]        # 结账（先打印 pre_close_checks）
main.py reopen --period 2026-01 --reason "..."  # 反结账
main.py periods [--limit 12]                    # 期间状态与金额概览
main.py archive --period 2026Q1 [--no-pdf]      # 归档申报包
main.py backups                                 # 备份列表
main.py backup [--tag ...]                      # 创建备份
main.py restore --file <zip> [--yes]            # 恢复（默认 dry-run 预览）
```

## 6. 完成定义（DoD）

1. `pytest tests/test_closing.py tests/test_archiving.py tests/test_ui_smoke.py -q` 全绿；
2. 公开接口与本文档一致；不修改只读文件；
3. 中文 docstring、无 TODO 占位、错误信息可操作；
4. 完成后 `send_message` 给 Lead：文件清单、接口、测试结果、遗留问题。
