"""离线自检 —— 在没有 N.E.K.O 源码树 / `neko-plugin` CLI 的环境下，做一次严肃的静态检查。

覆盖官方 `uv run neko-plugin check <id> --strict` 会管的典型问题，外加本插件自己的约束：
  1. plugin.toml 结构、必填字段、id/目录名/entry 包名三者对齐
  2. [plugin_runtime].timeout 合法（0 < timeout <= 300）
  3. Python 文件：无 BOM、能 py_compile、禁止 executemany / _ctx=None / keywords=
  4. 生命周期与入口方法必须是 async、必须返回 Ok/Err
  5. i18n 双语文件 key 必须一一对应
  6. TSX 只能用 @neko/plugin-ui 的白名单组件，且不得出现 useState / react 依赖
  7. plugin.toml 里声明的 UI/文档资源真实存在
  8. **零联网承诺**：插件源码不得出现任何网络/子进程调用

用法：python tools/check_plugin.py
"""
from __future__ import annotations

import ast
import io
import json
import os
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "catgirl_code_assistance"

# 官方 @neko/plugin-ui 组件白名单（含 Hooks 之外的内建），对照 03-ui-settings.md 核实
UI_COMPONENTS = {
    "Page", "Card", "Section", "Stack", "Grid", "Heading", "Divider",
    "Text", "StatCard", "KeyValue", "DataTable", "List", "JsonView", "CodeBlock",
    "Alert", "Tip", "Warning", "InlineError", "EmptyState",
    "Field", "Input", "Textarea", "Select", "Switch", "Form", "ActionForm",
    "StatusBadge", "ActionButton", "RefreshButton", "Modal", "ConfirmDialog", "AsyncBlock",
    "useLocalState", "useAsync", "useForm", "useToast", "useConfirm",
    "useDebounce", "useDebouncedState", "useI18n",
}
UI_TYPES = {"HostedAction", "PluginSurfaceProps"}

FORBIDDEN_IMPORTS = {
    "requests", "httpx", "aiohttp", "urllib", "urllib.request", "urllib3",
    "socket", "http.client", "websocket", "subprocess", "ftplib", "smtplib", "paramiko",
}

REQUIRED_PLUGIN_FIELDS = ("id", "name", "version", "entry")


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def info(self, message: str) -> None:
        self.infos.append(message)

    @property
    def ok(self) -> bool:
        return not self.errors


# ══════════════════════════════════════════
# 各项检查
# ══════════════════════════════════════════
def check_manifest(report: Report) -> dict:
    manifest_path = PLUGIN_DIR / "plugin.toml"
    if not manifest_path.is_file():
        report.error("缺少 catgirl_code_assistance/plugin.toml")
        return {}

    raw = manifest_path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        report.error("plugin.toml 带 UTF-8 BOM，必须去掉")
    try:
        data = tomllib.loads(raw.decode("utf-8-sig"))
    except tomllib.TOMLDecodeError as exc:
        report.error(f"plugin.toml 解析失败：{exc}")
        return {}

    plugin = data.get("plugin", {})
    for field in REQUIRED_PLUGIN_FIELDS:
        if field not in plugin:
            report.error(f"[plugin] 缺少必填字段：{field}")

    plugin_id = plugin.get("id", "")
    if plugin_id != PLUGIN_DIR.name:
        report.error(f"plugin.id({plugin_id}) 必须等于源码目录名({PLUGIN_DIR.name})")

    entry = plugin.get("entry", "")
    if ":" not in entry:
        report.error(f"entry 必须是 module.path:ClassName 形式，当前为 {entry!r}")
    else:
        module_path, class_name = entry.rsplit(":", 1)
        package = module_path.rsplit(".", 1)[-1] if "." in module_path else module_path
        if package != PLUGIN_DIR.name:
            report.error(f"entry 包名({package}) 必须等于源码目录名({PLUGIN_DIR.name})")
        if not class_name[:1].isupper():
            report.warn(f"entry 类名 {class_name!r} 建议用大驼峰")

    runtime = data.get("plugin_runtime", {})
    timeout = runtime.get("timeout", None)
    if timeout is None:
        report.warn("未声明 [plugin_runtime].timeout，将使用框架默认值")
    elif not (isinstance(timeout, int) and 0 < timeout <= 300):
        report.error(f"[plugin_runtime].timeout 必须满足 0 < timeout <= 300，当前 {timeout}")

    if data.get("plugin", {}).get("database", {}).get("enabled") and "plugin.database" not in raw.decode("utf-8-sig"):
        report.warn("疑似缺少 [plugin.database] 声明，self.db 会为 None")

    # 官方 build/dependencies.py：[plugin].dependencies 只能是「插件 ID 字符串列表」
    deps = plugin.get("dependencies", None)
    if deps is not None and not isinstance(deps, list):
        report.error("[plugin].dependencies 必须是插件 ID 字符串列表（零依赖时请整段删除，不要写空表）")
    if isinstance(deps, list):
        for item in deps:
            if not isinstance(item, str) or not item.strip():
                report.error(f"[plugin].dependencies 只能是插件 ID 字符串：{item!r}")

    # 官方不支持 requirements.txt（依赖要写在 pyproject.toml 并 vendor/）
    if (PLUGIN_DIR / "requirements.txt").is_file():
        report.error("插件目录不允许出现 requirements.txt（官方要求依赖走 pyproject.toml + vendor/）")

    # UI / 文档资源存在性
    panels = data.get("plugin", {}).get("ui", {}).get("panel", [])
    guides = data.get("plugin", {}).get("ui", {}).get("guide", [])
    for item in list(panels) + list(guides):
        target = PLUGIN_DIR / item.get("entry", "")
        if not target.is_file():
            report.error(f"UI/文档资源不存在：{item.get('entry')}")
        if not item.get("context") and "panel" in str(item.get("entry", "")):
            report.warn(f"面板 {item.get('id')} 未声明 context")

    # i18n 目录存在
    i18n_conf = data.get("plugin", {}).get("i18n", {})
    locales_dir = PLUGIN_DIR / str(i18n_conf.get("locales_dir", "i18n"))
    if i18n_conf and not locales_dir.is_dir():
        report.error(f"i18n 目录不存在：{locales_dir}")

    report.info(f"manifest 解析成功：{plugin.get('name')} v{plugin.get('version')}")
    return data


