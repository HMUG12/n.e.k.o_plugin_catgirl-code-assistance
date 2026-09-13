"""猫娘代码协助 —— 核心能力包。

分层约定（深模块：`router` 薄、`core` 厚）：
  core/        纯业务逻辑，尽量不依赖 SDK（可在测试里单独跑）
  routers/     AI 入口 / UI 动作，只做「取参 → 调 core → Ok/Err」
"""
from .config import SECTION, Settings
from .pathguard import PathGuard, PathResolution
from .redactor import Redactor
from .indexer import WorkspaceIndex
from .persona import persona_line, persona_error

__all__ = [
    "SECTION",
    "Settings",
    "PathGuard",
    "PathResolution",
    "Redactor",
    "WorkspaceIndex",
    "persona_line",
    "persona_error",
]
