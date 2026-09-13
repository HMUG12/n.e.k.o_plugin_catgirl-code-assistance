"""记忆 Router：跨会话的项目笔记（存在本地 store，不上传）。

用途示例：
  「记一下：这个项目的环境变量在 deploy/env.sh」→ remember_note
  「之前记的项目笔记有哪些？」→ list_notes
"""
from __future__ import annotations

import time

from plugin.sdk.plugin import Err, Ok, SdkError, plugin_entry

from ..core import Settings, persona_error, persona_line

NOTES_KEY = "cca_notes"
MAX_NOTES = 200
MAX_NOTE_CHARS = 2000

REMEMBER_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "topic": {"type": "string", "description": "笔记标题/主题，简短且唯一（同主题会覆盖）"},
        "content": {"type": "string", "description": "笔记内容，最多 2000 字"},
    },
    "required": ["topic", "content"],
}

FORGET_SCHEMA: dict = {
    "type": "object",
    "properties": {"topic": {"type": "string", "description": "要删除的笔记主题"}},
    "required": ["topic"],
}


class MemoryRouter:
    """所有笔记留在本地 store —— 不联网、不外发。"""

    async def _notes(self) -> dict:
        saved = await self.store.get(NOTES_KEY, {}) or {}
        return saved if isinstance(saved, dict) else {}

    async def _persist_notes(self, notes: dict) -> None:
        if len(notes) > MAX_NOTES:
            oldest = sorted(notes.items(), key=lambda kv: kv[1].get("updated_at", 0))[
                : len(notes) - MAX_NOTES
            ]
            for key, _ in oldest:
                notes.pop(key, None)
        await self.store.set(NOTES_KEY, notes)

    @plugin_entry(
        id="remember_note",
        name="记住一条项目笔记",
        description=(
            "把关于当前项目的约定、踩坑、目录说明等记下来，之后的会话依然记得（存在本地，不上传）。"
            "用途：用户说「记一下」「以后都按这个来」「记住这个项目用的是 xx 框架」。"
            "同 topic 会覆盖旧内容。"
        ),
        input_schema=REMEMBER_SCHEMA,
        llm_result_fields=["saved", "topic", "count"],
    )
    async def remember_note(self, topic: str = "", content: str = "", **_) -> Ok | Err:
        settings: Settings = self.settings
        topic = str(topic or "").strip()
        content = str(content or "")
        if not topic:
            return Err(SdkError(persona_error(settings.reply_style, "topic 不能为空")))
        if len(content) > MAX_NOTE_CHARS:
            content = content[:MAX_NOTE_CHARS]
        notes = await self._notes()
        notes[topic] = {"content": content, "updated_at": time.time()}
        await self._persist_notes(notes)
        return Ok(
            {
                "saved": True,
                "topic": topic,
                "count": len(notes),
                "hint": persona_line(settings.reply_style, f"记住啦，现在一共有 {len(notes)} 条笔记"),
            }
        )

    @plugin_entry(
        id="list_notes",
        name="查看项目笔记",
        description=(
            "列出所有记住的项目笔记（主题 + 内容摘要 + 更新时间）。"
            "用途：新会话开始时想起来这个项目之前聊过「记住 xx」，或用户问「你还记得吗」。"
        ),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["notes", "count"],
    )
    async def list_notes(self, **_) -> Ok | Err:
        notes = await self._notes()
        items = [
            {
                "topic": key,
                "content": str(value.get("content", "")),
                "updated_at": float(value.get("updated_at", 0) or 0),
            }
            for key, value in sorted(
                notes.items(), key=lambda kv: kv[1].get("updated_at", 0), reverse=True
            )
        ]
        return Ok(
            {
                "count": len(items),
                "notes": items[:50],
                "hint": persona_line(self.settings.reply_style, f"一共 {len(items)} 条笔记")
                if items
                else persona_error(self.settings.reply_style, "还没有记过东西"),
            }
        )

    @plugin_entry(
        id="forget_note",
        name="删除项目笔记",
        description="删除一条已记住的项目笔记（按 topic）。删除后无法恢复，请向用户确认后再调用。",
        input_schema=FORGET_SCHEMA,
        llm_result_fields=["deleted", "topic"],
    )
    async def forget_note(self, topic: str = "", **_) -> Ok | Err:
        settings: Settings = self.settings
        topic = str(topic or "").strip()
        notes = await self._notes()
        if topic not in notes:
            return Err(SdkError(persona_error(settings.reply_style, f"没有这条笔记：{topic}")))
        notes.pop(topic, None)
        await self._persist_notes(notes)
        return Ok(
            {
                "deleted": True,
                "topic": topic,
                "remaining": len(notes),
                "hint": persona_line(settings.reply_style, "已经忘掉啦（其实只是删掉了本地记录）"),
            }
        )
