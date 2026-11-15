# v1.36.0 发布说明（Release Notes）

发布日期：2026-11-15 · 标签：`v1.36.0`

## 亮点：启动检查更新

### 行为

- 打开系统时请求官网 `version.json`（3 秒超时）；
- 发现新版本时，在页面顶部提示：
  - 当前版本 / 最新版本；
  - 更新说明；
  - 下载按钮（macOS/Windows 自动匹配）；
  - 「稍后提醒」。
- **不自动下载、不自动替换程序**，避免未签名自动更新风险。

### 隐私

- 只发送版本查询请求，不发送账目、公司、API Key；
- 网络失败静默，不影响启动；
- 可用环境变量 `TAX_DISABLE_UPDATE_CHECK=1` 关闭。

### 下载

- macOS：https://tax.flacn.net/download/zhixiaoshui-macos-desktop.zip
- Windows：https://tax.flacn.net/download/zhixiaoshui-windows-desktop.zip
- 源码：https://tax.flacn.net/download/zhixiaoshui-v1.36.0.zip
- GitHub：https://github.com/tyjsnz/tax_assistant/releases/tag/v1.36.0

## 升级方式

1. 看到提示后点「下载」；
2. 退出旧版；
3. 替换 App/安装目录；
4. 数据目录不变，不会丢账套。

## 变更

- 全量离线测试 853 项；
- 官网/下载包/在线演示同步更新。
