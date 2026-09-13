"""离线运行的 SDK 替身。

为什么要替身：真实 SDK（`plugin.sdk.plugin`）只存在于 N.E.K.O 主程序里，
仓库里没法直接 import。这里按官方 API 语义复刻最小可用版本，
让插件的所有入口、 Router 绑定、数据库写法都能在纯 Python 环境跑起来。

复刻的对象：neko_plugin / lifecycle / plugin_entry / timer_interval /
ui.action / ui.context / llm_tool / Ok / Err / SdkError / tr / NekoPluginBase。
"""
from __future__ import annotations

import copy
import logging
import os
import sqlite3
import tempfile
import types
from typing import Any

_INSTALLED = False


# ─────────────────────────────────────────
# 结果类型
# ─────────────────────────────────────────
class _Ok:
    ok = True

    def __init__(self, value: Any = None):
        self.value = value or {}

    def __getitem__(self, key):
        return self.value[key]

    def get(self, key, default=None):
        return self.value.get(key, default) if isinstance(self.value, dict) else default

    def __repr__(self) -> str:
        return f"Ok({self.value!r})"


class _Err:
    ok = False

    def __init__(self, error: Any = None):
        self.error = error
        self.message = str(error)

    def __getitem__(self, key):
        return {"error": self.error}[key]

    def __repr__(self) -> str:
        return f"Err({self.error!r})"


class SdkError(Exception):
    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message


def Ok(value: Any = None) -> _Ok:  # noqa: N802 —— 与 SDK 同名
    return _Ok(value)


def Err(error: Any = None) -> _Err:  # noqa: N802
    return _Err(error)


def tr(key: str, default: str = "", **kwargs) -> str:
    return default.format(**kwargs) if kwargs else (default or key)


# ─────────────────────────────────────────
# 装饰器：只做「登记元数据 + 原样返回」
# ─────────────────────────────────────────
def _meta(fn, meta: dict):
    existing = list(getattr(fn, "__cca_meta__", []))
    existing.append(meta)
    fn.__cca_meta__ = existing
    return fn


def neko_plugin(cls):
    cls.__cca_plugin__ = True
    return cls


def lifecycle(**kwargs):
    return lambda fn: _meta(fn, {"kind": "lifecycle", **kwargs})


def plugin_entry(**kwargs):
    return lambda fn: _meta(fn, {"kind": "entry", **kwargs})


def timer_interval(**kwargs):
    return lambda fn: _meta(fn, {"kind": "timer", **kwargs})


def llm_tool(**kwargs):
    return lambda fn: _meta(fn, {"kind": "llm_tool", **kwargs})


def quick_action(**kwargs):
    return lambda fn: _meta(fn, {"kind": "quick_action", **kwargs})


class ui:  # noqa: N801 —— 命名空间形式，与官方一致
    @staticmethod
    def action(**kwargs):
        return lambda fn: _meta(fn, {"kind": "ui_action", **kwargs})

    @staticmethod
    def context(**kwargs):
        return lambda fn: _meta(fn, {"kind": "ui_context", **kwargs})


# ─────────────────────────────────────────
# 假的基础设施
# ─────────────────────────────────────────
class FakeConfig:
    def __init__(self, data: dict | None = None):
        self._data: dict = copy.deepcopy(data or {})

    async def dump(self, timeout: float | None = None) -> dict:
        return copy.deepcopy(self._data)

    async def update(self, patch: dict) -> dict:
        for key, value in (patch or {}).items():
            if isinstance(value, dict) and isinstance(self._data.get(key), dict):
                self._data[key].update(value)
            else:
                self._data[key] = value
        return copy.deepcopy(self._data)

    async def save(self, data: dict) -> None:
        self._data = copy.deepcopy(data or {})


class FakeStore:
    def __init__(self):
        self._data: dict = {}

    async def get(self, key: str, default: Any = None) -> Any:
        return copy.deepcopy(self._data.get(key, default))

    async def set(self, key: str, value: Any) -> None:
        self._data[key] = copy.deepcopy(value)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)


