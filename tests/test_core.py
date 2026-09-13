"""core 层单元测试（不依赖 SDK，纯逻辑）。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _path in (_ROOT, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# 必须先注入 SDK 替身，`catgirl_code_assistance` 包才会被父包的 __init__ 拉起来
from _fake_sdk import install  # noqa: E402

install()

from catgirl_code_assistance.core import (  # noqa: E402
    PathGuard,
    Redactor,
    Settings,
    persona_error,
    persona_line,
)
from catgirl_code_assistance.core.analyzer import build_outline, review_source
from catgirl_code_assistance.core.patch import build_diff, compute_replacement


class SettingsTest(unittest.TestCase):
    def test_defaults_are_secure(self):
        s = Settings.from_mapping({})
        self.assertEqual(s.readable_roots, [])
        self.assertFalse(s.enable_write)
        self.assertFalse(s.allow_modify)
        self.assertFalse(s.allow_create)
        self.assertTrue(s.redact_sensitive)

    def test_dirty_values_are_coerced(self):
        s = Settings.from_mapping(
            {
                "readable_roots": "D:\\a\nE:\\b",
                "blocked_dirs": None,
                "allowed_extensions": ["PY", ".C"],
                "max_file_kb": "999999",
                "max_matches": -5,
                "enable_write": "yes",
                "allow_modify": 1,
                "reply_style": "unknown",
                "index_limit": 0,
            }
        )
        self.assertEqual(s.readable_roots, ["D:\\a", "E:\\b"])
        self.assertIn(".py", s.allowed_extensions)
        self.assertIn(".c", s.allowed_extensions)
        self.assertEqual(s.max_file_kb, 8192)          # 上限裁剪
        self.assertEqual(s.max_matches, 1)             # 下限裁剪
        self.assertTrue(s.enable_write)
        self.assertTrue(s.allow_modify)
        self.assertEqual(s.reply_style, "gentle")      # 非法值回落默认
        self.assertEqual(s.index_limit, 100)

    def test_write_allowed_needs_both_switches(self):
        s = Settings.from_mapping({"enable_write": True})
        self.assertFalse(s.write_allowed("modify"))
        s.allow_modify = True
        self.assertTrue(s.write_allowed("modify"))

    def test_ext_allowed(self):
        s = Settings.from_mapping({})
        self.assertTrue(s.ext_allowed("a/b/main.py"))
        self.assertTrue(s.ext_allowed("kernel/driver.c"))
        self.assertFalse(s.ext_allowed("logo.png"))


class PathGuardTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cca-root-")
        self.outside = tempfile.mkdtemp(prefix="cca-outside-")
        self.settings = Settings.from_mapping({"readable_roots": [self.root]})
        self.guard = PathGuard(self.settings)

    def test_rejects_relative_path(self):
        result = self.guard.check("src/main.py", need="any", must_exist=False)
        self.assertFalse(result.ok)
        self.assertIn("绝对路径", result.error)

    def test_rejects_path_outside_root(self):
        result = self.guard.check(os.path.join(self.outside, "secret.c"), need="any", must_exist=False)
        self.assertFalse(result.ok)
        self.assertIn("不在允许的可读文件夹内", result.error)

    def test_rejects_blocked_dir(self):
        blocked = os.path.join(self.root, "node_modules", "index.py")
        result = self.guard.check(blocked, need="any", must_exist=False)
        self.assertFalse(result.ok)
        self.assertIn("屏蔽目录", result.error)

    def test_accepts_root_itself(self):
        result = self.guard.check(self.root, need="dir")
        self.assertTrue(result.ok, result.error)

    def test_rejects_symlink_escape(self):
        link = os.path.join(self.root, "escape_link")
        target = os.path.join(self.outside, "linked")
        os.makedirs(target, exist_ok=True)
        if os.name == "nt":
            self.skipTest("Windows 下需要管理员权限才能创建符号链接")
        os.symlink(target, link)
        result = self.guard.check(link, need="any")
        self.assertFalse(result.ok)
        self.assertIn("符号链接", result.error)

    def test_no_roots_configured(self):
        guard = PathGuard(Settings.from_mapping({}))
        result = guard.check(self.root, need="dir")
        self.assertFalse(result.ok)
        self.assertIn("还没有配置可读文件夹", result.error)


class RedactorTest(unittest.TestCase):
    def setUp(self):
        self.redactor = Redactor([])

    def test_redacts_credentials(self):
        text = 'api_key = "sk-abcdefghijklmnop123456"\npassword = "hunter2hunter2"\n'
        cleaned, counts = self.redactor.redact(text)
        self.assertNotIn("sk-abcdefghijklmnop123456", cleaned)
        self.assertIn("[REDACTED:", cleaned)
        self.assertTrue(counts)

    def test_redacts_private_key_block(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"
        cleaned, counts = self.redactor.redact(text)
        self.assertIn("PRIVATE_KEY", cleaned)
        self.assertIn("private_key", counts)

    def test_findings_report_line_numbers(self):
        findings = self.redactor.findings("email = 'a@b.com'\nphone = 13800138000\n")
        rules = {f["rule"] for f in findings}
        self.assertIn("email", rules)
        self.assertTrue(all(f["line"] >= 1 for f in findings))

    def test_custom_pattern(self):
        redactor = Redactor([r"SECRET-[A-Z]{4}"])
        cleaned, counts = redactor.redact("token SECRET-ABCD here")
        self.assertIn("[REDACTED:CUSTOM_0]", cleaned)
        self.assertIn("custom_0", counts)

    def test_bad_custom_pattern_is_ignored(self):
        redactor = Redactor(["([unclosed"])
        cleaned, _counts = redactor.redact("plain text")
        self.assertEqual(cleaned, "plain text")


class AnalyzerTest(unittest.TestCase):
    PY = '''import os


class Greeter:
    """A greeter."""

    def greet(self, name):
        return f"hi {name} {os.getpid()}"


def helper(items=[]):
    try:
        eval("1+1")
    except:
        pass
    return items
'''

    def test_python_outline(self):
        outline = build_outline("demo.py", self.PY, "python")
        names = {s["name"]: s["kind"] for s in outline["symbols"]}
        self.assertEqual(names.get("Greeter"), "class")
        self.assertEqual(names.get("helper"), "function")
        self.assertEqual(outline["language"], "python")

    def test_c_outline(self):
        source = "#include <stdio.h>\nint main(void) {\n    return 0;\n}\n"
        outline = build_outline("main.c", source, "c")
        self.assertEqual(outline["language"], "c")
        self.assertIn("main", {s["name"] for s in outline["symbols"]})

    def test_review_catches_issues(self):
        findings = review_source("demo.py", self.PY, Redactor([]))
        rules = {f["rule"] for f in findings}
        self.assertIn("bare_except", rules)
        self.assertIn("dynamic_exec", rules)
        self.assertIn("mutable_default", rules)
        self.assertTrue(all("suggestion" in f for f in findings))

    def test_review_handles_syntax_error(self):
        findings = review_source("bad.py", "def oops(:\n", Redactor([]))
        self.assertTrue(any(f["rule"] == "syntax_error" for f in findings))

    def test_review_flags_hardcoded_secret(self):
        source = 'TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz012345"\n'
        findings = review_source("cfg.py", source, Redactor([]))
        self.assertTrue(any(f["rule"].startswith("secret:") for f in findings))


class PatchTest(unittest.TestCase):
    def test_compute_replacement_exact(self):
        updated, hits, error = compute_replacement("a-b-a", "a", "X", count=1)
        self.assertEqual(hits, 1)
        self.assertEqual(updated, "X-b-a")
        self.assertEqual(error, "")

    def test_compute_replacement_missing(self):
        _updated, hits, error = compute_replacement("abc", "zzz", "X")
        self.assertEqual(hits, 0)
        self.assertIn("没有匹配", error)

    def test_compute_replacement_regex(self):
        updated, hits, _error = compute_replacement(
            "x = 1\ny = 2\n", r"(?m)^x = \d+$", "x = 42\n", use_regex=True
        )
        self.assertEqual(hits, 1)
        self.assertIn("x = 42", updated)

    def test_build_diff_has_context(self):
        diff = build_diff("a.py", "one\ntwo\nthree\n", "one\nTWO\nthree\n")
        self.assertIn("@@", diff)
        self.assertIn("-two", diff)
        self.assertIn("+TWO", diff)


class PersonaTest(unittest.TestCase):
    def test_styles(self):
        self.assertEqual(persona_line("professional", "hi"), "hi")
        self.assertTrue(persona_line("gentle", "hi").startswith("hi"))
        self.assertIn("hi", persona_line("tsundere", "hi"))
        self.assertIn("hi", persona_error("gentle", "hi"))


if __name__ == "__main__":
    unittest.main()
