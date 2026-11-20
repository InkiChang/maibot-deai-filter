"""Opt-in real-host reply contract probe. Run only in a disposable container.

Uses real host imports and real SDK. Synthetic supervisors replace IPC, never
contact adapters/models. No production configuration or user messages are read.
This fixture is not production evidence and must not run in the host process.
"""
import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import patch
import importlib.metadata
import hashlib
from pathlib import Path

# SHA-256 of PUBLIC source files at upstream commit 478f671e64467121fd3e39205c8d3507b5ce0bd6.
PUBLIC_SOURCES = {
    "src/maisaka/builtin_tool/context.py": "80645bc18f1581676073d60f57ef3dcc0b3422ec285ab6b6c6ca61878b5cd3ca",
    "src/chat/utils/utils.py": "2c014b628cb13f9a483307894ed942fe7cc21b72ed04a9afebe5923d836d995f",
    "src/plugin_runtime/host/hook_dispatcher.py": "f2915286d7a5fa1fde00db5416340e5dd7801ed1b5dc69d77b5065417bd76e3e",
    "src/plugin_runtime/host/component_registry.py": "682cfa60a6ecb1029ae421f4127ca4ef58c4283cb51a89255c0466a773fc58bc",
    "src/plugin_runtime/host/hook_spec_registry.py": "4471369de6a4773d304ce8d23e09d48bdbda700716968910a5bdf7f869efb0f8",
    "src/maisaka/chat_loop_service.py": "95fac04fc042e7be288085307c78108718f3f6c958a0ab4e09d3efa87aa1ae7b",
}
for relative, expected in PUBLIC_SOURCES.items():
    assert hashlib.sha256((Path("/MaiMBot") / relative).read_bytes()).hexdigest() == expected, relative

assert importlib.metadata.version("maibot-plugin-sdk") == "2.23.0", "SDK version mismatch"

from src.maisaka.chat_loop_service import register_maisaka_hook_specs
from src.maisaka.builtin_tool.context import BuiltinToolRuntimeContext
from src.chat.utils.utils import ProcessedResponseSegment
from src.plugin_runtime.host.component_registry import ComponentRegistry
from src.plugin_runtime.host.hook_dispatcher import HookDispatcher
from src.plugin_runtime.host.hook_spec_registry import HookSpecRegistry

HOOK = "maisaka.reply.after_split"


def segment(text="synthetic"):
    return {"text": text, "separator": "", "quote_previous": False, "is_correction": False}


def supervisor(specs, plugin_id, order="normal", mode="blocking", policy="skip", callback=None,
               group="external", timeout_ms=0):
    registry = ComponentRegistry(specs)
    registry.register_plugin_components(plugin_id, [{
        "name": "probe", "component_type": "HOOK_HANDLER", "metadata": {
            "hook": HOOK, "mode": mode, "order": order,
            "error_policy": policy, "timeout_ms": timeout_ms,
        },
    }])
    class Supervisor:
        component_registry = registry
        group_name = group
        async def invoke_plugin(self, method, pid, name, args, timeout_ms=None):
            assert method == "plugin.invoke_hook"
            return SimpleNamespace(payload=await callback(args))
    return Supervisor()


