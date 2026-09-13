"""通用小工具。

放这里的理由：这些函数会被多个 Router 用到，而 Router 会被 `include_router()`
挂到插件实例上 —— **模块级函数**比 staticmethod 更稳（不同 SDK 版本对
staticmethod 的绑定处理不一致，曾经踩过 `_truthy` 绑不上去的坑）。
"""
from __future__ import annotations

from typing import Any

_TRUE_WORDS = ("1", "true", "yes", "on", "y", "是")
_FALSE_WORDS = ("0", "false", "no", "off", "n", "", "否")


def truthy(value: Any, default: bool = False) -> bool:
    """宽松的真值解析：兼容 bool / 数字 / 各种字符串（LLM 常把 true 写成 "true"）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _TRUE_WORDS:
            return True
        if low in _FALSE_WORDS:
            return False
    return bool(value)


def clamp_int(value: Any, default: int, low: int, high: int) -> int:
    """把任意输入转成 [low, high] 区间内的整数，失败回落 default。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def payload_of(result: Any) -> dict:
    """把 Ok(...) / dict 结果统一取下层的 dict（LLM 工具只回普通 dict 才能被序列化）。"""
    for attr in ("value", "data"):
        candidate = getattr(result, attr, None)
        if isinstance(candidate, dict):
            return candidate
    if isinstance(result, dict):
        return result
    return {"raw": str(result)}


def error_of(result: Any) -> str:
    """从 Err(...) / dict 结果里取错误文案，没有则返回空串。"""
    if getattr(result, "ok", True) is False:
        return str(getattr(result, "error", "") or getattr(result, "message", "") or "未知错误")
    error = getattr(result, "error", None)
    if error and not callable(error):
        return str(error)
    return ""
