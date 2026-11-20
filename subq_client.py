"""Transport-agnostic SubQ Chat Completions client core.

No network transport or credential source is bundled. Callers must inject both.
"""
from __future__ import annotations

import asyncio
from email.utils import parsedate_to_datetime
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Awaitable, Callable, Mapping, Sequence

from subq_config import SubQConfig


class SubQClientError(RuntimeError):
    """Safe client error; never contains response bodies or credentials."""

    def __init__(self, message: str, *, category: str = "invalid_response",
                 retryable: bool = False, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class SubQRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    json_body: Mapping[str, object]
    timeout_seconds: float


@dataclass(frozen=True)
class SubQHttpResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


Transport = Callable[[SubQRequest], Awaitable[SubQHttpResponse]]
CredentialResolver = Callable[[str], str | Awaitable[str]]


class SubQBudget:
    """In-memory UTC-day budget ledger shared by SubQ clients in one process.

    Each reservation is conservative: once reserved, it is not refunded, since
    a timeout/disconnect may happen after the provider accepted the request.
    """

    def __init__(self, config: SubQConfig, *, today: Callable[[], date] | None = None):
        self._limits = (config.daily_request_budget, config.daily_token_budget,
                       Decimal(str(config.daily_cost_budget)))
        self._today = today or (lambda: datetime.now(timezone.utc).date())
        self._day: date | None = None
        self._requests = 0
        self._tokens = 0
        self._cost = Decimal("0")
        self._lock = asyncio.Lock()

    async def reserve(self, *, estimated_tokens: int, estimated_cost: float) -> None:
        if (isinstance(estimated_tokens, bool) or not isinstance(estimated_tokens, int)
                or estimated_tokens < 0 or estimated_tokens > 10**12):
            raise SubQClientError("estimated token usage must be a non-negative integer", category="budget")
        if (isinstance(estimated_cost, bool) or not isinstance(estimated_cost, (int, float))
                or not math.isfinite(estimated_cost) or estimated_cost < 0):
            raise SubQClientError("estimated cost must be a finite non-negative number", category="budget")
        async with self._lock:
            day = self._today()
            if day != self._day:
                self._day = day
                self._requests = self._tokens = 0
                self._cost = Decimal("0")
            req_limit, token_limit, cost_limit = self._limits
            if req_limit <= 0 or token_limit <= 0 or cost_limit <= 0:
                raise SubQClientError("SubQ budget is disabled by a zero limit", category="budget")
            next_cost = self._cost + Decimal(str(estimated_cost))
            if (self._requests + 1 > req_limit or self._tokens + estimated_tokens > token_limit
                    or next_cost > cost_limit):
                raise SubQClientError("SubQ daily budget exhausted", category="budget")
            self._requests += 1
            self._tokens += estimated_tokens
            self._cost = next_cost

    def matches(self, config: SubQConfig) -> bool:
        """Ensure a client cannot accidentally borrow a looser ledger."""
        return self._limits == (config.daily_request_budget, config.daily_token_budget,
                                Decimal(str(config.daily_cost_budget)))

    async def snapshot(self) -> Mapping[str, int | float | str | None]:
        async with self._lock:
            return {"date": self._day.isoformat() if self._day else None,
                    "requests": self._requests, "tokens": self._tokens, "estimated_cost": str(self._cost)}


def build_chat_request(config: SubQConfig, messages: Sequence[Mapping[str, str]],
                       api_key: str) -> SubQRequest:
    """Build a non-streaming request. This does not send it or retain the key."""
    if not config.enabled:
        raise SubQClientError("SubQ is disabled")
    if (not isinstance(api_key, str) or not api_key.strip()
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in api_key)):
        raise SubQClientError("SubQ credential is unavailable or invalid")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)) or not messages:
        raise SubQClientError("messages must be a non-empty sequence")
    clean_messages: list[dict[str, str]] = []
    for item in messages:
        if not isinstance(item, Mapping) or set(item) != {"role", "content"}:
            raise SubQClientError("each message must contain only role and content")
        role, content = item["role"], item["content"]
        if role not in {"system", "user", "assistant"} or not isinstance(content, str):
            raise SubQClientError("message role or content is invalid")
        clean_messages.append({"role": role, "content": content})
    return SubQRequest(
        method="POST",
        url=config.base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        json_body={"model": config.model, "messages": clean_messages, "stream": False},
        timeout_seconds=config.timeout_seconds,
    )


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def parse_chat_response(response: SubQHttpResponse) -> str:
    """Validate response and classify errors without echoing body data."""
    if not isinstance(response, SubQHttpResponse):
        raise SubQClientError("transport returned an invalid response")
    if response.status_code < 200 or response.status_code >= 300:
        status = response.status_code
        if status == 429:
            raise SubQClientError(
                "SubQ rate limited the request", category="rate_limited",
                retryable=True, retry_after_seconds=_retry_after_seconds(response.headers),
            )
        if status in {401, 403}:
            raise SubQClientError("SubQ authentication or permission failed", category="auth", retryable=False)
        if 500 <= status <= 599:
            raise SubQClientError(f"SubQ server error ({status})", category="server", retryable=True)
        if 400 <= status <= 499:
            raise SubQClientError(f"SubQ rejected the request ({status})", category="request_rejected", retryable=False)
        raise SubQClientError(f"SubQ returned unexpected HTTP status {status}", category="http", retryable=False)
    try:
        payload = json.loads(response.body)
    except (ValueError, TypeError, UnicodeDecodeError):
        raise SubQClientError("SubQ returned invalid JSON") from None
    try:
        content = payload["choices"][0]["message"]["content"]
    except (TypeError, KeyError, IndexError):
        raise SubQClientError("SubQ response is missing message content") from None
    if not isinstance(content, str) or not content.strip():
        raise SubQClientError("SubQ response content must be a non-empty string")
    return content


