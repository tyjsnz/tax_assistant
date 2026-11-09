# macOS Vision OCR 集成说明（已由 Lead 实测通过）

DeepSeek 官方 API 无视觉能力，申报表截图必须走**本地 OCR**。本项目使用 macOS 自带 Vision 框架
（支持简体中文 `zh-Hans`），通过 Swift 编译成独立小工具调用，**零 Python 依赖**。

## 已验证的 Swift 源码

```swift
import Foundation
import Vision
import AppKit

let args = CommandLine.arguments
guard args.count > 1, let img = NSImage(contentsOfFile: args[1]),
      let tiff = img.tiffRepresentation,
      let bmp = NSBitmapImageRep(data: tiff),
      let cg = bmp.cgImage else { print("ERR_IMAGE"); exit(1) }
let request = VNRecognizeTextRequest()
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
let handler = VNImageRequestHandler(cgImage: cg, options: [:])
do {
    try handler.perform([request])
    for obs in request.results ?? [] {
        if let c = obs.topCandidates(1).first { print(c.string) }
    }
} catch { print("ERR_OCR: \(error)"); exit(2) }
```

## 关键坑：编译时必须把 module cache / TMPDIR 指到工作区内

本机会话运行在 workspace-write 沙箱中，`swiftc` 默认往 `/var/folders/.../ModuleCache` 写缓存会被拒绝：

```
error: error opening '.../C/clang/ModuleCache/Swift-*.swiftmodule' for output: Operation not permitted
```

**正确编译命令**（缓存与临时目录都放在项目目录内）：

```bash
cd /Users/shanengzhi/www/2026/prj/tax
mkdir -p .cache/swift .cache/tmp ocr/bin
TMPDIR="$PWD/.cache/tmp" swiftc -module-cache-path "$PWD/.cache/swift" -O ocr/vision_ocr.swift -o ocr/bin/macos_ocr
```

> 首次编译约 10–40 秒，产物 `ocr/bin/macos_ocr` 可重复使用；`ocr/bin/` 与 `.cache/` 已在 .gitignore 中。

## 调用与实测结果

```bash
./ocr/bin/macos_ocr /path/to/return_form.png
```

实测（900×420 的中文申报表样例图，`tests/fixtures/return_form.png`）输出：

```
增值税及附加税费申报表（小规模纳税人适用）
税款所属期：2026年01月01日 至2026年03月31日
1 应征增值税不含税销售额（3%征收率）
0.00
10小微企业免税销售额
101980.20
18应纳税额
0.00
19 本期免税
3059.41
22应纳税额减征额
0.00
24本期应补（退）税额
0.00
```

可见 Vision 会把「栏次说明」和「金额」识别到不同行，**字段映射的正则必须允许跨行**：
先取「栏次号/栏次名」所在行，再取其后最近的数字行；同时兼容同一行「栏次 名称 金额」的格式
（不同截图/手机拍照排版差异较大）。

## Python 侧建议实现（ocr/extractor.py）

1. `build_vision_binary()`：若 `ocr/bin/macos_ocr` 不存在或源文件更新，则按上面的命令编译；
   编译失败返回 None 并记录原因，不抛异常。
2. `ocr_image(path)`：优先调用编译好的二进制（`subprocess.run`，timeout 30，捕获
   `stderr`/`returncode`）；失败回退 `pytesseract`（本机只有 eng，能识别数字即可）；
   都不可用抛 `OCRAvailableError`，由 UI 提示用户手动粘贴文本。
3. `is_available()` 返回 `{"macos_vision": bool, "tesseract": bool, "manual": True}`。
4. 二进制路径用 `config.BASE_DIR` 拼接，避免依赖当前工作目录。

## 备选方案

- `pip install pyobjc-framework-Vision`（无需 Swift）也可实现，但增加依赖且本机未安装；
- Windows/Linux 部署时回退 paddleocr / tesseract（需 `chi_sim` 语言包），项目 requirements 中列为可选依赖。
