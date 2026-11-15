# 桌面版打包说明

目标：给不懂命令行的用户提供**双击即可运行**的本地版本，而不是让他们安装 Python 再执行 `streamlit run`。

## 打包原理

- `desktop_launcher.py`：启动本地 Streamlit 服务（只监听 127.0.0.1），再用 pywebview 打开原生窗口；
- PyInstaller 把 Python、依赖、政策文件、操作指引等打包进 `.app` / `.exe`；
- 数据默认保存在用户目录，不写入安装包：
  - macOS：`~/Library/Application Support/税小智`
  - Windows：`%APPDATA%\税小智`
- 不要求用户安装 Python、不要求命令行。

## macOS 打包

```bash
.venv/bin/pip install -r packaging/requirements-desktop.txt
.venv/bin/pyinstaller packaging/tax.spec --noconfirm --clean
# 产物：dist/税小智.app
```

测试：

```bash
TAX_DESKTOP_TEST=1 dist/税小智.app/Contents/MacOS/税小智
# 打开输出的 http://127.0.0.1:xxxx 验证
```

## Windows 打包

在 Windows 上：

```powershell
python -m pip install -r requirements.txt -r packaging/requirements-desktop.txt
pyinstaller packaging\tax.spec --noconfirm --clean
# 产物：dist\税小智\税小智.exe
```

也可用 GitHub Actions 的 `desktop-build.yml` 产出 Windows/macOS 工件。

## 分发注意

- macOS 未签名/未公证时，首次打开会被 Gatekeeper 拦截：右键 → 打开；正式分发建议 Apple Developer 签名+公证；
- Windows 未签名时 SmartScreen 会提示；正式分发建议代码签名证书；
- 不要把 `data/`、`.env`、API Key 打进安装包；
- 安装包体积会较大（包含 Python/Streamlit/依赖），建议 one-folder 分发后压缩。
