"""猫娘代码协助 —— 配置模型。

设计要点：
  · 单一事实来源：所有默认值集中在本文件，plugin.toml 只放同源的空/默认值。
  · 防御性读取：外部配置可能是脏数据（None / 错类型 / 越界值），一律安全强转。
  · 安全默认：写权限全部 false；脱敏默认 true；可读根目录默认空（必须用户显式授权）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# ── 默认常量 ────────────────────────────────────────────────
SECTION = "catgirl_code_assistance"

DEFAULT_BLOCKED_DIRS: list[str] = [
    ".git", ".svn", ".hg", ".idea", ".vscode", ".github",
    "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".venv", "venv", "env", "dist", "build", "site-packages",
    "target", ".next", "coverage", ".gradle", ".cache",
]

DEFAULT_ALLOWED_EXTENSIONS: list[str] = [
    ".py", ".pyi", ".pyx",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".hh", ".cxx", ".hxx",
    ".cs", ".java", ".scala", ".kt", ".kts",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".go", ".rs", ".rb", ".php", ".swift", ".m", ".mm",
    ".lua", ".pl", ".r", ".sh", ".bash", ".zsh", ".ps1", ".bat",
    ".sql", ".proto", ".gradle", ".cmake", ".toml", ".yaml", ".yml",
    ".json", ".ini", ".cfg", ".conf", ".md", ".txt", ".dockerfile",
]

MAX_FILE_KB_LIMIT = (32, 8192)
MAX_MATCHES_LIMIT = (1, 500)
MAX_CONTEXT_LINES_LIMIT = (20, 5000)
MAX_UNDO_STEPS_LIMIT = (1, 200)
INDEX_LIMIT_LIMIT = (100, 500000)

REPLY_STYLES = ("gentle", "professional", "tsundere")


# ── 安全强转工具 ────────────────────────────────────────────
def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on", "y"):
            return True
        if low in ("0", "false", "no", "off", "n", ""):
            return False
    return default


def _as_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, num))


def _as_text_list(value: Any, default: Iterable[str]) -> list[str]:
    """接受 list / 换行分隔字符串，输出去重后的干净列表。"""
    raw = value
    if isinstance(raw, str):
        raw = raw.splitlines()
    if not isinstance(raw, (list, tuple, set)):
        return list(default)
    out: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out or list(default)


def _as_lower_set(value: Any, default: Iterable[str]) -> list[str]:
    items = _as_text_list(value, default)
    out: list[str] = []
    for item in items:
        low = item.lower().lstrip(".") if item.startswith(".") else item.lower()
        norm = f".{low}" if low else low
        if norm and norm not in out:
            out.append(norm)
    return out or [str(x).lower() for x in default]


def _as_choice(value: Any, choices: Iterable[str], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in tuple(choices) else default


# ── 配置模型 ────────────────────────────────────────────────
@dataclass
class Settings:
    """插件运行时配置（由 plugin.toml 的 [catgirl_code_assistance] 段映射而来）。"""

    # 可读根目录 —— 安全默认：空，必须由用户在设置面板显式授权
    readable_roots: list[str] = field(default_factory=list)
    blocked_dirs: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_DIRS))
    allowed_extensions: list[str] = field(
        default_factory=lambda: list(DEFAULT_ALLOWED_EXTENSIONS)
    )

    # 读取与返回上限
    max_file_kb: int = 512
    max_matches: int = 50
    max_context_lines: int = 400
    follow_symlinks: bool = False

    # 写权限（默认全关）
    enable_write: bool = False
    allow_create: bool = False
    allow_modify: bool = False
    allow_delete: bool = False

    # 本地脱敏
    redact_sensitive: bool = True
    custom_redact_patterns: list[str] = field(default_factory=list)

    # 索引与撤销
    index_enabled: bool = True
    index_limit: int = 20000
    max_undo_steps: int = 20

    # 交互风格
    reply_style: str = "gentle"

    # ── 构造 / 序列化 ──
    @classmethod
    def from_mapping(cls, raw: dict | None) -> "Settings":
        raw = raw or {}
        return cls(
            readable_roots=_as_text_list(raw.get("readable_roots"), []),
            blocked_dirs=_as_text_list(raw.get("blocked_dirs"), DEFAULT_BLOCKED_DIRS),
            allowed_extensions=_as_lower_set(
                raw.get("allowed_extensions"), DEFAULT_ALLOWED_EXTENSIONS
            ),
            max_file_kb=_as_int(raw.get("max_file_kb"), 512, *MAX_FILE_KB_LIMIT),
            max_matches=_as_int(raw.get("max_matches"), 50, *MAX_MATCHES_LIMIT),
            max_context_lines=_as_int(
                raw.get("max_context_lines"), 400, *MAX_CONTEXT_LINES_LIMIT
            ),
            follow_symlinks=_as_bool(raw.get("follow_symlinks"), False),
            enable_write=_as_bool(raw.get("enable_write"), False),
            allow_create=_as_bool(raw.get("allow_create"), False),
            allow_modify=_as_bool(raw.get("allow_modify"), False),
            allow_delete=_as_bool(raw.get("allow_delete"), False),
            redact_sensitive=_as_bool(raw.get("redact_sensitive"), True),
            custom_redact_patterns=_as_text_list(raw.get("custom_redact_patterns"), []),
            index_enabled=_as_bool(raw.get("index_enabled"), True),
            index_limit=_as_int(raw.get("index_limit"), 20000, *INDEX_LIMIT_LIMIT),
            max_undo_steps=_as_int(raw.get("max_undo_steps"), 20, *MAX_UNDO_STEPS_LIMIT),
            reply_style=_as_choice(raw.get("reply_style"), REPLY_STYLES, "gentle"),
        )

    def to_mapping(self) -> dict:
        return {
            "readable_roots": list(self.readable_roots),
            "blocked_dirs": list(self.blocked_dirs),
            "allowed_extensions": list(self.allowed_extensions),
            "max_file_kb": self.max_file_kb,
            "max_matches": self.max_matches,
            "max_context_lines": self.max_context_lines,
            "follow_symlinks": self.follow_symlinks,
            "enable_write": self.enable_write,
            "allow_create": self.allow_create,
            "allow_modify": self.allow_modify,
            "allow_delete": self.allow_delete,
            "redact_sensitive": self.redact_sensitive,
            "custom_redact_patterns": list(self.custom_redact_patterns),
            "index_enabled": self.index_enabled,
            "index_limit": self.index_limit,
            "max_undo_steps": self.max_undo_steps,
            "reply_style": self.reply_style,
        }

    # ── 派生视图 ──
    def ext_allowed(self, path: str) -> bool:
        if "*" in self.allowed_extensions:
            return True
        dot = path.rfind(".")
        if dot < 0:
            return False
        return path[dot:].lower() in self.allowed_extensions

    def write_allowed(self, action: str) -> bool:
        """写操作总开关 + 逐操作授权（两层权限模型）。"""
        if not self.enable_write:
            return False
        mapping = {
            "create": self.allow_create,
            "modify": self.allow_modify,
            "write": self.allow_modify,
            "delete": self.allow_delete,
        }
        return mapping.get(action, False)
