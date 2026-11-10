# 发布检查清单

> 维护者发布新版本前逐项确认；版本号规则：`MAJOR.MINOR.PATCH`（当前 v1.6.0）。

## 1. 代码与版本

- [ ] `config.APP_VERSION` 已更新（与 README/CHANGELOG 一致）
- [ ] `CHANGELOG.md` 增加本版本条目（新增/修复/破坏性变更）
- [ ] `docs/ROADMAP.md` 状态更新
- [ ] 无调试代码、无真实客户数据、无 API Key（`git grep -i "sk-"` 检查）
- [ ] `data/user_settings.json`、`data/companies/*/backups|archive|uploads` 未被提交

## 2. 测试

- [ ] `.venv/bin/python -m pytest tests/ -q` 全绿
- [ ] 真实启动：`streamlit run ui/streamlit_app.py` → 11 页切换无异常
- [ ] `python scripts/seed_demo.py --reset` → 演示账套生成成功、页面数据正常
- [ ] 关键流程手工回归：票据导入 → 匹配入账 → 工资个税 → 申报表 → 结账 → 归档 → 备份恢复 → 汇算
- [ ] CI（GitHub Actions）在 Python 3.11/3.12 通过

## 3. 文档与示例

- [ ] README 功能/政策口径/截图与实际一致
- [ ] docs/QUICKSTART、USER_GUIDE、DEPLOYMENT、ARCHITECTURE、FAQ 已更新
- [ ] 新增政策已按 `docs/POLICY_MAINTENANCE.md` 维护并补测试
- [ ] 截图已重新生成（如界面变化），无真实数据/密钥
- [ ] `examples/` 示例文件可用

## 4. 构建与分发

- [ ] `docker build -t tax-agent .` 成功，容器可启动并挂载 data
- [ ] Docker 内健康检查：`curl http://localhost:8501/_stcore/health` → `ok`
- [ ] `pip install -r requirements.txt` 在干净虚拟环境成功
- [ ] 版本 tag：`git tag -a vX.Y.Z -m "..."` 并推送

## 5. GitHub Release

标题：`vX.Y.Z`

正文模板：

```markdown
## 亮点
- ...

## 新增
- ...

## 修复
- ...

## 升级说明
- 备份：`python main.py backup --tag before_upgrade`
- 数据迁移：首次启动自动执行（schema vX）
- 兼容性：...

## 验证
- pytest tests/ -q → N passed
```

- [ ] Release Notes 附截图/演示命令
- [ ] 标签与 Release 对应同一 commit

## 6. 发布后

- [ ] 在 README 更新版本徽章/下载指引（如有）
- [ ] 观察 Issue 反馈，必要时发 patch 版本
