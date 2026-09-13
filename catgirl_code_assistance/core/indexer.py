"""工作区索引（SQLite）。

为什么需要索引：可读目录可能有上万文件，逐个 content-scan 太慢；
索引只存「路径 → 元信息」，先用 SQL 把候选文件缩到几十个，再读盘 grep。

数据库写法严格遵循官方 SDK 模式：
  · await session.execute(...)（同步 fetchall，**不要**两层 await）
  · 逐行 execute，禁止 executemany
  · 每批 commit，避免长事务
"""
from __future__ import annotations

import fnmatch
import os
import sqlite3
import time
from typing import Any, Iterable

TABLE_FILES = "cca_files"
TABLE_META = "cca_meta"
BATCH_SIZE = 500


def rows_to_dicts(rows: Any, columns: Iterable[str] | None = None) -> list[dict]:
    """把各种形态的查询结果统一转成 dict 列表（兼容 sqlite3.Row / tuple / dict）。"""
    out: list[dict] = []
    cols = list(columns or [])
    for row in rows or []:
        if isinstance(row, dict):
            out.append(dict(row))
            continue
        try:
            out.append(dict(row))  # sqlite3.Row / 支持 Mapping 的行
            continue
        except (TypeError, ValueError):
            pass
        if cols and isinstance(row, (tuple, list)):
            out.append(dict(zip(cols, row)))
    return out


