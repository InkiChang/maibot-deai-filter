"""Synthetic regressions for the November audit; no network or host access."""
import asyncio
from dataclasses import replace
import io
import json
import tempfile
import types
from pathlib import Path
import unittest
from unittest.mock import patch

import test_plugin
from deai import DeAiOptions, deai_text
from memory_capture import parse_extraction, MemoryCaptureQueue
from memory_store import MemoryStore, OwnerScope
from subq_client import SubQBudget, SubQClient, SubQClientError, SubQHttpResponse
from subq_config import parse_subq_config
import test_hook_runtime_e2e as e2e


class AuditRegressionTests(unittest.IsolatedAsyncioTestCase):
    def candidate(self, message_id="synthetic-1", fact="synthetic fact"):
        body = json.dumps({"candidates": [{"domain": "user_fact", "fact": fact,
                                          "quote": "synthetic quote"}]})
        return parse_extraction(body, "synthetic quote", OwnerScope("owner", "session"), message_id)[0]

    def config(self, **overrides):
        config = parse_subq_config({
            "enabled": True, "provider": "synthetic", "model": "synthetic",
            "base_url": "https://example.invalid/v1", "credential_ref": "TEST_REF",
            "timeout_seconds": 1, "max_retries": 0, "daily_request_budget": 10,
            "daily_token_budget": 100, "daily_cost_budget": 1,
        })
        return replace(config, **overrides)

    def test_code_markup_bypasses_every_text_rule(self):
        options = DeAiOptions(extra_phrases=["synthetic"])
        for text in (
            '```python\nif True:\n    print("synthetic")\n```',
            '~~~python\n    synthetic = "作为AI，<think>x</think>"\n~~~',
            '好问题！`synthetic  =  1`。',
            '```python\n    synthetic',  # unclosed fence
        ):
            with self.subTest(text=text):
                marks = set()
                self.assertEqual(deai_text(text, options, marks), text)
                self.assertEqual(marks, set())

    async def test_split_code_reply_skips_trimming_and_deduplication(self):
        instance = test_plugin.mod.DeAiFilterPlugin()
        instance.config = test_plugin.CONFIG
        segments = [test_plugin.seg(t) for t in (
            "好问题！", "```python", "    repeated_call()", "    repeated_call()", "```")]
        original = json.loads(json.dumps(segments))
        result = await instance.deai_filter(segments=segments, session_id="synthetic")
        self.assertEqual(result, {"action": "continue"})
        self.assertEqual(segments, original)

    def test_duplicate_quotes_drop_only_ambiguous_quote_not_references(self):
        store = MemoryStore()
        first = self.candidate()
        store.add(first)
        store.add(first)
        self.assertEqual(store.get(first.scope, first.item_id).evidence_quotes, first.evidence_quotes)
        store.add(self.candidate("synthetic-2"))
        merged = store.get(first.scope, first.item_id)
        self.assertEqual(merged.evidence_refs, ("synthetic-1", "synthetic-2"))
        self.assertEqual(merged.evidence_quotes, ())
        self.assertEqual(merged.status, "pending")

    async def test_capture_queue_merges_repeated_fact_from_two_messages(self):
        store = MemoryStore()
        async def extract(_):
            return json.dumps({"candidates": [{"domain": "user_fact", "fact": "synthetic fact",
                                              "quote": "synthetic quote"}]})
        queue = MemoryCaptureQueue(store, extract)
        scope = OwnerScope("owner", "session")
        for mid in ("synthetic-1", "synthetic-2"):
            self.assertTrue(queue.enqueue(scope, mid, "synthetic quote"))
            self.assertEqual(await queue.process_one(), 1)
        self.assertEqual(store.count(), 1)
        self.assertEqual(len(store.pending_for_sessions(frozenset({"session"}))[0].evidence_refs), 2)

    def test_quoted_conflicts_are_quarantined_without_raw_quotes(self):
        store = MemoryStore()
        first = replace(self.candidate(), conflict_key="synthetic-key")
        second = replace(self.candidate("synthetic-2", "different fact"), conflict_key="synthetic-key")
        store.add(first)
        store.add(second)
        conflicts = store.find_conflicts(first.scope, "synthetic-key")
        self.assertEqual(len(conflicts), 2)
        for item in conflicts:
            self.assertEqual(item.status, "quarantined")
            self.assertEqual(item.evidence_quotes, ())
            self.assertTrue(item.evidence_refs)
        self.assertFalse(store.approve(first.scope, first.item_id, authorized_domains={"user_fact"}))

    async def test_each_zero_limit_rejects_even_zero_estimates_before_resolver(self):
        def never(_):
            self.fail("resolver or transport invoked")
        for field in ("daily_request_budget", "daily_token_budget", "daily_cost_budget"):
            with self.subTest(field=field):
                cfg = self.config(**{field: 0})
                budget = SubQBudget(cfg)
                client = SubQClient(cfg, never, never, budget)
                with self.assertRaises(SubQClientError) as caught:
                    await client.decide([{"role": "user", "content": "synthetic"}],
                                        estimated_tokens=0, estimated_cost=0)
                self.assertEqual(caught.exception.category, "budget")
                self.assertEqual((await budget.snapshot())["requests"], 0)

    async def test_resolver_shares_deadline_and_is_cancelled(self):
        cancelled = asyncio.Event()
        async def resolver(_):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        def never(_):
            self.fail("transport invoked")
        # Short internal fixture timeout keeps the regression fast.
        cfg = self.config(timeout_seconds=0.03)
        budget = SubQBudget(cfg)
        client = SubQClient(cfg, never, resolver, budget)
        with self.assertRaises(SubQClientError) as caught:
            await asyncio.wait_for(client.decide([{"role": "user", "content": "synthetic"}],
                                                estimated_tokens=1, estimated_cost=0.1), 1)
        self.assertEqual(caught.exception.category, "timeout")
        self.assertFalse(caught.exception.retryable)
        self.assertTrue(cancelled.is_set())
        self.assertEqual((await budget.snapshot())["requests"], 1)

    async def test_external_cancellation_reaches_resolver(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def resolver(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        cfg = self.config()
        client = SubQClient(cfg, lambda _: None, resolver, SubQBudget(cfg))
        task = asyncio.create_task(client.decide([{"role": "user", "content": "synthetic"}],
                                                estimated_tokens=1, estimated_cost=0.1))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())

    def test_e2e_success_exports_only_boolean_evidence(self):
        sentinel = "SYNTHETIC_PRIVATE_DIAGNOSTIC"
        class Output(io.BytesIO):
            @property
            def channel(self):
                return self
            def recv_exit_status(self):
                return 0
        class Client:
            def load_system_host_keys(self): pass
            def set_missing_host_key_policy(self, policy): pass
            def connect(self, **kwargs): pass
            def open_sftp(self): return self
            def put(self, *args): pass
            def chmod(self, *args): pass
            def close(self): pass
            def exec_command(self, command, **kwargs):
                text = ""
                if command.startswith("mkdir -m 755 "):
                    self.remote = command.split()[-1]
                elif command.startswith("docker run "):
                    text = "E2E_PASS " + sentinel
                elif "{{json .}}" in command:
                    text = json.dumps({"HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True,
                        "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"]},
                        "Mounts": [{"Source": self.remote, "Destination": "/test", "RW": False}]})
                elif command.startswith("docker inspect "):
                    text = sentinel
                return None, Output(text.encode()), Output(sentinel.encode())
        fake = types.SimpleNamespace(SSHClient=Client, RejectPolicy=object)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence.json"
            env = {"MAIBOT_E2E_SSH_HOST": "synthetic.invalid", "MAIBOT_E2E_SSH_USER": "synthetic",
                   "MAIBOT_E2E_IMAGE": "synthetic", "MAIBOT_E2E_SSH_KEY_FILE": "synthetic",
                   "MAIBOT_E2E_EVIDENCE_FILE": str(output)}
            with patch.dict("sys.modules", {"paramiko": fake}), patch.dict("os.environ", env, clear=True):
                case = e2e.HookRuntimeE2ETests()
                case._run_isolated(Path(__file__), "E2E_PASS")
            self.assertEqual(json.loads(output.read_text()),
                             {"fixture_passed": True, "production_unchanged": True})
            self.assertNotIn(sentinel, output.read_text())

    def test_e2e_failure_traceback_never_exports_private_diagnostics(self):
        sentinel = "SYNTHETIC_PRIVATE_DIAGNOSTIC"
        class FailureProbe(e2e.HookRuntimeE2ETests):
            def runTest(self):
                self._run_isolated(None, "E2E_PASS")
        stream = io.StringIO()
        with patch.object(e2e.HookRuntimeE2ETests, "_run_isolated_private", side_effect=RuntimeError(sentinel)):
            result = unittest.TextTestRunner(stream=stream).run(FailureProbe())
        self.assertEqual(len(result.failures), 1)
        self.assertNotIn(sentinel, stream.getvalue())
        self.assertIn("private diagnostics withheld", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
