# 报税 Agent 开发规范文档

## 📌 项目背景

**企业**：禄劝云睿网络科技开发有限公司（网络科技类）
**纳税人类型**：小规模纳税人
**经营规模**：季度含税流水 ＜ 30 万
**核心诉求**：搭建一个 AI Agent，辅助完成日常记账、季度算税、申报核对、研发归集、政策更新等工作

---

## 🎯 Agent 定位

| 维度 | 说明 |
|---|---|
| 角色 | 企业税务助手 + 记账助理 + 政策解读员 |
| 能力边界 | 算税、解释、提醒、生成分录、核对申报表 |
| 不可越界 | 不替代登录电子税务局提交申报、不做税务鉴证 |
| 输出原则 | 所有算税结果标注"仅供参考，以主管税务机关核定为准" |

---

## 🏗️ 系统架构

```
┌─────────────────────────────────────────────────────┐
│                  用户交互层                           │
│         （对话输入 / 文件上传 / 截图OCR）              │
└───────────────────┬─────────────────────────────────┘
                    │
┌───────────────────▼─────────────────────────────────┐
│                  Agent 编排层                         │
│         （意图识别 → 路由 → 工具调用 → 回复）         │
└──────┬──────────┬──────────┬───────────┬────────────┘
       │          │          │           │
┌──────▼──┐  ┌────▼───┐  ┌──▼────┐  ┌───▼────────┐
│ 算税引擎 │  │ 台账   │  │ 政策  │  │ 申报表     │
│ (确定性) │  │ 管理   │  │ 知识库 │  │ 核对(OCR)  │
└─────────┘  └────────┘  └───────┘  └─────────────┘
       │          │          │           │
┌──────▼──────────▼──────────▼───────────▼────────────┐
│                  本地数据存储层                        │
│         （Excel/SQLite + 政策PDF + 申报表截图）        │
└─────────────────────────────────────────────────────┘
```

---

## 🧩 功能模块清单

### Module 1：智能算税引擎（确定性代码）

> **核心原则**：算税逻辑必须硬编码，不允许 LLM 自由发挥计算。

#### 1.1 增值税计算

**输入参数**：
- `quarterly_total_inclusive`：季度含税总销售额（元）
- `special_invoice_inclusive`：季度专票含税金额（元），默认为 0
- `period`：所属季度（如 2026Q1），用于政策版本匹配

**计算逻辑**：

```
1. 含税转不含税（征收率 1%）：
   total_excl = quarterly_total_inclusive / 1.01
   special_excl = special_invoice_inclusive / 1.01
   ordinary_excl = total_excl - special_excl

2. 判断免税额度（季度不含税 ≤ 300,000）：
   IF ordinary_excl ≤ 300,000:
       vat_payable = special_excl × 0.01    （专票部分不免）
       vat_exempt = ordinary_excl × 0.01    （普票+未开票免征）
       surtax = 0                            （附加税免征）
   ELSE:
       vat_payable = total_excl × 0.01      （全额按 1%）
       surtax = vat_payable × 0.06          （附加税：城建+教育近似）

3. 六税两费（小规模减半）：
   IF 小规模纳税人和小型微利企业:
       surtax_after_discount = surtax × 0.5  （若需缴纳）
```

**输出格式**：

```json
{
  "quarter": "2026Q1",
  "total_inclusive": 103000,
  "total_exclusive": 101980.20,
  "ordinary_exclusive": 101980.20,
  "special_exclusive": 0,
  "vat_payable": 0,
  "vat_exempt": 1019.80,
  "surtax": 0,
  "within_exemption_limit": true,
  "note": "普票+未开票不含税 101,980.20 ≤ 300,000，免征增值税 1,019.80 元",
  "disclaimer": "仅供参考，以主管税务机关核定为准"
}
```

#### 1.2 企业所得税计算（季度预缴 + 年度汇算）

**输入参数**：
- `annual_revenue_exclusive`：年度不含税收入
- `annual_cost`：年度成本费用
- `annual_rd_cost`：年度研发费用（用于加计扣除）
- `is_small_low_profit`：是否符合小型微利企业条件

**小型微利企业判定标准**：
- 从事国家非限制和禁止行业
- 年度应纳税所得额 ≤ 300 万元
- 从业人数 ≤ 300 人
- 资产总额 ≤ 5,000 万元

