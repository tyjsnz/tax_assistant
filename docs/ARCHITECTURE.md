# 架构说明（开发者）

## 1. 分层

```text
UI (Streamlit, ui/)
  └── Agent 编排 (agent/) ──工具调用──┐
  └── 归档/备份 (archiving/)          │
  └── 结账 (closing/)                 │
  └── 申报表 (filing/)                │
  └── 导入 (ingest/) ────────────────┤
  └── 台账 (ledger/) ────────────────┤
  └── 知识库 (knowledge/) / OCR (ocr/)┘
                 └── 确定性引擎 (engine/)：算税、政策参数、日历、金额工具
                        └── 配置/工作区 (config.py, workspace.py)
```

* **engine/**：纯函数、无 IO、无 LLM；所有税额计算都在这里（Decimal + ROUND_HALF_UP）；
* **ledger/**：CSV 台账 CRUD、凭证、期间锁、报表；数据表 schema 唯一来源 `ledger/models.py`；
* **filing/**：申报表 bundle 生成/校验/导出/进度；
* **closing/**：月结/季结状态机、结转凭证、快照；
* **archiving/**：申报包归档（Excel/PDF/SHA-256）、数据备份/恢复；
* **ingest/**：数电票 XML/OFD/PDF 与银行流水解析、匹配、入账；
* **agent/**：DeepSeek 客户端、工具、意图路由、编排（无 Key 自动离线）；
* **knowledge/**、**ocr/**：政策检索/时效检查、本地 OCR；
* **ui/**：11 个页面与主题，页面只负责展示与交互。

## 2. 关键设计

### 2.1 确定性算税（LLM 不参与计算）

所有金额由 `engine` 计算并返回结构化结果 + 政策依据；Agent 只允许调用工具获取结果，
Prompt 明确禁止心算。测试覆盖税率跳档、免税边界、四舍五入等。

### 2.2 政策版本化

`engine/policy_params.py` 按「期间 → 版本」维护税率/限额/依据（文号、生效日、原文链接、停用文件），
2023—2025 与 2026—2027 两版；新增政策只追加版本、不改历史。见 `docs/POLICY_MAINTENANCE.md`。

### 2.3 多公司/多账套

`workspace.py` 维护 `data/companies.json` 注册表；`activate(id)` 切换 `config.DATA_DIR` 等路径，
其余模块运行时读取配置，因此无需重启。旧版扁平数据由 `ensure_schema()` 自动迁移（schema v2）。

### 2.4 期间锁与审计

`ledger/locks.py` 在台账/凭证/工资/匹配入账等写入口校验期间状态（已结账禁止写入、禁止倒记）；
`ledger/store.py` 提供原子写入、备份、审计日志（CSV）。

### 2.5 申报表 bundle

所有申报表返回统一结构（`form_id/form_name/period/sections/lines/totals/calc/checks/summary/
guide/data/disclaimer`），便于 UI 渲染、Excel/PDF 导出、Agent 引用与回归测试。

### 2.6 数据与规则分离

表单/政策/地区/银行列别名等尽量以配置/数据结构表达；新增税种或地区时优先扩展注册表，
而非散落硬编码。

## 3. 数据目录

```text
data/
├── companies.json / schema.json / user_settings.json
├── logs/                          # 全局审计日志
└── companies/{id}/                # 每公司独立账套（全部 CSVs + archive/ backups/ uploads/）
```

## 4. 扩展指南

| 想做什么 | 改哪里 |
|---|---|
| 新增申报表 | `filing/` 新模块 + `filing/registry.py` 注册 + 测试；UI 复用 bundle 渲染 |
| 新增税种/调整规则 | `engine/` 新模块（确定性）+ 台账/申报/UI 接入 + `docs/CONTRACTS*.md` |
| 政策变化 | `engine/policy_params.py` 追加版本 + 知识库文档 + 回归测试（见政策维护手册） |
| 新地区税率 | `config.REGION_PRESETS` + 引擎附加税已按地区计算 |
| 新发票/银行格式 | `ingest/invoice_parser.py` / `bank_parser.py`（别名表 + 测试 fixture） |
| 新 AI 工具 | `agent/tools.py` 注册 schema + handler，调用 engine/ledger/filing |
| 新页面 | `ui/views/*.py` + `ui/components/nav.py` + `tests/test_ui_smoke.py` |

## 5. 测试策略

* 离线全量 pytest（`tests/`），按模块分文件；UI 使用 `streamlit.testing.v1.AppTest`；
* 金额/边界/政策期间/多公司隔离/期间锁/归档哈希/恢复演练均有回归；
* CI：GitHub Actions（Python 3.11/3.12）跑全量离线测试。

## 6. 技术栈

Python 3.11+、Streamlit、pandas、openpyxl、reportlab（PDF）、pypdf、requests（DeepSeek API），
数据以 CSV 存储（原子写入 + 审计日志），无外部数据库与云依赖。
