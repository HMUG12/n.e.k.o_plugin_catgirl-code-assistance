"""路径守卫 —— 猫娘只在你允许的文件夹里活动喵。

两条硬规则：
  1. 绝对路径：相对路径一律拒绝（防 AI 瞎拼路径）。
  2. 根目录围栏：目标路径的 abspath **与** realpath 都必须落在某个可读根目录内
     —— 双判定用来挡住符号链接 / 目录穿越（`../../`）越界读写。

错误消息遵循「发生了什么 + 为什么 + 怎么做」三要素（AI 友好设计）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from .config import Settings


@dataclass(frozen=True)
class PathResolution:
    """路径检查结果。ok 为 True 时 path 才是可安全使用的绝对路径。"""

    ok: bool
    path: str = ""
    error: str = ""


class PathGuard:
    """基于配置的可读根目录白名单校验器。"""

    def __init__(self, settings: Settings):
        self.configure(settings)

    # ── 配置热更新 ──
    def configure(self, settings: Settings) -> None:
        self.roots: list[str] = [
            os.path.normpath(p) for p in settings.readable_roots if str(p).strip()
        ]
        self.blocked_dirs = {d.lower() for d in settings.blocked_dirs}
        self.follow_symlinks: bool = settings.follow_symlinks
        self.settings = settings

    # ── 围栏判定（rstrip(os.sep) 修复根目录 bug，见 skill 铁律 #8）──
    def _within(self, candidate: str) -> bool:
        norm = os.path.normpath(candidate).lower().rstrip(os.sep)
        for root in self.roots:
            root_norm = os.path.normpath(root).lower().rstrip(os.sep)
            if norm == root_norm or norm.startswith(root_norm + os.sep):
                return True
        return False

    def has_roots(self) -> bool:
        return bool(self.roots)

    # ── 屏蔽目录（中间路径成分命中即拒）──
    def _blocked_component(self, abspath: str) -> Optional[str]:
        parts = [p.lower() for p in abspath.replace("/", os.sep).split(os.sep)]
        for part in parts:
            if part in self.blocked_dirs:
                return part
        return None

    # ── 主校验 ──
    def check(
        self,
        raw_path: str,
        *,
        need: str = "file",  # "file" | "dir" | "any"
        must_exist: bool = True,
    ) -> PathResolution:
        text = str(raw_path or "").strip()
        if not text:
            return PathResolution(False, error="路径为空。请先在设置面板配置可读文件夹，或调用 list_directory 查看可用目录喵。")
        if not os.path.isabs(text):
            return PathResolution(
                False,
                error=(
                    f"路径不是绝对路径：{text}。"
                    "请先用 search_files / list_directory 取得完整绝对路径再操作。"
                ),
            )
        if not self.has_roots():
            return PathResolution(
                False,
                error=(
                    "还没有配置可读文件夹。请打开插件设置面板，"
                    "把允许猫娘阅读的项目根目录（绝对路径，每行一个）填进「可读文件夹」并保存喵。"
                ),
            )

        candidate = os.path.normpath(os.path.abspath(text))
        if not self.follow_symlinks:
            # 双判定：即使 abspath 在围栏内，realpath 一旦越界也拒绝
            real = os.path.normpath(os.path.realpath(candidate))
            if not self._within(real):
                return PathResolution(False, error=f"路径（解析符号链接后）不在允许的可读文件夹内：{candidate}。疑似符号链接越界，已拒绝喵。")
        if not self._within(candidate):
            preview = "、".join(self.roots[:3])
            return PathResolution(
                False,
                error=(
                    f"路径不在允许的可读文件夹内：{candidate}。"
                    f"当前授权根目录：{preview}。"
                    "如需访问请先把它加入设置面板的「可读文件夹」。"
                ),
            )

        blocked = self._blocked_component(candidate)
        if blocked:
            return PathResolution(False, error=f"命中屏蔽目录（{blocked}）：{candidate}。该目录默认不索引，可在设置面板调整「屏蔽目录」喵。")

        if must_exist and not os.path.exists(candidate):
            return PathResolution(
                False,
                error=f"路径不存在：{candidate}。请先用 search_files 搜索确认名字，再拿完整绝对路径来操作喵。",
            )
        if must_exist:
            if need == "file" and not os.path.isfile(candidate):
                return PathResolution(False, error=f"不是文件：{candidate}。如果是目录请用 list_directory 喵。")
            if need == "dir" and not os.path.isdir(candidate):
                return PathResolution(False, error=f"不是目录：{candidate}。如果是文件请用 read_code_file 喵。")
        return PathResolution(True, path=candidate)

    # ── 列出根目录（供 AI 起步探索）──
    def roots_status(self) -> list[dict]:
        items = []
        for root in self.roots:
            items.append(
                {
                    "path": root,
                    "exists": os.path.isdir(root),
                    "is_dir": os.path.isdir(root),
                }
            )
        return items