class WorkspaceIndex:
    """围绕 self.db 的轻量封装；self.db 不可用时自动降级为「每次实时遍历」。"""

    def __init__(self, plugin):
        self.plugin = plugin
        self.available = plugin.db is not None
        self.last_error: str = ""

    # ── 建表 ──
    async def ensure_tables(self) -> bool:
        if not self.available:
            self.last_error = "数据库未启用（plugin.toml 的 [plugin.database] enabled 需为 true），索引功能降级为实时遍历。"
            return False
        session = await self.plugin.db.session()
        try:
            await session.execute(
                f"""CREATE TABLE IF NOT EXISTS {TABLE_FILES} (
                    path TEXT PRIMARY KEY,
                    root TEXT NOT NULL,
                    name TEXT NOT NULL,
                    ext TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    lines INTEGER NOT NULL DEFAULT 0
                )"""
            )
            await session.execute(f"CREATE INDEX IF NOT EXISTS idx_cca_name ON {TABLE_FILES}(name)")
            await session.execute(f"CREATE INDEX IF NOT EXISTS idx_cca_ext ON {TABLE_FILES}(ext)")
            await session.execute(
                f"""CREATE TABLE IF NOT EXISTS {TABLE_META} (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )"""
            )
            await session.commit()
            return True
        except Exception as exc:  # 防御性：建表失败不拖垮插件
            self.last_error = str(exc)
            self.available = False
            self.plugin.logger.warning("cca index init failed: %s", exc)
            return False
        finally:
            await session.close()

    # ── 插入（逐行 execute，分批 commit）──
    async def _upsert_files(self, rows: list[tuple]) -> int:
        if not rows or not self.available:
            return 0
        session = await self.plugin.db.session()
        written = 0
        try:
            for index, row in enumerate(rows, start=1):
                await session.execute(
                    f"INSERT OR REPLACE INTO {TABLE_FILES} "
                    "(path, root, name, ext, size, mtime, lines) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
                written += 1
                if index % BATCH_SIZE == 0:
                    await session.commit()
            await session.commit()
            return written
        except Exception as exc:
            self.plugin.logger.warning("cca upsert failed: %s", exc)
            return written
        finally:
            await session.close()

    async def set_meta(self, key: str, value: str) -> None:
        if not self.available:
            return
        session = await self.plugin.db.session()
        try:
            await session.execute(
                f"INSERT OR REPLACE INTO {TABLE_META} (key, value) VALUES (?, ?)", (key, value)
            )
            await session.commit()
        except Exception as exc:
            self.plugin.logger.warning("cca set_meta failed: %s", exc)
        finally:
            await session.close()

    async def get_meta(self, key: str, default: str = "") -> str:
        if not self.available:
            return default
        session = await self.plugin.db.session()
        try:
            cursor = await session.execute(f"SELECT value FROM {TABLE_META} WHERE key = ?", (key,))
            rows = rows_to_dicts(cursor.fetchall())
            return str(rows[0].get("value", default)) if rows else default
        except Exception as exc:
            self.plugin.logger.warning("cca get_meta failed: %s", exc)
            return default
        finally:
            await session.close()

    # ── 重建：root 维度先删后插，天然幂等 ──
    async def rebuild(self, entries: list[tuple], root: str) -> int:
        if not self.available:
            return 0
        session = await self.plugin.db.session()
        try:
            await session.execute(f"DELETE FROM {TABLE_FILES} WHERE root = ?", (root,))
            await session.commit()
        except Exception as exc:
            self.plugin.logger.warning("cca purge failed: %s", exc)
        finally:
            await session.close()
        return await self._upsert_files(entries)

    # ── 统计 ──
    async def stats(self) -> dict:
        base = {
            "files": 0,
            "bytes": 0,
            "last_scan_at": 0.0,
            "backend": "sqlite" if self.available else "live",
            "error": self.last_error,
        }
        if not self.available:
            last = await self.plugin.store.get("cca_last_scan_at", 0) or 0
            base["last_scan_at"] = float(last)
            return base
        session = await self.plugin.db.session()
        try:
            cursor = await session.execute(
                f"SELECT COUNT(*) AS files, COALESCE(SUM(size), 0) AS bytes FROM {TABLE_FILES}"
            )
            rows = rows_to_dicts(cursor.fetchall())
            if rows:
                base["files"] = int(rows[0].get("files", 0) or 0)
                base["bytes"] = int(rows[0].get("bytes", 0) or 0)
        except Exception as exc:
            self.plugin.logger.warning("cca stats failed: %s", exc)
        finally:
            await session.close()
        base["last_scan_at"] = float(await self.get_meta("last_scan_at", "0") or 0)
        return base

    # ── 候选文件查询 ──
    async def candidates(self, pattern: str = "", ext: str = "", limit: int = 400) -> list[dict]:
        if not self.available:
            return []
        sql = f"SELECT path, name, ext, size, lines FROM {TABLE_FILES}"
        clauses: list[str] = []
        params: list[Any] = []
        if ext:
            clauses.append("ext = ?")
            params.append(ext.lower())
        if pattern:
            like = "%" + pattern.replace("*", "%").replace("?", "_") + "%"
            clauses.append("(name LIKE ? OR path LIKE ?)")
            params.extend([like, like])
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY name LIMIT ?"
        params.append(max(1, min(limit, 2000)))
        session = await self.plugin.db.session()
        try:
            cursor = await session.execute(sql, params)
            rows = rows_to_dicts(cursor.fetchall())
        except Exception as exc:
            self.plugin.logger.warning("cca candidates failed: %s", exc)
            rows = []
        finally:
            await session.close()

        if pattern and "*" in pattern or ("?" in pattern):
            rows = [r for r in rows if fnmatch.fnmatch(str(r.get("name", "")), pattern)]
        seen: set[str] = set()
        unique: list[dict] = []
        for row in rows:
            path = str(row.get("path", ""))
            if path and path not in seen:
                seen.add(path)
                unique.append(row)
        return unique[:limit]

    async def clear(self) -> None:
        if not self.available:
            return
        session = await self.plugin.db.session()
        try:
            await session.execute(f"DELETE FROM {TABLE_FILES}")
            await session.execute(f"DELETE FROM {TABLE_META}")
            await session.commit()
        except Exception as exc:
            self.plugin.logger.warning("cca clear failed: %s", exc)
        finally:
            await session.close()


# ───────────────────────────────────────────────
# 目录遍历（纯 CPU / IO，放线程池）
# ───────────────────────────────────────────────
def walk_collect(root: str, blocked: set[str], allowed_exts: list[str], limit: int) -> list[tuple]:
    """遍历一个根目录，返回可直接入库的元组列表（同步，跑在 to_thread 里）。"""
    out: list[tuple] = []
    root_norm = os.path.normpath(root)
    for dirpath, dirnames, filenames in os.walk(root_norm, topdown=True):
        dirnames[:] = [d for d in dirnames if d.lower() not in blocked and not d.startswith(".git")]
        for name in filenames:
            if len(out) >= limit:
                return out
            dot = name.rfind(".")
            ext = name[dot:].lower() if dot >= 0 else ""
            if allowed_exts and ext not in allowed_exts:
                continue
            full = os.path.join(dirpath, name)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            lines = 0
            try:
                if stat.st_size <= 2_000_000:
                    with open(full, "rb") as handle:
                        lines = handle.read().count(b"\n") + 1
            except OSError:
                lines = 0
            out.append((full, root_norm, name, ext, int(stat.st_size), float(stat.st_mtime), lines))
    return out


async def sqlite_selfcheck() -> bool:
    """索引可用性自检：本机是否真的能用 sqlite3（云端环境兜底）。"""
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (a TEXT)")
        conn.close()
        return True
    except Exception:
        return False


def now_ts() -> float:
    return time.time()
