"""探索 Router：列目录 / 找文件 / 全项目正则搜索。

三条设计原则：
  · AI 友好：所有结果都返回**完整绝对路径**，并提示下一步该调哪个入口；
  · 深模块：真正的磁盘扫描藏在 `_grep_sync` / `_walk_live` 里，入口只做编排；
  · 本地脱敏：任何要离开插件返回给大模型的文本，先过 Redactor 再出门。
"""
from __future__ import annotations

import asyncio
import fnmatch
import os
import re
from typing import Any

from plugin.sdk.plugin import plugin_entry, llm_tool, Ok, Err, SdkError

from ..core import Settings, persona_error, persona_line
from ..core.indexer import walk_collect
from ..core.util import error_of, payload_of, truthy

MAX_FILES_SCANNED = 400
MAX_LIST_ENTRIES = 300

SEARCH_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "文件名片段或通配符，如 '*.py'、'main'"},
        "ext": {"type": "string", "description": "按扩展名过滤，如 '.py'、'.c'（可选）"},
        "max_results": {"type": "integer", "description": "最多返回多少个文件，默认 50"},
    },
    "required": ["pattern"],
}

GREP_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "要搜索的正则表达式"},
        "path": {
            "type": "string",
            "description": "限定在某个文件或目录的绝对路径内搜索；留空则搜索全部可读文件夹",
        },
        "file_glob": {"type": "string", "description": "文件名通配符限定，如 '*.py'、'*.c'（可选）"},
        "max_matches": {"type": "integer", "description": "最多返回多少条命中，默认 50"},
        "case_sensitive": {"type": "boolean", "description": "是否区分大小写，默认 true"},
    },
    "required": ["pattern"],
}

LIST_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "要列目录的绝对路径（必须在可读文件夹内）"},
        "depth": {"type": "integer", "description": "递归层数，默认 1（只看一层）"},
        "include_hidden": {"type": "boolean", "description": "是否包含以 . 开头的条目，默认 false"},
    },
    "required": ["path"],
}


