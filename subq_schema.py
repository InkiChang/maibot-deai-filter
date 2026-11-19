"""Strict parser for low-authority SubQ decisions; never executes decisions."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import FrozenSet


class SubQSchemaError(ValueError):
    """Invalid or out-of-policy SubQ decision."""


@dataclass(frozen=True)
class DecisionPolicy:
    actions: FrozenSet[str]
    reason_codes: FrozenSet[str]
    max_ttl: int


@dataclass(frozen=True)
class SubQDecision:
    action: str
    reason_code: str
    confidence: float
    context_refs: tuple[str, ...]
    ttl: int


POLICIES = {
    "reply_ambiguity": DecisionPolicy(
        frozenset({"reply_now", "defer", "clarify"}),
        frozenset({"clear_intent", "ambiguous_intent", "insufficient_context", "safety_boundary"}),
        3600,
    ),
    "life_share": DecisionPolicy(
        frozenset({"share", "do_not_share"}),
        frozenset({"relevant", "not_relevant", "timeline_conflict", "reality_boundary"}),
        86400,
    ),
    "memory_candidate": DecisionPolicy(
        frozenset({"keep_candidate", "discard", "hold"}),
        frozenset({"supported", "duplicate", "sensitive", "ambiguous", "conflicting"}),
        86400,
    ),
    "plan_conflict": DecisionPolicy(
        frozenset({"proceed", "adjust", "skip"}),
        frozenset({"consistent", "conflict", "uncertain", "boundary"}),
        86400,
    ),
}
_REQUIRED = {"action", "reason_code", "confidence", "context_refs", "ttl"}
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def _reject_constant(value: str):
    raise SubQSchemaError("decision JSON contains a non-finite number")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SubQSchemaError("decision JSON contains duplicate keys")
        result[key] = value
    return result


def parse_decision(raw: str, *, task: str, allowed_context_refs: set[str] | frozenset[str]) -> SubQDecision:
    """Parse a JSON object against task-specific allowlists and supplied refs."""
    if not isinstance(raw, str) or not raw or len(raw) > 8192:
        raise SubQSchemaError("decision payload must be a non-empty string up to 8192 characters")
    policy = POLICIES.get(task)
    if policy is None:
        raise SubQSchemaError("unknown decision task")
    if not isinstance(allowed_context_refs, (set, frozenset)) or any(
        not isinstance(ref, str) or not _REF_RE.fullmatch(ref) for ref in allowed_context_refs
    ):
        raise SubQSchemaError("allowed context references are invalid")
    try:
        obj = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except SubQSchemaError:
        raise
    except (ValueError, TypeError, RecursionError):
        raise SubQSchemaError("decision payload is invalid JSON") from None
    if not isinstance(obj, dict) or set(obj) != _REQUIRED:
        raise SubQSchemaError("decision object has missing or unknown fields")
    action, reason = obj["action"], obj["reason_code"]
    if not isinstance(action, str) or action not in policy.actions:
        raise SubQSchemaError("action is not allowed for this task")
    if not isinstance(reason, str) or reason not in policy.reason_codes:
        raise SubQSchemaError("reason_code is not allowed for this task")
    confidence = obj["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise SubQSchemaError("confidence must be a finite number from 0 to 1")
    refs = obj["context_refs"]
    if not isinstance(refs, list) or len(refs) > 16:
        raise SubQSchemaError("context_refs must be a list of at most 16 references")
    if any(not isinstance(ref, str) or not _REF_RE.fullmatch(ref) or ref not in allowed_context_refs for ref in refs):
        raise SubQSchemaError("context_refs contains an unknown or invalid reference")
    if len(refs) != len(set(refs)):
        raise SubQSchemaError("context_refs must not contain duplicates")
    ttl = obj["ttl"]
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 0 <= ttl <= policy.max_ttl:
        raise SubQSchemaError("ttl is outside the allowed task range")
    return SubQDecision(action, reason, float(confidence), tuple(refs), ttl)
