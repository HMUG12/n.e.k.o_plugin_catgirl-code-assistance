"""轻量代码分析：结构大纲 / 符号定位 / 启发式改进建议。

全部纯标准库实现（Python 用 ast，其余语言用正则），这样本地和云端都能直接跑，
不需要任何 pip 依赖，也不需要联网。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from .redactor import Redactor

EXT_LANGUAGE = {
    ".py": "python", ".pyi": "python", ".pyx": "python",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp",
    ".hh": "cpp", ".cxx": "cpp", ".hxx": "cpp",
    ".cs": "csharp", ".java": "java", ".scala": "scala", ".kt": "kotlin",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php",
    ".swift": "swift", ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".lua": "lua", ".sql": "sql", ".md": "markdown", ".json": "json",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
}

_C_FAMILY = {"c", "cpp", "csharp", "java", "javascript", "typescript", "go", "scala", "kotlin", "rust", "swift", "php"}

_MARKERS = re.compile(r"\b(TODO|FIXME|HACK|XXX|BUG)\b[:\s]*(.{0,80})?")


@dataclass
class SymbolHit:
    name: str
    kind: str
    line: int
    signature: str
    doc: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "line": self.line,
            "signature": self.signature[:300],
            "doc": (self.doc or "")[:300],
        }


def detect_language(path: str) -> str:
    dot = path.rfind(".")
    if dot < 0:
        name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if name in ("dockerfile", "makefile", "cmakelists.txt"):
            return name
        return "text"
    return EXT_LANGUAGE.get(path[dot:].lower(), "text")


# ───────────────────────────────────────────────
# 大纲
# ───────────────────────────────────────────────
def _outline_python(source: str) -> list[SymbolHit]:
    hits: list[SymbolHit] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return hits
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
            hits.append(SymbolHit(node.name, kind, node.lineno, f"def {node.name}({', '.join(args)})", ast.get_docstring(node) or ""))
        elif isinstance(node, ast.ClassDef):
            bases = [ast.unparse(b) for b in node.bases] if hasattr(ast, "unparse") else []
            hits.append(SymbolHit(node.name, "class", node.lineno, f"class {node.name}({', '.join(bases)})", ast.get_docstring(node) or ""))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                hits.append(SymbolHit(alias.name, "import", node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            hits.append(SymbolHit(node.module or ".", "import_from", node.lineno, f"from {node.module} import ..."))
    hits.sort(key=lambda s: s.line)
    return hits


def _outline_generic(source: str, language: str) -> list[SymbolHit]:
    patterns = [
        ("function", re.compile(r"^\s*(?:[\w<>:&*\s]*?\b)([\w~]+)\s*\([^;{]*\)\s*(?:const)?\s*\{", re.M)),
        ("class", re.compile(r"^\s*(?:class|struct|interface|enum|protocol)\s+(\w+)", re.M)),
        ("include", re.compile(r"^\s*#include\s+[<\"]([^>\"]+)[>\"]", re.M)),
        ("import", re.compile(r"^\s*(?:import|using|package|require)\s+([^\s;]+)", re.M)),
        ("macro", re.compile(r"^\s*#define\s+(\w+)", re.M)),
    ]
    hits: list[SymbolHit] = []
    for kind, pattern in patterns:
        for match in pattern.finditer(source):
            line = source.count("\n", 0, match.start()) + 1
            hits.append(SymbolHit(match.group(1), kind, line, match.group(0).strip()[:300]))
    allow = {"function", "class", "include", "macro"} if language in ("c", "cpp") else {"function", "class", "import", "include", "macro"}
    seen: set[tuple[str, int]] = set()
    unique: list[SymbolHit] = []
    for hit in sorted(hits, key=lambda s: s.line):
        if hit.kind not in allow:
            continue
        key = (hit.name, hit.line)
        if key in seen:
            continue
        seen.add(key)
        unique.append(hit)
    return unique


def build_outline(path: str, source: str, language: str | None = None) -> dict:
    language = language or detect_language(path)
    hits = _outline_python(source) if language == "python" else _outline_generic(source, language)
    lines = source.splitlines()
    return {
        "language": language,
        "total_lines": len(lines),
        "symbol_count": len(hits),
        "symbols": [h.to_dict() for h in hits[:200]],
    }


# ───────────────────────────────────────────────
# 符号定位（给 LLM 精准上下文，省 token）
# ───────────────────────────────────────────────
def find_symbol(source: str, name: str, language: str, context_lines: int = 6) -> list[dict]:
    lines = source.splitlines()
    results: list[dict] = []
    pattern = re.compile(
        r"^\s*(?:def|async def|class|struct|function|func|fn|public|private|static|inline|virtual)?\s*"
        r"[\w<>:&*~\s]*\b" + re.escape(name) + r"\b",
        re.M,
    )
    for match in pattern.finditer(source):
        start = source.count("\n", 0, match.start())
        end = min(len(lines), start + context_lines * 6)
        # 简单块终止：遇到下一行顶格且非空且缩进为 0 的行就停（粗略但够用）
        base_indent = len(lines[start]) - len(lines[start].lstrip())
        for idx in range(start + 1, min(len(lines), start + 400)):
            text = lines[idx].strip()
            indent = len(lines[idx]) - len(lines[idx].lstrip())
            if text and indent <= base_indent and idx > start:
                end = idx
                break
        results.append(
            {
                "line": start + 1,
                "end_line": end,
                "snippet": "\n".join(lines[start:end])[:4000],
            }
        )
        if len(results) >= 3:
            break
    return results


# ───────────────────────────────────────────────
# 启发式 Review（本地提出改进建议）
# ───────────────────────────────────────────────
def _finding(rule: str, severity: str, line: int, message: str, suggestion: str) -> dict:
    return {
        "rule": rule,
        "severity": severity,
        "line": line,
        "message": message,
        "suggestion": suggestion,
    }


_PY_RULES = [
    ("bare_except", "medium", re.compile(r"^\s*except\s*:"), "裸 except 会吞掉所有异常（含 KeyboardInterrupt）。", "改成 `except Exception as exc:` 并至少记日志。"),
    ("wildcard_import", "low", re.compile(r"^\s*from\s+[\w.]+\s+import\s+\*"), "通配符导入污染命名空间。", "显式列出需要的符号。"),
    ("dynamic_exec", "high", re.compile(r"\b(eval|exec)\s*\("), "动态执行字符串可能被注入利用。", "改用 ast.literal_eval 或显式分派。"),
    ("shell_true", "high", re.compile(r"subprocess\.[^\n]*shell\s*=\s*True"), "shell=True 存在命令注入风险。", "传参数列表 args=[...]，去掉 shell=True。"),
    ("mutable_default", "medium", re.compile(r"def\s+\w+\([^)]*=\s*(\[\]|\{\}|dict\(\)|list\(\))"), "可变对象作默认参数会被跨调用共享。", "默认值用 None，函数内再初始化。"),
    ("assert_usage", "low", re.compile(r"^\s*assert\s+"), "assert 在 -O 模式下会被剥离，不能做输入校验。", "生产代码改用显式 raise。"),
]

_C_RULES = [
    ("unsafe_copy", "high", re.compile(r"\b(strcpy|strcat|gets|sprintf)\s*\("), "不检查长度的拷贝函数会导致缓冲区溢出。", "换成 snprintf / strncpy / fgets 等带长度限制的版本。"),
    ("magic_free", "low", re.compile(r"\bfree\s*\("), "free 后请把指针置 NULL，避免悬垂指针。", "free(p); p = NULL;"),
]


def review_source(
    path: str,
    source: str,
    redactor: Redactor,
    *,
    max_findings: int = 40,
    language: str | None = None,
) -> list[dict]:
    language = language or detect_language(path)
    lines = source.splitlines()
    findings: list[dict] = []

    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if len(line) > 120:
            findings.append(_finding("long_line", "low", line_no, f"第 {line_no} 行过长（{len(line)} 字符）。", "拆分长行或提取中间变量，保持在 120 字符内。"))
        match = _MARKERS.search(stripped)
        if match:
            findings.append(_finding("todo_marker", "low", line_no, f"遗留标记：{match.group(0)[:100]}", "确认这条 TODO 是否还有效，无效就删掉。"))
        rules = _PY_RULES if language == "python" else (_C_RULES if language in ("c", "cpp") else [])
        for rule, severity, pattern, message, suggestion in rules:
            if pattern.search(line):
                findings.append(_finding(rule, severity, line_no, message, suggestion))
        if len(findings) >= max_findings:
            break

    if len(findings) < max_findings:
        for item in redactor.findings(source, limit=max_findings - len(findings)):
            findings.append(_finding(f"secret:{item['rule']}", item["severity"], item["line"], item["message"], item["suggestion"]))

    if language == "python":
        findings.extend(_python_metrics(source, max_findings - len(findings)))

    findings.sort(key=lambda f: ({"high": 0, "medium": 1, "low": 2}.get(f["severity"], 3), f["line"]))
    return findings[:max_findings]


def _python_metrics(source: str, budget: int) -> list[dict]:
    if budget <= 0:
        return []
    out: list[dict] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [_finding("syntax_error", "high", getattr(exc, "lineno", 1) or 1, f"Python 语法错误：{exc.msg}", "先修正语法，否则无法静态分析。")]
    for node in ast.walk(tree):
        if len(out) >= budget:
            break
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = node.lineno
            end = getattr(node, "end_lineno", start) or start
            length = end - start + 1
            if length > 80:
                out.append(_finding("long_function", "medium", start, f"函数 {node.name} 过长（{length} 行）。", "按职责拆成若干小函数，每个只做一件事。"))
            nesting = _max_depth(node)
            if nesting >= 5:
                out.append(_finding("deep_nesting", "medium", start, f"函数 {node.name} 嵌套过深（{nesting} 层）。", "用早返回 / 卫语句拍平嵌套。"))
    return out


def _max_depth(node: ast.AST, depth: int = 0) -> int:
    best = depth
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.If, ast.For, ast.While, ast.With, ast.Try)):
            best = max(best, _max_depth(child, depth + 1))
        else:
            best = max(best, _max_depth(child, depth))
    return best