**计算逻辑**：

```
1. 会计利润 = 收入 - 成本费用
2. 纳税调整：
   加计扣除前利润 = 会计利润
   加计扣除额 = rd_cost × 100%    （未形成无形资产）
   调整后利润 = 会计利润 - 加计扣除额

3. 小微优惠（2027年底前）：
   实际税率 = 5%（减按 25% 计入，按 20% 税率）
   IF 调整后利润 ≤ 3,000,000:
       corp_tax = 调整后利润 × 0.05
   ELSE:
       corp_tax = 调整后利润 × 0.25   （全额不享受优惠）

4. 季度预缴：按季度利润 × 5%（暂估）
```

#### 1.3 研发费用加计扣除

**归集范围**（加计扣除100%）：
| 费用类别 | 说明 |
|---|---|
| 人员人工 | 研发人员工资、五险一金 |
| 直接投入 | 材料、燃料、动力费用 |
| 折旧费用 | 研发设备折旧 |
| 无形资产摊销 | 软件、专利摊销 |
| 新产品设计费 | 新产品/新工艺设计费 |
| 其他相关费用 | 差旅、会议等（不超过总额的10%） |

**计算公式**：

```
可加计扣除总额 = 上述各项费用之和（其他相关费用按限额归集）
2023年起：未形成无形资产 → 100%加计扣除
         形成无形资产 → 按200%税前摊销
```

---

### Module 2：自然语言交互层

#### 2.1 意图识别

Agent 需识别以下意图并路由到对应模块：

| 意图 | 示例用户问句 | 路由目标 |
|---|---|---|
| 算税 | "我这个季度开了8万普票，增值税怎么算？" | 算税引擎 |
| 记账 | "今天收到XX公司5000块" | 台账管理 |
| 分录生成 | "帮我把这笔做成分录" | 分录模板 |
| 政策咨询 | "小规模现在增值税税率多少？" | 政策知识库 |
| 申报核对 | "帮我看看这个申报表填对了没"（附截图） | 申报表OCR核对 |
| 研发归集 | "这个月研发人员工资总共2万" | 研发辅助账 |
| 日历提醒 | "下个月有什么要报的？" | 申报日历 |
| 红利账单解读 | "我的红利账单9942元是怎么来的？" | 减免明细拆解 |

#### 2.2 Prompt 模板

**系统 Prompt（System Prompt）**：

```
你是"税小智"，一个专业的中国税务助手，服务于禄劝云睿网络科技开发有限公司。

【企业画像】
- 纳税人类型：小规模纳税人
- 行业：网络科技（技术服务、软件开发）
- 季度含税流水 ＜ 30 万
- 适用优惠：小微企业普惠、六税两费减半、研发加计扣除100%

【行为准则】
1. 算税结果必须基于确定性代码计算，不可凭感觉估算
2. 涉及政策内容时，必须标注政策生效日期和来源
3. 不确定时明确说"建议咨询主管税务机关"，不可编造
4. 所有算税输出末尾附加："⚠️ 仅供参考，以主管税务机关核定为准"
5. 涉及企业敏感信息（金额、客户名）时提醒用户注意保密

【回复风格】
- 中文回复，简洁专业
- 数字精确到位（保留2位小数）
- 必要时用表格或分点列出
- 重要提醒用 ⚠️ 标注
```

**算税 Prompt 模板**：

```
用户提供了以下数据：
- 季度含税总收入：{total}
- 其中专票金额：{special}
- 季度：{period}

请调用 calc_vat 工具计算增值税及附加税，并以自然语言解释结果。
```

---

### Module 3：台账管理

#### 3.1 数据结构

**收入台账（income_ledger.csv）**：

| 字段 | 类型 | 说明 |
|---|---|---|
| date | string | 收款日期 YYYY-MM-DD |
| client | string | 客户名称（可脱敏） |
| description | string | 业务描述 |
| inclusive_amount | float | 含税金额 |
| exclusive_amount | float | 不含税金额 |
| invoice_type | enum | 普票/专票/未开票 |
| invoice_no | string | 发票号码 |
| category | enum | 技术服务/软件开发/运维/其他 |
| is_rd_related | bool | 是否与研发相关 |

**费用台账（expense_ledger.csv）**：

