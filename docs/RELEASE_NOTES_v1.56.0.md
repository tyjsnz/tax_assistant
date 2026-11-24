# v1.56.0 发布说明（Release Notes）

发布日期：2026-11-25 · 标签：`v1.56.0`

## 新增：MCP Server（只读）

目录：`mcp_server/`

支持：

- stdio（推荐本地 MCP 客户端）；
- 本机 Streamable HTTP：`--http`，默认 `127.0.0.1:8766/mcp`。

### 只读工具（11 个）

- `get_company_profile`
- `get_period_summary`
- `get_calendar_deadlines`
- `get_filing_progress`
- `estimate_vat`
- `estimate_cit`
- `calculate_iit`
- `get_employee_summary`（脱敏）
- `search_policy`
- `get_draft_center`
- `list_invoices`

### DeepSeek Harness 配置

见 `mcp_server/README.md` 和 `mcp_server/dsh-bundle/`：

- `cordis.patch.yml`：stdio 示例；
- `cordis.http.patch.yml`：HTTP 示例；
- 需要把 `/ABSOLUTE/PATH/TO/tax` 替换成实际路径；
- 连接后工具名形如 `mcp__zhixiaoshui__get_company_profile`。

### 安全

- 默认只读，不修改账套、不申报、不删除；
- 不返回完整身份证、API Key、密码；
- 默认只监听 127.0.0.1；
- MCP 客户端可读取本地数据，请只在可信环境启用。

### 安装

```bash
.venv/bin/pip install -r requirements-mcp.txt
.venv/bin/python mcp_server/server.py
```

## 验证

- stdio MCP 客户端实测初始化、列工具、调用公司信息/草稿中心成功；
- 全量测试 893 项通过。