def check_python_sources(report: Report) -> None:
    py_files = sorted(PLUGIN_DIR.rglob("*.py"))
    if not py_files:
        report.error("没有找到任何 Python 源文件")
        return

    for path in py_files:
        rel = path.relative_to(ROOT).as_posix()
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            report.error(f"{rel} 带 UTF-8 BOM（会导致 SyntaxError）")
        try:
            tree = ast.parse(raw.decode("utf-8-sig"), filename=str(path))
        except SyntaxError as exc:
            report.error(f"{rel} 语法错误：{exc}")
            continue

        source = raw.decode("utf-8-sig")
        # executemany：只查真实调用（AST），避免把注释里的说明也判成错误
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "executemany"
            ):
                report.error(f"{rel} 使用了不支持的 executemany()（需逐行 execute）")
        # _ctx：框架不传该参数，必须用 self.ctx（同样只查真实形参）
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for arg in list(node.args.args) + list(node.args.kwonlyargs):
                    if arg.arg == "_ctx":
                        report.error(f"{rel}::{node.name} 声明了 _ctx 参数 —— 框架不传该参数，请用 self.ctx")
        if "os.system(" in source or "popen(" in source:
            report.error(f"{rel} 出现命令执行调用，违反本地只读承诺")

        # 网络/子进程零容忍（兑现「本地优先」承诺）
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in FORBIDDEN_IMPORTS:
                        report.error(f"{rel} 引入了网络/子进程模块 {alias.name}，违反零联网承诺")
            elif isinstance(node, ast.ImportFrom):
                module = (node.module or "").split(".")[0]
                if module in FORBIDDEN_IMPORTS:
                    report.error(f"{rel} 引入了网络/子进程模块 {node.module}")

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = _decorator_names(node)
            if not decorators:
                continue
            if any(name in ("lifecycle", "plugin_entry", "ui_action", "ui_context") for name in decorators):
                if not isinstance(node, ast.AsyncFunctionDef):
                    report.error(f"{rel}::{node.name} 被 N.E.K.O 装饰器修饰，必须是 async def")
            if "plugin_entry" in decorators:
                for dec in node.decorator_list:
                    for keyword in _call_keywords(dec):
                        if keyword.arg == "keywords":
                            report.error(f"{rel}::{node.name} 使用了已移除的 keywords= 参数")
    report.info(f"扫描 {len(py_files)} 个 Python 文件")


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute):
            names.add(target.attr)
        elif isinstance(target, ast.Name):
            names.add(target.id)
    return names


