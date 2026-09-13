"""交付包冒烟测试 —— 验证「打包出来的东西真的装得上、跑得起来」。

做的四件事（对照官方 install.py / inspect.py 的检查点）：
  1. 包结构：根目录 manifest.toml / metadata.toml 存在，payload/plugins/<id>/ 存在
  2. 包级清单字段：package_type / id / version 非空，id 是安全路径段
  3. payload 完整性：按官方算法重算 sha256，与 metadata.toml 比对
  4. 功能：解压出插件源码 → 用 SDK 替身加载入口类 → 跑一遍核心入口

用法：python tools/smoke_package.py [包路径]
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import re
import sys
import tempfile
import tomllib
import unicodedata
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from _fake_sdk import FakeConfig, install  # noqa: E402

install()

SAMPLE = '''TOKEN = "sk-abcdefghijklmnop1234567890"


def compute(items):
    # TODO: validate input
    total = 0
    for item in items:
        total += item
    return total
'''

_SAFE_PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def read_toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def payload_hash_of(payload_dir: Path) -> str:
    """官方 compute_payload_hash：按 NFC posix 相对路径排序，path+NUL+content+NUL。"""
    digest = hashlib.sha256()
    entries = [
        (unicodedata.normalize("NFC", path.relative_to(payload_dir).as_posix()), path)
        for path in payload_dir.rglob("*")
        if not path.is_dir()
    ]
    for relative, path in sorted(entries, key=lambda item: item[0]):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_entry_class(source_dir: Path, entry: str):
    module_path, class_name = entry.rsplit(":", 1)
    package = module_path.rsplit(".", 1)[-1]
    spec = importlib.util.spec_from_file_location(
        package, source_dir / "__init__.py", submodule_search_locations=[str(source_dir)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[package] = module
    spec.loader.exec_module(module)
    return getattr(module, class_name)


async def smoke(package_path: Path) -> int:
    failures: list[str] = []

    def expect(condition: bool, message: str, detail: str = "") -> None:
        if condition:
            print(f"  ✓ {message}{('：' + detail) if detail else ''}")
        else:
            failures.append(f"{message}（{detail}）")

    with tempfile.TemporaryDirectory(prefix="cca-smoke-") as tmp:
        extract_dir = Path(tmp) / "extracted"
        with zipfile.ZipFile(package_path) as archive:
            archive.extractall(extract_dir)

        # ── 1. 包结构 ──
        expect((extract_dir / "manifest.toml").is_file(), "包根目录存在 manifest.toml")
        expect((extract_dir / "metadata.toml").is_file(), "包根目录存在 metadata.toml")
        payload_dir = extract_dir / "payload"
        expect((payload_dir / "dependencies.toml").is_file(), "payload/dependencies.toml 存在")

        # ── 2. 包级清单 ──
        manifest = read_toml(extract_dir / "manifest.toml")
        package_id = str(manifest.get("id", ""))
        package_type = str(manifest.get("package_type", ""))
        version = str(manifest.get("version", ""))
        expect(package_type == "plugin", "package_type 合法", package_type)
        expect(bool(package_id) and bool(_SAFE_PACKAGE_ID_RE.fullmatch(package_id)), "id 是安全包 ID", package_id)
        expect(bool(version), "version 非空", version)

        plugin_dir = payload_dir / "plugins" / package_id
        expect(plugin_dir.is_dir(), f"payload/plugins/{package_id}/ 存在")
        expect((plugin_dir / "plugin.toml").is_file(), "插件源码含 plugin.toml")

        # ── 3. payload 完整性 ──
        metadata = read_toml(extract_dir / "metadata.toml")
        expected = str(metadata.get("payload", {}).get("hash", ""))
        actual = payload_hash_of(payload_dir)
        expect(bool(expected) and expected == actual, "payload sha256 与 metadata.toml 一致", actual[:16] + "…")

        # ── 4. 功能 ──
        project = Path(tmp) / "project"
        project.mkdir()
        target = project / "core.py"
        target.write_text(SAMPLE, encoding="utf-8")

        entry = str(read_toml(plugin_dir / "plugin.toml").get("plugin", {}).get("entry", ""))
        cls = load_entry_class(plugin_dir, entry)
        plugin = cls(None)
        plugin.config = FakeConfig(
            {
                "catgirl_code_assistance": {
                    "readable_roots": [str(project)],
                    "enable_write": True,
                    "allow_modify": True,
                }
            }
        )

        started = await plugin.startup()
        expect(started.ok, "插件启动", getattr(started, "error", "") or "ok")
        await plugin._scan_background(force=True)

        async def check(label: str, coro, assertion) -> None:
            result = await coro
            if not result.ok:
                failures.append(f"{label} 返回错误：{result.error}")
                return
            ok, detail = assertion(result.value)
            expect(ok, label, detail)

        await check(
            "get_workspace_status",
            plugin.get_workspace_status(),
            lambda v: (v["index"]["files"] >= 1, f"索引 {v['index']['files']} 个文件"),
        )
        await check(
            "read_code_file（脱敏）",
            plugin.read_code_file(path=str(target)),
            lambda v: (
                "sk-abcdefghijklmnop1234567890" not in v["content"] and "compute" in v["content"],
                "明文密钥已被本地过滤",
            ),
        )
        await check(
            "code_outline",
            plugin.code_outline(path=str(target)),
            lambda v: ("compute" in {s["name"] for s in v["symbols"]}, f"{v['symbol_count']} 个符号"),
        )
        await check(
            "review_code",
            plugin.review_code(path=str(target)),
            lambda v: (v["count"] >= 1, f"{v['count']} 条改进建议"),
        )
        await check(
            "grep_code",
            plugin.grep_code(pattern="total \\+="),
            lambda v: (v["count"] >= 1, f"命中 {v['count']} 条"),
        )
        await check(
            "search_files",
            plugin.search_files(pattern="core"),
            lambda v: (v["count"] == 1, f"找到 {v['count']} 个文件"),
        )
        await check(
            "propose_edit",
            plugin.propose_edit(path=str(target), old_text="return total", new_text="return total * 2"),
            lambda v: ("+    return total * 2" in v["diff"], "diff 预览正常"),
        )

        applied = await plugin.apply_edit(path=str(target), old_text="return total", new_text="return total * 2")
        expect(applied.ok, "apply_edit", getattr(applied, "error", "") or "已落盘")
        undone = await plugin.undo_edit()
        expect(
            undone.ok and "return total * 2" not in target.read_text(encoding="utf-8"),
            "undo_edit",
            "文件已还原",
        )
        await check(
            "remember_note",
            plugin.remember_note(topic="smoke", content="ok"),
            lambda v: (v["saved"] is True, "笔记已写入本地 store"),
        )
        await plugin.shutdown()

    if failures:
        print()
        for line in failures:
            print(f"  ✗ {line}")
        print(f"\n冒烟测试失败：{len(failures)} 项")
        return 1
    print("\n冒烟测试全部通过 ✓")
    return 0


def main() -> int:
    package = sys.argv[1] if len(sys.argv) > 1 else None
    if package:
        target = Path(package)
        if not target.is_file():
            print(f"包不存在：{target}")
            return 1
    else:
        candidates = sorted((ROOT / "dist").glob("*.neko-plugin"))
        if not candidates:
            print("没有找到 dist/*.neko-plugin，请先运行 python tools/build_plugin.py")
            return 1
        target = candidates[-1]
    print(f"冒烟测试：{target.name}")
    return asyncio.run(smoke(target))


if __name__ == "__main__":
    sys.exit(main())
