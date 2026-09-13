"""交付包冒烟测试 —— 验证「打包出来的东西真的能跑」。

流程：把 dist/*.neko-plugin 解压到临时目录 → 用 SDK 替身加载入口类 →
在临时项目里跑一遍核心入口（读 / 大纲 / 审查 / 搜索 / 预览 diff / 落盘 / 撤销）。

用法：python tools/verify_package.py [包路径]
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
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
    with tempfile.TemporaryDirectory(prefix="cca-smoke-") as tmp:
        extract_dir = Path(tmp) / "extracted"
        with zipfile.ZipFile(package_path) as archive:
            archive.extractall(extract_dir)

        manifest = json.loads((extract_dir / "manifest.json").read_text(encoding="utf-8"))
        plugin_id = manifest["id"]
        print(f"  ✓ 包已解压：{plugin_id} v{manifest['version']}（{len(manifest['files'])} 个文件）")

        project = Path(tmp) / "project"
        project.mkdir()
        target = project / "core.py"
        target.write_text(SAMPLE, encoding="utf-8")

        cls = load_entry_class(extract_dir, manifest["entry"])
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
        if not started.ok:
            failures.append(f"startup 失败：{started.error}")
        print("  ✓ 插件启动")

        await plugin._scan_background(force=True)

        async def check(label: str, coro, assertion) -> None:
            result = await coro
            if not result.ok:
                failures.append(f"{label} 返回错误：{result.error}")
                return
            ok, detail = assertion(result.value)
            if ok:
                print(f"  ✓ {label}：{detail}")
            else:
                failures.append(f"{label} 断言失败：{detail}")

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
        if not applied.ok:
            failures.append(f"apply_edit 失败：{applied.error}")
        else:
            print(f"  ✓ apply_edit：替换 {applied.value['replacements']} 处")
        undone = await plugin.undo_edit()
        if not undone.ok or "return total * 2" in target.read_text(encoding="utf-8"):
            failures.append(f"undo_edit 失败：{undone}")
        else:
            print("  ✓ undo_edit：文件已还原")

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
