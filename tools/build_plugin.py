"""打包 —— 产出官方格式的 `.neko-plugin` 交付包。

包结构（zip）：
    catgirl_code_assistance-1.0.0.neko-plugin
    ├── manifest.json     # 安装器元数据：id / version / entry / 文件清单 + sha256
    ├── plugin.toml       # 插件清单
    ├── __init__.py       # 入口类
    ├── core/  routers/   # 后端逻辑
    ├── ui/     i18n/     # 面板与翻译
    └── docs/guide.md     # 用户指南

排除项：__pycache__ / *.pyc / tsconfig.json（仅开发期用）/ tests / tools。

用法：
    python tools/build_plugin.py                 # 默认按 plugin.toml 的版本号
    python tools/build_plugin.py --version 1.0.1 # 指定版本
    python tools/build_plugin.py --no-check      # 跳过自检（不推荐）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_plugin import PLUGIN_DIR, ROOT, run_checks  # noqa: E402

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore

EXCLUDED_DIRS = {"__pycache__", ".mypy_cache", ".pytest_cache", ".git"}
EXCLUDED_FILES = {"tsconfig.json", ".DS_Store"}
EXCLUDED_SUFFIX = {".pyc", ".pyo"}


def load_manifest_meta() -> dict:
    if tomllib is None:
        raise SystemExit("需要 Python 3.11+ 才能解析 plugin.toml")
    data = tomllib.loads((PLUGIN_DIR / "plugin.toml").read_text(encoding="utf-8-sig"))
    plugin = data.get("plugin", {})
    return {
        "id": plugin.get("id", PLUGIN_DIR.name),
        "name": plugin.get("name", plugin.get("id", "")),
        "version": plugin.get("version", "0.0.0"),
        "description": plugin.get("description", ""),
        "short_description": plugin.get("short_description", ""),
        "keywords": plugin.get("keywords", []),
        "entry": plugin.get("entry", ""),
        "author": data.get("plugin", {}).get("author", {}),
        "sdk": data.get("plugin", {}).get("sdk", {}),
        "runtime": data.get("plugin_runtime", {}),
        "store": data.get("plugin", {}).get("store", {}),
        "database": data.get("plugin", {}).get("database", {}),
        "i18n": data.get("plugin", {}).get("i18n", {}),
        "ui": data.get("plugin", {}).get("ui", {}),
        "dependencies": data.get("plugin", {}).get("dependencies", {}),
        "sections": {k: v for k, v in data.items() if k not in ("plugin", "plugin_runtime")},
    }


def collect_files() -> list[Path]:
    files: list[Path] = []
    for path in sorted(PLUGIN_DIR.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(PLUGIN_DIR)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        if path.name in EXCLUDED_FILES or path.suffix in EXCLUDED_SUFFIX:
            continue
        files.append(path)
    return files


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(version: str | None = None, *, skip_check: bool = False, out_dir: Path | None = None) -> Path:
    if not skip_check:
        report = run_checks()
        if report.errors:
            for line in report.errors:
                print(f"  ✗ {line}")
            raise SystemExit("自检未通过，拒绝打包。")
        print("  ✓ 静态自检通过")

    meta = load_manifest_meta()
    version = version or meta["version"]
    out_dir = out_dir or (ROOT / "dist")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{meta['id']}-{version}.neko-plugin"

    files = collect_files()
    manifest = {
        "format": "neko-plugin",
        "format_version": 1,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **{k: meta[k] for k in (
            "id", "name", "version", "description", "short_description",
            "keywords", "entry", "author", "sdk", "runtime", "store",
            "database", "i18n", "ui", "dependencies",
        )},
        "files": [
            {
                "path": str(path.relative_to(PLUGIN_DIR).as_posix()),
                "sha256": sha256_of(path),
                "size": path.stat().st_size,
            }
            for path in files
        ],
    }

    if target.exists():
        target.unlink()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for path in files:
            archive.write(path, path.relative_to(PLUGIN_DIR).as_posix())

    checksum_value = sha256_of(target)
    (out_dir / f"{target.name}.sha256").write_text(f"{checksum_value}  {target.name}\n", encoding="utf-8")

    print(f"  ✓ 打包完成：{target.relative_to(ROOT).as_posix()}")
    print(f"    文件数 {len(files) + 1} · 体积 {target.stat().st_size / 1024:.1f} KB · sha256 {checksum_value[:16]}…")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="打包猫娘代码协助插件")
    parser.add_argument("--version", help="覆盖 plugin.toml 中的版本号")
    parser.add_argument("--out", help="输出目录，默认 <repo>/dist")
    parser.add_argument("--no-check", action="store_true", help="跳过静态自检")
    args = parser.parse_args()
    out_dir = Path(args.out) if args.out else None
    build(args.version, skip_check=args.no_check, out_dir=out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
