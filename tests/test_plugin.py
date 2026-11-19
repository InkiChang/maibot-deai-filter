"""离线验证：用最小 SDK 桩调用插件的 after_split handler。

不连接宿主、不发送消息；只验证段结构、空段策略、异常安全。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tomllib
import types
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

sdk = types.ModuleType("maibot_sdk")


class HookHandler:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, fn):
        return fn


class MaiBotPlugin:
    def __init__(self):
        self.config = {}
        self.ctx = types.SimpleNamespace(logger=None)


sdk.HookHandler = HookHandler
sdk.MaiBotPlugin = MaiBotPlugin

types_mod = types.ModuleType("maibot_sdk.types")


class HookMode:
    BLOCKING = "blocking"
    OBSERVE = "observe"


class HookOrder:
    EARLY = "early"
    NORMAL = "normal"
    LATE = "late"


class ErrorPolicy:
    ABORT = "abort"
    SKIP = "skip"
    LOG = "log"


types_mod.HookMode = HookMode
types_mod.HookOrder = HookOrder
types_mod.ErrorPolicy = ErrorPolicy
sys.modules["maibot_sdk"] = sdk
sys.modules["maibot_sdk.types"] = types_mod

import plugin as mod  # noqa: E402

CONFIG = tomllib.loads((BASE / "config.toml").read_text(encoding="utf-8"))


def seg(text, **extra):
    return {"text": text, "separator": "", "quote_previous": False, "is_correction": False, **extra}


def run(segments):
    inst = mod.DeAiFilterPlugin()
    inst.config = CONFIG
    return asyncio.run(inst.deai_filter(segments=segments, session_id="test", enable_chinese_typo=True))


def main() -> int:
    failures = []

    # 1. 正常规则 + 丢弃整段套话
    r = run([
        seg("**结论**：明天会下雨。"),
        seg("作为 AI 助手，我理解你的感受。"),
        seg("（揉了揉眼睛）那就带伞吧。"),
        seg("希望这些内容对你有帮助。"),
    ])
    out = r.get("modified_kwargs", {}).get("segments", [])
    texts = [s["text"] for s in out]
    print("CASE1", json.dumps(texts, ensure_ascii=False))
    if texts != ["结论：明天会下雨", "那就带伞吧"]:
        failures.append(f"case1 unexpected: {texts}")
    for s in out:
        if set(s) != {"text", "separator", "quote_previous", "is_correction"}:
            failures.append(f"case1 bad keys: {set(s)}")
        if not s["text"].strip():
            failures.append("case1 empty text leaked")

    # 2. 全部为空 → 必须原样 continue，不能返回空 segments
    r2 = run([seg("作为AI，希望以上内容对你有帮助。")])
    if "modified_kwargs" in r2:
        failures.append("case2 should not modify when everything is filtered")
    print("CASE2", r2)

    # 3. 无需改动 → continue
    r3 = run([seg("今天风挺大的")])
    if "modified_kwargs" in r3:
        failures.append("case3 unexpected modification")
    print("CASE3", r3)

    # 4. 跨段复读
    dup = "早起真的太难了，我每次都要和被窝搏斗十分钟"
    r4 = run([seg(dup), seg(dup + "。")])
    t4 = [s["text"] for s in r4.get("modified_kwargs", {}).get("segments", [])]
    print("CASE4", json.dumps(t4, ensure_ascii=False))
    if len(t4) != 1:
        failures.append(f"case4 dedupe failed: {t4}")

    # 5. 异常输入 → 不抛错
    r5 = run([{"bad": "segment"}])
    print("CASE5", r5)
    if r5 != {"action": "continue"}:
        failures.append(f"case5 unexpected: {r5}")

    # 6. 关闭总开关 → 不动
    off = {"plugin": {"enabled": False}, "deai": {"enabled": True}}
    inst = mod.DeAiFilterPlugin()
    inst.config = off
    r6 = asyncio.run(inst.deai_filter(segments=[seg("作为AI，我理解你。")]))
    print("CASE6", r6)
    if "modified_kwargs" in r6:
        failures.append("case6 disabled plugin still modified")

    # 7. URL、反例与常见格式边界：不要误删有效语义或链接
    opts = mod.DeAiOptions.from_dict(CONFIG["deai"])
    checks = [
        ("plain URL preserved", "文档链接：https://example.com/a?b=1", "文档链接：https://example.com/a?b=1"),
        ("URL markdown unwrapped", "[https://example.com](https://example.com)", "https://example.com"),
        ("negation preserved", "我不是 AI 助手，这只是玩笑。", "我不是 AI 助手，这只是玩笑"),
        ("think tag removed", "<think>先想一下</think>答案。", "答案"),
        ("single ordinal preserved", "首先，先坐下。", "首先，先坐下"),
        ("multiple ordinals removed", "首先，先坐下。其次，再喝水。", "先坐下。再喝水"),
        ("fenced numbered list", "```\n1. item\n2. next\n```", "item\nnext"),
        ("ordinary short parentheticals preserved", "（笑）（小声）今天真冷。", "（笑）（小声）今天真冷"),
        ("action parenthetical removed", "（揉了揉眼睛）今天真冷。", "今天真冷"),
        ("single em-dash preserved", "我想说——算了。", "我想说——算了"),
        ("two em-dashes normalized", "我想说——算了——回家吧。", "我想说，算了，回家吧"),
    ]
    for label, source, expected in checks:
        actual = mod.deai_text(source, opts)
        if actual != expected:
            failures.append(f"case7 {label}: expected={actual!r}, wanted={expected!r}")
    print("CASE7", len(checks), "rule assertions")

    # 8. Hook 对坏段结构 fail-open；合法段输出严格规范化为四个宿主字段
    bad_inputs = [None, "not a list", [], [None], [{"text": None}], [{"text": 17}]]
    for bad in bad_inputs:
        inst = mod.DeAiFilterPlugin()
        inst.config = CONFIG
        r8 = asyncio.run(inst.deai_filter(segments=bad, session_id="test"))
        if r8 != {"action": "continue"}:
            failures.append(f"case8 malformed input was not fail-open: {bad!r} -> {r8!r}")
    inst = mod.DeAiFilterPlugin()
    inst.config = CONFIG
    r8b = asyncio.run(inst.deai_filter(
        segments=[seg("好问题！今天真冷。", separator="。", quote_previous=True, extra="discard")],
        session_id="test",
    ))
    modified = r8b.get("modified_kwargs", {})
    normalized = modified.get("segments", [])
    expected_keys = {"text", "separator", "quote_previous", "is_correction"}
    if len(normalized) != 1 or set(normalized[0]) != expected_keys:
        failures.append(f"case8 unexpected normalized segment: {normalized!r}")
    elif normalized[0]["separator"] != "。" or not normalized[0]["quote_previous"]:
        failures.append(f"case8 host segment metadata changed: {normalized[0]!r}")
    if modified.get("session_id") != "test":
        failures.append(f"case8 hook kwargs not preserved: {modified!r}")
    ordered = asyncio.run(inst.deai_filter(
        segments=[seg("**第一段**。"), seg("第二段！")],
        session_id="sequence",
    ))
    ordered_texts = [
        item["text"] for item in ordered.get("modified_kwargs", {}).get("segments", [])
    ]
    if ordered_texts != ["第一段", "第二段！"]:
        failures.append(f"case8 segment order changed: {ordered_texts!r}")
    print("CASE8 malformed-input, segment-shape and order assertions")

    # 9. 选项、名字前缀和自定义过滤词
    named_opts = mod.DeAiOptions.from_dict({**CONFIG["deai"], "bot_names": ["小鱼"]})
    name_out = mod.deai_text("小鱼：今天风挺大的。", named_opts)
    if name_out != "今天风挺大的":
        failures.append(f"case9 bot name prefix: {name_out!r}")
    custom_opts = mod.DeAiOptions.from_dict({**CONFIG["deai"], "extra_phrases": ["请多关照"]})
    custom_out = mod.deai_text("请多关照，明天见。", custom_opts)
    if custom_out != "明天见":
        failures.append(f"case9 custom phrase removal: {custom_out!r}")
    disabled_opts = mod.DeAiOptions.from_dict({"enabled": False})
    disabled_out = mod.deai_text("**保留原样**。", disabled_opts)
    if disabled_out != "**保留原样**。":
        failures.append(f"case9 disabled pure function changed text: {disabled_out!r}")
    malformed_regex_opts = mod.DeAiOptions.from_dict({**CONFIG["deai"], "extra_phrases": ["/(abc/"]})
    malformed_regex_out = mod.deai_text("正常内容。", malformed_regex_opts)
    if malformed_regex_out != "正常内容":
        failures.append(f"case9 malformed custom regex disrupted filtering: {malformed_regex_out!r}")
    print("CASE9 options, bot-name and custom-filter assertions")

    print("RESULT", "PASS" if not failures else "FAIL")
    for f in failures:
        print(" -", f)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
