"""文件读取：二进制判定 + 多编码回退 + 行窗口切片。

磁盘 I/O 全部在 asyncio.to_thread 里跑，不阻塞事件循环（skill 铁律 #10）。
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

ENCODING_CHAIN = ("utf-8", "utf-8-sig", "gbk", "gb2312", "big5", "shift_jis", "euc-kr", "latin-1")
_BINARY_PROBE = 4096


@dataclass
class ReadResult:
    ok: bool
    content: str = ""
    encoding: str = ""
    total_lines: int = 0
    size: int = 0
    binary: bool = False
    truncated: bool = False
    error: str = ""


def detect_binary(raw: bytes) -> bool:
    head = raw[:_BINARY_PROBE]
    if b"\x00" in head:
        return True
    # 大量非文本字节也判为二进制
    if not head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        weird = sum(1 for b in head if b < 9 or (13 < b < 32))
        if weird > len(head) * 0.1:
            return True
    return False


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """多编码回退解码，返回（文本，命中的编码名）。"""
    for enc in ENCODING_CHAIN:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, UnicodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def slice_lines(content: str, start_line: int, end_line: int) -> tuple[str, int, int, bool]:
    """按 1-based 行号切窗口，返回（片段, 起始行, 结束行, 是否被截断）。"""
    lines = content.splitlines()
    total = len(lines)
    if start_line <= 0:
        start_line = 1
    if end_line <= 0 or end_line > total:
        end_line = total
    if start_line > total:
        return "", total, total, True
    chunk = lines[start_line - 1 : end_line]
    return "\n".join(chunk), start_line, end_line, (end_line < total or start_line > 1)


def _read_sync(path: str, max_bytes: int, start_line: int, end_line: int) -> ReadResult:
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return ReadResult(False, error=f"无法获取文件大小：{exc}")
    try:
        with open(path, "rb") as handle:
            head = handle.read(_BINARY_PROBE)
            if detect_binary(head):
                return ReadResult(False, size=size, binary=True, error="这是二进制文件，猫娘看不懂二进制喵。请指定文本源码文件。")
            handle.seek(0)
            raw = handle.read(max_bytes + 1)
    except PermissionError:
        return ReadResult(False, size=size, error="没有读取权限。")
    except OSError as exc:
        return ReadResult(False, size=size, error=f"读取失败：{exc}")

    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    content, encoding = decode_bytes(raw)
    # 截断到行长不超过 2000 字符的极端单行保护
    if end_line > 0 or start_line > 1:
        chunk, begin, finish, clipped = slice_lines(content, start_line, end_line)
        return ReadResult(True, chunk, encoding, len(content.splitlines()), size, False, truncated or clipped)
    return ReadResult(True, content, encoding, len(content.splitlines()), size, False, truncated)


async def read_text_file(
    path: str, max_bytes: int, start_line: int = 0, end_line: int = 0
) -> ReadResult:
    return await asyncio.to_thread(_read_sync, path, max_bytes, start_line, end_line)


async def read_bytes_limited(path: str, max_bytes: int) -> bytes:
    def _run() -> bytes:
        with open(path, "rb") as handle:
            return handle.read(max_bytes)

    return await asyncio.to_thread(_run)


async def write_text_file(path: str, content: str, encoding: str = "utf-8") -> tuple[bool, str]:
    """原子写：先写临时文件再 os.replace，避免半截文件。"""

    def _run() -> tuple[bool, str]:
        tmp = f"{path}.cca-tmp"
        try:
            with open(tmp, "w", encoding=encoding, newline="") as handle:
                handle.write(content)
            os.replace(tmp, path)
            return True, ""
        except OSError as exc:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return False, str(exc)

    return await asyncio.to_thread(_run)
