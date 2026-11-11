# v1.29.1 发布说明（Release Notes）

发布日期：2026-11-12 · 标签：`v1.29.1`

## 修复：补充交接包加密依赖

v1.29.0 引入交接包密码加密（PBKDF2 + Fernet），但未在 `requirements.txt` 中声明 `cryptography`，导致 CI 环境导入失败。

- 已在 `requirements.txt` 添加 `cryptography>=42`；
- 本地加密/解密/导入测试通过；
- 全量 838 项测试通过。

> 使用加密交接包前请执行 `pip install -r requirements.txt`。
