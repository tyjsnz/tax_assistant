"""申报包归档与数据备份/恢复。

模块划分
--------
* :mod:`archiving.hash`          —— SHA-256 与规范化 JSON 哈希工具；
* :mod:`archiving.manifest`      —— 归档/备份清单（manifest）构造与校验；
* :mod:`archiving.pdf`           —— 申报表归档 PDF（reportlab + 中文字体）；
* :mod:`archiving.filing_archive`—— 申报包归档（Excel + PDF + SHA-256 清单）；
* :mod:`archiving.backup`        —— 台账数据备份（zip，绝不含 API Key）；
* :mod:`archiving.restore`       —— 备份预览与安全恢复。

安全约定
--------
1. 备份 zip **绝不包含** ``data/user_settings.json``（DeepSeek API Key）、
   ``data/uploads/``、``data/backups/``、``data/archive/``；
2. 恢复只接受 manifest 白名单内的 ``data/*.csv``，拒绝 zip-slip、未知文件、
   哈希不匹配与损坏 zip；
3. 归档与备份均为只读留痕，不改变台账与期间状态。
"""

from __future__ import annotations

__all__ = ["hash", "manifest", "pdf", "filing_archive", "backup", "restore"]
