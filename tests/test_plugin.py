"""端到端测试：真实插件实例 + 假 SDK，跑完所有 AI 入口。

运行（仓库根目录）：
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _path in (_ROOT, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from _fake_sdk import FakeConfig, install  # noqa: E402

install()  # 必须先注入 SDK 替身，再导入插件包

from catgirl_code_assistance import CatgirlCodeAssistancePlugin  # noqa: E402

PY_SOURCE = '''"""Sample module."""


API_TOKEN = "sk-abcdefghijklmnop1234567890"


def add(a, b):
    # TODO: add type hints so mypy can actually help us here
    return a + b


class Calculator:
    """Very serious calculator."""

    def multiply(self, a, b):
        result = 0
        for _ in range(b):
            result += a
        return result
'''

C_SOURCE = """#include <stdio.h>
#include <string.h>

int copy_string(char *dst, const char *src) {
    strcpy(dst, src);
    return 0;
}
"""


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def build_project(root: str) -> None:
    os.makedirs(os.path.join(root, "app"), exist_ok=True)
    with open(os.path.join(root, "app", "calc.py"), "w", encoding="utf-8") as handle:
        handle.write(PY_SOURCE)
    with open(os.path.join(root, "app", "driver.c"), "w", encoding="utf-8") as handle:
        handle.write(C_SOURCE)
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as handle:
        handle.write("# demo\n")
    os.makedirs(os.path.join(root, "node_modules"), exist_ok=True)
    with open(os.path.join(root, "node_modules", "junk.py"), "w", encoding="utf-8") as handle:
        handle.write("raise SystemExit\n")


class PluginEndToEndTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = tempfile.mkdtemp(prefix="cca-project-")
        self.outside = tempfile.mkdtemp(prefix="cca-private-")
        build_project(self.root)
        with open(os.path.join(self.outside, "secret.py"), "w", encoding="utf-8") as handle:
            handle.write('PRIVATE = "should-never-be-read"\n')

        self.plugin = CatgirlCodeAssistancePlugin(None)
        self.plugin.config = FakeConfig(
            {"catgirl_code_assistance": {"readable_roots": [self.root], "enable_write": False}}
        )
        started = await self.plugin.startup()
        self.assertTrue(started.ok, getattr(started, "error", ""))
        await self.plugin._scan_background(force=True)  # 确定性：等索引真正跑完

    async def asyncTearDown(self):
        await self.plugin.shutdown()
        self.plugin.db.conn.close()
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)
        shutil.rmtree(self.plugin._workdir, ignore_errors=True)

    # ── 基础设施 ──
    def test_routers_are_bound(self):
        self.assertIn("WorkspaceRouter", self.plugin._router_names)
        self.assertTrue(hasattr(self.plugin, "grep_code"))
        self.assertTrue(hasattr(self.plugin, "read_code_file"))
        self.assertTrue(hasattr(self.plugin, "cca_code_search"))

    async def test_startup_and_status(self):
        result = await self.plugin.get_workspace_status()
        self.assertTrue(result.ok)
        payload = result.value
        self.assertEqual(payload["root_count"], 1)
        self.assertGreaterEqual(payload["index"]["files"], 3)
        self.assertFalse(payload["permissions"]["enable_write"])
        self.assertTrue(payload["permissions"]["redact_sensitive"])

    # ── 探索 ──
    async def test_search_files_returns_absolute_paths(self):
        result = await self.plugin.search_files(pattern="calc")
        self.assertTrue(result.ok)
        self.assertEqual(result.value["count"], 1)
        self.assertTrue(os.path.isabs(result.value["files"][0]["path"]))

    async def test_blocked_dirs_are_skipped(self):
        result = await self.plugin.search_files(pattern="junk")
        self.assertTrue(result.ok)
        self.assertEqual(result.value["count"], 0)

    async def test_grep_and_redaction(self):
        result = await self.plugin.grep_code(pattern="API_TOKEN")
        self.assertTrue(result.ok)
        matches = result.value["matches"]
        self.assertEqual(len(matches), 1)
        self.assertNotIn("sk-abcdefghijklmnop1234567890", matches[0]["text"])
        self.assertIn("[REDACTED:", matches[0]["text"])
        self.assertTrue(result.value["redacted"])

    async def test_read_outside_root_is_rejected(self):
        result = await self.plugin.read_code_file(path=os.path.join(self.outside, "secret.py"))
        self.assertFalse(result.ok)
        self.assertIn("不在允许的可读文件夹内", str(result.error))

    async def test_relative_path_is_rejected(self):
        result = await self.plugin.list_directory(path="app")
        self.assertFalse(result.ok)
        self.assertIn("绝对路径", str(result.error))

    async def test_list_directory_lists_entries(self):
        result = await self.plugin.list_directory(path=self.root, depth=2)
        self.assertTrue(result.ok)
        names = {entry["name"] for entry in result.value["entries"]}
        self.assertIn("calc.py", names)
        self.assertIn("driver.c", names)

    # ── 阅读 ──
    async def test_read_code_file_redacts_secrets(self):
        path = os.path.join(self.root, "app", "calc.py")
        result = await self.plugin.read_code_file(path=path)
        self.assertTrue(result.ok)
        self.assertNotIn("sk-abcdefghijklmnop1234567890", result.value["content"])
        self.assertEqual(result.value["language"], "python")
        self.assertTrue(result.value["redacted_hits"])

    async def test_code_outline_lists_symbols(self):
        path = os.path.join(self.root, "app", "calc.py")
        result = await self.plugin.code_outline(path=path)
        self.assertTrue(result.ok)
        kinds = {s["name"]: s["kind"] for s in result.value["symbols"]}
        self.assertEqual(kinds["Calculator"], "class")
        self.assertEqual(kinds["add"], "function")

    async def test_c_language_detected(self):
        path = os.path.join(self.root, "app", "driver.c")
        result = await self.plugin.code_outline(path=path)
        self.assertTrue(result.ok)
        self.assertEqual(result.value["language"], "c")

    async def test_review_reports_issues(self):
        path = os.path.join(self.root, "app", "calc.py")
        result = await self.plugin.review_code(path=path)
        self.assertTrue(result.ok)
        rules = {f["rule"] for f in result.value["findings"]}
        self.assertIn("todo_marker", rules)
        self.assertIn("secret:openai_key", rules)
        self.assertGreaterEqual(result.value["summary"]["high"], 1)

    async def test_c_review_flags_strcpy(self):
        path = os.path.join(self.root, "app", "driver.c")
        result = await self.plugin.review_code(path=path)
        self.assertTrue(result.ok)
        rules = {f["rule"] for f in result.value["findings"]}
        self.assertIn("unsafe_copy", rules)

    async def test_explain_code_bundle(self):
        path = os.path.join(self.root, "app", "calc.py")
        result = await self.plugin.explain_code(path=path, symbol="multiply")
        self.assertTrue(result.ok)
        self.assertIn("outline", result.value)
        self.assertTrue(result.value["focus"]["hits"])
        self.assertIn("multiply", result.value["content"])

    async def test_find_symbol(self):
        result = await self.plugin.find_symbol_entry(name="multiply")
        self.assertTrue(result.ok)
        self.assertGreaterEqual(result.value["count"], 1)

    # ── 编辑 ──
    async def test_propose_edit_is_readonly(self):
        path = os.path.join(self.root, "app", "calc.py")
        before = read_text(path)
        result = await self.plugin.propose_edit(path=path, old_text="return a + b", new_text="return b + a")
        self.assertTrue(result.ok)
        self.assertFalse(result.value["applied"])
        self.assertIn("-    return a + b", result.value["diff"])
        self.assertEqual(read_text(path), before)

    async def test_apply_edit_requires_permission(self):
        path = os.path.join(self.root, "app", "calc.py")
        result = await self.plugin.apply_edit(path=path, old_text="return a + b", new_text="return b + a")
        self.assertFalse(result.ok)
        self.assertIn("写操作总开关", str(result.error))

    async def test_apply_edit_and_undo(self):
        path = os.path.join(self.root, "app", "calc.py")
        original = read_text(path)

        await self.plugin.update_settings(config={"enable_write": True, "allow_modify": True})
        applied = await self.plugin.apply_edit(path=path, old_text="return a + b", new_text="return b + a")
        self.assertTrue(applied.ok, getattr(applied, "error", ""))
        self.assertEqual(applied.value["replacements"], 1)
        self.assertIn("return b + a", read_text(path))
        self.assertTrue(os.path.isfile(applied.value["backup"]))

        history = await self.plugin.list_edit_history()
        self.assertTrue(history.ok)
        self.assertEqual(history.value["count"], 1)

        undone = await self.plugin.undo_edit()
        self.assertTrue(undone.ok, getattr(undone, "error", ""))
        self.assertEqual(read_text(path), original)

    async def test_create_file_requires_permission(self):
        target = os.path.join(self.root, "app", "new_module.py")
        denied = await self.plugin.create_file(path=target, content="x = 1\n")
        self.assertFalse(denied.ok)
        self.assertIn("写操作总开关", str(denied.error))

        await self.plugin.update_settings(config={"enable_write": True, "allow_create": True})
        created = await self.plugin.create_file(path=target, content="x = 1\n")
        self.assertTrue(created.ok, getattr(created, "error", ""))
        self.assertTrue(os.path.isfile(target))

    async def test_edit_outside_root_is_rejected(self):
        target = os.path.join(self.outside, "secret.py")
        await self.plugin.update_settings(config={"enable_write": True, "allow_modify": True})
        result = await self.plugin.apply_edit(path=target, old_text="PRIVATE", new_text="PUBLIC")
        self.assertFalse(result.ok)
        self.assertIn("不在允许的可读文件夹内", str(result.error))

    # ── 笔记 ──
    async def test_notes_roundtrip(self):
        saved = await self.plugin.remember_note(topic="env", content="环境变量放在 deploy/env.sh")
        self.assertTrue(saved.ok)
        listed = await self.plugin.list_notes()
        self.assertTrue(listed.ok)
        self.assertEqual(listed.value["count"], 1)
        await self.plugin.forget_note(topic="env")
        listed = await self.plugin.list_notes()
        self.assertEqual(listed.value["count"], 0)

    # ── UI ──
    async def test_ui_context(self):
        context = await self.plugin.workspace_context()
        self.assertIn("config", context)
        self.assertIn("status", context)
        self.assertEqual(context["status"]["root_count"], 1)
        self.assertFalse(context["status"]["write_enabled"])
        self.assertGreaterEqual(context["status"]["index_files"], 3)

    async def test_update_settings_persists(self):
        result = await self.plugin.update_settings(
            config={"readable_roots": [self.root], "max_file_kb": 1024, "reply_style": "tsundere"}
        )
        self.assertTrue(result.ok)
        self.assertEqual(self.plugin.settings.max_file_kb, 1024)
        self.assertEqual(self.plugin.settings.reply_style, "tsundere")
        self.assertEqual(len(self.plugin.settings.readable_roots), 1)

    async def test_update_settings_rejects_non_object(self):
        result = await self.plugin.update_settings(config="not-a-dict")
        self.assertFalse(result.ok)

    # ── LLM 工具 ──
    async def test_llm_tool_returns_plain_dict(self):
        outcome = await self.plugin.cca_code_search(query="multiply")
        self.assertIsInstance(outcome, dict)
        self.assertFalse(outcome["is_error"])
        self.assertIn("matches", outcome["output"])

        bad = await self.plugin.cca_code_search(query="")
        self.assertTrue(bad["is_error"])

    # ── 生命周期 ──
    async def test_config_change_and_freeze(self):
        self.plugin.config = FakeConfig(
            {"catgirl_code_assistance": {"readable_roots": [self.root], "enable_write": True}}
        )
        reloaded = await self.plugin.on_config_change()
        self.assertTrue(reloaded.ok)
        self.assertTrue(self.plugin.settings.enable_write)

        await self.plugin.on_freeze()
        self.assertTrue(self.plugin._frozen)
        await self.plugin.on_unfreeze()
        self.assertFalse(self.plugin._frozen)

    async def test_reload_lifecycle(self):
        result = await self.plugin.on_reload()
        self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main()
