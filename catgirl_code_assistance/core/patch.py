"""补丁：预览 diff / 应用替换 / 备份 / 撤销。

所有写操作都遵循：
  1. 先生成 unified diff 预览（propose_edit，不落盘）；
  2. 真正写入前检查两层权限（总开关 + 逐操作授权）；
  3. 写入前把原文完整备份到插件私有目录（self.data_path），失败即中止；
  4. 撤销栈有上限（max_undo_steps），备查随机内容不会进日志。
"""
from __future__ import annotations

import difflib
import hashlib
import os
import re
import time
from dataclasses import dataclass, asdict
from typing import Any

import asyncio

from .reader import read_text_file, write_text_file


@dataclass
class EditRecord:
    """一次成功的编辑，用于撤销与历史查询。"""

    path: str
    action: str
    backup: str
    replacements: int
    timestamp: float
    old_sha256: str = ""
    new_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


# ───────────────────────────────────────────────
# 替换预览（纯函数，不碰磁盘）
# ───────────────────────────────────────────────
def compute_replacement(
    source: str, old: str, new: str, *, count: int = 1, use_regex: bool = False
) -> tuple[str, int, str]:
    """返回（新内容，替换次数，错误信息）。"""
    if old == "":
        return source, 0, "old_text 不能为空。"
    if use_regex:
        try:
            pattern = re.compile(old, re.M)
        except re.error as exc:
            return source, 0, f"正则编译失败：{exc}"
        new_content, hits = pattern.subn(new, source, count=0 if count <= 0 else count)
    else:
        hits = source.count(old)
        if hits == 0:
            return source, 0, "没有匹配到 old_text。请确认原文完全一致（含缩进与换行），或改用 read_code_file 取回最新内容。"
        if 0 < count < hits:
            new_content = source.replace(old, new, count)
            hits = count
        else:
            new_content = source.replace(old, new)
    if hits == 0:
        return source, 0, "没有匹配到 old_text。"
    return new_content, hits, ""


def build_diff(path: str, before: str, after: str, *, context: int = 3) -> str:
    diff = difflib.unified_diff(
        before.splitlines(keepends=False),
        after.splitlines(keepends=False),
        fromfile=f"a/{os.path.basename(path)}",
        tofile=f"b/{os.path.basename(path)}",
        n=context,
    )
    return "\n".join(diff)


# ───────────────────────────────────────────────
# 备份 / 写回 / 撤销（磁盘 I/O，异步）
# ───────────────────────────────────────────────
async def read_source(path: str, max_bytes: int) -> tuple[bool, str, str]:
    result = await read_text_file(path, max_bytes)
    return result.ok, result.content, result.error


async def write_backup(backup_dir: str, path: str, content: str) -> tuple[bool, str, str]:
    """把原文写进备份目录，返回（是否成功，备份路径，错误信息）。"""

    def _run() -> tuple[bool, str, str]:
        try:
            os.makedirs(backup_dir, exist_ok=True)
        except OSError as exc:
            return False, "", f"无法创建备份目录：{exc}"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        digest = sha256_of(content)[:8]
        name = f"{os.path.basename(path)}.{stamp}.{digest}.bak"
        target = os.path.join(backup_dir, name)
        try:
            with open(target, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
        except OSError as exc:
            return False, "", f"备份写入失败：{exc}"
        return True, target, ""

    return await asyncio.to_thread(_run)


async def write_source(path: str, content: str) -> tuple[bool, str]:
    return await write_text_file(path, content)


async def restore_backup(backup_path: str, target: str) -> tuple[bool, str]:
    def _run() -> tuple[bool, str]:
        if not os.path.isfile(backup_path):
            return False, f"备份文件已丢失：{backup_path}"
        try:
            with open(backup_path, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read()
        except OSError as exc:
            return False, f"读取备份失败：{exc}"
        tmp = f"{target}.cca-tmp"
        try:
            with open(tmp, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
            os.replace(tmp, target)
            return True, ""
        except OSError as exc:
            return False, f"恢复失败：{exc}"

    return await asyncio.to_thread(_run)
