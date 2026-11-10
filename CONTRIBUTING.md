# 贡献指南

感谢你愿意参与「税小智」！这是一个本地优先、开源的小规模纳税人自主税务填报工具。

## 开发环境

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests/ -q     # 全量离线测试
.venv/bin/streamlit run ui/streamlit_app.py
```

## 代码约定

- 面向用户的文字、docstring、日志使用中文；变量/函数名用英文 snake_case；
- **算税/申报金额必须由确定性代码计算**，禁止让 LLM 参与税额计算；
- 所有算税/申报/政策输出必须附带 `config.DISCLAIMER`；
- 数据表结构只在 `ledger/models.py` 定义，模块通过 `ledger/store` 读写；
- 不提交 `data/user_settings.json`、`data/companies/*/backups`、`data/companies/*/archive`、`logs/` 等本地数据。

## 政策变更

政策变化请按 [docs/POLICY_MAINTENANCE.md](docs/POLICY_MAINTENANCE.md) 追加政策版本并补回归测试，
**不要修改历史版本**；同时在 `knowledge/policy_files/` 同步解读文档。

## 测试要求

- 新功能必须带 pytest 用例（离线，不访问真实 DeepSeek API）；
- 涉及金额时覆盖边界（免税额度、税率跳档、四舍五入）；
- UI 改动请更新 `tests/test_ui_smoke.py`（AppTest）。

## 提交 PR

1. 从 `main` 建分支，保持提交信息清晰（`feat:` / `fix:` / `docs:` / `test:`）；
2. 本地跑通 `pytest tests/ -q`；
3. 在 PR 描述中说明变更动机、影响范围、测试方式；
4. 涉及税务口径的改动，请附政策文号与生效日期。

## 免责声明

本项目为辅助工具，不替代税务师/代理记账机构，不代替电子税务局申报。
所有结果仅供参考，以主管税务机关核定为准。
