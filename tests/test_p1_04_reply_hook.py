"""P1-04 isolated reply-Hook contract tests.

These tests are deliberately self-contained: they install a tiny in-process SDK
stub and a synthetic dispatcher, then load ``plugin.py``.  No MaiBot process,
network, model, account, or real message is touched.  The dispatcher models the
contract already exercised by the checked-in isolated fixture: a blocking Hook
may return ``modified_kwargs`` and ``action=continue``; on fail-open/skip the
original kwargs remain unchanged. These assertions cover the plugin boundary only;
the pinned host contract is documented separately.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from typing import Any, Callable

BASE = Path(__file__).resolve().parents[1]


class _HookHandler:
    """Capture decorator metadata without importing a real MaiBot SDK."""

    registry: list[tuple[Callable[..., Any], dict[str, Any]]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs

    def __call__(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        self.registry.append((fn, {"args": self.args, **self.kwargs}))
        setattr(fn, "_p1_04_hook_metadata", {"args": self.args, **self.kwargs})
        return fn


class _MaiBotPlugin:
    def __init__(self) -> None:
        self.config: dict[str, Any] = {}
        self.ctx = types.SimpleNamespace(logger=None)


class _HookMode:
    BLOCKING = "blocking"
    OBSERVE = "observe"


class _HookOrder:
    EARLY = "early"
    NORMAL = "normal"
    LATE = "late"


class _ErrorPolicy:
    ABORT = "abort"
    SKIP = "skip"
    LOG = "log"


def _install_sdk_stub() -> None:
    sdk = types.ModuleType("maibot_sdk")
    sdk.HookHandler = _HookHandler
    sdk.MaiBotPlugin = _MaiBotPlugin
    sdk_types = types.ModuleType("maibot_sdk.types")
    sdk_types.HookMode = _HookMode
    sdk_types.HookOrder = _HookOrder
    sdk_types.ErrorPolicy = _ErrorPolicy
    # Replace only the process-local import entries.  Nothing here can reach a
    # host process; this module is loaded solely for the synthetic test.
    sys.modules["maibot_sdk"] = sdk
    sys.modules["maibot_sdk.types"] = sdk_types


def _load_plugin() -> types.ModuleType:
    _install_sdk_stub()
    _HookHandler.registry.clear()
    spec = importlib.util.spec_from_file_location("p1_04_isolated_plugin", BASE / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _segment(text: str, **extra: Any) -> dict[str, Any]:
    return {
        "text": text,
        "separator": "",
        "quote_previous": False,
        "is_correction": False,
        **extra,
    }


class _SyntheticReplyHost:
    """Minimal, deterministic host boundary used by the isolation tests."""

    def __init__(self, handler: Callable[..., Any]) -> None:
        self.handler = handler
        self.events: list[str] = []

    async def dispatch(self, **kwargs: Any) -> dict[str, Any]:
        original = dict(kwargs)
        self.events.append("before_hook")
        self.events.append("handler_enter")
        result = await self.handler(**kwargs)
        self.events.append("handler_exit")
        if not isinstance(result, dict) or result.get("action") != "continue":
            self.events.append("host_keeps_original")
            return original
        modified = result.get("modified_kwargs")
        if isinstance(modified, dict):
            # MaiBot's blocking dispatcher treats modified_kwargs as the
            # complete replacement mapping; this plugin returns **kwargs plus
            # the replacement segments, so supported top-level fields survive.
            applied = dict(modified)
            self.events.append("host_applies_modified_kwargs")
            return applied
        self.events.append("host_keeps_original")
        return original


class P104ReplyHookIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_plugin()
        cls.config = {
            "plugin": {"enabled": True},
            "deai": {"enabled": True, "strip_markdown": True,
                     "drop_trailing_period": True, "dedupe_segments": True,
                     "dedupe_threshold": 0.75, "dedupe_min_len": 12},
        }

    def new_plugin(self):
        instance = self.mod.DeAiFilterPlugin()
        instance.config = self.config
        return instance

    def test_registration_declares_expected_blocking_early_skip_contract(self):
        matches = [meta for fn, meta in _HookHandler.registry
                   if meta["args"] and meta["args"][0] == "maisaka.reply.after_split"]
        self.assertEqual(len(matches), 1)
        meta = matches[0]
        self.assertEqual(meta["mode"], _HookMode.BLOCKING)
        self.assertEqual(meta["order"], _HookOrder.EARLY)
        self.assertEqual(meta["error_policy"], _ErrorPolicy.SKIP)

    def test_dispatch_order_and_kwargs_are_preserved_while_segments_change(self):
        plugin = self.new_plugin()
        host = _SyntheticReplyHost(plugin.deai_filter)
        original_segments = [_segment("**你好。**", separator="。", quote_previous=True,
                                      is_correction=True, host_only="discard")]
        result = asyncio.run(host.dispatch(
            segments=original_segments,
            session_id="synthetic-session",
            correlation_marker="synthetic-marker",
        ))
        self.assertEqual(host.events, [
            "before_hook", "handler_enter", "handler_exit",
            "host_applies_modified_kwargs",
        ])
        self.assertEqual(result["session_id"], "synthetic-session")
        self.assertEqual(result["correlation_marker"], "synthetic-marker")
        self.assertEqual(result["segments"], [{
            "text": "你好",
            "separator": "。",
            "quote_previous": True,
            "is_correction": True,
        }])
        # Segment metadata is retained only through the explicit host fields;
        # arbitrary input fields must not leak into the normalized output.
        self.assertNotIn("host_only", result["segments"][0])

    def test_only_supported_segment_fields_are_emitted_in_original_order(self):
        plugin = self.new_plugin()
        result = asyncio.run(plugin.deai_filter(
            segments=[_segment("**第一段**。", separator="A"),
                      _segment("第二段！", quote_previous=True)],
            session_id="synthetic-session",
        ))
        output = result["modified_kwargs"]["segments"]
        self.assertEqual([item["text"] for item in output], ["第一段", "第二段！"])
        self.assertTrue(all(set(item) == {
            "text", "separator", "quote_previous", "is_correction",
        } for item in output))
        self.assertEqual(output[0]["separator"], "A")
        self.assertTrue(output[1]["quote_previous"])

    def test_malformed_input_and_all_removed_content_fail_open_to_original(self):
        plugin = self.new_plugin()
        host = _SyntheticReplyHost(plugin.deai_filter)
        for malformed in (None, "not-a-list", [], [None], [{"text": 7}]):
            original = {"segments": malformed, "session_id": "synthetic-session"}
            result = asyncio.run(host.dispatch(**original))
            self.assertEqual(result, original)
        original = {"segments": [_segment("作为 AI 助手，希望这些内容对你有帮助。")],
                    "session_id": "synthetic-session"}
        self.assertEqual(asyncio.run(host.dispatch(**original)), original)
        self.assertTrue(all(event == "host_keeps_original" or event == "before_hook"
                            or event == "handler_enter" or event == "handler_exit"
                            for event in host.events))

    def test_handler_exception_is_converted_to_continue_and_never_blocks_reply(self):
        plugin = self.new_plugin()
        original_filter = self.mod.deai_text

        def explode(*args: Any, **kwargs: Any) -> str:
            raise RuntimeError("synthetic filter failure")

        self.mod.deai_text = explode
        try:
            host = _SyntheticReplyHost(plugin.deai_filter)
            original = {"segments": [_segment("保留原样。")],
                        "session_id": "synthetic-session", "extra": "kept"}
            result = asyncio.run(host.dispatch(**original))
            self.assertEqual(result, original)
            self.assertEqual(host.events[-1], "host_keeps_original")
        finally:
            self.mod.deai_text = original_filter


if __name__ == "__main__":
    unittest.main()
