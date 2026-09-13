"""Router 子包：按业务域拆分 AI 入口（真实插件的范式 A）。

每个 Router 都是纯类，被 `include_router()` 挂到插件实例上，
因此 Router 内部写 `self.xxx` 访问的就是插件实例属性（settings / index / redactor / 核心方法）。
"""
from .explore import ExploreRouter
from .reading import ReadingRouter
from .editing import EditRouter
from .memory import MemoryRouter
from .workspace import WorkspaceRouter

__all__ = [
    "WorkspaceRouter",
    "ExploreRouter",
    "ReadingRouter",
    "EditRouter",
    "MemoryRouter",
]
