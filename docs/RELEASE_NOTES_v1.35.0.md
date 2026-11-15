# v1.35.0 发布说明（Release Notes）

发布日期：2026-11-15 · 标签：`v1.35.0`

## 亮点：桌面安装版（双击运行，不要求命令行）

### 为什么

原来的启动方式需要：
- 安装 Python；
- 解压后在终端执行 `bash start.sh`；
- 或在浏览器打开 localhost。

对不懂技术的操作人员门槛太高。

### 现在的桌面版

- **macOS**：下载 `.app`，拖进 Applications，双击运行；
- **Windows**：下载 `.exe` 文件夹，双击 `税小智.exe`；
- 内置 Python、依赖、政策文件、操作指引；
- 使用 pywebview 原生窗口，不是让用户自己开浏览器；
- 数据存在用户目录，不写入安装包：
  - macOS：`~/Library/Application Support/税小智`
  - Windows：`%APPDATA%\税小智`
- 无需命令行、无需数据库、无需服务器。

### 构建方式

- `desktop_launcher.py`：启动本地 Streamlit（子进程，信号安全）+ pywebview 原生窗口；
- `packaging/tax.spec`：PyInstaller 配置；
- `.github/workflows/desktop-build.yml`：macOS/Windows 构建并上传 Release 资产；
- 首次打开未签名版本会被系统拦截：macOS 右键→打开，Windows 更多信息→仍要运行；
  正式对外分发建议代码签名/公证。

## 验证

- 打包后的 macOS `.app` 实测：本地服务健康检查 200，应用可启动；
- 全量离线测试 853 项；
- 官网下载页新增桌面版入口。

> 桌面版与源码包功能一致；源码包保留给技术用户/服务器部署。
