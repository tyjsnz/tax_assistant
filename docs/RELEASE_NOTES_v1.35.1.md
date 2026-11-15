# v1.35.1 发布说明（Release Notes）

发布日期：2026-11-15 · 标签：`v1.35.1`

## 修复：桌面版 `ledger` 包未被打包

### 问题

macOS 桌面版打开部分页面时报：

```text
ImportError: cannot import name 'locks' from 'ledger'
```

原因：PyInstaller 的 hidden imports 没有显式收集 `ledger` 顶层包及关键子模块，
页面动态导入时才暴露。

### 修复

- `packaging/tax.spec` 显式收集所有业务包：`ui/agent/engine/ledger/filing/closing/archiving/ingest/...`；
- 补充 `ledger.locks`、`ledger.store`、`ui.views.dashboard` 等关键子模块；
- 重新构建 macOS 桌面版，实测首页与页面渲染正常；
- 全量 853 项测试通过。

> 下载最新桌面版：https://tax.flacn.net/download/zhixiaoshui-macos-desktop.zip
