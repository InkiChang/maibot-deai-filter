"""Bounded, volatile extraction queue. Nothing here persists chat text or approves facts."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from memory_store import MemoryError, MemoryItem, MemoryStore, OwnerScope


_ALLOWED_DOMAINS = frozenset({"user_fact", "topic", "commitment", "open_loop"})
_SENSITIVE_MARKERS = (
    "password", "passwd", "secret", "token", "api key", "credit card", "银行卡",
    "密码", "验证码", "病史", "疾病", "诊断", "药物", "医疗", "收入", "工资",
    "欠款", "债务", "身份证", "护照", "账号", "账户",
)


def _plain_text(message: dict[str, Any]) -> str | None:
    direct = message.get("plain_text")
    if isinstance(direct, str):
        text = direct.strip()
        return text if text and len(text) <= 4000 else None
    segments = message.get("raw_message")
    if not isinstance(segments, list) or not segments:
        return None
    parts: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict) or segment.get("type") != "text":
            return None
        data = segment.get("data")
        if isinstance(data, str):
            part = data
        elif isinstance(data, dict) and isinstance(data.get("text"), str):
            part = data["text"]
        else:
            return None
        parts.append(part)
    text = "".join(parts).strip()
    return text if text and len(text) <= 4000 else None


def _sensitive(text: str) -> bool:
    folded = text.casefold()
    return any(marker.casefold() in folded for marker in _SENSITIVE_MARKERS)


def parse_extraction(response: Any, source_text: str, scope: OwnerScope,
                     message_id: str, now: datetime | None = None) -> list[MemoryItem]:
    """Validate strict JSON and exact quote provenance; return pending-only items."""
    if (not isinstance(response, str) or not isinstance(source_text, str)
            or not isinstance(message_id, str) or not message_id.strip()):
        return []
    try:
        payload = json.loads(response)
    except (ValueError, TypeError):
        return []
    if not isinstance(payload, dict) or set(payload) != {"candidates"}:
        return []
    candidates = payload["candidates"]
    if not isinstance(candidates, list) or len(candidates) > 5:
        return []
    stamp = now or datetime.now(timezone.utc)
    result: list[MemoryItem] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != {"domain", "fact", "quote"}:
            continue
        domain, fact, quote = (candidate.get("domain"), candidate.get("fact"), candidate.get("quote"))
        if (not isinstance(domain, str) or domain not in _ALLOWED_DOMAINS
                or not isinstance(fact, str) or not fact.strip() or len(fact) > 500
                or not isinstance(quote, str) or not quote.strip() or len(quote) > 500
                or quote not in source_text or _sensitive(quote) or _sensitive(fact)):
            continue
        key = (domain, " ".join(fact.casefold().split()))
        if key in seen:
            continue
        seen.add(key)
        try:
            result.append(MemoryItem(
                scope=scope, domain=domain, text=fact.strip(),
                evidence_refs=(message_id,), evidence_quotes=(quote.strip(),),
                confidence=0.5, sensitivity="normal",
                created_at=stamp, expires_at=stamp + timedelta(days=30),
                importance=0.5, status="pending",
            ))
        except MemoryError:
            continue
    return result


class MemoryCaptureQueue:
    """Bounded async in-memory worker; inputs are discarded after processing."""

    def __init__(self, store: MemoryStore,
                 extractor: Callable[[str], Awaitable[Any]], max_queue: int = 32):
        if not isinstance(store, MemoryStore) or not callable(extractor):
            raise MemoryError("store and async extractor are required")
        if isinstance(max_queue, bool) or not isinstance(max_queue, int) or not 1 <= max_queue <= 256:
            raise MemoryError("max_queue must be in [1, 256]")
        self.store = store
        self.extractor = extractor
        self.queue: asyncio.Queue[tuple[OwnerScope, str, str]] = asyncio.Queue(maxsize=max_queue)

    def enqueue(self, scope: OwnerScope, message_id: str, text: str) -> bool:
        if not isinstance(scope, OwnerScope) or not isinstance(message_id, str) or not message_id.strip():
            return False
        if not isinstance(text, str) or not text.strip() or len(text) > 4000 or _sensitive(text):
            return False
        try:
            self.queue.put_nowait((scope, message_id.strip(), text.strip()))
        except asyncio.QueueFull:
            return False
        return True

    async def process_one(self) -> int:
        scope, message_id, source_text = await self.queue.get()
        try:
            try:
                response = await self.extractor(source_text)
            except Exception:
                return 0
            items = parse_extraction(response, source_text, scope, message_id)
            added = 0
            for item in items:
                try:
                    self.store.add(item)
                    added += 1
                except MemoryError:
                    continue
            return added
        finally:
            del source_text
            self.queue.task_done()

    def clear(self) -> None:
        """Discard queued text during shutdown or configuration disable."""
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            else:
                self.queue.task_done()
