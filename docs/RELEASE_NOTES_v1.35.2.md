# v1.35.2 发布说明（Release Notes）

发布日期：2026-11-15 · 标签：`v1.35.2`

## 修复：桌面版仍未完整收集动态模块

v1.35.1 修复了 `ledger` 顶层包，但其他动态导入页面仍可能报：

- `No module named 'engine.calendar_profile'`
- `No module named 'engine.invoice_rules'`
- `cannot import name 'content' from 'guidebooks'`
- `ledger.settlement/filing.cit_annual 模块尚未就绪`

原因：PyInstaller spec 里的 `collect_submodules()` 在 spec 环境下可能静默失败，
导致只收集了部分模块。

### 修复

- spec 改为**直接枚举每个业务包目录下的全部 `.py` 文件**，生成 hiddenimports；
- 不再依赖 `collect_submodules` 的导入行为；
- 重新构建 macOS 桌面版，逐页实测 18 个页面均可打开；
- 全量 853 项测试通过。

## 下载

- macOS：https://tax.flacn.net/download/zhixiaoshui-macos-desktop.zip
- Windows：https://tax.flacn.net/download/zhixiaoshui-windows-desktop.zip
- GitHub Release：https://github.com/tyjsnz/tax_assistant/releases/tag/v1.35.2

> 请删除旧 App 后重新解压，数据目录不变。
