from __future__ import annotations

import re

CJK = re.compile(r"[\u4e00-\u9fff]")

# ---- 归一化（P0-4：让"同义写法"落到同一个键上）----
# 全角 ASCII（！-～）与全角空格 → 半角
_FULLWIDTH_ASCII_OFFSET = 0xFEE0
_IDEOGRAPHIC_SPACE = "\u3000"
# 需要剔除的分隔符：空白 + 常见连接符/标点。剔除后
# `CPU 使用率` / `cpu_usage` / `cpu-usage` / `CPU使用率` 全部归一为 `cpu使用率`。
_SEPARATOR_TABLE = str.maketrans(
    "", "", " \t\r\n\v\f_-./\\|:：;,，；、()（）[]【】{}<>《》\"'`+*#~!?？!@$%^&="
)


def to_halfwidth(text: str) -> str:
    """全角 ASCII 与全角空格转半角（中文与其它字符原样保留）。"""
    out: list[str] = []
    for ch in text:
        if ch == _IDEOGRAPHIC_SPACE:
            out.append(" ")
        elif 0xFF01 <= ord(ch) <= 0xFF5E:
            out.append(chr(ord(ch) - _FULLWIDTH_ASCII_OFFSET))
        else:
            out.append(ch)
    return "".join(out)


def normalize_text(text: str | None) -> str:
    """归一化文本：全角→半角、转小写、剔除空白与分隔符。

    目的：解决裸子串匹配对分隔符敏感导致的漏命中。例如规则关键词
    `cpu 使用率`（含空格）匹配不到告警里的 `CPU使用率`；归一后两者都是
    `cpu使用率`，可以命中。P0-4 用它同时归一化规则关键词与待匹配文本。
    """
    if not text:
        return ""
    return to_halfwidth(str(text)).lower().translate(_SEPARATOR_TABLE)


# 技术实体：字母开头、长度 >= 2 的英文/数字 token（如 redis / oom / upstream）。
_ENTITY_RE = re.compile(r"[a-z][a-z0-9]{1,}")


def extract_entities(text: str | None) -> set[str]:
    """从文本中抽取英文技术实体，用于"结论实体是否能在证据里找到"的交叉校验。

    注意只用半角化 + 小写，**不能走 `normalize_text`** —— 后者会剔除空格，
    把 `used_memory exceeds` 粘成单个 token，实体抽取就失效了。
    """
    if not text:
        return set()
    return set(_ENTITY_RE.findall(to_halfwidth(str(text)).lower()))


def tokenize(text: str) -> set[str]:
    """中文字符/相邻 bigram 与英文数字整词混合分词。"""
    tokens: set[str] = set()
    cjk_run: list[str] = []
    latin: list[str] = []

    def flush_latin() -> None:
        if latin:
            tokens.add("".join(latin))
            latin.clear()

    def flush_cjk() -> None:
        if cjk_run:
            tokens.update(cjk_run)
            tokens.update("".join(cjk_run[i : i + 2]) for i in range(len(cjk_run) - 1))
            cjk_run.clear()

    for ch in text.lower():
        if ch.isascii() and ch.isalnum():
            flush_cjk()
            latin.append(ch)
        elif CJK.match(ch):
            flush_latin()
            cjk_run.append(ch)
        else:
            flush_latin()
            flush_cjk()
    flush_latin()
    flush_cjk()
    return tokens


def similarity(a: str, b: str, same_severity: bool = True) -> float:
    left, right = tokenize(a), tokenize(b)
    score = len(left & right) / len(left | right) if left or right else 1.0
    score += 0.1 if same_severity else -0.2
    return max(0.0, min(1.0, score))


# 文档代码片段中的兼容名称。
_tokenize = tokenize
_similarity = similarity
