"""语义分层切片：保护 Markdown 标题、代码块和表格。"""
from __future__ import annotations

from dataclasses import dataclass
import re

try:
    import tiktoken
except ImportError:  # pragma: no cover - optional dependency in minimal installs
    tiktoken = None

MAX_CHARS = 4096
WINDOW = 512
OVERLAP = 128

@dataclass(frozen=True)
class Chunk:
    text: str
    component: str
    kind: str = "text"

def _clean(text: str, preserve_lines: bool = False) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text or "")
    if preserve_lines:
        return "\n".join(line.rstrip() for line in text.splitlines()).strip()
    return re.sub(r"\s+", " ", text).strip()

def _hard_split(
    text: str,
    component: str,
    kind: str,
    preserve_lines: bool = False,
    max_chars: int = MAX_CHARS,
) -> list[Chunk]:
    text = _clean(text, preserve_lines=preserve_lines)
    if not text:
        return []
    max_chars = max(1, max_chars)
    if len(text) <= max_chars:
        return [Chunk(text, component, kind)]
    if preserve_lines:
        chunks: list[Chunk] = []
        current: list[str] = []
        current_size = 0
        for line in text.splitlines():
            # Keep code/table rows intact whenever a row fits the field limit.
            if current and current_size + len(line) + 1 > max_chars:
                chunks.append(Chunk("\n".join(current), component, kind))
                current, current_size = [], 0
            if len(line) > max_chars:
                if current:
                    chunks.append(Chunk("\n".join(current), component, kind))
                    current, current_size = [], 0
                chunks.extend(
                    Chunk(line[start:start + max_chars], component, kind)
                    for start in range(0, len(line), max_chars)
                )
            else:
                current.append(line)
                current_size += len(line) + 1
        if current:
            chunks.append(Chunk("\n".join(current), component, kind))
        return chunks
    step = max(1, max_chars - 256)
    return [Chunk(text[i:i + max_chars], component, kind) for i in range(0, len(text), step)]

def _token_windows(text: str, budget: int, component: str) -> list[Chunk]:
    """Create 512/128-token windows while retaining the 4096-character bound."""
    if not text:
        return []
    if tiktoken is None:
        # Conservative fallback for deployments that intentionally omit the
        # tokenizer package. Four UTF-8 characters is a useful upper bound for
        # mixed Chinese/English text and keeps the public contract stable.
        size, overlap = min(WINDOW * 4, budget), min(OVERLAP * 4, max(0, budget - 1))
        step = max(1, size - overlap)
        return [Chunk(text[i:i + size], component, "text") for i in range(0, len(text), step)]
    encoder = tiktoken.get_encoding("cl100k_base")
    tokens = encoder.encode(text)
    if not tokens:
        return []
    size = min(WINDOW, max(1, budget // 2))
    overlap = min(OVERLAP, max(0, size - 1))
    step = max(1, size - overlap)
    result = []
    for start in range(0, len(tokens), step):
        value = encoder.decode(tokens[start:start + size])
        if len(value) > budget:
            value = value[:budget]
        if value:
            result.append(Chunk(value, component, "text"))
    return result


def split(content: str, component: str = "common") -> list[Chunk]:
    """按 Markdown 结构切分；普通文本采用 512/128 token 窗口。"""
    content = content or ""
    blocks: list[tuple[str, str]] = []
    lines = content.splitlines()
    i = 0
    headings: list[str] = []
    buffer: list[str] = []
    def flush() -> None:
        nonlocal buffer
        if buffer:
            blocks.append(("\n".join(buffer), "text"))
            buffer = []
    while i < len(lines):
        line = lines[i]
        if line.startswith("```") or line.startswith("~~~"):
            flush(); fence = line[:3]; code = [line]; i += 1
            while i < len(lines):
                code.append(lines[i]); end = lines[i].startswith(fence)
                i += 1
                if end: break
            blocks.append(("\n".join(code), "code")); continue
        if i + 1 < len(lines) and "|" in line and re.match(r"^\s*\|?\s*:?-{3,}", lines[i + 1]):
            flush(); table = [line, lines[i + 1]]; i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip(): table.append(lines[i]); i += 1
            blocks.append(("\n".join(table), "table")); continue
        match = re.match(r"^(#{2,3})\s+(.+)$", line)
        if match:
            flush(); level, title = len(match.group(1)), _clean(match.group(2))
            headings = headings[:level - 2] + [title]
            blocks.append((" > ".join(headings), "heading")); i += 1; continue
        buffer.append(line); i += 1
    flush()
    result: list[Chunk] = []
    prefix = ""
    for raw, kind in blocks:
        if kind == "heading": prefix = raw; continue
        preserve_lines = kind in {"code", "table"}
        body = _clean(raw, preserve_lines=preserve_lines)
        # Reserve room for the heading path on every slice.  Without this,
        # long sections lose their Markdown context after the first window.
        prefix_text = prefix[:MAX_CHARS]
        body_budget = max(1, MAX_CHARS - len(prefix_text) - (1 if prefix_text else 0))

        def add_with_prefix(chunks: list[Chunk]) -> None:
            if not chunks:
                if prefix_text:
                    result.append(Chunk(prefix_text, component, "text"))
                return
            for chunk in chunks:
                value = f"{prefix_text}\n{chunk.text}" if prefix_text else chunk.text
                result.append(Chunk(value[:MAX_CHARS], component, chunk.kind))

        if kind in {"code", "table"}:
            add_with_prefix(_hard_split(body, component, kind, preserve_lines=True, max_chars=body_budget))
            continue
        if len(body) <= body_budget:
            add_with_prefix(_hard_split(body, component, "text", max_chars=body_budget))
            continue
        add_with_prefix(_token_windows(body, body_budget, component))
    return result or _hard_split(content, component, "text")
