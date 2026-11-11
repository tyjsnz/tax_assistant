# v1.24.2 发布说明（Release Notes）

发布日期：2026-11-12 · 标签：`v1.24.2`

## 修复：AI 代办页预览报 session_state 冲突

### 问题

点击「⚡ AI 代办」里的「🔍 预览」时报：

> `st.session_state['auto_pipeline_preview'] cannot be modified after the widget with that key is instantiated`

并导致页面渲染失败。

原因：预览按钮的 widget key 与保存预览结果的 session_state key 同名。

### 修复

- 预览按钮 key 保持 `auto_*_preview`；
- 预览结果改用独立的 `_auto_*_result` 键；
- 四类代办（流水线/工资/票据/申报/离职）全部修复；
- 浏览器实测各预览不再报错。

## 验证

- 全量 822 项离线测试通过；
- 在线演示 v1.24.2。
