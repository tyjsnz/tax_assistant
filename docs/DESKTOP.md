# 桌面版（双击运行）

目标：让不懂命令行的用户双击即可使用，不需要安装 Python、不需要运行 `streamlit run`。

## 产物

- macOS：`税小智.app`（压缩为 `税小智-macos.zip`）
- Windows：`税小智\税小智.exe`（压缩为 `税小智-windows.zip`）

## 首次打开

- macOS 未签名版本会被 Gatekeeper 提示：右键 →「打开」；正式分发建议 Apple Developer 签名 + 公证。
- Windows 未签名版本 SmartScreen 可能提示：更多信息 → 仍要运行；正式分发建议代码签名证书。

## 数据位置

- macOS：`~/Library/Application Support/税小智`
- Windows：`%APPDATA%\税小智`
- 不写入安装包，升级/重装不会覆盖账套。

## 打包

见 [packaging/README.md](../packaging/README.md)。

GitHub Actions 提供 `Build desktop installers` 工作流（手动触发或 tag），产出 macOS/Windows 工件。

## 已构建产物

- macOS arm64：`dist_desktop/税小智.app` / `税小智-macos.zip`（约 101 MB）
- Windows：由 GitHub Actions `Build desktop installers` 产出 `税小智-windows.zip`

> 未签名版本首次打开可能被系统拦截；这是代码签名/公证问题，不是应用本身故障。