| 字段 | 类型 | 说明 |
|---|---|---|
| date | string | 发生日期 |
| description | string | 费用描述 |
| amount | float | 金额（含税，小规模不可抵扣） |
| category | enum | 工资/社保/房租/办公/差旅/软件订阅/其他 |
| is_rd_related | bool | 是否可归入研发费用 |
| vendor | string | 供应商 |
| receipt_no | string | 凭证号 |

**研发辅助账（rd_auxiliary.csv）**：

| 字段 | 类型 | 说明 |
|---|---|---|
| project_name | string | 研发项目名称 |
| period | string | 所属期间 |
| personnel_cost | float | 人员人工费用 |
| material_cost | float | 直接投入费用 |
| depreciation | float | 折旧费用 |
| other_cost | float | 其他相关费用 |
| total | float | 合计 |
| deduction_amount | float | 加计扣除金额（= total × 100%） |

**分录日志（journal_entries.csv）**：

| 字段 | 类型 | 说明 |
|---|---|---|
| date | string | 凭证日期 |
| entry_no | string | 凭证号 |
| description | string | 摘要 |
| account_debit | string | 借方科目 |
| account_credit | string | 贷方科目 |
| amount | float | 金额 |

#### 3.2 自然语言录入

用户说："今天收到云图科技 20,600 元技术服务费，开了普票"

Agent 应自动：
1. 提取：date=today, client=云图科技, amount=20600, invoice_type=普票, category=技术服务
2. 计算：不含税 = 20600/1.01 = 20396.04
3. 生成分录：
   ```
   借：银行存款 20,600.00
   贷：主营业务收入 20,396.04
   贷：应交税费—应交增值税 203.96
   ```
4. 追加到收入台账和分录日志

---

### Module 4：申报表核对（OCR + 规则比对）

#### 4.1 流程

```
用户上传申报表截图
       ↓
OCR 提取关键字段
       ↓
字段映射（截图文字 → 标准字段名）
       ↓
从台账读取本期数据
       ↓
规则引擎逐栏比对
       ↓
输出差异报告
```

#### 4.2 增值税申报表核对规则

| 申报表栏次 | 对应数据来源 | 校验规则 |
|---|---|---|
| 第1栏"应征增值税不含税销售额" | 台账：专票不含税 | 应 = 台账专票不含税 |
| 第10栏"小微企业免税销售额" | 台账：普票+未开票不含税 | 应 ≤ 300,000 |
| 第18栏"应纳税额" | 专票不含税 × 1% | 自动校验 |
| 第22栏"应纳税额减征额" | 免税部分 × 1% | 自动校验 |
| 第24栏"本期应补（退）税额" | = 第18栏 - 第22栏 | 应 = 0（全普票场景） |

#### 4.3 输出差异报告

```json
{
  "status": "PASS / WARNING / ERROR",
  "details": [
    {
      "field": "第10栏 小微企业免税销售额",
      "expected": 101980.20,
      "actual": 101980.20,
      "status": "PASS"
    },
    {
      "field": "第24栏 本期应补（退）税额",
      "expected": 0,
      "actual": 0,
      "status": "PASS"
    }
  ],
  "summary": "申报表各项与台账数据一致，可以放心提交",
  "disclaimer": "仅供参考，以主管税务机关核定为准"
}
```

---

### Module 5：政策知识库

#### 5.1 知识库结构

```
政策知识库/
├── 增值税/
│   ├── 小规模纳税人减免.md
│   ├── 六税两费减半.md
│   └── 技术合同免征增值税.md
├── 企业所得税/
│   ├── 小型微利企业优惠.md
│   └── 研发费用加计扣除.md
├── 其他税费/
│   ├── 印花税.md
│   └── 个人所得税.md
└── 申报流程/
    ├── 云南电子税务局_增值税申报.md
    ├── 自然人扣缴端_个税申报.md
    └── 企业所得税_汇算清缴.md
```

#### 5.2 每条政策的标准格式

```markdown
---
title: {政策名称}
category: {增值税/企业所得税/其他}
effective_date: {YYYY-MM-DD}
expiry_date: {YYYY-MM-DD 或 长期}
source: {国家税务总局公告XX号 / 财税XX号}
last_verified: {YYYY-MM-DD}
---

## 适用条件
{谁可以用}

## 优惠内容
{具体减免多少}

## 办理方式
{自行申报享受 / 需备案 / 需审批}

## 留存资料
{需要留哪些备查资料}

## 注意事项
{容易踩的坑}
```

