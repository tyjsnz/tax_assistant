// macOS Vision 本地 OCR 小工具（由 ocr/extractor.py 编译并调用）
// 用法：macos_ocr <图片路径>，逐行输出识别到的文本。
// 说明：DeepSeek 官方 API 无视觉能力，截图文字识别必须走本地 OCR；
//      本工具零 Python 依赖，使用系统自带 Vision 框架（支持简体中文 zh-Hans）。
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
