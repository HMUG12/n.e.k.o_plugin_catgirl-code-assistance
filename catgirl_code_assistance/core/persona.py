"""猫娘语气层：只负责把结论包装成一句话，不影响任何功能逻辑。

可在设置面板切换：
  gentle       —— 温和陪伴（默认）
  professional —— 只讲事实
  tsundere     —— 傲娇
"""
from __future__ import annotations

_STYLES = ("gentle", "professional", "tsundere")


def persona_line(style: str, text: str) -> str:
    style = style if style in _STYLES else "gentle"
    if style == "professional":
        return text
    if style == "tsundere":
        return f"{text}（才、才不是特意帮你做的呢）"
    return f"{text} 喵~"


def persona_error(style: str, text: str) -> str:
    style = style if style in _STYLES else "gentle"
    if style == "professional":
        return text
    if style == "tsundere":
        return f"{text}（不许乱来）"
    return f"{text} 喵……"