async def main():
    specs = HookSpecRegistry()
    register_maisaka_hook_specs(specs)
    spec = specs.get_hook_spec(HOOK)
    assert spec is not None and not spec.allow_abort and spec.allow_kwargs_mutation
    assert spec.default_timeout_ms == 3000
    checks = ["real_hook_spec"]
    trace = []
    observed = asyncio.Event()
    def recorder(label, observe=False):
        async def call(args):
            trace.append(label)
            if observe:
                observed.set()
                return {"action": "abort", "modified_kwargs": {"segments": []}}
            return {"action": "continue"}
        return call
    dispatcher = HookDispatcher(hook_spec_registry=specs)
    try:
        # Deliberately supply reverse order; host sorts by mode/order/source/id.
        targets = [
            supervisor(specs, "z.observe", "early", "observe", callback=recorder("observe", True)),
            supervisor(specs, "a.late", "late", callback=recorder("late")),
            supervisor(specs, "a.normal", callback=recorder("normal")),
            supervisor(specs, "z.early", "early", callback=recorder("early-z")),
            supervisor(specs, "a.early", "early", callback=recorder("early-a")),
            supervisor(specs, "z.builtin", "early", callback=recorder("builtin"), group="builtin"),
        ]
        original = {"segments": [segment()], "session_id": "synthetic-session", "enable_chinese_typo": False}
        result = await dispatcher.invoke_hook(HOOK, supervisors=targets, **original)
        await asyncio.wait_for(observed.wait(), 1)
        assert trace == ["builtin", "early-a", "early-z", "normal", "late", "observe"]
        assert not result.aborted and result.kwargs == original
        checks.append("real_dispatch_order_and_observe_ignored")

        for kind in ("raise", "timeout", "abort"):
            async def failure(args):
                if kind == "raise":
                    raise RuntimeError("synthetic_failure")
                if kind == "timeout":
                    await asyncio.sleep(1)
                return {"action": "abort"}
            trace.clear()
            result = await dispatcher.invoke_hook(HOOK, supervisors=[
                supervisor(specs, "a.failure." + kind, "early", policy="skip", callback=failure, timeout_ms=10),
                supervisor(specs, "z.after." + kind, "late", callback=recorder("after")),
            ], **original)
            assert not result.aborted and result.kwargs == original and result.errors
            assert trace == ["after"]
            checks.append("real_dispatch_" + kind + "_continues")

        async def replace_kwargs(args):
            return {"modified_kwargs": {"segments": [segment("replacement")]}}
        result = await dispatcher.invoke_hook(HOOK, supervisors=[
            supervisor(specs, "replace", callback=replace_kwargs)], **original)
        assert set(result.kwargs) == {"segments"}
        checks.append("kwargs_replacement_not_merge")

        # Call the actual host consumer/validator, not a copied schema.
        received = []
        response = dict(original)
        class Manager:
            async def invoke_hook(self, hook_name, **kwargs):
                assert hook_name == HOOK
                received.append(kwargs)
                return SimpleNamespace(kwargs=response)
        context = SimpleNamespace(runtime=SimpleNamespace(session_id="synthetic-session"),
                                  get_runtime_manager=lambda: Manager())
        source = [ProcessedResponseSegment(**segment())]
        transform = BuiltinToolRuntimeContext._transform_reply_segments
        actual = await transform(context, source, enable_chinese_typo=False)
        assert received[-1] == original and [asdict(x) for x in actual] == original["segments"]
        checks.append("real_consumer_payload")
        response = {**original, "session_id": "ignored", "extra": "ignored", "segments": [
            {"text": "changed", "separator": "!", "quote_previous": True, "is_correction": True}]}
        actual = await transform(context, source, enable_chinese_typo=False)
        assert [asdict(x) for x in actual] == response["segments"]
        assert context.runtime.session_id == "synthetic-session"
        checks.append("four_fields_adopted_identity_not_reassigned")
        for bad in ([], None, [None], [{**segment(), "extra": 1}],
                    [{**segment(), "text": " "}], [{**segment(), "separator": None}],
                    [{**segment(), "quote_previous": 1}], [{**segment(), "is_correction": 0}]):
            response = {**original, "segments": bad}
            try:
                await transform(context, source, enable_chinese_typo=False)
            except ValueError:
                pass
            else:
                raise AssertionError("invalid segments accepted")
        response = {}
        try:
            await transform(context, source, enable_chinese_typo=False)
        except KeyError:
            pass
        else:
            raise AssertionError("missing segments accepted")
        checks.append("invalid_segments_raise_not_fallback")

        # Exercise this repository's real plugin through the actual dispatcher
        # and consumer. Only transport and configuration access are injected.
        import plugin as plugin_module
        instance = plugin_module.create_plugin()
        instance._set_context(SimpleNamespace(logger=None))  # inert stand-in for Runner injection
        instance.set_plugin_config({"deai": {"enabled": True}})
        async def filter_callback(args):
            return await instance.deai_filter(**args)
        active = [supervisor(specs, "synthetic.deai", "early", callback=filter_callback)]
        class DispatchManager:
            async def invoke_hook(self, name, **kwargs):
                return await dispatcher.invoke_hook(name, supervisors=active, **kwargs)
        context.get_runtime_manager = lambda: DispatchManager()
        source = [ProcessedResponseSegment(**segment("**synthetic**。"))]
        actual = await transform(context, source, enable_chinese_typo=False)
        assert actual[0].text == "synthetic"
        with patch.object(plugin_module, "deai_text", side_effect=RuntimeError("synthetic_filter_error")):
            actual = await transform(context, source, enable_chinese_typo=False)
            assert asdict(actual[0]) == asdict(source[0])
        checks.append("real_plugin_dispatch_consumer_and_fail_open")

        # A newline prevents random pre-merging; force the rule splitter and
        # one final message. Assert both intermediate and final observations.
        import src.chat.utils.utils as utils
        seen = []
        async def transform_phase(segments):
            seen.append([s.text for s in segments])
            return await transform(context, segments, enable_chinese_typo=False)
        with patch.multiple(utils.global_config.response_splitter, mode="rule", enable=True,
                            enable_kaomoji_protection=False, max_sentence_num=8, max_split_num=1), \
                patch.multiple(utils.global_config.response_post_process,
                               enable_response_post_process=True, remove_bracket_content=False):
            merged = await utils.process_llm_response_segments_async(
                "**first**\n**second**", transform_segments=transform_phase)
        assert seen == [["**first**", "**second**"]]
        assert len(merged) == 1 and merged[0].text == "first\nsecond"
        checks.append("real_rule_split_plugin_transform_and_final_merge")
        print("REPLY_CONTRACT_PASS " + json.dumps({"checks": checks, "production_access": False}))
    finally:
        await dispatcher.stop()


if __name__ == "__main__":
    asyncio.run(main())
