"""猫娘代码协助（Catgirl Code Assistance）—— N.E.K.O 插件主入口。

做什么：
  · 把你指定的本地文件夹列进「可读文件夹」，然后在聊天里陪你读代码、讲代码、查问题、提改进；
  · 支持 Python / C / C++ / Java / JS / TS / Go / Rust 等常见源码的**结构大纲**与**本地静态审查**；
  · 改动走「先 diff 预览 → 用户确认 → 落盘 → 可撤销」，写权限默认全部关闭；
  · 敏感信息（密钥 / Token / 邮箱 / 手机号 / 身份证 / 私钥块）在**本地打码后**才返回给模型；
  · **零第三方依赖**（纯标准库）、**零外发网络请求** —— 本地与云端部署都可直接使用。

怎么拆的（真实插件范式 A：Router 子包）：
  主类只做生命周期 + 基础设施；AI 入口分布在 routers/ 五个业务域里。
"""
from __future__ import annotations

import asyncio

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    lifecycle,
    neko_plugin,
    timer_interval,
)

from .core import SECTION, PathGuard, Redactor, Settings, WorkspaceIndex
from .routers import (
    EditRouter,
    ExploreRouter,
    MemoryRouter,
    ReadingRouter,
    WorkspaceRouter,
)

PLUGIN_VERSION = "1.0.0"


@neko_plugin
class CatgirlCodeAssistancePlugin(NekoPluginBase):
    """猫娘代码协助插件。

    约定（与 routers/ 共享的运行时状态）：
      self.settings  —— 当前配置快照
      self.redactor  —— 本地脱敏器
      self.index     —— SQLite 工作区索引
      self._guard()  —— 路径守卫（方法形式，便于热更新后取最新实例）
    """

    __routers__ = (WorkspaceRouter, ExploreRouter, ReadingRouter, EditRouter, MemoryRouter)

    def __init__(self, ctx):
        super().__init__(ctx)

        # ── 运行态基础设施 ──
        self.settings: Settings = Settings()
        self.redactor: Redactor = Redactor([])
        self._path_guard: PathGuard = PathGuard(self.settings)
        self.index: WorkspaceIndex = WorkspaceIndex(self)

        self._undo_stack: list[dict] = []
        self._ready: bool = False
        self._frozen: bool = False
        self._scanning: bool = False
        self._scan_lock: asyncio.Lock = asyncio.Lock()
        self._bg_tasks: set = set()

        # Router 必须在 super().__init__(ctx) 之后、__init__ 返回之前注册（skill 铁律 #14）
        for router_cls in self.__routers__:
            self.include_router(router_cls())

    # ══════════════════════════════════════
    # 基础设施
    # ══════════════════════════════════════
    def _guard(self) -> PathGuard:
        """取当前路径守卫（配置变化后仍是同一个实例，只是内部值已更新）。"""
        return self._path_guard

    def _t(self, key: str, default: str, **kwargs) -> str:
        """i18n 安全调用：翻译文件缺失/损坏时退回 default，绝不抛异常。"""
        try:
            return self.i18n.t(key, default=default, **kwargs)
        except Exception:
            try:
                return default.format(**kwargs) if kwargs else default
            except Exception:
                return default

    def _spawn(self, coro) -> None:
        """开后台任务并登记，关停时统一取消（避免野协程堆积）。"""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _dump_config(self) -> dict:
        try:
            raw = await self.config.dump(timeout=5.0)
        except TypeError:
            raw = await self.config.dump()
        except Exception:
            raw = await self.config.dump()
        return raw if isinstance(raw, dict) else {}

    async def _save_config(self, mapping: dict) -> None:
        """写回运行时配置（优先官方 update，失败退化为全量 save）。"""
        try:
            await self.config.update({SECTION: mapping})
            return
        except Exception as exc:
            self.logger.warning("config.update 失败，退化为全量保存：%s", exc)
        raw = await self._dump_config()
        raw[SECTION] = mapping
        try:
            await self.config.save(raw)
        except Exception as exc:
            raise RuntimeError(f"配置保存失败：{exc}") from exc

    async def _reload_config(self) -> dict:
        """配置热更新入口（startup / reload / config_change 三处共用）。"""
        raw = await self._dump_config()
        section = raw.get(SECTION, {}) if isinstance(raw, dict) else {}
        self.settings = Settings.from_mapping(section)
        self._path_guard.configure(self.settings)
        self.redactor = Redactor(self.settings.custom_redact_patterns)
        return self.settings.to_mapping()

    # ══════════════════════════════════════
    # 生命周期
    # ══════════════════════════════════════
    @lifecycle(id="startup")
    async def startup(self, **_) -> Ok | Err:
        try:
            await self._reload_config()
            await self.index.ensure_tables()   # 短同步等待（<1s）
            await self.restore_undo_stack()    # 恢复上一次的可撤销记录
            self._ready = True
            if self.settings.index_enabled and self.settings.readable_roots:
                self._spawn(self._scan_background())   # 耗时扫描丢后台，保证启动不被拖慢
            return Ok(
                {
                    "status": "ready",
                    "version": PLUGIN_VERSION,
                    "roots": len(self.settings.readable_roots),
                    "index_backend": "sqlite" if self.index.available else "live",
                }
            )
        except Exception as exc:  # 防御性：异常绝不冒泡出生命周期
            self.logger.exception("猫娘代码协助启动失败")
            return Err(str(exc))

    @lifecycle(id="shutdown")
    async def shutdown(self, **_) -> Ok | Err:
        self._ready = False
        for task in list(self._bg_tasks):
            task.cancel()
        for task in list(self._bg_tasks):
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.logger.warning("后台任务退出异常：%s", exc)
        self._bg_tasks.clear()
        return Ok({"status": "stopped"})

    @lifecycle(id="reload")
    async def on_reload(self, **_) -> Ok | Err:
        await self._reload_config()
        return Ok({"status": "reloaded"})

    @lifecycle(id="config_change")
    async def on_config_change(self, **_) -> Ok | Err:
        # 框架不保证回传旧/新值，统一重新 dump（官方 Best Practice）
        await self._reload_config()
        return Ok({"status": "config_updated"})

    @lifecycle(id="freeze")
    async def on_freeze(self, **_) -> Ok | Err:
        self._frozen = True
        return Ok({"status": "frozen"})

    @lifecycle(id="unfreeze")
    async def on_unfreeze(self, **_) -> Ok | Err:
        self._frozen = False
        return Ok({"status": "unfrozen"})

    # ══════════════════════════════════════
    # 定时器：每小时增量刷新索引
    # ══════════════════════════════════════
    @timer_interval(id="cca_index_refresh", seconds=3600, name="索引增量刷新", auto_start=True)
    def _on_index_timer(self, **_):
        """定时器跑在独立线程里 —— 必须自建事件循环（skill 铁律 #5）。"""
        if self._frozen or not self.settings.index_enabled or not self.settings.readable_roots:
            return Ok({"skipped": True})
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._scan_background())
        except Exception as exc:
            self.logger.warning("定时索引刷新失败：%s", exc)
            return Ok({"refreshed": False, "error": str(exc)})
        finally:
            loop.close()
        return Ok({"refreshed": True})


__all__ = ["CatgirlCodeAssistancePlugin", "PLUGIN_VERSION"]
