"""Fail-closed intake for caller-supplied memory candidates; no extraction or host access."""
from __future__ import annotations

from collections.abc import Callable

from memory_store import MemoryError, MemoryItem, MemoryStore, OwnerScope


EvidenceVerifier = Callable[[OwnerScope, str], bool]


class CandidateIntake:
    """Validate supplied evidence references before placing candidates in review."""

    def __init__(self, store: MemoryStore, evidence_verifier: EvidenceVerifier):
        if not isinstance(store, MemoryStore):
            raise MemoryError("memory store is required")
        if not callable(evidence_verifier):
            raise MemoryError("trusted evidence verifier is required")
        self._store = store
        self._verify = evidence_verifier

    def submit(self, candidate: MemoryItem) -> str | None:
        """Return item id on intake; return None when evidence cannot be verified.

        The verifier must resolve a reference against a trusted source and ensure
        both owner and session match the supplied scope. This adapter never
        extracts facts, changes scope, or approves a candidate.
        """
        if not isinstance(candidate, MemoryItem):
            raise MemoryError("candidate is required")
        if candidate.status != "pending" or not candidate.evidence_refs:
            return None
        try:
            verified = all(self._verify(candidate.scope, ref) is True
                           for ref in candidate.evidence_refs)
        except Exception:
            # Resolver failures must not turn unverified text into a candidate.
            return None
        if not verified:
            return None
        return self._store.add(candidate)
