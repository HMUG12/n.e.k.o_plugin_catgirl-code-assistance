"""阅读 Router：读文件 / 看结构 / 生成解释素材 / 本地代码审查 / 符号定位。

所有外发文本统一经过 `_safe_text()` —— 这是「敏感信息不出本地」的**唯一出口**。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from plugin.sdk.plugin import plugin_entry, Ok, Err, SdkError

from ..core import Settings, persona_error, persona_line
from ..core.analyzer import build_outline, detect_language, find_symbol, review_source
from ..core.reader import read_text_file

READ_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "文件的完整绝对路径（必须先用 search_files 获取）"},
        "start_line": {"type": "integer", "description": "起始行（1-based，含），留空从第一行开始"},
        "end_line": {"type": "integer", "description": "结束行（含），留空到文件末尾"},
    },
    "required": ["path"],
}

OUTLINE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "文件的完整绝对路径"},
    },
    "required": ["path"],
}

EXPLAIN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "文件的完整绝对路径"},
        "symbol": {"type": "string", "description": "只解释某个函数/类时可填它的名字（可选）"},
        "start_line": {"type": "integer", "description": "起始行（可选）"},
        "end_line": {"type": "integer", "description": "结束行（可选）"},
    },
    "required": ["path"],
}

REVIEW_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "要审查的文件的完整绝对路径"},
        "max_findings": {"type": "integer", "description": "最多返回多少条问题，默认 20"},
    },
    "required": ["path"],
}

SYMBOL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "函数 / 类 / 变量名"},
        "path": {"type": "string", "description": "限定在某个文件或目录的绝对路径内搜索（可选）"},
    },
    "required": ["name"],
}


class ReadingRouter:
    # ─────────────────────────────────────────
    # 深函数：读取 + 脱敏 + 限长
    # ─────────────────────────────────────────
    async def _load_source(
        self, path: str, start_line: int = 0, end_line: int = 0, *, keep_raw: bool = False
    ) -> tuple[dict, str]:
        """keep_raw=True 时不做脱敏 —— 仅供「本地审查」这类不外发的场景使用。"""
        settings: Settings = self.settings
        check = self._guard().check(path, need="file")
        if not check.ok:
            return {}, check.error
        if not settings.ext_allowed(check.path):
            return {}, f"扩展名不在允许清单内：{os.path.basename(check.path)}。可在设置面板的「允许扩展名」里添加。"

        try:
            start = int(start_line or 0)
            finish = int(end_line or 0)
        except (TypeError, ValueError):
            start, finish = 0, 0
        if start > 0 and finish > 0 and finish - start + 1 > settings.max_context_lines:
            finish = start + settings.max_context_lines - 1

        result = await read_text_file(check.path, settings.max_file_kb * 1024, start, finish)
        if not result.ok:
            return {}, result.error
        content, hits = (result.content, {}) if keep_raw else self._safe_text(result.content)
        if result.total_lines > settings.max_context_lines and not (start or finish):
            lines = content.splitlines()[: settings.max_context_lines]
            content = "\n".join(lines)
            truncated = True
        else:
            truncated = result.truncated
        return (
            {
                "path": check.path,
                "language": detect_language(check.path),
                "encoding": result.encoding,
                "size": result.size,
                "total_lines": result.total_lines,
                "start_line": max(1, start if start > 0 else 1),
                "end_line": min(result.total_lines, finish if finish > 0 else result.total_lines),
                "truncated": truncated,
                "content": content,
                "redacted_hits": hits,
                "redaction_enabled": settings.redact_sensitive,
            },
            "",
        )

    def _safe_text(self, text: str) -> tuple[str, dict]:
        """唯一外发出口：开启脱敏时必然在这里被过滤。"""
        settings: Settings = self.settings
        if not settings.redact_sensitive:
            return text, {}
        return self.redactor.redact(text)

    # ─────────────────────────────────────────
    # 入口 1：读文件
    # ─────────────────────────────────────────
    @plugin_entry(
        id="read_code_file",
        name="读取源码文件",
        description=(
            "读取单个源码文件的内容并返回带行号的正文（Python / C / C++ / Java / JS / Go / Rust 等均可）。"
            "⚠️ path 必须是完整绝对路径，请先用 search_files 获取；二进制文件会被拒绝。"
            "大文件会被截断到配置的行数上限，需要更多行请指定 start_line / end_line。"
        ),
        input_schema=READ_SCHEMA,
        llm_result_fields=["path", "language", "total_lines", "content", "truncated"],
    )
    async def read_code_file(
        self, path: str = "", start_line: Any = 0, end_line: Any = 0, **_
    ) -> Ok | Err:
        settings: Settings = self.settings
        payload, error = await self._load_source(path, start_line, end_line)
        if error:
            return Err(SdkError(persona_error(settings.reply_style, error)))
        payload["hint"] = persona_line(
            settings.reply_style, f"读好啦，共 {payload['total_lines']} 行"
        )
        payload["next_actions"] = [
            "用 code_outline 看整体结构",
            "用 review_code 让我指出可以改进的地方",
            "要改动用 propose_edit 先看 diff",
        ]
        return Ok(payload)

    # ─────────────────────────────────────────
    # 入口 2：结构大纲
    # ─────────────────────────────────────────
    @plugin_entry(
        id="code_outline",
        name="查看代码结构大纲",
        description=(
            "给出文件的结构大纲：类、函数、方法签名与行号（Python 走 AST，其它语言走正则）。"
            "用途：先整体理解文件，再决定读哪一段，比整篇读省 token。"
        ),
        input_schema=OUTLINE_SCHEMA,
        llm_result_fields=["language", "total_lines", "symbols"],
    )
    async def code_outline(self, path: str = "", **_) -> Ok | Err:
        settings: Settings = self.settings
        check = self._guard().check(path, need="file")
        if not check.ok:
            return Err(SdkError(persona_error(settings.reply_style, check.error)))
        result = await read_text_file(check.path, settings.max_file_kb * 1024)
        if not result.ok:
            return Err(SdkError(persona_error(settings.reply_style, result.error)))
        outline = await asyncio.to_thread(build_outline, check.path, result.content)
        outline["path"] = check.path
        outline["hint"] = persona_line(
            settings.reply_style, f"这里共有 {outline['symbol_count']} 个符号"
        )
        outline["next_actions"] = [
            "挑一个符号用 read_code_file 加 start_line/end_line 精读",
            "用 review_code 让我给出改进建议",
        ]
        return Ok(outline)

    # ─────────────────────────────────────────
    # 入口 3：解释素材包（给大模型用的上下文包）
    # ─────────────────────────────────────────
    @plugin_entry(
        id="explain_code",
        name="生成代码解释上下文",
        description=(
            "把「文件内容 + 结构大纲 + 局部审查结论」打包成一个上下文，供你在聊天里向用户解释代码。"
            "用途：用户说「帮我看看这段代码」「讲讲这个函数干嘛的」。"
            "symbol 参数可只聚焦某个函数/类，返回的 snippet 更少更省 token。"
        ),
        input_schema=EXPLAIN_SCHEMA,
        llm_result_fields=["path", "language", "outline", "focus", "findings"],
    )
    async def explain_code(
        self,
        path: str = "",
        symbol: str = "",
        start_line: Any = 0,
        end_line: Any = 0,
        **_,
    ) -> Ok | Err:
        settings: Settings = self.settings
        payload, error = await self._load_source(path, start_line, end_line, keep_raw=True)
        if error:
            return Err(SdkError(persona_error(settings.reply_style, error)))

        focus: dict = {}
        if str(symbol or "").strip():
            full = await read_text_file(payload["path"], settings.max_file_kb * 1024)
            if full.ok:
                hits = await asyncio.to_thread(
                    find_symbol, full.content, symbol.strip(), payload["language"]
                )
                hits = [{**h, "snippet": self._safe_text(h["snippet"])[0]} for h in hits]
                focus = {"name": symbol, "hits": hits}
                if hits:
                    payload["content"] = hits[0]["snippet"]

        findings = await asyncio.to_thread(
            review_source,
            payload["path"],
            payload["content"],
            self.redactor,
            max_findings=10,
            language=payload["language"],
        )
        outline = await asyncio.to_thread(build_outline, payload["path"], payload["content"], payload["language"])
        payload["content"] = self._safe_text(payload["content"])[0]  # 出门前补一道脱敏
        payload.update(
            {
                "outline": outline.get("symbols", [])[:80],
                "focus": focus,
                "findings": findings,
                "hint": persona_line(
                    settings.reply_style,
                    "素材已准备好（内容已本地脱敏），你可以据此向用户解释并给出改进建议",
                ),
            }
        )
        return Ok(payload)

    # ─────────────────────────────────────────
    # 入口 4：本地代码审查
    # ─────────────────────────────────────────
    @plugin_entry(
        id="review_code",
        name="审查代码并给出改进建议",
        description=(
            "对单个文件做本地静态审查，返回问题清单（行号 + 严重级别 + 改进建议）："
            "包含长函数、深层嵌套、TODO 残留、不安全的动态执行/命令调用、缓冲区不安全函数、疑似硬编码密钥等。"
            "⚠️ 只读分析，不会修改任何文件；真正的修改请用 propose_edit → apply_edit。"
        ),
        input_schema=REVIEW_SCHEMA,
        llm_result_fields=["findings", "summary", "next_actions"],
    )
    async def review_code(self, path: str = "", max_findings: Any = 20, **_) -> Ok | Err:
        settings: Settings = self.settings
        try:
            limit = max(1, min(int(max_findings or 20), 100))
        except (TypeError, ValueError):
            limit = 20
        # 用原文审查（否则密钥已被过滤，反而报不出来）；findings 的摘录本身会再次脱敏
        payload, error = await self._load_source(path, keep_raw=True)
        if error:
            return Err(SdkError(persona_error(settings.reply_style, error)))
        findings = await asyncio.to_thread(
            review_source,
            payload["path"],
            payload["content"],
            self.redactor,
            max_findings=limit,
            language=payload["language"],
        )
        summary = {"high": 0, "medium": 0, "low": 0}
        for item in findings:
            severity = item.get("severity", "low")
            summary[severity] = summary.get(severity, 0) + 1
        return Ok(
            {
                "path": payload["path"],
                "language": payload["language"],
                "summary": summary,
                "count": len(findings),
                "findings": findings,
                "next_actions": [
                    "挑一条问题，用 propose_edit 生成 diff 预览给用户看",
                    "用户确认后再调 apply_edit 真正落盘",
                ],
                "hint": persona_line(
                    settings.reply_style,
                    f"审查完成，高危 {summary['high']} 项、中危 {summary['medium']} 项",
                ),
            }
        )

    # ─────────────────────────────────────────
    # 入口 5：符号定位
    # ─────────────────────────────────────────
    @plugin_entry(
        id="find_symbol",
        name="定位符号定义",
        description=(
            "按名字定位函数/类/变量的定义位置，返回文件、行号与代码片段（已脱敏）。"
            "用途：「xxx 定义在哪」「把那个函数给我看看」。"
        ),
        input_schema=SYMBOL_SCHEMA,
        llm_result_fields=["hits", "count"],
    )
    async def find_symbol_entry(self, name: str = "", path: str = "", **_) -> Ok | Err:
        settings: Settings = self.settings
        if not str(name or "").strip():
            return Err(SdkError(persona_error(settings.reply_style, "name 不能为空")))
        explore = getattr(self, "_candidates", None)
        if path:
            check = self._guard().check(path, need="any")
            if not check.ok:
                return Err(SdkError(persona_error(settings.reply_style, check.error)))
            scope = check.path
            candidates = [scope] if os.path.isfile(scope) else []
            if os.path.isdir(scope):
                candidates = (await self._candidates(within=scope, limit=200))[0] if explore else []
        else:
            candidates = (await self._candidates(limit=200))[0] if explore else []

        hits: list[dict] = []
        for item in candidates[:200]:
            result = await read_text_file(item, settings.max_file_kb * 1024)
            if not result.ok:
                continue
            found = await asyncio.to_thread(
                find_symbol, result.content, name.strip(), detect_language(item)
            )
            for hit in found:
                snippet, counted = self._safe_text(hit["snippet"])
                hits.append({"path": item, "line": hit["line"], "snippet": snippet, "redacted": bool(counted)})
            if len(hits) >= 10:
                break
        return Ok(
            {
                "name": name,
                "count": len(hits),
                "hits": hits,
                "hint": persona_line(settings.reply_style, f"找到 {len(hits)} 处定义/引用"),
            }
        )