class SubQClient:
    """Small async client core with injected transport and secret resolver.

    Synchronous resolvers must be nonblocking. Awaitable resolvers must cooperate
    with cancellation; they share the transport's total monotonic deadline.
    """

    def __init__(self, config: SubQConfig, transport: Transport,
                 credential_resolver: CredentialResolver, budget: SubQBudget | None = None):
        self._config = config
        self._transport = transport
        self._credential_resolver = credential_resolver
        self._budget = budget

    async def decide(self, messages: Sequence[Mapping[str, str]], *,
                     estimated_tokens: int | None = None,
                     estimated_cost: float | None = None) -> str:
        if not self._config.enabled:
            raise SubQClientError("SubQ is disabled")
        if self._budget is None:
            raise SubQClientError("SubQ budget ledger is required", category="budget")
        if not self._budget.matches(self._config):
            raise SubQClientError("SubQ budget ledger does not match client limits", category="budget")
        if estimated_tokens is None or estimated_cost is None:
            raise SubQClientError("per-request token and cost estimates are required", category="budget")
        if not callable(self._transport) or not callable(self._credential_resolver):
            raise SubQClientError("SubQ transport and credential resolver are required")
        max_attempts = 1 + self._config.max_retries
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._config.timeout_seconds * max_attempts
        request = None
        for attempt in range(max_attempts):
            await self._budget.reserve(estimated_tokens=estimated_tokens, estimated_cost=estimated_cost)
            if request is None:
                try:
                    secret = self._credential_resolver(self._config.credential_ref)
                    if hasattr(secret, "__await__"):
                        secret = await asyncio.wait_for(secret, timeout=max(0, deadline - loop.time()))
                except asyncio.TimeoutError:
                    raise SubQClientError("SubQ credential resolution timed out", category="timeout") from None
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise SubQClientError("SubQ credential resolution failed") from None
                request = build_chat_request(self._config, messages, secret)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise SubQClientError("SubQ total request time budget exhausted", category="timeout", retryable=False)
            try:
                response = await asyncio.wait_for(
                    self._transport(request), timeout=min(self._config.timeout_seconds, remaining)
                )
                return parse_chat_response(response)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                error = SubQClientError("SubQ request timed out", category="timeout", retryable=True)
            except SubQClientError as exc:
                error = exc
            except Exception:
                error = SubQClientError("SubQ transport failed", category="transport", retryable=True)

            if attempt + 1 >= max_attempts or not error.retryable:
                raise error
            if error.category == "rate_limited":
                delay = error.retry_after_seconds
                # Never retry a 429 without a valid server-provided delay.
                if delay is None:
                    raise error
            else:
                delay = 0.25
            remaining = deadline - loop.time()
            if delay >= remaining:
                raise error
            await asyncio.sleep(delay)
        raise SubQClientError("SubQ retry loop ended unexpectedly")
