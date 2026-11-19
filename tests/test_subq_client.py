"""Offline tests for SubQ request/response core; no network or real credentials."""
import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
import unittest

from subq_client import (
    SubQBudget, SubQClient, SubQClientError, SubQHttpResponse, build_chat_request, parse_chat_response,
)
from subq_config import parse_subq_config


class SubQClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = parse_subq_config({
            "enabled": True, "protocol": "openai_chat_completions", "provider": "mock",
            "base_url": "https://mock.invalid/v1", "model": "mock-model",
            "credential_ref": "SUBQ_TEST_KEY", "timeout_seconds": 1.0,
            "max_retries": 0, "daily_request_budget": 100,
            "daily_token_budget": 100000, "daily_cost_budget": 100.0,
        })
        self.messages = [{"role": "user", "content": "hello"}]
        self.budget = SubQBudget(self.config)

    def client(self, config, transport, resolver, budget=None):
        return SubQClient(config, transport, resolver, budget or SubQBudget(config))

    async def decide(self, client):
        return await client.decide(self.messages, estimated_tokens=10, estimated_cost=0.01)

    def test_builds_non_streaming_request(self):
        req = build_chat_request(self.config, self.messages, "test-secret")
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.url, "https://mock.invalid/v1/chat/completions")
        self.assertEqual(req.headers["Authorization"], "Bearer test-secret")
        self.assertEqual(req.json_body, {
            "model": "mock-model", "messages": self.messages, "stream": False,
        })
        self.assertEqual(req.timeout_seconds, 1.0)

    def test_rejects_bad_messages_and_header_injection(self):
        for msgs, key in (([], "key"), ([{"role": "tool", "content": "x"}], "key"),
                          (self.messages, "key\nInjected: yes")):
            with self.subTest(msgs=msgs, key=key):
                with self.assertRaises(SubQClientError):
                    build_chat_request(self.config, msgs, key)

    def test_parses_minimal_response(self):
        content = parse_chat_response(SubQHttpResponse(200, json.dumps({
            "choices": [{"message": {"content": " decision "}}]
        }).encode()))
        self.assertEqual(content, " decision ")

    def test_http_errors_have_safe_retry_classification(self):
        for status in (401, 403):
            with self.subTest(status=status), self.assertRaises(SubQClientError) as ctx:
                parse_chat_response(SubQHttpResponse(status, b"private response"))
            self.assertEqual(ctx.exception.category, "auth")
            self.assertFalse(ctx.exception.retryable)
            self.assertNotIn("private response", str(ctx.exception))
        for status in (400, 404):
            with self.subTest(status=status), self.assertRaises(SubQClientError) as ctx:
                parse_chat_response(SubQHttpResponse(status, b"private response"))
            self.assertEqual(ctx.exception.category, "request_rejected")
            self.assertFalse(ctx.exception.retryable)
        with self.assertRaises(SubQClientError) as ctx:
            parse_chat_response(SubQHttpResponse(503, b"private response"))
        self.assertEqual(ctx.exception.category, "server")
        self.assertTrue(ctx.exception.retryable)
        self.assertNotIn("private response", str(ctx.exception))

    def test_rate_limit_retry_after_seconds_date_and_malformed(self):
        cases = (("Retry-After", "12", 12.0), ("retry-after", "bad-value", None))
        for key, value, expected in cases:
            with self.subTest(value=value), self.assertRaises(SubQClientError) as ctx:
                parse_chat_response(SubQHttpResponse(429, b"hidden", {key: value}))
            self.assertEqual(ctx.exception.category, "rate_limited")
            self.assertTrue(ctx.exception.retryable)
            if expected is None:
                self.assertIsNone(ctx.exception.retry_after_seconds)
            else:
                self.assertEqual(ctx.exception.retry_after_seconds, expected)
        target = datetime.now(timezone.utc) + timedelta(seconds=30)
        date_header = format_datetime(target, usegmt=True)
        with self.assertRaises(SubQClientError) as ctx:
            parse_chat_response(SubQHttpResponse(429, b"hidden", {"Retry-After": date_header}))
        self.assertTrue(ctx.exception.retryable)
        self.assertIsNotNone(ctx.exception.retry_after_seconds)
        self.assertGreaterEqual(ctx.exception.retry_after_seconds, 0)
        self.assertLess(ctx.exception.retry_after_seconds, 31)

    def test_rejects_http_bad_json_and_bad_shapes_without_body_echo(self):
        secret = "RESPONSE_SECRET_MARKER"
        cases = [SubQHttpResponse(401, secret.encode()),
                 SubQHttpResponse(200, secret.encode()),
                 SubQHttpResponse(200, b'{"choices":[]}'),
                 SubQHttpResponse(200, b'{"choices":[{"message":{"content":null}}]}')]
        for response in cases:
            with self.subTest(status=response.status_code):
                with self.assertRaises(SubQClientError) as ctx:
                    parse_chat_response(response)
                self.assertNotIn(secret, str(ctx.exception))

    async def test_client_uses_injected_transport_and_resolver(self):
        seen = {}
        async def transport(request):
            seen["request"] = request
            return SubQHttpResponse(200, b'{"choices":[{"message":{"content":"ok"}}]}')
        async def resolver(ref):
            seen["ref"] = ref
            return "dummy-secret"
        result = await self.client(self.config, transport, resolver).decide(self.messages, estimated_tokens=10, estimated_cost=0.01)
        self.assertEqual(result, "ok")
        self.assertEqual(seen["ref"], "SUBQ_TEST_KEY")
        self.assertEqual(seen["request"].headers["Authorization"], "Bearer dummy-secret")

    async def test_retries_transient_failure_once_then_succeeds(self):
        config = parse_subq_config({
            "enabled": True, "provider": "mock", "base_url": "https://mock.invalid/v1",
            "model": "mock-model", "credential_ref": "SUBQ_TEST_KEY", "max_retries": 1,
            "daily_request_budget": 10, "daily_token_budget": 1000, "daily_cost_budget": 1.0,
        })
        calls = 0
        async def flaky(_):
            nonlocal calls
            calls += 1
            if calls == 1:
                return SubQHttpResponse(503, b"private")
            return SubQHttpResponse(200, b'{"choices":[{"message":{"content":"ok"}}]}')
        result = await self.client(config, flaky, lambda _: "key").decide(self.messages, estimated_tokens=10, estimated_cost=0.01)
        self.assertEqual(result, "ok")
        self.assertEqual(calls, 2)

    async def test_retry_limit_and_permanent_error(self):
        config = parse_subq_config({
            "enabled": True, "provider": "mock", "base_url": "https://mock.invalid/v1",
            "model": "mock-model", "credential_ref": "SUBQ_TEST_KEY", "max_retries": 1,
            "daily_request_budget": 10, "daily_token_budget": 1000, "daily_cost_budget": 1.0,
        })
        calls = 0
        async def always_503(_):
            nonlocal calls
            calls += 1
            return SubQHttpResponse(503, b"private")
        with self.assertRaises(SubQClientError) as ctx:
            await self.client(config, always_503, lambda _: "key").decide(self.messages, estimated_tokens=10, estimated_cost=0.01)
        self.assertEqual(ctx.exception.category, "server")
        self.assertEqual(calls, 2)
        calls = 0
        async def unauthorized(_):
            nonlocal calls
            calls += 1
            return SubQHttpResponse(401, b"private")
        with self.assertRaises(SubQClientError):
            await self.client(config, unauthorized, lambda _: "key").decide(self.messages, estimated_tokens=10, estimated_cost=0.01)
        self.assertEqual(calls, 1)

    async def test_429_retries_only_with_bounded_retry_after(self):
        config = parse_subq_config({
            "enabled": True, "provider": "mock", "base_url": "https://mock.invalid/v1",
            "model": "mock-model", "credential_ref": "SUBQ_TEST_KEY", "max_retries": 1,
            "daily_request_budget": 10, "daily_token_budget": 1000, "daily_cost_budget": 1.0,
        })
        calls = 0
        async def limited_then_ok(_):
            nonlocal calls
            calls += 1
            if calls == 1:
                return SubQHttpResponse(429, b"private", {"Retry-After": "0.01"})
            return SubQHttpResponse(200, b'{"choices":[{"message":{"content":"ok"}}]}')
        self.assertEqual(await self.client(config, limited_then_ok, lambda _: "key").decide(self.messages, estimated_tokens=10, estimated_cost=0.01), "ok")
        self.assertEqual(calls, 2)
        calls = 0
        async def limited_without_delay(_):
            nonlocal calls
            calls += 1
            return SubQHttpResponse(429, b"private")
        with self.assertRaises(SubQClientError) as ctx:
            await self.client(config, limited_without_delay, lambda _: "key").decide(self.messages, estimated_tokens=10, estimated_cost=0.01)
        self.assertEqual(ctx.exception.category, "rate_limited")
        self.assertEqual(calls, 1)
        calls = 0
        async def limited_too_long(_):
            nonlocal calls
            calls += 1
            return SubQHttpResponse(429, b"private", {"Retry-After": "999"})
        with self.assertRaises(SubQClientError):
            await self.client(config, limited_too_long, lambda _: "key").decide(self.messages, estimated_tokens=10, estimated_cost=0.01)
        self.assertEqual(calls, 1)

    async def test_budget_requires_ledger_and_per_call_estimates(self):
        async def ok(_): return SubQHttpResponse(200, b'{"choices":[{"message":{"content":"ok"}}]}')
        with self.assertRaises(SubQClientError) as ctx:
            await SubQClient(self.config, ok, lambda _: "key").decide(
                self.messages, estimated_tokens=10, estimated_cost=0.01)
        self.assertEqual(ctx.exception.category, "budget")
        client = self.client(self.config, ok, lambda _: "key")
        with self.assertRaises(SubQClientError) as ctx:
            await client.decide(self.messages, estimated_tokens=10)
        self.assertEqual(ctx.exception.category, "budget")
        self.assertEqual((await client._budget.snapshot())["requests"], 0)

    async def test_budget_rejects_mismatched_ledger_and_invalid_estimates(self):
        config = self.config
        other = parse_subq_config({
            "enabled": True, "provider": "mock", "base_url": "https://mock.invalid/v1",
            "model": "mock-model", "credential_ref": "SUBQ_TEST_KEY",
            "daily_request_budget": 1, "daily_token_budget": 100, "daily_cost_budget": 1.0,
        })
        async def ok(_): return SubQHttpResponse(200, b'{"choices":[{"message":{"content":"ok"}}]}')
        mismatched = SubQClient(config, ok, lambda _: "key", budget=SubQBudget(other))
        with self.assertRaises(SubQClientError) as ctx:
            await mismatched.decide(self.messages, estimated_tokens=1, estimated_cost=0.01)
        self.assertEqual(ctx.exception.category, "budget")
        client = self.client(config, ok, lambda _: "key")
        for tokens, cost in [(True, 0.01), (-1, 0.01), (10**13, 0.01), (1, float("nan")), (1, float("inf")), (1, -0.1)]:
            with self.subTest(tokens=tokens, cost=cost), self.assertRaises(SubQClientError):
                await client.decide(self.messages, estimated_tokens=tokens, estimated_cost=cost)
        self.assertEqual((await client._budget.snapshot())["requests"], 0)

    async def test_zero_budget_fails_closed_before_resolver(self):
        zero = parse_subq_config({
            "enabled": True, "provider": "mock", "base_url": "https://mock.invalid/v1",
            "model": "mock-model", "credential_ref": "SUBQ_TEST_KEY",
        })
        async def never(_): self.fail("credential resolver should not run")
        client = self.client(zero, never, never)
        with self.assertRaises(SubQClientError) as ctx:
            await client.decide(self.messages, estimated_tokens=1, estimated_cost=0)
        self.assertEqual(ctx.exception.category, "budget")

    async def test_budget_counts_each_retry_attempt(self):
        config = parse_subq_config({
            "enabled": True, "provider": "mock", "base_url": "https://mock.invalid/v1",
            "model": "mock-model", "credential_ref": "SUBQ_TEST_KEY", "max_retries": 1,
            "daily_request_budget": 2, "daily_token_budget": 20, "daily_cost_budget": 0.02,
        })
        ledger = SubQBudget(config)
        calls = 0
        async def retry_once(_):
            nonlocal calls
            calls += 1
            if calls == 1: return SubQHttpResponse(503, b"hidden")
            return SubQHttpResponse(200, b'{"choices":[{"message":{"content":"ok"}}]}')
        client = self.client(config, retry_once, lambda _: "key", budget=ledger)
        self.assertEqual(await client.decide(self.messages, estimated_tokens=10, estimated_cost=0.01), "ok")
        snap = await ledger.snapshot()
        self.assertEqual((snap["requests"], snap["tokens"], snap["estimated_cost"]), (2, 20, "0.02"))

    async def test_budget_reservations_are_atomic_under_concurrency(self):
        ledger = SubQBudget(parse_subq_config({
            "enabled": True, "provider": "mock", "base_url": "https://mock.invalid/v1",
            "model": "mock-model", "credential_ref": "SUBQ_TEST_KEY",
            "daily_request_budget": 5, "daily_token_budget": 50, "daily_cost_budget": 0.05,
        }))
        results = await asyncio.gather(*[
            ledger.reserve(estimated_tokens=10, estimated_cost=0.01) for _ in range(20)
        ], return_exceptions=True)
        self.assertEqual(sum(not isinstance(r, Exception) for r in results), 5)
        snap = await ledger.snapshot()
        self.assertEqual((snap["requests"], snap["tokens"], snap["estimated_cost"]), (5, 50, "0.05"))

    async def test_decimal_cost_accumulation_has_no_float_rounding_overrun(self):
        config = parse_subq_config({
            "enabled": True, "provider": "mock", "base_url": "https://mock.invalid/v1",
            "model": "mock-model", "credential_ref": "SUBQ_TEST_KEY",
            "daily_request_budget": 2, "daily_token_budget": 2, "daily_cost_budget": 0.3,
        })
        ledger = SubQBudget(config)
        await ledger.reserve(estimated_tokens=1, estimated_cost=0.1)
        await ledger.reserve(estimated_tokens=1, estimated_cost=0.2)
        self.assertEqual((await ledger.snapshot())["estimated_cost"], "0.3")

    async def test_budget_resets_on_utc_date_change(self):
        day = [datetime(2026, 1, 1, tzinfo=timezone.utc).date()]
        config = parse_subq_config({
            "enabled": True, "provider": "mock", "base_url": "https://mock.invalid/v1",
            "model": "mock-model", "credential_ref": "SUBQ_TEST_KEY",
            "daily_request_budget": 1, "daily_token_budget": 10, "daily_cost_budget": 0.01,
        })
        ledger = SubQBudget(config, today=lambda: day[0])
        await ledger.reserve(estimated_tokens=10, estimated_cost=0.01)
        with self.assertRaises(SubQClientError):
            await ledger.reserve(estimated_tokens=1, estimated_cost=0.001)
        day[0] = datetime(2026, 1, 2, tzinfo=timezone.utc).date()
        await ledger.reserve(estimated_tokens=10, estimated_cost=0.01)
        self.assertEqual((await ledger.snapshot())["requests"], 1)

    async def test_disabled_client_never_resolves_or_transports(self):
        config = parse_subq_config(None)
        async def never(_): self.fail("resolver should not run")
        with self.assertRaises(SubQClientError):
            await self.client(config, never, never).decide(self.messages, estimated_tokens=10, estimated_cost=0.01)

    async def test_transport_timeout_and_cancellation_propagate(self):
        started = asyncio.Event()
        async def slow(_):
            started.set()
            await asyncio.sleep(5)
        client = self.client(self.config, slow, lambda _: "secret")
        with self.assertRaisesRegex(SubQClientError, "timed out") as timeout_ctx:
            await client.decide(self.messages, estimated_tokens=10, estimated_cost=0.01)
        self.assertEqual(timeout_ctx.exception.category, "timeout")
        self.assertTrue(timeout_ctx.exception.retryable)
        async def disconnected(_): raise OSError("private transport detail")
        with self.assertRaises(SubQClientError) as transport_ctx:
            await self.client(self.config, disconnected, lambda _: "secret").decide(self.messages, estimated_tokens=10, estimated_cost=0.01)
        self.assertEqual(transport_ctx.exception.category, "transport")
        self.assertTrue(transport_ctx.exception.retryable)
        self.assertNotIn("private transport detail", str(transport_ctx.exception))
        task = asyncio.create_task(client.decide(self.messages, estimated_tokens=10, estimated_cost=0.01))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_resolver_errors_are_sanitized(self):
        secret = "RESOLVER_SECRET_MARKER"
        def broken(_): raise RuntimeError(secret)
        with self.assertRaises(SubQClientError) as ctx:
            await self.client(self.config, lambda _: None, broken).decide(self.messages, estimated_tokens=10, estimated_cost=0.01)
        self.assertNotIn(secret, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