class _Session:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    async def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    async def commit(self) -> None:
        self._conn.commit()

    async def rollback(self) -> None:
        self._conn.rollback()

    async def close(self) -> None:
        pass


class FakeDatabase:
    """sqlite3 内存库，行对象为 sqlite3.Row（与官方 dict(row) 用法一致）。"""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # 与真实 SDK 一致：不支持 executemany，调用会暴露问题
        if hasattr(self.conn, "executemany"):
            pass

    async def session(self) -> _Session:
        return _Session(self.conn)


class FakeI18n:
    def __init__(self, locale: str = "zh-CN"):
        self.locale = locale

    def t(self, key: str, default: str = "", **kwargs) -> str:
        return default.format(**kwargs) if kwargs else (default or key)


class FakeBus:
    """占位：插件本身不使用 Bus，留一个空壳防止误用。"""

    async def get(self, *_args, **_kwargs):
        raise AssertionError("本插件不应使用 Bus")


class FakeNekoPluginBase:
    """最小可用基类：提供官方列出的属性与方法，并实现 include_router 绑定。"""

    def __init__(self, ctx=None):
        self.ctx = ctx
        self.plugin_id = getattr(ctx, "plugin_id", "catgirl_code_assistance")
        self.config = FakeConfig()
        self.store = FakeStore()
        self.db = FakeDatabase()
        self.logger = logging.getLogger("cca.test")
        self.i18n = FakeI18n()
        self.bus = FakeBus()
        self.system_info = {}
        self.metadata = {}
        self.status_reports: list[dict] = []
        self.pushed_messages: list[dict] = []
        self._router_names: list[str] = []
        self._workdir = tempfile.mkdtemp(prefix="cca-data-")

    # ── 路径 ──
    def data_path(self, *parts: str) -> str:
        target = os.path.join(self._workdir, *[str(p) for p in parts])
        os.makedirs(target, exist_ok=True)
        return target

    def cache_path(self, *parts: str) -> str:
        return self.data_path("cache", *parts)

    # ── 输出 ──
    def report_status(self, payload: dict) -> None:
        self.status_reports.append(payload)

    def push_message(self, **kwargs) -> None:
        self.pushed_messages.append(kwargs)

    # ── Router 绑定：把 Router 实例的方法绑到插件实例上 ──
    def include_router(self, router: Any) -> None:
        name = type(router).__name__
        self._router_names.append(name)
        for attr in dir(router):
            if attr.startswith("__"):
                continue
            value = getattr(router, attr)
            if callable(value):
                try:
                    unbound = value.__func__
                except AttributeError:
                    continue
                setattr(self, attr, types.MethodType(unbound, self))
            elif not attr.endswith("__"):
                setattr(self, attr, value)


# ─────────────────────────────────────────
# 安装到 sys.modules
# ─────────────────────────────────────────
def install() -> dict:
    """把假 SDK 注入 `plugin.sdk.plugin`，返回模块字典（幂等）。"""
    global _INSTALLED
    if _INSTALLED:
        import plugin.sdk.plugin as existing

        return existing.__dict__

    sdk = types.ModuleType("plugin.sdk.plugin")
    sdk.NekoPluginBase = FakeNekoPluginBase
    sdk.neko_plugin = neko_plugin
    sdk.lifecycle = lifecycle
    sdk.plugin_entry = plugin_entry
    sdk.timer_interval = timer_interval
    sdk.llm_tool = llm_tool
    sdk.quick_action = quick_action
    sdk.ui = ui
    sdk.Ok = Ok
    sdk.Err = Err
    sdk.SdkError = SdkError
    sdk.tr = tr
    sdk.unwrap = lambda value: value.value if isinstance(value, _Ok) else value

    package = types.ModuleType("plugin")
    package.__path__ = []  # 标记为包
    subpackage = types.ModuleType("plugin.sdk")
    subpackage.__path__ = []

    import sys

    sys.modules["plugin"] = package
    sys.modules["plugin.sdk"] = subpackage
    sys.modules["plugin.sdk.plugin"] = sdk
    package.sdk = subpackage
    subpackage.plugin = sdk
    _INSTALLED = True
    return sdk.__dict__


install()
