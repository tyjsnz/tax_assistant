"""SHA-256 与规范化 JSON 哈希工具。

归档/备份的「可自证」能力依赖本模块：所有文件哈希均为文件字节的 SHA-256；
JSON 哈希先通过 :func:`canonical_json` 归一化（键排序、UTF-8 中文不转义、
无多余空白），保证同一份数据在不同时间/进程下得到相同哈希。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

#: 读取文件时的分块大小（1 MiB）
_CHUNK_SIZE = 1024 * 1024


def sha256_bytes(data: bytes) -> str:
    """返回字节内容的 SHA-256 十六进制摘要。"""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("sha256_bytes 只接受 bytes 类型数据")
    return hashlib.sha256(bytes(data)).hexdigest()


def sha256_file(path: Any) -> str:
    """返回文件的 SHA-256 十六进制摘要（分块读取，适合大文件）。

    文件不存在或不可读时抛 ``OSError``，由调用方决定降级或提示。
    """
    target = Path(path)
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(obj: Any) -> str:
    """把对象序列化为稳定 JSON 文本：键排序、中文不转义、紧凑分隔符。"""
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def hash_json(obj: Any) -> str:
    """返回对象规范化 JSON 的 SHA-256 摘要（可复现）。"""
    return sha256_bytes(canonical_json(obj).encode("utf-8"))


__all__ = ["sha256_bytes", "sha256_file", "canonical_json", "hash_json"]
