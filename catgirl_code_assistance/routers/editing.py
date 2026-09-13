"""编辑 Router：预览 diff / 真实落盘 / 撤销 / 历史。

安全模型（两层权限 + 备份 + 撤销）：
  propose_edit  → 只读！产出 unified diff，供用户在聊天里确认，**不需要任何写权限**；
  apply_edit    → 需要 enable_write 且 allow_modify；写入前必定备份原文；
  create_file   → 需要 enable_write 且 allow_create；父目录必须已存在且在可读文件夹内；
  undo_edit     → 从备份回滚（备份存在插件私有目录，永不外传）；
  delete        → **故意不提供**：删除源码风险远大于收益，请用 IDE 自己管理。
"""
from __future__ import annotations

import os
import time
from typing import Any

from plugin.sdk.plugin import plugin_entry, Ok, Err, SdkError

from ..core import Settings, persona_error, persona_line
from ..core.util import truthy
from ..core.patch import (
    EditRecord,
    build_diff,
    compute_replacement,
    read_source,
    restore_backup,
    sha256_of,
    write_backup,
    write_source,
)

UNDO_STORE_KEY = "cca_undo_stack"

EDIT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "目标文件的完整绝对路径（必须先用 search_files 获取）"},
        "old_text": {"type": "string", "description": "要被替换的原文（必须与文件当前内容完全一致，含缩进）"},
        "new_text": {"type": "string", "description": "替换后的新内容"},
        "count": {"type": "integer", "description": "最多替换几处，0 或留空表示全部，默认 1"},
        "regex": {"type": "boolean", "description": "old_text 是否作为正则表达式使用，默认 false"},
    },
    "required": ["path", "old_text", "new_text"],
}

CREATE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "新文件的完整绝对路径"},
        "content": {"type": "string", "description": "文件内容"},
        "overwrite": {"type": "boolean", "description": "已存在时是否覆盖，默认 false"},
    },
    "required": ["path", "content"],
}