#### 5.3 政策更新检查机制

- 每月定时触发搜索：最新小规模纳税人增值税政策、最新小微企业所得税优惠
- 对比知识库中的 `last_verified` 日期，如有新政策则更新并推送提醒
- 更新时记录变更日志

---

### Module 6：申报日历与提醒

#### 6.1 关键日期

| 事项 | 频率 | 截止日 | 提醒日 |
|---|---|---|---|
| 增值税及附加申报 | 季度 | 1/4/7/10月15日 | 截止前3天 |
| 企业所得税预缴 | 季度 | 同上 | 截止前3天 |
| 个人所得税申报 | 月度 | 次月15日 | 截止前2天 |
| 印花税申报 | 按季/按次 | 季度申报随增值税 | 截止前3天 |
| 企业所得税汇算清缴 | 年度 | 次年5月31日 | 提前1个月、2周、3天 |
| 工商年报 | 年度 | 次年6月30日 | 提前1个月 |

#### 6.2 提醒输出

```
📅 申报提醒

距离【2026年Q1增值税及附加申报】截止还有 3 天
截止日期：2026年4月15日

本季度数据概览：
- 不含税收入：101,980.20 元
- 增值税：免征 1,019.80 元
- 附加税：免征
- 状态：可申报

⚠️ 请登录云南电子税务局完成申报，零申报也需提交。
```

---

## 🛠️ 技术选型建议

| 层级 | 推荐方案 | 说明 |
|---|---|---|
| Agent 框架 | **LangChain** / **扣子(Coze)** | 代码级用 LangChain；零代码用扣子 |
| LLM API | 腾讯混元 / 元宝 API | 中文财税场景表现好 |
| 算税引擎 | Python（硬编码） | 确定性计算，不依赖 LLM |
| OCR | PaddleOCR / Tesseract | 申报表截图文字提取 |
| 数据存储 | SQLite + CSV | 本地轻量，无需数据库服务 |
| 前端界面 | Streamlit / Gradio | Python 原生，快速出 UI |
| 部署方式 | 本地运行 / 私有服务器 | 财务数据不出内网 |

---

## 📋 开发任务拆解（MVP → 完整版）

### 🟢 Phase 1：MVP（1-2周）

- [ ] **T1-01** 搭建 Python 项目结构（算税引擎 + 台账 + Agent 入口）
- [ ] **T1-02** 实现增值税算税函数 `calc_vat()`
- [ ] **T1-03** 实现企业所得税算税函数 `calc_corp_tax()`
- [ ] **T1-04** 实现研发费用加计扣除 `calc_rd_deduction()`
- [ ] **T1-05** 创建收入台账/费用台账/研发辅助账 CSV 模板
- [ ] **T1-06** 接入 LLM API，实现基础对话（System Prompt 写入）
- [ ] **T1-07** 实现意图识别 → 算税路由
- [ ] **T1-08** 用 Streamlit 出最简界面（输入框 + 结果显示）
- [ ] **T1-09** 编写单元测试（至少覆盖：全普票免税、含专票、超30万三种场景）
- [ ] **T1-10** 输出算税结果 + 分录建议

### 🟡 Phase 2：台账 + 记账（2-3周）

- [ ] **T2-01** 实现自然语言录入（解析用户口述 → 结构化数据）
- [ ] **T2-02** 分录自动生成（收入/费用/工资/研发）
- [ ] **T2-03** 季度末结转逻辑（增值税免征转"其他收益"）
- [ ] **T2-04** 台账按月汇总报表（收入、费用、利润）
- [ ] **T2-05** 研发辅助账自动归集 + 加计扣除计算
- [ ] **T2-06** 数据导入/导出（CSV 备份）

### 🔵 Phase 3：申报核对 + 政策库（3-4周）

- [ ] **T3-01** 集成 OCR（申报表截图 → 文字提取）
- [ ] **T3-02** 申报表栏次映射规则
- [ ] **T3-03** 逐栏比对 + 差异报告输出
- [ ] **T3-04** 搭建政策知识库（RAG 检索）
- [ ] **T3-05** 政策更新自动检查脚本
- [ ] **T3-06** 政策变更推送提醒

