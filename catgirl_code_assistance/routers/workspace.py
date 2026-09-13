"""工作区 Router：设置面板数据 / 保存配置 / 重建索引 / 状态刷新。

双装饰器示范（skill 铁律 #12）：`@ui.action` 在上、`@plugin_entry` 在下，
这样同一个方法既能被面板按钮调用，也能被 AI 在聊天里直接调用。
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from plugin.sdk.plugin import plugin_entry, ui, Ok, Err, SdkError

from ..core import (
    Settings,
    persona_error,
    persona_line,
)
from ..core.indexer import walk_collect

UPDATE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "config": {
            "type": "object",
            "description": "要保存的配置项。未提供的字段保持原值。",
        }
    },
}


class WorkspaceRouter:
    """挂载到插件实例后，self 即插件实例（可访问 self.settings / self.index 等）。"""

    # ─────────────────────────────────────────
    # UI 数据
    # ─────────────────────────────────────────
    @ui.context(id="workspace")
    async def workspace_context(self) -> dict:
        settings: Settings = self.settings
        stats = await self.index.stats()
        return {
            "config": settings.to_mapping(),
            "status": {
                "ready": self._ready,
                "frozen": self._frozen,
                "root_count": len(settings.readable_roots),
                "roots": self._guard().roots_status()[:10],
                "index_files": stats.get("files", 0),
                "index_bytes": stats.get("bytes", 0),
                "index_backend": stats.get("backend", "live"),
                "index_error": stats.get("error", ""),
                "last_scan_at": stats.get("last_scan_at", 0),
                "scanning": self._scanning,
                "write_enabled": settings.enable_write,
                "redaction_enabled": settings.redact_sensitive,
                "undo_depth": len(self._undo_stack),
                "reply_style": settings.reply_style,
                "note_count": len(await self._notes()),
            },
        }

    # ─────────────────────────────────────────
    # 保存设置（UI 按钮 + AI 入口）
    # ─────────────────────────────────────────
    @ui.action(label="保存设置", tone="primary", refresh_context=True)
    @plugin_entry(
        id="update_settings",
        name="更新代码协助配置",
        description=(
            "更新猫娘代码协助的运行配置（可读文件夹、屏蔽目录、写权限、脱敏开关等）。"
            "⚠️ 只有在用户明确要求「修改/放开设置」时才调用；日常查代码不要用。"
            "参数 config 为对象，键名与当前配置一致；未提供的字段保持原值。"
        ),
        input_schema=UPDATE_SCHEMA,
        llm_result_fields=["saved", "config", "hint"],
    )
    async def update_settings(self, config: Any = None, **_) -> Ok | Err:
        try:
            if config is None:
                config = {}
            if isinstance(config, str):
                # LLM 偶尔把对象塞成字符串，做一次宽松解析
                import json

                try:
                    config = json.loads(config)
                except ValueError:
                    return Err(SdkError("config 解析失败：请传对象或 JSON 字符串。"))
            if not isinstance(config, dict):
                return Err(SdkError("config 必须是对象。"))

            current = self.settings.to_mapping()
            merged = dict(current)
            for key, value in config.items():
                key = str(key)
                if key in merged or key in ("readable_roots", "blocked_dirs", "allowed_extensions",
                                           "max_file_kb", "max_matches", "max_context_lines",
                                           "follow_symlinks", "enable_write", "allow_create",
                                           "allow_modify", "allow_delete", "redact_sensitive",
                                           "custom_redact_patterns", "index_enabled",
                                           "index_limit", "max_undo_steps", "reply_style"):
                    merged[key] = value
            # 白名单 + 类型归一化：脏数据不会污染运行态
            clean = Settings.from_mapping(merged)
            await self._save_config(clean.to_mapping())
            await self._reload_config()

            if clean.index_enabled and clean.readable_roots:
                self._spawn(self._scan_background(force=True))
            return Ok(
                {
                    "saved": True,
                    "config": clean.to_mapping(),
                    "hint": persona_line(
                        clean.reply_style,
                        self._t(
                            "hint.saved",
                            "设置已生效；可读文件夹之外的路径一律不可读，写权限默认关闭",
                        ),
                    ),
                }
            )
        except Exception as exc:  # 异常绝不冒泡出 entry
            self.logger.exception("update_settings failed")
            return Err(SdkError(str(exc)))

    # ─────────────────────────────────────────
    # 重建索引
    # ─────────────────────────────────────────
    @ui.action(label="重建索引", refresh_context=True)
    @plugin_entry(
        id="rebuild_index",
        name="重建代码索引",
        description=(
            "重新遍历所有已授权的可读文件夹并建立/刷新代码索引（只记录路径与元信息，不上传任何内容）。"
            "在用户说「重新扫描」「索引一下项目」「找不到新文件」时调用。"
        ),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["started", "roots", "hint"],
    )
    async def rebuild_index(self, **_) -> Ok | Err:
        settings: Settings = self.settings
        if not settings.readable_roots:
            return Err(
                SdkError(
                    persona_error(
                        settings.reply_style,
                        self._t("error.no_roots_for_scan", "还没有可读文件夹，先去设置面板填一个绝对路径吧"),
                    )
                )
            )
        if self._scanning:
            return Ok(
                {
                    "started": False,
                    "roots": settings.readable_roots,
                    "hint": persona_line(settings.reply_style, self._t("hint.scan_running", "索引正在重建中，稍等一下")),
                }
            )
        self._spawn(self._scan_background(force=True))
        return Ok(
            {
                "started": True,
                "roots": list(settings.readable_roots),
                "hint": persona_line(settings.reply_style, self._t("hint.scan_started", "已经开始重建索引，完成后可以直接让我搜索或读文件")),
            }
        )

    # ─────────────────────────────────────────
    # 刷新状态（UI）
    # ─────────────────────────────────────────
    @ui.action(label="刷新状态", refresh_context=True)
    async def refresh_status(self, **_) -> Ok | Err:
        await self._reload_config()
        return Ok({"refreshed": True})

    # ─────────────────────────────────────────
    # AI：查看当前工作区状态
    # ─────────────────────────────────────────
    @plugin_entry(
        id="get_workspace_status",
        name="查看代码协助工作区状态",
        description=(
            "查看猫娘当前能读哪些文件夹、索引了多少文件、写权限是否开放。"
            "⚠️ 当用户问「你能读哪些目录」「这个文件夹你读得到吗」「为什么读不到文件」时先调用本入口；"
            "这是排查路径相关问题的最快手段。"
        ),
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["roots", "index", "permissions", "next_actions"],
    )
    async def get_workspace_status(self, **_) -> Ok | Err:
        settings: Settings = self.settings
        stats = await self.index.stats()
        payload = {
            "roots": self._guard().roots_status(),
            "root_count": len(settings.readable_roots),
            "index": {
                "files": stats.get("files", 0),
                "bytes": stats.get("bytes", 0),
                "backend": stats.get("backend", "live"),
                "last_scan_at": stats.get("last_scan_at", 0),
                "scanning": self._scanning,
            },
            "permissions": {
                "enable_write": settings.enable_write,
                "allow_create": settings.allow_create,
                "allow_modify": settings.allow_modify,
                "allow_delete": settings.allow_delete,
                "redact_sensitive": settings.redact_sensitive,
            },
            "limits": {
                "max_file_kb": settings.max_file_kb,
                "max_matches": settings.max_matches,
                "max_context_lines": settings.max_context_lines,
            },
            "next_actions": [
                "用 search_files 按文件名/模糊名找文件（不要凭空猜路径）",
                "用 list_directory 看目录结构",
                "用 read_code_file 读取完整绝对路径的文件",
            ],
        }
        if not settings.readable_roots:
            payload["hint"] = persona_error(
                settings.reply_style,
                self._t("hint.no_roots", "还没有授权任何可读文件夹，请先到设置面板配置"),
            )
        else:
            payload["hint"] = persona_line(
                settings.reply_style,
                self._t(
                    "hint.roots_ready",
                    "已授权 {roots} 个根目录，索引 {files} 个文件",
                    roots=len(settings.readable_roots),
                    files=stats.get("files", 0),
                ),
            )
        return Ok(payload)

    # ─────────────────────────────────────────
    # 深函数：后台索引重建（供主类 / 本 Router 复用）
    # ─────────────────────────────────────────
    async def _scan_background(self, force: bool = False) -> dict:
        settings: Settings = self.settings
        if not settings.index_enabled:
            return {"skipped": True, "reason": "index_disabled"}
        if self._scanning and not force:
            return {"skipped": True, "reason": "already_scanning"}
        async with self._scan_lock:
            if self._scanning:
                return {"skipped": True, "reason": "already_scanning"}
            self._scanning = True
        summary = {"roots": 0, "files": 0, "skipped_roots": []}
        try:
            await self.index.ensure_tables()
            blocked = {d.lower() for d in settings.blocked_dirs}
            for root in settings.readable_roots:
                if not os.path.isdir(root):
                    summary["skipped_roots"].append(root)
                    continue
                entries = await asyncio.to_thread(
                    walk_collect, root, blocked, list(settings.allowed_extensions), settings.index_limit
                )
                written = await self.index.rebuild(entries, os.path.normpath(root))
                summary["roots"] += 1
                summary["files"] += written
                self.report_status(
                    {"phase": "indexing", "scanned": summary["files"], "root": root}
                )
            await self.index.set_meta("last_scan_at", str(time.time()))
            await self.store.set("cca_last_scan_at", time.time())
            summary["done"] = True
            self.report_status({"phase": "idle", "indexed": summary["files"]})
            self.logger.info("cca index rebuilt: %s files in %s roots", summary["files"], summary["roots"])
            return summary
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.exception("cca index rebuild failed")
            summary["error"] = str(exc)
            return summary
        finally:
            async with self._scan_lock:
                self._scanning = False
