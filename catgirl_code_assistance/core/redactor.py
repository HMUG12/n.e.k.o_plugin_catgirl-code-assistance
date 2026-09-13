"""本地脱敏 + 敏感信息扫描。

承诺：所有代码片段在**离开本插件进程之前**（也就是返回给大模型之前）先过这一层。
命中项会被替换成 `[REDACTED:TYPE]`，明文永远不会进入返回值、也不会写进日志。

注意声明：正则脱敏是**降低风险**的手段，不是完备的保密方案；
它挡不住「把密钥拆成两半再拼接」这类写法。真正的安全边界来自
「可读根目录白名单 + 默认不联网」。
"""
from __future__ import annotations

import re
from typing import Iterable

# (名称, 正则, 严重级别, 建议)
_BUILTIN_SPECS: list[tuple[str, str, str, str]] = [
    ("private_key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----", "high", "私钥不得入库，改用密钥管理服务或环境变量注入。"),
    ("aws_access_key", r"\bAKIA[0-9A-Z]{16}\b", "high", "疑似 AWS Access Key，请轮换并改用 IAM 角色。"),
    ("github_token", r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", "high", "疑似 GitHub Token，请吊销并改用 CI Secrets。"),
    ("openai_key", r"\bsk-[A-Za-z0-9_-]{20,}\b", "high", "疑似 OpenAI API Key，请轮换并从环境变量读取。"),
    ("slack_token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "high", "疑似 Slack Token，请吊销。"),
    ("google_api_key", r"\bAIza[0-9A-Za-z_-]{35}\b", "high", "疑似 Google API Key，请轮换。"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b", "medium", "疑似 JWT，不要把签发密钥写死在源码里。"),
    ("bearer_token", r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}", "medium", "疑似 Bearer Token，请改为运行时注入。"),
    ("private_ip_credential", r"(?i)\b(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|amqp)://[^\s\"']{6,}", "high", "连接串里可能含账密，请改用配置中心或环境变量。"),
    ("hardcoded_secret", r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|apikey|access[_-]?key|auth[_-]?token|private[_-]?key)\s*(?::|=|==)\s*[\"']([^\"'\s]{4,})[\"']", "high", "检测到硬编码凭据，建议改为 os.environ 读取。"),
    ("email", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "low", "检测到邮箱地址。"),
    ("cn_id_card", r"\b\d{17}[\dXx]\b", "medium", "疑似身份证号，请勿写入源码。"),
    ("cn_mobile", r"\b1[3-9]\d{9}\b", "low", "疑似手机号。"),
    ("bank_card", r"\b\d{16,19}\b", "low", "疑似银行卡号。"),
]


class Redactor:
    """内置规则 + 用户自定义正则的脱敏器（全部本地执行）。"""

    def __init__(self, custom_patterns: Iterable[str] | None = None):
        compiled: list[tuple[str, "re.Pattern[str]", str, str]] = []
        for name, pattern, severity, advice in _BUILTIN_SPECS:
            compiled.append((name, re.compile(pattern), severity, advice))
        for index, pattern in enumerate(custom_patterns or []):
            text = str(pattern or "").strip()
            if not text:
                continue
            try:
                compiled.append((f"custom_{index}", re.compile(text), "medium", "命中用户自定义脱敏规则。"))
            except re.error:
                # 坏正则不应让插件崩溃 —— 静默跳过
                continue
        self._rules = compiled

    # ── 替换型脱敏 ──
    def redact(self, text: str) -> tuple[str, dict]:
        """返回（脱敏后的文本，命中统计）。"""
        if not text:
            return text, {}
        counts: dict[str, int] = {}
        output = text
        for name, pattern, _severity, _advice in self._rules:
            def _sub(match: "re.Match[str]") -> str:
                return f"[REDACTED:{name.upper()}]"

            output, hit = pattern.subn(_sub, output)
            if hit:
                counts[name] = counts.get(name, 0) + hit
        return output, counts

    # ── 扫描型（只报告，不修改源码）──
    def findings(self, text: str, *, limit: int = 30) -> list[dict]:
        if not text:
            return []
        lines = text.splitlines()
        out: list[dict] = []
        seen: set[tuple[str, int]] = set()
        for name, pattern, severity, advice in self._rules:
            for line_no, line in enumerate(lines, start=1):
                if pattern.search(line):
                    key = (name, line_no)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(
                        {
                            "rule": name,
                            "severity": severity,
                            "line": line_no,
                            "message": f"第 {line_no} 行命中敏感信息规则 {name}",
                            "suggestion": advice,
                            "excerpt": self.redact(line.strip()[:160])[0],
                        }
                    )
                    if len(out) >= limit:
                        return out
        return out

    @property
    def rule_names(self) -> list[str]:
        return [name for name, *_ in self._rules]