### 🔴 Phase 4：自动化 + 完善（4-6周）

- [ ] **T4-01** 申报日历 + 定时提醒
- [ ] **T4-02** 红利账单拆解（减免明细分析）
- [ ] **T4-03** 年度汇算清缴辅助
- [ ] **T4-04** 数据可视化（月度利润趋势、季度税负分析）
- [ ] **T4-05** 多用户/多企业支持（可选）
- [ ] **T4-06** 安全加固（本地数据加密、访问控制）

---

## 🔐 安全与合规要求

### 数据安全
1. 所有企业财务数据 **仅存储于本地**，不上传任何第三方云服务
2. 客户名称在台账中支持 **脱敏存储**（如"客户A"）
3. 文件传输和 API 调用使用 HTTPS
4. 本地数据库/CSV 文件建议加密存储

### Agent 行为约束
1. **算税必须由代码执行**，LLM 只负责解释结果，不允许 LLM 直接计算税额
2. 所有输出必须携带免责声明
3. 涉及政策变更时，必须标注信息来源和生效日期
4. 禁止 Agent 给出"可以逃税/避税"类建议
5. Agent 不得主动建议用户进行任何需要转账的操作

### 审计日志
```
logs/
├── calculation_log.csv    # 每次算税记录（输入、输出、时间）
├── journal_log.csv        # 每笔分录生成记录
├── policy_check_log.csv   # 政策更新检查记录
└── user_action_log.csv    # 用户操作日志
```


## 📂 项目目录结构

```
tax_agent/
├── README.md
├── requirements.txt
├── config.py                    # 全局配置（企业信息、政策参数）
├── main.py                      # Agent 入口
│
├── engine/                      # 算税引擎（确定性代码）
│   ├── __init__.py
│   ├── vat.py                   # 增值税计算
│   ├── corporate_tax.py         # 企业所得税计算
│   ├── rd_deduction.py          # 研发费用加计扣除
│   └── tax_calendar.py          # 申报日历计算
│
├── ledger/                      # 台账管理
│   ├── __init__.py
│   ├── models.py                # 数据模型定义
│   ├── income_ledger.py         # 收入台账 CRUD
│   ├── expense_ledger.py        # 费用台账 CRUD
│   ├── rd_auxiliary.py          # 研发辅助账
│   └── journal.py               # 分录生成
│
├── agent/                       # Agent 编排层
│   ├── __init__.py
│   ├── prompt_templates.py      # 所有 Prompt 模板
│   ├── intent_router.py         # 意图识别 + 路由
│   ├── tools.py                 # 工具定义（供 LLM 调用）
│   └── llm_client.py            # LLM API 封装
│
├── ocr/                         # OCR 申报表核对
│   ├── __init__.py
│   ├── extractor.py             # OCR 文字提取
│   ├── field_mapper.py          # 栏次映射
│   └── validator.py             # 规则比对
│
├── knowledge/                   # 政策知识库
│   ├── vector_store.py          # 向量存储 + 检索
│   ├── policy_files/            # 政策 Markdown 文件
│   └── update_checker.py        # 政策更新检查
│
├── ui/                          # 前端界面
│   ├── streamlit_app.py         # Streamlit 主界面
│   └── components/              # UI 组件
│
├── data/                        # 数据存储
│   ├── income_ledger.csv
│   ├── expense_ledger.csv
│   ├── rd_auxiliary.csv
│   ├── journal_entries.csv
│   └── backups/                 # 定期备份
│
├── logs/                        # 审计日志
│   ├── calculation_log.csv
│   ├── journal_log.csv
│   └── user_action_log.csv
│
└── tests/                       # 单元测试
    ├── test_vat.py
    ├── test_corporate_tax.py
    ├── test_rd_deduction.py
    └── test_intent_router.py
```


## 🧪 测试用例（算税引擎）

### 增值税测试

