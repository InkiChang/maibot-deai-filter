"""Deterministic, bounded scoring for behavior candidates; no model calls."""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType
from typing import Mapping, Sequence


FACTOR_WEIGHTS = MappingProxyType({
    "personality": 0.20,
    "relationship": 0.20,
    "state": 0.20,
    "continuity": 0.20,
    "relevance": 0.20,
})
RISK_WEIGHTS = MappingProxyType({
    "conflict": 0.30,
    "repetition": 0.20,
    "boundary": 0.50,
    "intrusion": 0.20,
})


def _unit(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number from 0 to 1")
    try:
        valid = isfinite(value) and 0 <= value <= 1
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be a number from 0 to 1")
    return float(value)


def _measure_map(value: Mapping[str, float], allowed: Mapping[str, float], name: str):
    if not isinstance(value, Mapping) or set(value) - set(allowed):
        raise ValueError(f"unknown {name} factor")
    clean = {key: _unit(item, name) for key, item in value.items()}
    return MappingProxyType(clean)


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    factors: Mapping[str, float] = field(default_factory=dict)
    risks: Mapping[str, float] = field(default_factory=dict)
    impact: float = 0.0

    def __post_init__(self):
        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip() or len(self.candidate_id) > 128:
            raise ValueError("candidate_id must be a non-empty short string")
        object.__setattr__(self, "factors", _measure_map(self.factors, FACTOR_WEIGHTS, "candidate"))
        object.__setattr__(self, "risks", _measure_map(self.risks, RISK_WEIGHTS, "risk"))
        object.__setattr__(self, "impact", _unit(self.impact, "impact"))


@dataclass(frozen=True)
class ScoredCandidate:
    candidate_id: str
    score: float
    impact: float


@dataclass(frozen=True)
class ScoringContext:
    minimum_score: float = 0.15
    minimum_margin: float = 0.12
    arbitration_impact: float = 0.7

    def __post_init__(self):
        _unit(self.minimum_score, "minimum_score")
        _unit(self.minimum_margin, "minimum_margin")
        _unit(self.arbitration_impact, "arbitration_impact")


@dataclass(frozen=True)
class Choice:
    candidate_id: str | None
    score: float | None
    needs_arbitration: bool
    reason: str


def score_candidates(candidates: Sequence[Candidate]) -> list[ScoredCandidate]:
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise ValueError("candidates must be a sequence")
    if any(not isinstance(candidate, Candidate) for candidate in candidates):
        raise ValueError("each candidate must be validated")
    ids = [candidate.candidate_id for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise ValueError("candidate IDs must be unique")
    result = []
    for candidate in candidates:
        if candidate.risks.get("boundary", 0) >= 1.0:
            continue
        positive = sum(candidate.factors.get(key, 0) * weight for key, weight in FACTOR_WEIGHTS.items())
        risk = sum(candidate.risks.get(key, 0) * weight for key, weight in RISK_WEIGHTS.items())
        result.append(ScoredCandidate(candidate.candidate_id, positive - risk, candidate.impact))
    return sorted(result, key=lambda item: (-item.score, item.candidate_id))


def choose_candidate(candidates: Sequence[Candidate], context: ScoringContext | None = None) -> Choice:
    context = context or ScoringContext()
    if not isinstance(context, ScoringContext):
        raise ValueError("context must be a ScoringContext")
    scored = score_candidates(candidates)
    if not scored:
        return Choice(None, None, False, "no_candidates")
    best = scored[0]
    if best.score < context.minimum_score:
        return Choice(None, best.score, False, "below_minimum")
    if len(scored) > 1 and best.score - scored[1].score < context.minimum_margin:
        if best.impact >= context.arbitration_impact:
            return Choice(None, best.score, True, "close_high_impact")
    return Choice(best.candidate_id, best.score, False, "highest_score")
