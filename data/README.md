# 数据目录（运行时生成，默认不入库）

本目录保存**本机**账套数据，不属于代码仓库内容（已在 `.gitignore` 中忽略）：

```text
data/
├── companies.json          # 公司（账套）注册表
├── schema.json             # 数据布局版本（当前 v2：多公司目录）
├── user_settings.json      # 用户设置（含 DeepSeek API Key，权限 600，不进入备份）
├── logs/                   # 全局审计日志
└── companies/{id}/         # 每家公司独立账套
    ├── income_ledger.csv / expense_ledger.csv / rd_auxiliary.csv / contracts.csv
    ├── journal_entries.csv / journal_lines.csv
    ├── employees.csv / payroll.csv
    ├── invoices.csv / bank_transactions.csv / matches.csv
    ├── filings.csv / periods.csv / archives.csv / backups.csv
    ├── tax_adjustments.csv / loss_carryforwards.csv / annual_settlements.csv
    ├── archive/             # 申报包归档（xlsx + pdf + manifest）
    ├── backups/             # 数据备份 zip
    └── uploads/             # 导入的原始文件
```

* 首次启动会自动创建默认公司并初始化数据表；
* 演示数据：`.venv/bin/python scripts/seed_demo.py --reset` 生成虚构演示公司；
* 备份/迁移：`python main.py backup` / 恢复，或直接复制整个 `data/` 目录（保持结构）；
* **不要将本目录（尤其 `user_settings.json`、客户财务数据）提交到公开仓库。**
