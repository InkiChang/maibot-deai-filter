"""去 AI 味的纯文本规则（移植自 dsh-fish-soul lib/behavior.js 的 deAi / splitChat 思路）。

只做删、改两类动作，不生成新内容；不截断长度（长度策略由宿主配置决定）。
所有函数都是纯函数，方便离线单测与在插件里复用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


def _num(value, default, lo, hi):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if n != n:  # NaN
        return default
    return max(lo, min(hi, n))


@dataclass
class DeAiOptions:
    enabled: bool = True
    strip_markdown: bool = True
    strip_actions: bool = True
    strip_ai_phrases: bool = True
    strip_model_tags: bool = True
    strip_self_name: bool = True
    dash_to_comma: bool = True
    strip_ordinals: bool = True
    drop_trailing_period: bool = True
    dedupe_segments: bool = True
    dedupe_threshold: float = 0.75
    dedupe_min_len: int = 12
    extra_phrases: list[str] = field(default_factory=list)
    bot_names: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw) -> "DeAiOptions":
        v = raw or {}
        opts = cls()
        for name in (
            "enabled",
            "strip_markdown",
            "strip_actions",
            "strip_ai_phrases",
            "strip_model_tags",
            "strip_self_name",
            "dash_to_comma",
            "strip_ordinals",
            "drop_trailing_period",
            "dedupe_segments",
        ):
            if name in v and v[name] is not None:
                setattr(opts, name, bool(v[name]))
        opts.dedupe_threshold = _num(v.get("dedupe_threshold", 0.75), 0.75, 0.5, 1.0)
        opts.dedupe_min_len = int(_num(v.get("dedupe_min_len", 12), 12, 4, 60))
        opts.extra_phrases = clean_list(v.get("extra_phrases"))
        opts.bot_names = clean_list(v.get("bot_names"))
        return opts


def clean_list(value) -> list[str]:
    if isinstance(value, str):
        items = value.splitlines()
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return []
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and len(text) <= 100 and text not in out:
            out.append(text)
    return out[:50]


# ───────────────────────── markdown ─────────────────────────

def strip_markdown(text: str) -> str:
    t = text
    t = re.sub(r"```[^\n]*\n([\s\S]*?)```", r"\1", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s+", "", t, flags=re.M)
    t = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", t)
    t = re.sub(r"__([^_\n]+)__", r"\1", t)
    t = re.sub(r"^\s*>\s?", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*•]\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*\d{1,2}[.、)]\s+", "", t, flags=re.M)
    t = re.sub(
        r"(?<!!)\[([^\]\n]+)\]\((https?://[^)\s]+)\)",
        lambda m: m.group(2) if m.group(1).strip() == m.group(2) else f"{m.group(1)} {m.group(2)}",
        t,
    )
    t = re.sub(r"^\s*\|?\s*:?-{3,}.*$", "", t, flags=re.M)
    t = re.sub(
        r"^\s*\|(.+)\|\s*$",
        lambda m: "，".join(c.strip() for c in m.group(1).split("|") if c.strip()),
        t,
        flags=re.M,
    )
    t = re.sub(r"`([^`\n]+)`", r"\1", t)
    return t


# ───────────────────────── 动作描写 ─────────────────────────

ACTION_HEAD = re.compile(
    r"^(?:她|他|鱼|小鱼)?(?:轻轻|微微|默默|悄悄|偷偷|慢慢|缓缓|小声地?|低声|歪|眨|揉|摸|拍|笑|叹|挠|托|抱|蹭|戳|探|翻|撇|嘟|鼓|眯|伸|扭|撅|捂|捏|晃|摇|点了点|点点|低下|抬起|抬头|低头|转身|转过|凑|靠|躺|趴|打了个|打个|吐了吐|吐吐|咬|舔|皱|瞪|红着|脸红|脸一红|耳朵|尾巴|双手|一边|眼睛|眼神|声音|嘴角|嘟囔|嘀咕|扶额|捂脸|叉腰|耸肩|摊手|托腮)"
)
HAN_RE = re.compile(r"[\u4e00-\u9fff]")
PAREN_RE = re.compile(r"[（(]([^（）()\n]{1,40})[）)]")
STAR_RE = re.compile(r"(?<![*\w])\*([^*\n]{1,40})\*(?![*\w])")


def looks_like_action(inner: str) -> bool:
    s = str(inner or "").strip()
    han = len(HAN_RE.findall(s))
    if han < 4 or han < len(s) * 0.7:
        return False
    if ACTION_HEAD.match(s):
        return True
    return bool(re.search(r"[了着]", s)) and not re.search(r"[我你]", s)


def strip_actions(text: str) -> str:
    t = PAREN_RE.sub(lambda m: "" if looks_like_action(m.group(1)) else m.group(0), text)
    return STAR_RE.sub(lambda m: "" if looks_like_action(m.group(1)) else m.group(1), t)


# ───────────────────────── AI 腔 ─────────────────────────

_AI_SELF = r"(?:AI|ai|人工智能|语言模型|大模型|AI\s*助手|智能助手|虚拟助手|聊天机器人)"

AI_PHRASES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?:作为|身为)(?:一个|一名|一款)?\s*" + _AI_SELF + r"[^，。,.!！?？\n]*[，,]?\s*"), "AI 自称"),
    (re.compile(r"(?:希望|但愿)(?:这|以上|这些|我的回答|我的内容|我的建议)?(?:些)?(?:内容|信息|回答|建议)?(?:能|可以)?(?:对你|对您)?(?:有(?:所)?帮助|有(?:所)?用|帮(?:到|上)你)[^。！!\n]*[。！!～~]?"), "客套收尾"),
    (re.compile(r"(?:如果|若)(?:你|您)(?:还)?有(?:任何|其他|别的)?(?:问题|疑问|需要)[^。！!\n]*[。！!～~]?"), "客套收尾"),
    (re.compile(r"(?:综上所述|总而言之|总的来说|总结一下|简而言之)[，,：:]?\s*"), "总结腔"),
    (re.compile(r"(?:请注意|需要注意的是|值得注意的是)[，,：:]\s*"), "说明书腔"),
    (re.compile(r"^[ \t]*(?:好问题|问得好|这是(?:一个|个)(?:很|非常)?好(?:的)?问题)[！!，,。.～~]*[ \t]*", re.M), "客服腔"),
    (re.compile(r"我(?:完全|非常|很)?(?:能够|能|可以)?理解你(?:现在)?的(?:感受|心情)[，,。！!]?\s*"), "客服腔"),
    (re.compile(r"(?:接下来|下面)?让我们(?:一起)?(?:来)?(?:看看|看一下|聊聊|分析一下|深入(?:探讨|了解)?)[^，。！？!?：:\n]{0,15}[：:，,。！!]\s*"), "起手铺垫"),
    (re.compile(r"^[ \t]*(?:以下是|下面是)[^，。！？!?\n]{0,20}[：:][ \t]*", re.M), "起手铺垫"),
    (re.compile(r"(?:有(?:任何|什么|其他|别的)?(?:问题|疑问|需要)(?:的话)?[，,]?\s*)?欢迎随时[^。！!\n]*[。！!～~]?"), "客套收尾"),
    (re.compile(r"有(?:任何|什么|其他|别的)?(?:问题|疑问|需要)(?:的话)?[，,]?\s*(?:随时|尽管)(?:找我|告诉我|问我|联系我|跟我说)[^。！!\n]*[。！!～~]?"), "客套收尾"),
]

# 「我只是一个 AI」的自称：排除吐槽 / 反问语境（Python 不支持变长 lookbehind，用扫描判断）
SELF_AI_RE = re.compile(
    r"我(?:只)?是(?:一个|一名|一款|个)?\s*" + _AI_SELF + r"(?!\s*(?:吗|嘛|么|？|\?|的话))[^，。,.!！?？\n]*[，,。.!！]?\s*"
)
DOUBT_TAIL_RE = re.compile(r"(?:怀疑|以为|觉得|当|说|问|猜|认为|觉着|嫌)[^，。,.!！?？\n]{0,3}$")


def _self_ai_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for m in SELF_AI_RE.finditer(text):
        prefix = text[max(0, m.start() - 8) : m.start()]
        if DOUBT_TAIL_RE.search(prefix):
            continue
        spans.append(m.span())
    return spans


def strip_ai_phrases(text: str, changes: set[str] | None = None) -> str:
    t = text
    for pattern, label in AI_PHRASES:
        new = pattern.sub("", t)
        if new != t:
            if changes is not None:
                changes.add(label)
            t = new
    spans = _self_ai_spans(t)
    if spans:
        for start, end in reversed(spans):
            t = t[:start] + t[end:]
        if changes is not None:
            changes.add("AI 自称")
    return t


def extra_regexps(extra: list[str]) -> list[tuple[re.Pattern[str], str]]:
    out = []
    for line in extra:
        m = re.fullmatch(r"/(.+)/([imsu]*)", line)
        try:
            pattern = re.compile(m.group(1) if m else re.escape(line), re.I if m and "i" in m.group(2) else 0)
        except re.error:
            continue
        out.append((pattern, line))
    return out


# ───────────────────────── 标点 / 枚举 ─────────────────────────

ORDINAL_RE = re.compile(r"(?:^|[。！？!?\n，,；;]\s*)(首先|其次|然后|再次|最后|第一|第二|第三)[，,、：:]\s*")


def clean_punct(text: str, *, dash_to_comma: bool, strip_ordinals: bool, changes: set[str]) -> str:
    t = text
    if dash_to_comma and len(re.findall(r"——", t)) >= 2:
        t = re.sub(r"\s*——+\s*", "，", t)
        changes.add("破折号")
    t = re.sub(r"[，,]\s*(?=[，,。！？!?～~…])", "", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    if strip_ordinals:
        words = ORDINAL_RE.findall(t)
        if len(words) >= 2:
            t = ORDINAL_RE.sub(lambda m: m.group(0)[: m.start(1) - m.start(0)], t)
            changes.add("首先其次")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"^[，,、 \t]+", "", t, flags=re.M)
    return t.strip()


# ───────────────────────── 模型模板标签 ─────────────────────────

MODEL_TAGS = "think|thinking|reasoning|tool_call|tool_response|function_call|function_response|function|parameter|json"
TAG_BLOCK_RE = re.compile(rf"<({MODEL_TAGS})\b[^>]*>[\s\S]*?</\1\s*>", re.I)
TAG_OPEN_THINK_RE = re.compile(r"<(?:think|thinking|reasoning)\b[^>]*>[\s\S]*$", re.I)
TAG_STRAY_RE = re.compile(rf"</?(?:{MODEL_TAGS})\b[^>]*>", re.I)
SPECIAL_TOKEN_RE = re.compile(r"<\|[^|>\n]{1,40}\|>")


def strip_model_tags(text: str) -> str:
    t = text
    for _ in range(8):
        new = TAG_BLOCK_RE.sub("", t)
        if new == t:
            break
        t = new
    t = TAG_OPEN_THINK_RE.sub("", t)
    t = TAG_STRAY_RE.sub("", t)
    t = SPECIAL_TOKEN_RE.sub("", t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"^[ \t]+$", "", t, flags=re.M)
    return t.strip()


def strip_self_name(text: str, names: list[str]) -> str:
    clean = [n for n in names if n and len(n) <= 12]
    if not clean:
        return text
    esc = "|".join(re.escape(n) for n in clean)
    return re.sub(rf"^([ \t]*)(?:{esc})[ \t]*[:：][ \t]*", r"\1", text, flags=re.M)


# ───────────────────────── 复读判定 ─────────────────────────

_NORM_STRIP_RE = re.compile(r"[\s。，、！？!?～~…·,.:;:\"'“”‘’「」『』()（）【】\[\]*#\-—|]")


def norm_para(text: str) -> str:
    return _NORM_STRIP_RE.sub("", (text or "").lower().replace("[语音]", ""))


def _bigrams(s: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in range(len(s) - 1):
        g = s[i : i + 2]
        out[g] = out.get(g, 0) + 1
    return out


def bigram_dice(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if len(a) < 2 or len(b) < 2:
        return 0.0
    ga = _bigrams(a)
    inter = 0
    for g, n in _bigrams(b).items():
        inter += min(n, ga.get(g, 0))
    return (2 * inter) / ((len(a) - 1) + (len(b) - 1))


def dedupe_paragraphs(texts: list[str], *, threshold: float = 0.75, min_len: int = 12) -> list[int]:
    """返回应当保留的下标（同轮跨条复读只留第一份）。"""
    kept: list[int] = []
    seen: list[str] = []
    for i, text in enumerate(texts):
        n = norm_para(text)
        length = len(n)
        if length >= min_len:
            dup = any(bigram_dice(k, n) >= threshold for k in seen)
        elif length >= 3:
            dup = n in seen
        else:
            dup = False
        if dup:
            continue
        seen.append(n)
        kept.append(i)
    return kept


# ───────────────────────── 主流程 ─────────────────────────

_DOT_END_RE = re.compile(r"(?<![。.])。$")


def contains_code_markup(text: str) -> bool:
    """Conservatively preserve whole replies with backticks or tilde fences.

    Whole-reply bypass also protects fences split across host segments. Bare
    unmarked code cannot be reliably distinguished from prose by this filter.
    """
    return "`" in text or bool(re.search(r"(?m)^\s*~{3,}", text))


def deai_text(text: str, opts: DeAiOptions, changes: set[str] | None = None) -> str:
    if not opts.enabled or contains_code_markup(text):
        return text
    marks = set() if changes is None else changes
    t = str(text or "")
    before = t

    if opts.strip_model_tags:
        new = strip_model_tags(t)
        if new != t:
            marks.add("模型标签")
            t = new
    if opts.strip_markdown:
        new = strip_markdown(t)
        if new != t:
            marks.add("markdown")
            t = new
    if opts.strip_actions:
        new = strip_actions(t)
        if new != t:
            marks.add("动作描写")
            t = new
    if opts.strip_ai_phrases:
        t = strip_ai_phrases(t, marks)
    for pattern, _label in extra_regexps(opts.extra_phrases):
        new = pattern.sub("", t)
        if new != t:
            marks.add("自定义")
            t = new
    t = clean_punct(t, dash_to_comma=opts.dash_to_comma, strip_ordinals=opts.strip_ordinals, changes=marks)
    if opts.strip_self_name:
        new = strip_self_name(t, opts.bot_names)
        if new != t:
            marks.add("自称名字")
            t = new
    if opts.drop_trailing_period:
        new = "\n".join(_DOT_END_RE.sub("", line) if line.strip() else line for line in t.split("\n"))
        if new != t:
            marks.add("句尾句号")
            t = new
    if t != before:
        t = t.strip()
    return t
