"""Scoped in-memory short-term memory primitives; no host or disk integration."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from hashlib import sha256
from math import exp, isfinite
import re
from threading import RLock
from typing import Iterable


class MemoryError(ValueError):
    pass


DOMAINS = frozenset({"user_fact", "topic", "commitment", "open_loop", "persona_experience"})
SENSITIVITIES = frozenset({"low", "normal", "sensitive"})
STATUSES = frozenset({"pending", "approved", "quarantined", "rejected", "archived"})


def _text(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MemoryError(f"invalid {name}")
    return value.strip()


def _aware(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MemoryError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _unit(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or not 0 <= value <= 1:
        raise MemoryError(f"{name} must be in [0, 1]")
    return float(value)


@dataclass(frozen=True)
class OwnerScope:
    owner_id: str
    session_id: str

    def __post_init__(self):
        object.__setattr__(self, "owner_id", _text(self.owner_id, "owner_id", 256))
        object.__setattr__(self, "session_id", _text(self.session_id, "session_id", 256))


@dataclass(frozen=True)
class MemoryItem:
    scope: OwnerScope
    domain: str
    text: str
    evidence_refs: tuple[str, ...] = ()
    confidence: float = 0.0
    sensitivity: str = "normal"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    importance: float = 0.5
    status: str = "pending"
    item_id: str = ""
    conflict_key: str | None = None
    content_hash: str = field(init=False)
    evidence_quotes: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.scope, OwnerScope):
            raise MemoryError("scope is required")
        if not isinstance(self.domain, str) or self.domain not in DOMAINS:
            raise MemoryError("unknown memory domain")
        text = _text(self.text, "text", 4000)
        if (not isinstance(self.sensitivity, str) or self.sensitivity not in SENSITIVITIES
                or not isinstance(self.status, str) or self.status not in STATUSES):
            raise MemoryError("invalid sensitivity or status")
        conflict_key = self.conflict_key
        if conflict_key is not None:
            conflict_key = _text(conflict_key, "conflict_key", 256)
        if not isinstance(self.evidence_refs, (tuple, list)):
            raise MemoryError("evidence_refs must be a sequence of references")
        refs = tuple(dict.fromkeys(_text(ref, "evidence reference", 512) for ref in self.evidence_refs))
        if not isinstance(self.evidence_quotes, (tuple, list)):
            raise MemoryError("evidence_quotes must be a sequence of quoted spans")
        quotes = tuple(dict.fromkeys(_text(quote, "evidence quote", 500) for quote in self.evidence_quotes))
        if len(quotes) > 1 or (quotes and (self.status != "pending" or len(quotes) != len(refs))):
            raise MemoryError("evidence quote is limited to one pending reference")
        confidence = _unit(self.confidence, "confidence")
        importance = _unit(self.importance, "importance")
        created = _aware(self.created_at, "created_at")
        expiry = _aware(self.expires_at, "expires_at") if self.expires_at is not None else None
        if expiry is not None and expiry <= created:
            raise MemoryError("expires_at must follow created_at")
        if self.status == "approved" and not refs:
            raise MemoryError("approved memory requires evidence")
        norm = " ".join(text.casefold().split())
        digest = sha256((self.domain + "\0" + norm).encode("utf-8")).hexdigest()
        item_id = self.item_id or digest[:24]
        _text(item_id, "item_id", 128)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "evidence_quotes", quotes)
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "importance", importance)
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expiry)
        object.__setattr__(self, "item_id", item_id)
        object.__setattr__(self, "conflict_key", conflict_key)
        object.__setattr__(self, "content_hash", digest)


def search_score(item: MemoryItem, query: str, now: datetime) -> float:
    now = _aware(now, "now")
    if not isinstance(item, MemoryItem) or not isinstance(query, str):
        raise MemoryError("item and query are required")
    if now < item.created_at:
        raise MemoryError("query time predates memory")
    query_text = " ".join(query.casefold().split())
    if not query_text:
        return 0.0
    text = " ".join(item.text.casefold().split())
    terms = set(re.findall(r"[a-z0-9_]+|[^\W\d_]+", query_text))
    has_cjk = any("\u3400" <= char <= "\u9fff" for char in query_text)
    if has_cjk:
        terms.update(query_text[i:i + 2] for i in range(len(query_text) - 1)
                     if "\u3400" <= query_text[i] <= "\u9fff"
                     and "\u3400" <= query_text[i + 1] <= "\u9fff")
    if not terms:
        return 0.0
    words = set(re.findall(r"[a-z0-9_]+|[^\W\d_]+", text))
    if has_cjk:
        words.update(text[i:i + 2] for i in range(len(text) - 1)
                     if "\u3400" <= text[i] <= "\u9fff"
                     and "\u3400" <= text[i + 1] <= "\u9fff")
    relevance = len(terms & words) / len(terms)
    age_days = (now - item.created_at).total_seconds() / 86400
    # E-folding time, not half-life: 15..45 days; midpoint preserves the old curve.
    decay_days = 30.0 * (0.5 + item.importance)
    freshness = exp(-age_days / decay_days)
    return relevance * freshness * item.confidence * (0.5 + 0.5 * item.importance)


class MemoryStore:
    """Thread-safe bounded store partitioned by owner and session."""

    def __init__(self, max_items: int = 500):
        if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
            raise MemoryError("max_items must be positive")
        self.max_items = max_items
        self._items: dict[tuple[OwnerScope, str], MemoryItem] = {}
        self._lock = RLock()
        self._closed = False

    def _ensure_open(self):
        if self._closed:
            raise MemoryError("memory store is closed")

    def add(self, item: MemoryItem) -> str:
        if not isinstance(item, MemoryItem):
            raise MemoryError("item is required")
        if item.status in {"approved", "archived"}:
            raise MemoryError("approved memories must pass through approve(); archived state is internal")
        key = (item.scope, item.content_hash)
        with self._lock:
            self._ensure_open()
            for (scope, digest), existing in self._items.items():
                if scope == item.scope and digest == item.content_hash:
                    if existing.status == "pending" and item.status == "pending":
                        refs = tuple(dict.fromkeys(existing.evidence_refs + item.evidence_refs))
                        expiry_values = (existing.expires_at, item.expires_at)
                        expiry = min((x for x in expiry_values if x is not None), default=None)
                        self._items[(scope, digest)] = replace(
                            existing, evidence_refs=refs,
                            confidence=max(existing.confidence, item.confidence),
                            importance=max(existing.importance, item.importance),
                            expires_at=expiry,
                        )
                    return existing.item_id
            item = self._quarantine_conflicts(item)
            self._items[key] = item
            self._enforce_capacity(item.scope)
            return item.item_id

    def _quarantine_conflicts(self, item: MemoryItem) -> MemoryItem:
        if item.conflict_key is None or item.status != "pending" or not item.evidence_refs:
            return item
        group = [(key, existing) for key, existing in self._items.items()
                 if existing.scope == item.scope and existing.domain == item.domain
                 and existing.conflict_key == item.conflict_key
                 and existing.status in {"pending", "approved", "quarantined"}
                 and existing.content_hash != item.content_hash]
        if not group:
            return item
        for key, existing in group:
            if existing.status != "quarantined":
                self._items[key] = replace(existing, status="quarantined")
        return replace(item, status="quarantined")

    def _reconcile_conflicts(self, scope: OwnerScope, domain: str, conflict_key: str) -> None:
        group = [(key, item) for key, item in self._items.items()
                 if item.scope == scope and item.domain == domain
                 and item.conflict_key == conflict_key
                 and item.status in {"pending", "approved", "quarantined"}]
        distinct = {item.content_hash for _, item in group}
        if len(distinct) > 1:
            for key, item in group:
                if item.status != "quarantined":
                    self._items[key] = replace(item, status="quarantined")
        else:
            for key, item in group:
                if item.status == "quarantined":
                    self._items[key] = replace(item, status="pending")

    def find_conflicts(self, scope: OwnerScope, conflict_key: str) -> list[MemoryItem]:
        if not isinstance(scope, OwnerScope):
            raise MemoryError("scope is required")
        conflict_key = _text(conflict_key, "conflict_key", 256)
        with self._lock:
            self._ensure_open()
            matches = [item for item in self._items.values()
                       if item.scope == scope and item.conflict_key == conflict_key
                       and item.status == "quarantined"]
        conflicting_domains = {
            domain for domain in {item.domain for item in matches}
            if len({item.content_hash for item in matches if item.domain == domain}) >= 2
        }
        matches = [item for item in matches if item.domain in conflicting_domains]
        return sorted(matches, key=lambda item: (item.domain, item.created_at, item.item_id))

    def _enforce_capacity(self, scope: OwnerScope):
        items = [(key, value) for key, value in self._items.items() if key[0] == scope]
        while len(items) > self.max_items:
            key, removed = min(items, key=lambda pair: (
                pair[1].status == "approved",
                pair[1].importance * pair[1].confidence,
                pair[1].created_at, pair[1].item_id,
            ))
            del self._items[key]
            if removed.conflict_key is not None:
                self._reconcile_conflicts(scope, removed.domain, removed.conflict_key)
            items = [(k, v) for k, v in items if k != key]

    def pending_for_sessions(self, session_ids: frozenset[str]) -> tuple[MemoryItem, ...]:
        """Immutable, live, non-sensitive pending snapshot for explicit session IDs.

        This narrow admin-view interface never mutates or approves records.
        """
        if (not isinstance(session_ids, frozenset)
                or any(not isinstance(x, str) or not x for x in session_ids)):
            raise MemoryError("session_ids must be a frozenset of non-empty strings")
        now = datetime.now(timezone.utc)
        with self._lock:
            self._ensure_open()
            rows = [item for item in self._items.values()
                    if item.scope.session_id in session_ids and item.status == "pending"
                    and item.sensitivity != "sensitive"
                    and (item.expires_at is None or now < item.expires_at)]
            rows.sort(key=lambda item: (item.created_at, item.scope.owner_id,
                                       item.scope.session_id, item.item_id), reverse=True)
            return tuple(rows)

    def get(self, scope: OwnerScope, item_id: str) -> MemoryItem | None:
        with self._lock:
            self._ensure_open()
            return next((item for (owner, _), item in self._items.items()
                         if owner == scope and item.item_id == item_id), None)

    def search(self, scope: OwnerScope, query: str, now: datetime, *, limit: int = 10,
               include_sensitive: bool = False, domain: str | None = None) -> list[MemoryItem]:
        now = _aware(now, "now")
        if not isinstance(scope, OwnerScope):
            raise MemoryError("scope is required")
        if not isinstance(query, str):
            raise MemoryError("query must be text")
        if type(include_sensitive) is not bool:
            raise MemoryError("include_sensitive must be bool")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise MemoryError("limit must be positive")
        if domain is not None and (not isinstance(domain, str) or domain not in DOMAINS):
            raise MemoryError("unknown domain")
        with self._lock:
            self._ensure_open()
            matches = [(search_score(item, query, now), item) for (owner, _), item in self._items.items()
                       if owner == scope and item.status == "approved"
                       and (domain is None or item.domain == domain)
                       and (include_sensitive or item.sensitivity != "sensitive")
                       and (item.expires_at is None or now < item.expires_at)]
        matches = [(score, item) for score, item in matches if score > 0]
        matches.sort(key=lambda pair: (-pair[0], pair[1].item_id))
        return [item for _, item in matches[:limit]]

    def approve(self, scope: OwnerScope, item_id: str, *, authorized_domains: Iterable[str]) -> bool:
        if not isinstance(scope, OwnerScope):
            raise MemoryError("scope is required")
        if isinstance(authorized_domains, (str, bytes)):
            raise MemoryError("authorized_domains must be a collection of domains")
        try:
            allowed = set(authorized_domains)
        except TypeError as exc:
            raise MemoryError("authorized_domains must be iterable") from exc
        if any(not isinstance(domain, str) or domain not in DOMAINS for domain in allowed):
            raise MemoryError("authorized_domains contains an unknown domain")
        with self._lock:
            self._ensure_open()
            item = self.get(scope, item_id)
            if (item is None or item.status != "pending" or item.domain not in allowed
                    or not item.evidence_refs or item.sensitivity == "sensitive"):
                return False
            key = (scope, item.content_hash)
            self._items[key] = replace(item, status="approved", evidence_quotes=())
            return True

    def export(self, scope: OwnerScope, now: datetime) -> list[dict]:
        now = _aware(now, "now")
        if not isinstance(scope, OwnerScope):
            raise MemoryError("scope is required")
        with self._lock:
            self._ensure_open()
            items = [item for (owner, _), item in self._items.items()
                     if owner == scope and item.status == "approved"
                     and item.sensitivity != "sensitive"
                     and (item.expires_at is None or now < item.expires_at)]
        items.sort(key=lambda item: (item.domain, item.item_id))
        return [{
            "item_id": item.item_id,
            "scope": {"owner_id": item.scope.owner_id, "session_id": item.scope.session_id},
            "domain": item.domain,
            "text": item.text,
            "evidence_refs": list(item.evidence_refs),
            "confidence": item.confidence,
            "sensitivity": item.sensitivity,
            "created_at": item.created_at.isoformat(),
            "expires_at": item.expires_at.isoformat() if item.expires_at else None,
            "importance": item.importance,
            "status": item.status,
        } for item in items]

    def correct(self, scope: OwnerScope, item_id: str, replacement: MemoryItem) -> MemoryItem:
        if not isinstance(scope, OwnerScope) or not isinstance(replacement, MemoryItem):
            raise MemoryError("invalid correction request")
        if replacement.scope != scope or replacement.status != "pending":
            raise MemoryError("correction must be pending and match scope")
        with self._lock:
            self._ensure_open()
            old_key = next((key for key, item in self._items.items()
                            if key[0] == scope and item.item_id == item_id), None)
            if old_key is None:
                raise MemoryError("memory not found in scope")
            old = self._items[old_key]
            expiry = min((value for value in (old.expires_at, replacement.expires_at)
                          if value is not None), default=None)
            corrected = replace(replacement, expires_at=expiry)
            new_key = (scope, corrected.content_hash)
            if new_key != old_key and new_key in self._items:
                raise MemoryError("corrected content already exists")
            del self._items[old_key]
            if old.conflict_key is not None:
                self._reconcile_conflicts(scope, old.domain, old.conflict_key)
            corrected = self._quarantine_conflicts(corrected)
            self._items[(scope, corrected.content_hash)] = corrected
            self._enforce_capacity(scope)
            return corrected

    def archive_low_importance(self, scope: OwnerScope, now: datetime, *,
                               threshold: float = 0.25, min_age_days: int = 90) -> int:
        """Archive eligible approved low-importance items without extending retention."""
        now = _aware(now, "now")
        if not isinstance(scope, OwnerScope):
            raise MemoryError("scope is required")
        threshold = _unit(threshold, "threshold")
        if isinstance(min_age_days, bool) or not isinstance(min_age_days, int) or min_age_days < 1:
            raise MemoryError("min_age_days must be positive")
        with self._lock:
            self._ensure_open()
            archived = 0
            for key, item in list(self._items.items()):
                if (item.scope != scope or item.status != "approved"
                        or item.sensitivity == "sensitive" or item.conflict_key is not None
                        or not item.evidence_refs or item.importance > threshold
                        or (now - item.created_at).total_seconds() < min_age_days * 86400
                        or (item.expires_at is not None and now >= item.expires_at)):
                    continue
                self._items[key] = replace(item, status="archived")
                archived += 1
            return archived

    def restore(self, scope: OwnerScope, item_id: str) -> bool:
        """Restore archived content as pending, requiring explicit re-approval."""
        if not isinstance(scope, OwnerScope):
            raise MemoryError("scope is required")
        with self._lock:
            self._ensure_open()
            key = next((key for key, item in self._items.items()
                        if item.scope == scope and item.item_id == item_id
                        and item.status == "archived"), None)
            if key is None:
                return False
            self._items[key] = replace(self._items[key], status="pending")
            return True

    def remove(self, scope: OwnerScope, item_id: str) -> int:
        with self._lock:
            self._ensure_open()
            key = next((key for key, item in self._items.items()
                        if key[0] == scope and item.item_id == item_id), None)
            if key is None:
                return 0
            removed = self._items.pop(key)
            if removed.conflict_key is not None:
                self._reconcile_conflicts(scope, removed.domain, removed.conflict_key)
            return 1

    def purge_expired(self, now: datetime) -> int:
        now = _aware(now, "now")
        with self._lock:
            self._ensure_open()
            expired = [key for key, item in self._items.items()
                       if item.expires_at is not None and item.expires_at <= now]
            affected = {(self._items[key].scope, self._items[key].domain,
                         self._items[key].conflict_key)
                        for key in expired if self._items[key].conflict_key is not None}
            for key in expired:
                del self._items[key]
            for scope, domain, conflict_key in affected:
                self._reconcile_conflicts(scope, domain, conflict_key)
            return len(expired)

    def count(self) -> int:
        with self._lock:
            self._ensure_open()
            return len(self._items)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._items.clear()
                self._closed = True