class ExploreRouter:
    # ─────────────────────────────────────────
    # 深函数：候选文件集合（优先走索引，索引不可用时实时遍历）
    # ─────────────────────────────────────────
    async def _candidates(
        self, *, pattern: str = "", ext: str = "", within: str = "", limit: int = MAX_FILES_SCANNED
    ) -> tuple[list[str], str]:
        rows = await self.index.candidates(pattern=pattern, ext=ext, limit=limit)
        if rows:
            raw_paths: list[str] = [str(r.get("path", "")) for r in rows]
            source = "index"
        else:
            raw_paths = await self._walk_live(limit=limit)
            source = "live"

        guard = self._guard()
        needle = pattern.strip("*").lower()
        out: list[str] = []
        for item in raw_paths:
            if not item:
                continue
            check = guard.check(item, need="any", must_exist=True)
            if not check.ok:
                continue  # 索引里可能有已删除/越界的陈旧条目，一律复检
            target = check.path
            if within and not self._within(target, within):
                continue
            if ext and not target.lower().endswith(ext.lower()):
                continue
            if needle and needle not in os.path.basename(target).lower():
                continue
            out.append(target)
            if len(out) >= limit:
                break
        return out, source

    @staticmethod
    def _within(candidate: str, folder: str) -> bool:
        base = os.path.normpath(folder).lower().rstrip(os.sep)
        target = os.path.normpath(candidate).lower().rstrip(os.sep)
        return target == base or target.startswith(base + os.sep)

    async def _walk_live(self, limit: int = MAX_FILES_SCANNED) -> list[str]:
        settings: Settings = self.settings
        if not settings.readable_roots:
            return []
        blocked = {d.lower() for d in settings.blocked_dirs}
        out: list[str] = []
        for root in settings.readable_roots:
            if not os.path.isdir(root) or len(out) >= limit:
                continue
            entries = await asyncio.to_thread(
                walk_collect, root, blocked, list(settings.allowed_extensions), limit - len(out)
            )
            out.extend(entry[0] for entry in entries)
        return out

    # ─────────────────────────────────────────
    # 入口 1：列目录
    # ─────────────────────────────────────────
    @plugin_entry(
        id="list_directory",
        name="列出目录结构",
        description=(
            "列出某个目录下的文件与子目录，返回每个条目的完整绝对路径。"
            "⚠️ path 必须是绝对路径；不确定路径时请先用 search_files。"
            "用途：用户问「这个项目有哪些文件」「xx 目录下有什么」。"
        ),
        input_schema=LIST_SCHEMA,
        llm_result_fields=["path", "entries", "next_actions"],
    )
    async def list_directory(
        self, path: str = "", depth: int = 1, include_hidden: bool = False, **_
    ) -> Ok | Err:
        settings: Settings = self.settings
        check = self._guard().check(path, need="dir")
        if not check.ok:
            return Err(SdkError(persona_error(settings.reply_style, check.error)))
        try:
            depth = max(1, min(int(depth or 1), 4))
        except (TypeError, ValueError):
            depth = 1

        result = await asyncio.to_thread(
            self._list_sync, check.path, depth, bool(include_hidden), settings
        )
        result["hint"] = persona_line(settings.reply_style, f"这里有 {len(result['entries'])} 个条目")
        result["next_actions"] = [
            "对感兴趣的文件调用 read_code_file（用上面返回的 path）",
            "想按名字找文件用 search_files",
            "想按内容找东西用 grep_code",
        ]
        return Ok(result)

    def _list_sync(self, root: str, depth: int, include_hidden: bool, settings: Settings) -> dict:
        entries: list[dict] = []
        blocked = {d.lower() for d in settings.blocked_dirs}
        skipped_dirs: list[str] = []

        def _visit(folder: str, level: int) -> None:
            if level > depth or len(entries) >= MAX_LIST_ENTRIES:
                return
            try:
                names = sorted(os.listdir(folder))
            except OSError:
                return
            for name in names:
                if not include_hidden and name.startswith("."):
                    continue
                full = os.path.join(folder, name)
                if os.path.isdir(full):
                    if name.lower() in blocked:
                        skipped_dirs.append(name)
                        continue
                elif not settings.ext_allowed(full):
                    continue
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                entries.append(
                    {
                        "path": os.path.normpath(full),
                        "name": name,
                        "is_dir": os.path.isdir(full),
                        "size": size,
                        "depth": level,
                    }
                )
                if os.path.isdir(full):
                    _visit(full, level + 1)

        _visit(root, 1)
        return {
            "path": root,
            "entries": entries,
            "truncated": len(entries) >= MAX_LIST_ENTRIES,
            "skipped_dirs": sorted(set(skipped_dirs))[:10],
        }

    # ─────────────────────────────────────────
    # 入口 2：找文件
    # ─────────────────────────────────────────
    @plugin_entry(
        id="search_files",
        name="搜索文件名",
        description=(
            "按文件名片段或通配符在已授权的可读文件夹里查文件，返回完整绝对路径。"
            "⚠️ 读任何文件之前请先调用本入口拿到绝对路径，不要凭空猜测路径。"
            "支持通配符：'*.py'、'*util*'、'main.*'。"
        ),
        input_schema=SEARCH_SCHEMA,
        llm_result_fields=["files", "count", "next_actions"],
    )
    async def search_files(
        self, pattern: str = "", ext: str = "", max_results: Any = 50, **_
    ) -> Ok | Err:
        settings: Settings = self.settings
        if not str(pattern or "").strip():
            return Err(SdkError(persona_error(settings.reply_style, "pattern 不能为空")))
        try:
            limit = max(1, min(int(max_results or 50), settings.max_matches, 200))
        except (TypeError, ValueError):
            limit = 50

        paths, source = await self._candidates(pattern=pattern, ext=ext, limit=limit)
        files: list[dict] = []
        for item in paths:
            try:
                stat = os.stat(item)
            except OSError:
                continue
            files.append(
                {
                    "path": item,
                    "name": os.path.basename(item),
                    "ext": os.path.splitext(item)[1].lower(),
                    "size": stat.st_size,
                }
            )
        hint = (
            persona_line(settings.reply_style, f"找到 {len(files)} 个文件")
            if files
            else persona_error(settings.reply_style, "没有匹配的文件，换个关键词或先 rebuild_index 试试")
        )
        return Ok(
            {
                "pattern": pattern,
                "source": source,
                "count": len(files),
                "files": files,
                "next_actions": [
                    "用 read_code_file 读取内容（务必用返回的 path）",
                    "用 code_outline 看整体结构",
                    "用 grep_code 按内容进一步定位",
                ],
                "hint": hint,
            }
        )

    # ─────────────────────────────────────────
    # 入口 3：全项目正则搜索
    # ─────────────────────────────────────────
    @plugin_entry(
        id="grep_code",
        name="在代码中搜索文本",
        description=(
            "在可读文件夹的所有源码里按正则表达式搜索内容，返回文件、行号与该行原文（已本地脱敏）。"
            "用途：「这个函数在哪被调用」「项目里有没有用到 xxx API」。"
            "注意：返回的行文本会经过本地脱敏（密钥 / 邮箱 / 手机号会被打码）。"
        ),
        input_schema=GREP_SCHEMA,
        llm_result_fields=["matches", "count", "truncated"],
    )
    async def grep_code(
        self,
        pattern: str = "",
        path: str = "",
        file_glob: str = "",
        max_matches: Any = 50,
        case_sensitive: Any = True,
        **_,
    ) -> Ok | Err:
        settings: Settings = self.settings
        if not str(pattern or "").strip():
            return Err(SdkError(persona_error(settings.reply_style, "pattern 不能为空")))
        flags = 0 if truthy(case_sensitive, True) else re.IGNORECASE
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            return Err(SdkError(f"正则表达式无法编译：{exc}（pattern={pattern!r}）"))
        try:
            limit = max(1, min(int(max_matches or 50), settings.max_matches, 300))
        except (TypeError, ValueError):
            limit = 50

        scope = ""
        if str(path or "").strip():
            check = self._guard().check(path, need="any")
            if not check.ok:
                return Err(SdkError(persona_error(settings.reply_style, check.error)))
            scope = check.path

        paths, source = await self._candidates(within=scope, limit=MAX_FILES_SCANNED)
        if file_glob:
            paths = [p for p in paths if fnmatch.fnmatch(os.path.basename(p), file_glob)]
        if scope and os.path.isfile(scope):
            paths = [scope]

        raw = await asyncio.to_thread(
            self._grep_sync, paths, compiled, settings.max_file_kb * 1024, limit
        )
        redactor = self.redactor
        matches: list[dict] = []
        for hit in raw["matches"]:
            text = hit["text"]
            if settings.redact_sensitive:
                text = redactor.redact(text)[0]
            matches.append({**hit, "text": text})

        hint = (
            persona_line(settings.reply_style, f"命中 {len(matches)} 条")
            if matches
            else persona_error(settings.reply_style, "没有匹配内容，可以换个正则或放宽 file_glob")
        )
        return Ok(
            {
                "pattern": pattern,
                "scope": scope or "(全部可读文件夹)",
                "source": source,
                "files_scanned": raw["scanned"],
                "count": len(matches),
                "matches": matches,
                "truncated": raw["truncated"],
                "redacted": settings.redact_sensitive,
                "next_actions": [
                    "用 read_code_file 打开命中文件看上下文",
                    "要改动先用 propose_edit 预览 diff，确认后再 apply_edit",
                ],
                "hint": hint,
            }
        )

    def _grep_sync(
        self, paths: list[str], compiled: "re.Pattern[str]", max_bytes: int, limit: int
    ) -> dict:
        matches: list[dict] = []
        scanned = 0
        truncated = False
        for item in paths:
            if len(matches) >= limit:
                truncated = True
                break
            scanned += 1
            try:
                with open(item, "rb") as handle:
                    raw = handle.read(max_bytes)
            except OSError:
                continue
            if b"\x00" in raw[:4096]:
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("gbk", errors="replace")
            for line_no, line in enumerate(text.splitlines(), start=1):
                if compiled.search(line):
                    matches.append({"path": item, "line": line_no, "text": line.strip()[:400]})
                    if len(matches) >= limit:
                        truncated = True
                        break
        return {"matches": matches, "scanned": scanned, "truncated": truncated}

    # ─────────────────────────────────────────
    # LLM 工具：让大模型无需用户显式指令也能查代码
    # ─────────────────────────────────────────
    CCA_SEARCH_SCHEMA: dict = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "要搜索的标识符、报错信息或正则片段"},
            "file_glob": {"type": "string", "description": "限定文件名通配符，如 '*.py'"},
        },
        "required": ["query"],
    }

    @llm_tool(
        name="cca_code_search",
        description="在用户授权的本地代码目录里搜索内容，返回文件、行号与已脱敏的代码行。用于定位符号、报错或配置项。",
        parameters=CCA_SEARCH_SCHEMA,
        timeout=30.0,
    )
    async def cca_code_search(self, *, query: Any = None, file_glob: Any = None) -> dict:
        # query 用 Any：LLM 偶尔违反 schema，硬类型会 TypeError（skill 44 §5）
        if not query:
            return {"output": "query 不能为空", "is_error": True}
        outcome = await self.grep_code(
            pattern=str(query), file_glob=str(file_glob or ""), max_matches=20
        )
        error = error_of(outcome)
        if error:
            return {"output": error, "is_error": True}
        return {"output": payload_of(outcome), "is_error": False}
