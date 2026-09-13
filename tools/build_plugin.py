"""打包 —— 产出**官方格式**的 `.neko-plugin` 安装包。

包结构（zip，根目录即 staging root；字段与布局对照官方
`plugin/neko_plugin_cli/core/build.py` / `install.py` / `inspect.py` 实现）：

    catgirl_code_assistance-1.0.0.neko-plugin
    ├── manifest.toml                      # 包级清单（安装器首先读它，缺失即报「缺少包级 manifest.toml」）
    ├── metadata.toml                      # payload 完整性校验（sha256）
    └── payload/
        ├── dependencies.toml              # 依赖清单（零依赖也要有，空的）
        ├── plugins/catgirl_code_assistance/...   # 插件源码（plugin.toml / __init__.py / …）
        └── profiles/default.toml          # 默认启用配置

manifest.toml 字段（官方 `write_manifest`）：
    schema_version / package_type / id / package_name / version / package_description

payload hash 算法（官方 `compute_payload_hash`，必须与实现逐字节一致，否则安装时报 hash mismatch）：
    遍历 payload 下所有文件，按 **NFC 归一化的 posix 相对路径**排序（区分大小写），
    每个文件贡献 `path + NUL + content + NUL` 进 sha256。

排除规则沿用官方 `_DEFAULT_*`（build_rules.py），另加 tsconfig.json 等开发期文件。

用法：
    python tools/build_plugin.py
    python tools/build_plugin.py --version 1.0.1
    python tools/build_plugin.py --no-check
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
import tomllib
import unicodedata
import zipfile
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_plugin import PLUGIN_DIR, ROOT, run_checks  # noqa: E402

SCHEMA_VERSION = "1.0"
PACKAGE_TYPE = "plugin"

# 官方 build_rules.py 的内建排除项（安全默认值，用户规则只能叠加不能替换）
DEFAULT_EXCLUDE_DIR_NAMES = {
    "__pycache__", ".github", ".vscode", ".idea", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".venv", ".git",
}
DEFAULT_ROOT_EXCLUDE_DIR_NAMES = {"dist", "build"}
DEFAULT_EXCLUDE_FILE_NAMES = {".DS_Store"}
DEFAULT_EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
# 本项目额外的开发期文件（官方 CLI 也会因为 pyproject 规则把它们挡掉，这里显式列出）
EXTRA_EXCLUDE_FILE_NAMES = {"tsconfig.json", "package-lock.json"}


# ─────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────
def escape_string(value: str) -> str:
    """TOML 基本字符串转义（与官方 toml_utils.escape_string 语义一致）。"""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def toml_bare_or_quoted_key(key: str) -> str:
    return key if key.replace("_", "").replace("-", "").isalnum() else f'"{escape_string(key)}"'


def normalize_relative_posix(path: Path, root: Path) -> str:
    return unicodedata.normalize("NFC", path.relative_to(root).as_posix())


def load_manifest_meta() -> dict:
    data = tomllib.loads((PLUGIN_DIR / "plugin.toml").read_text(encoding="utf-8-sig"))
    plugin = data.get("plugin", {})
    return {
        "id": plugin.get("id", PLUGIN_DIR.name),
        "name": plugin.get("name", plugin.get("id", "")),
        "version": plugin.get("version", "0.0.0"),
        "description": plugin.get("description", "") or plugin.get("short_description", ""),
        "entry": plugin.get("entry", ""),
        "auto_start": data.get("plugin_runtime", {}).get("auto_start", True),
    }


def should_skip(relative: Path, is_dir: bool) -> bool:
    dir_parts = relative.parts if is_dir else relative.parts[:-1]
    if dir_parts and dir_parts[0] in DEFAULT_ROOT_EXCLUDE_DIR_NAMES:
        return True
    if any(part in DEFAULT_EXCLUDE_DIR_NAMES for part in dir_parts):
        return True
    if not is_dir:
        if relative.name in DEFAULT_EXCLUDE_FILE_NAMES or relative.name in EXTRA_EXCLUDE_FILE_NAMES:
            return True
        if relative.suffix in DEFAULT_EXCLUDE_SUFFIXES:
            return True
    return False


def copy_plugin_files(source_dir: Path, target_dir: Path) -> list[Path]:
    copied: list[Path] = []
    for path in sorted(source_dir.rglob("*")):
        relative = path.relative_to(source_dir)
        if should_skip(relative, is_dir=path.is_dir()):
            continue
        destination = target_dir / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        copied.append(destination)
    return copied


def compute_payload_hash(payload_dir: Path) -> str:
    """sha256 over payload：按 NFC posix 相对路径排序，逐文件 path+NUL+content+NUL。"""
    digest = hashlib.sha256()
    entries = [
        (normalize_relative_posix(path, payload_dir), path)
        for path in payload_dir.rglob("*")
        if not path.is_dir()
    ]
    for relative, path in sorted(entries, key=lambda item: item[0]):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 官方强制 LF，跨平台包体必须一致
    path.write_text(text.rstrip("\n") + "\n", encoding="utf-8", newline="\n")


# ─────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────
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
    plugin_id = meta["id"]
    out_dir = out_dir or (ROOT / "dist")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{plugin_id}-{version}.neko-plugin"

    staging = Path(tempfile.mkdtemp(prefix=f"neko_build_{plugin_id}_"))
    try:
        payload_dir = staging / "payload"
        plugin_payload = payload_dir / "plugins" / plugin_id
        profiles_dir = payload_dir / "profiles"
        plugin_payload.mkdir(parents=True, exist_ok=True)
        profiles_dir.mkdir(parents=True, exist_ok=True)

        # 1. 插件源码
        copied = copy_plugin_files(PLUGIN_DIR, plugin_payload)
        if not (plugin_payload / "plugin.toml").is_file():
            raise SystemExit("payload 里缺少 plugin.toml，拒绝打包。")

        # 2. payload/dependencies.toml（零依赖也必须有，否则安装期校验拿不到元数据）
        write_text(
            payload_dir / "dependencies.toml",
            "\n".join(
                [
                    f'schema_version = "{SCHEMA_VERSION}"',
                    "",
                    f"[plugins.{toml_bare_or_quoted_key(plugin_id)}]",
                    "python_requirements = []",
                    "host_python_requirements = []",
                    "plugin_dependencies = []",
                    "advanced_plugin_dependencies = []",
                    f'vendor_path = "plugins/{escape_string(plugin_id)}/vendor"',
                    "vendor_present = false",
                ]
            ),
        )

        # 3. payload/profiles/default.toml
        write_text(
            profiles_dir / "default.toml",
            "\n".join(
                [
                    'name = "default"',
                    f'enabled_plugins = ["{escape_string(plugin_id)}"]',
                    "",
                    f"[plugin.{toml_bare_or_quoted_key(plugin_id)}]",
                    "enabled = true",
                    f'auto_start = {"true" if meta["auto_start"] else "false"}',
                ]
            ),
        )

        # 4. metadata.toml —— hash 必须在所有 payload 文件写完后计算
        payload_hash = compute_payload_hash(payload_dir)
        write_text(
            staging / "metadata.toml",
            "\n".join(
                [
                    "[payload]",
                    'hash_algorithm = "sha256"',
                    f'hash = "{payload_hash}"',
                    "",
                    "[source]",
                    'kind = "local"',
                    f'paths = ["{escape_string(plugin_id)}"]',
                ]
            ),
        )

        # 5. manifest.toml —— 包级清单，安装器第一道门槛
        manifest_lines = [
            f'schema_version = "{SCHEMA_VERSION}"',
            f'package_type = "{escape_string(PACKAGE_TYPE)}"',
            "",
            f'id = "{escape_string(plugin_id)}"',
            f'package_name = "{escape_string(meta["name"])}"',
            f'version = "{escape_string(version)}"',
        ]
        if meta["description"]:
            manifest_lines.append(f'package_description = "{escape_string(meta["description"])}"')
        write_text(staging / "manifest.toml", "\n".join(manifest_lines))

        # 6. 打包：按 NFC 相对路径排序写入，保证跨平台字节序一致
        if target.exists():
            target.unlink()
        entries = [
            (normalize_relative_posix(path, staging), path)
            for path in staging.rglob("*")
            if not path.is_dir()
        ]
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for arcname, path in sorted(entries, key=lambda item: item[0]):
                archive.write(path, arcname=arcname)

        # 7. 整包 sha256 校验文件（方便下载后核对；包内完整性以 metadata.toml 为准）
        archive_digest = hashlib.sha256()
        with open(target, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                archive_digest.update(chunk)
        (out_dir / f"{target.name}.sha256").write_text(
            f"{archive_digest.hexdigest()}  {target.name}\n", encoding="utf-8", newline="\n"
        )

        print(f"  ✓ 打包完成：{target.relative_to(ROOT).as_posix()}")
        print(
            f"    插件文件 {len(copied)} 个 · 包内 {len(entries)} 条 · "
            f"体积 {target.stat().st_size / 1024:.1f} KB · payload sha256 {payload_hash[:16]}…"
        )
        return target
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="打包猫娘代码协助插件（官方 .neko-plugin 格式）")
    parser.add_argument("--version", help="覆盖 plugin.toml 中的版本号")
    parser.add_argument("--out", help="输出目录，默认 <repo>/dist")
    parser.add_argument("--no-check", action="store_true", help="跳过静态自检")
    args = parser.parse_args()
    build(args.version, skip_check=args.no_check, out_dir=Path(args.out) if args.out else None)
    print(f"  构建时间 {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