```python
import pytest
from engine.vat import calc_vat

class TestVAT:
    def test_full_exemption(self):
        """全普票+未开票，不超30万 → 全免"""
        result = calc_vat(quarterly_total_inclusive=101000, special_invoice_inclusive=0)
        assert result["vat_payable"] == 0
        assert result["within_exemption_limit"] == True
        assert result["vat_exempt"] > 0

    def test_with_special_invoice(self):
        """含专票 → 专票部分不免"""
        result = calc_vat(quarterly_total_inclusive=101000, special_invoice_inclusive=10100)
        assert result["vat_payable"] > 0
        assert result["vat_exempt"] > 0

    def test_exceed_limit(self):
        """超过30万 → 全额1%"""
        result = calc_vat(quarterly_total_inclusive=404000, special_invoice_inclusive=0)
        assert result["within_exemption_limit"] == False
        assert result["vat_payable"] > 0

    def test_exact_boundary(self):
        """边界值：恰好30万不含税"""
        result = calc_vat(quarterly_total_inclusive=303000, special_invoice_inclusive=0)
        assert result["within_exemption_limit"] == True
```

### 企业所得税测试

```python
class TestCorpTax:
    def test_small_profit_5pct(self):
        """年利润20万 → 5%"""
        result = calc_corp_tax(annual_profit=200000, rd_cost=50000)
        assert result["effective_rate"] == 0.05

    def test_exceed_3million(self):
        """年利润超300万 → 25%"""
        result = calc_corp_tax(annual_profit=3500000, rd_cost=0)
        assert result["effective_rate"] == 0.25
```

---

## 📝 配置示例（config.py）

```python
# config.py

# ===== 企业信息 =====
COMPANY = {
    "name": "禄劝云睿网络科技开发有限公司",
    "taxpayer_type": "小规模纳税人",
    "industry": "网络科技",
    "unified_social_credit_code": "91530128059467473X",
    "tax_authority": "国家税务总局昆明市税务局",
}

# ===== 政策参数（2026年现行） =====
POLICY = {
    # 增值税
    "vat_rate": 0.01,                    # 小规模征收率
    "vat_exemption_limit_quarter": 300000,  # 季度免税额度（不含税）
    "vat_exemption_limit_month": 100000,    # 月度免税额度（不含税）

    # 附加税
    "surtax_rate": 0.06,                 # 城建税(7%减半≈3.5%) + 教育附加 + 地方教育
    "liutax_liangfei_discount": 0.5,    # 六税两费减半

    # 企业所得税
    "small_low_profit_rate": 0.05,       # 实际税负5%
    "small_low_profit_threshold": 3000000,  # 应纳税所得额上限
    "employee_threshold": 300,           # 从业人数上限
    "asset_threshold": 50000000,         # 资产总额上限

    # 研发费用加计扣除
    "rd_deduction_ratio": 1.0,           # 100%加计扣除
    "rd_amortization_ratio": 2.0,        # 形成无形资产按200%摊销
}

# ===== 申报日历 =====
FILING_CALENDAR = {
    "vat_quarterly": {"months": [1, 4, 7, 10], "day": 15},
    "corp_tax_quarterly": {"months": [1, 4, 7, 10], "day": 15},
    "personal_tax_monthly": {"day": 15},
    "annual_settlement": {"month": 5, "day": 31},
}

# ===== LLM 配置 =====
LLM_CONFIG = {
    "model": "hunyuan-pro",
    "api_key_env": "HUNYUAN_API_KEY",
    "max_tokens": 2000,
    "temperature": 0.3,
}
```


## 🚀 快速启动（开发指引）

### 环境准备

```bash
# 1. 创建虚拟环境
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置 API Key
export HUNYUAN_API_KEY="your_api_key_here"

# 4. 运行测试
pytest tests/ -v

# 5. 启动 UI
streamlit run ui/streamlit_app.py
```

### requirements.txt

```
langchain>=0.1.0
tencentcloud-sdk-python>=3.0.0
pandas>=2.0.0
streamlit>=1.30.0
paddleocr>=2.7.0
python-docx>=0.8.11
openpyxl>=3.1.0
pytest>=8.0.0
python-dotenv>=1.0.0
```


## ⚠️ 重要声明

1. 本 Agent 为辅助工具，所有计算结果仅供参考
2. 正式申报前请以主管税务机关核定为准
3. 涉及重大涉税决策，建议咨询专业财税机构
4. 税务政策具有时效性，请定期更新知识库
5. 企业财务数据属商业机密，请妥善保管

---

*最后更新：2026年11月*