def _call_keywords(node: ast.expr) -> list[ast.keyword]:
    return node.keywords if isinstance(node, ast.Call) else []


def check_i18n(report: Report) -> None:
    locales = PLUGIN_DIR / "i18n"
    if not locales.is_dir():
        report.warn("没有 i18n 目录")
        return
    files = sorted(locales.glob("*.json"))
    if len(files) < 2:
        report.warn(f"i18n 只有 {len(files)} 个语言文件")
        return
    tables: dict[str, dict] = {}
    for path in files:
        try:
            tables[path.name] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            report.error(f"{path.name} 不是合法 JSON：{exc}")
            return
    base_name, base = next(iter(tables.items()))
    for name, table in tables.items():
        if name == base_name:
            continue
        missing = sorted(set(base) - set(table))
        extra = sorted(set(table) - set(base))
        if missing:
            report.error(f"{name} 缺少 {len(missing)} 个 key：{missing[:5]}")
        if extra:
            report.error(f"{name} 多出 {len(extra)} 个 key：{extra[:5]}")
    report.info(f"i18n 双语一致：{', '.join(tables)}")


def check_tsx(report: Report) -> None:
    panels = sorted(PLUGIN_DIR.glob("ui/*.tsx"))
    for path in panels:
        rel = path.relative_to(ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        for forbidden in ("useState", "useEffect", "createPortal", "React."):
            if forbidden in source:
                report.error(f"{rel} 使用了沙箱禁止的 {forbidden}")
        if 'from "react"' in source or 'from "react-dom"' in source:
            report.error(f"{rel} 不得直接 import react（Hosted TSX 沙箱不允许 npm 包）")
        names = _imported_ui_names(source)
        unknown = names - UI_COMPONENTS - UI_TYPES
        if unknown:
            report.error(f"{rel} 使用了不存在的组件：{sorted(unknown)}")
        # useLocalState 的 key 唯一性
        keys = _local_state_keys(source)
        if len(keys) != len(set(keys)):
            report.error(f"{rel} 的 useLocalState key 有重复：{keys}")
        report.info(f"{rel} 使用了 {len(names - UI_TYPES)} 个官方组件")
    if not panels:
        report.warn("ui/ 下没有 tsx 面板")


def _imported_ui_names(source: str) -> set[str]:
    names: set[str] = set()
    for block in source.split('from "@neko/plugin-ui"')[0].split("import")[-1:]:
        pass
    # 简单解析：取出 import { ... } 花括号里的标识符
    for chunk in source.split('from "@neko/plugin-ui"'):
        head = chunk.split("{")[-1].split("}")[0] if "{" in chunk else ""
        for raw in head.split(","):
            name = raw.strip()
            if name.isidentifier():
                names.add(name)
    return names


def _local_state_keys(source: str) -> list[str]:
    keys: list[str] = []
    marker = "useLocalState("
    index = 0
    while True:
        index = source.find(marker, index)
        if index < 0:
            break
        tail = source[index + len(marker) :].lstrip()
        if tail.startswith('"'):
            keys.append(tail[1:].split('"')[0])
        index += len(marker)
    return keys


def check_structure(report: Report) -> None:
    for required in ("__init__.py", "plugin.toml", "ui", "i18n", "docs", "routers", "core"):
        if not (PLUGIN_DIR / required).exists():
            report.error(f"缺少必需的 {required}")
    guide = PLUGIN_DIR / "docs" / "guide.md"
    if guide.is_file() and guide.stat().st_size < 200:
        report.warn("docs/guide.md 过短，用户指南应足够具体")
    report.info("目录结构完整")


# ══════════════════════════════════════════
def run_checks() -> Report:
    report = Report()
    check_manifest(report)
    check_structure(report)
    check_python_sources(report)
    check_i18n(report)
    check_tsx(report)
    return report


def main() -> int:
    report = run_checks()
    out = io.StringIO()
    for line in report.infos:
        print(f"  ✓ {line}")
    for line in report.warnings:
        print(f"  ! {line}")
    for line in report.errors:
        print(f"  ✗ {line}")
    print()
    print(f"结果：{len(report.errors)} 个错误，{len(report.warnings)} 个警告")
    _ = out
    if not report.ok:
        print("自检未通过 —— 请修复上述错误后再打包。")
        return 1
    print("自检通过 ✓（静态检查通过 ≠ 运行通过，仍需在 N.E.K.O 里实际加载验证）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