class EditRouter:
    # ─────────────────────────────────────────
    # 深函数：权限 / 撤销栈
    # ─────────────────────────────────────────
    def _require(self, action: str) -> str:
        settings: Settings = self.settings
        if not settings.enable_write:
            return (
                "写操作总开关未打开。请到插件设置面板勾选「允许修改文件」并保存，"
                "猫娘才会动你的代码喵。"
            )
        if not settings.write_allowed(action):
            names = {"create": "创建新文件", "modify": "修改已有文件", "delete": "删除文件"}
            return f"未授权「{names.get(action, action)}」。请在设置面板逐项开放后再操作。"
        if self._frozen:
            return "插件正处于冻结状态，暂时不能写入。"
        return ""

    async def _push_undo(self, record: EditRecord) -> None:
        self._undo_stack.append(record.to_dict())
        overflow = len(self._undo_stack) - self.settings.max_undo_steps
        if overflow > 0:
            self._undo_stack = self._undo_stack[overflow:]
        await self.store.set(UNDO_STORE_KEY, list(self._undo_stack))

    async def _pop_undo(self) -> dict | None:
        if not self._undo_stack:
            return None
        record = self._undo_stack.pop()
        await self.store.set(UNDO_STORE_KEY, list(self._undo_stack))
        return record

    # ─────────────────────────────────────────
    # 入口 1：预览（只读，永远不落盘）
    # ─────────────────────────────────────────
    @plugin_entry(
        id="propose_edit",
        name="预览代码修改（不落盘）",
        description=(
            "把 old_text 替换成 new_text 生成 unified diff 预览，**不会修改任何文件**，也不需要写权限。"
            "⚠️ 约定：向用户提出改动时先用本入口展示 diff，用户同意后再调 apply_edit。"
            "old_text 必须与文件内容完全一致（含缩进和换行）；不一致时会返回错误，请用 read_code_file 取回最新内容。"
        ),
        input_schema=EDIT_SCHEMA,
        llm_result_fields=["path", "diff", "replacements", "preview"],
    )
    async def propose_edit(
        self,
        path: str = "",
        old_text: str = "",
        new_text: str = "",
        count: Any = 1,
        regex: Any = False,
        **_,
    ) -> Ok | Err:
        settings: Settings = self.settings
        check = self._guard().check(path, need="file")
        if not check.ok:
            return Err(SdkError(persona_error(settings.reply_style, check.error)))
        ok_current, current, err = await read_source(check.path, settings.max_file_kb * 1024)
        if not ok_current:
            return Err(SdkError(persona_error(settings.reply_style, err)))
        try:
            limit = max(0, int(count or 1))
        except (TypeError, ValueError):
            limit = 1

        updated, hits, error = compute_replacement(
            current, old_text, new_text, count=limit, use_regex=bool(regex)
        )
        if error:
            return Err(SdkError(persona_error(settings.reply_style, error)))
        diff = build_diff(check.path, current, updated)
        return Ok(
            {
                "path": check.path,
                "replacements": hits,
                "diff": diff[:20000],
                "preview": updated[: settings.max_context_lines * 60],
                "applied": False,
                "next_actions": [
                    "把 diff 展示给用户确认",
                    "确认后调用 apply_edit（需要已开启写权限）",
                ],
                "hint": persona_line(settings.reply_style, f"预览生成完毕，将替换 {hits} 处，还没写入磁盘"),
            }
        )

    # ─────────────────────────────────────────
    # 入口 2：真正落盘
    # ─────────────────────────────────────────
    @plugin_entry(
        id="apply_edit",
        name="应用代码修改",
        description=(
            "把 old_text 替换成 new_text 并写入文件（写入前自动备份，可用 undo_edit 回滚）。"
            "⚠️ 前置条件：(1) 必须先 propose_edit 让用户看过 diff；(2) 用户明确同意；"
            "(3) 设置面板已开启写权限（enable_write + allow_modify）。"
            "缺少任一条件都不要调用。"
        ),
        input_schema=EDIT_SCHEMA,
        llm_result_fields=["applied", "path", "replacements", "backup", "diff"],
    )
    async def apply_edit(
        self,
        path: str = "",
        old_text: str = "",
        new_text: str = "",
        count: Any = 1,
        regex: Any = False,
        **_,
    ) -> Ok | Err:
        settings: Settings = self.settings
        denied = self._require("modify")
        if denied:
            return Err(SdkError(persona_error(settings.reply_style, denied)))
        check = self._guard().check(path, need="file")
        if not check.ok:
            return Err(SdkError(persona_error(settings.reply_style, check.error)))

        try:
            limit = max(0, int(count or 1))
        except (TypeError, ValueError):
            limit = 1
        ok_current, current, err = await read_source(check.path, settings.max_file_kb * 1024)
        if not ok_current:
            return Err(SdkError(persona_error(settings.reply_style, err)))
        updated, hits, error = compute_replacement(
            current, old_text, new_text, count=limit, use_regex=bool(regex)
        )
        if error:
            return Err(SdkError(persona_error(settings.reply_style, error)))

        backup_dir = os.path.join(str(self.data_path("backups")), "")
        ok_backup, backup_target, backup_err = await write_backup(backup_dir, check.path, current)
        if not ok_backup:
            return Err(SdkError(persona_error(settings.reply_style, f"备份失败，已中止写入：{backup_err}")))
        ok_write, write_err = await write_source(check.path, updated)
        if not ok_write:
            return Err(SdkError(persona_error(settings.reply_style, f"写入失败：{write_err}")))

        record = EditRecord(
            path=check.path,
            action="modify",
            backup=backup_target,
            replacements=hits,
            timestamp=time.time(),
            old_sha256=sha256_of(current),
            new_sha256=sha256_of(updated),
        )
        await self._push_undo(record)
        return Ok(
            {
                "applied": True,
                "path": check.path,
                "replacements": hits,
                "backup": backup_target,
                "diff": build_diff(check.path, current, updated)[:20000],
                "hint": persona_line(settings.reply_style, f"已写入 {hits} 处修改，想反悔随时叫我用 undo_edit 回滚"),
            }
        )

    # ─────────────────────────────────────────
    # 入口 3：新建文件
    # ─────────────────────────────────────────
    @plugin_entry(
        id="create_file",
        name="创建源码文件",
        description=(
            "在可读文件夹内创建新文件。需要预先开启写权限（enable_write + allow_create），"
            "且父目录必须已存在。⚠️ 不建议让用户盲创建，先在聊天里确认路径与内容。"
        ),
        input_schema=CREATE_SCHEMA,
        llm_result_fields=["created", "path", "size"],
    )
    async def create_file(
        self, path: str = "", content: str = "", overwrite: Any = False, **_
    ) -> Ok | Err:
        settings: Settings = self.settings
        denied = self._require("create")
        if denied:
            return Err(SdkError(persona_error(settings.reply_style, denied)))
        parent = os.path.dirname(os.path.abspath(str(path or "")))
        check_parent = self._guard().check(parent, need="dir")
        if not check_parent.ok:
            return Err(SdkError(persona_error(settings.reply_style, check_parent.error)))
        target = os.path.normpath(os.path.join(check_parent.path, os.path.basename(str(path))))
        if not settings.ext_allowed(target):
            return Err(SdkError(persona_error(settings.reply_style, f"扩展名不在允许清单内：{os.path.basename(target)}")))
        if os.path.exists(target) and not truthy(overwrite, False):
            return Err(SdkError(persona_error(settings.reply_style, f"文件已存在：{target}。需要覆盖请显式传 overwrite=true")))

        existing = ""
        if os.path.exists(target):
            ok_read, existing, err = await read_source(target, settings.max_file_kb * 1024)
            if not ok_read:
                return Err(SdkError(persona_error(settings.reply_style, err)))
            ok_backup, _, backup_err = await write_backup(
                os.path.join(str(self.data_path("backups")), ""), target, existing
            )
            if not ok_backup:
                return Err(SdkError(persona_error(settings.reply_style, f"备份失败：{backup_err}")))
        ok_write, write_err = await write_source(target, content)
        if not ok_write:
            return Err(SdkError(persona_error(settings.reply_style, f"写入失败：{write_err}")))
        if existing:
            await self._push_undo(
                EditRecord(target, "create_overwrite", os.path.join(str(self.data_path("backups")), ""), 1, time.time())
            )
        return Ok(
            {
                "created": True,
                "path": target,
                "size": len((content or "").encode("utf-8")),
                "hint": persona_line(settings.reply_style, "新文件已经放好啦"),
            }
        )

    # ─────────────────────────────────────────
    # 入口 4：撤销
    # ─────────────────────────────────────────
    @plugin_entry(
        id="undo_edit",
        name="撤销上一次代码修改",
        description=(
            "回滚最近一次 apply_edit / create_file 造成的改动（从插件本地备份还原）。"
            "用途：用户说「改错了」「撤回」「还是原来那样吧」。无历史可撤时会明确告知。"
        ),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["undone", "path", "remaining"],
    )
    async def undo_edit(self, **_) -> Ok | Err:
        settings: Settings = self.settings
        denied = self._require("modify")
        if denied:
            return Err(SdkError(persona_error(settings.reply_style, denied)))
        record = await self._pop_undo()
        if not record:
            return Err(SdkError(persona_error(settings.reply_style, "没有可撤销的记录。")))
        ok_restore, err = await restore_backup(record.get("backup", ""), record.get("path", ""))
        if not ok_restore:
            await self._push_undo(EditRecord(**record))  # 失败要还回去，不能静默丢
            return Err(SdkError(persona_error(settings.reply_style, f"回滚失败：{err}")))
        return Ok(
            {
                "undone": True,
                "path": record.get("path", ""),
                "backup": record.get("backup", ""),
                "remaining": len(self._undo_stack),
                "hint": persona_line(settings.reply_style, "已经帮你还原到修改前的样子"),
            }
        )

    # ─────────────────────────────────────────
    # 入口 5：历史
    # ─────────────────────────────────────────
    @plugin_entry(
        id="list_edit_history",
        name="查看代码修改历史",
        description="列出本插件做过的编辑记录（时间、文件、替换次数、备份位置），用于确认改动轨迹。",
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["history", "count"],
    )
    async def list_edit_history(self, **_) -> Ok | Err:
        items = [
            {k: v for k, v in record.items() if k in ("path", "action", "timestamp", "replacements")}
            for record in reversed(self._undo_stack)
        ]
        return Ok(
            {
                "count": len(items),
                "history": items[:50],
                "can_undo": bool(self._undo_stack),
                "hint": persona_line(self.settings.reply_style, f"共 {len(items)} 条可撤销记录"),
            }
        )

    # ─────────────────────────────────────────
    # 启动期：恢复撤销栈
    # ─────────────────────────────────────────
    async def restore_undo_stack(self) -> None:
        saved = await self.store.get(UNDO_STORE_KEY, []) or []
        cleaned: list[dict] = []
        for item in saved:
            if isinstance(item, dict) and item.get("path") and item.get("backup"):
                if os.path.isfile(str(item.get("backup"))):
                    cleaned.append(item)
        self._undo_stack = cleaned[: self.settings.max_undo_steps]
