# 税小智 MCP Server（只读）

让支持 MCP 的大模型客户端查询本地账套、申报日历、进度、测算和政策；**不修改数据、不申报**。

## 1. 安装

```bash
.venv/bin/pip install -r requirements-mcp.txt
```

## 2. 本地运行

### stdio（推荐，供桌面 MCP 客户端使用）

```bash
.venv/bin/python mcp_server/server.py
```

### HTTP（本机调试）

```bash
.venv/bin/python mcp_server/server.py --http
# 默认 http://127.0.0.1:8766/mcp
```

> 只监听 127.0.0.1，不要直接暴露到公网。局域网使用需额外加鉴权/HTTPS。

## 3. 工具

- `get_company_profile`
- `get_period_summary(period)`
- `get_calendar_deadlines(year?, lookahead_days?)`
- `get_filing_progress(period)`
- `estimate_vat(period)`
- `estimate_cit(period)`
- `calculate_iit(period)`
- `get_employee_summary()`（脱敏，不返回完整身份证）
- `search_policy(query, top_k?)`
- `get_draft_center(month?, quarter?)`
- `list_invoices(period?, limit?)`

所有金额为本地确定性引擎结果，仅供参考；实际申报以电子税务局为准。

## 4. DeepSeek Harness 配置

### 方式 A：stdio（推荐）

1. 复制 `dsh-bundle/` 到 DSH 可安装的本地 bundle 目录；
2. 把 `dsh-bundle/cordis.patch.yml` 里的 `/ABSOLUTE/PATH/TO/tax` 改成实际路径；
3. 在 DSH 中安装该 bundle（本地目录/插件管理）；
4. 验证新工具 `mcp__zhixiaoshui__get_company_profile`、`mcp__zhixiaoshui__get_draft_center`。

示例配置：

```yaml
- insert:
    - id: zhixiaoshui-mcp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: zhixiaoshui
        transport: stdio
        command: /ABSOLUTE/PATH/TO/tax/.venv/bin/python
        args:
          - /ABSOLUTE/PATH/TO/tax/mcp_server/server.py
        cwd: /ABSOLUTE/PATH/TO/tax
        env:
          TAX_DATA_DIR: /ABSOLUTE/PATH/TO/tax/data
        failOnStartupError: true
```

### 方式 B：Streamable HTTP

```bash
.venv/bin/python mcp_server/server.py --http
```

```yaml
- insert:
    - id: zhixiaoshui-mcp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: zhixiaoshui
        transport: streamable-http
        url: http://127.0.0.1:8766/mcp
        failOnStartupError: true
```

## 5. 安全

- 默认只读；
- 不返回完整身份证、API Key、密码；
- 不提供申报/删除/写入工具；
- 本地数据只在本机进程内读取；
- 后续若增加写工具，必须二次确认并写审计日志。
