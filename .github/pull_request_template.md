## 变更说明

<!-- 这个 PR 做了什么、为什么需要 -->

## 影响范围

- [ ] engine（算税口径）——请附政策文号/生效日期与测试
- [ ] ledger / filing / closing / archiving
- [ ] ingest / agent / knowledge / ocr
- [ ] ui / 文档 / 构建

## 测试

- [ ] `python -m pytest tests/ -q` 通过
- [ ] 涉及 UI 的已更新 `tests/test_ui_smoke.py`
- [ ] 涉及金额口径的覆盖边界用例（免税额度/税率跳档/四舍五入）

## 检查

- [ ] 未提交真实客户数据、API Key、本地 `data/` 中的敏感文件
- [ ] 政策类改动已按 `docs/POLICY_MAINTENANCE.md` 维护并注明来源
- [ ] 本 PR 不含无关改动
