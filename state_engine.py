"""Pure deterministic state core; no model, host I/O, or personality overrides."""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
from math import isfinite, exp, log
from types import MappingProxyType
from typing import Mapping


class StateError(ValueError):
    pass


def number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StateError("state values must be numbers")
    try:
        valid = isfinite(value) and low <= value <= high
    except OverflowError:
        valid = False
    if not valid:
        raise StateError("state value out of range")
    return float(value)


def aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise StateError("an aware datetime is required")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class AxisConfig:
    baseline: float
    half_life_seconds: float
    minimum: float
    maximum: float

    def __post_init__(self):
        number(self.minimum, -1, 1)
        number(self.maximum, -1, 1)
        if self.minimum >= self.maximum:
            raise StateError("invalid axis range")
        number(self.baseline, self.minimum, self.maximum)
        number(self.half_life_seconds, 0.001, 315360000)


DEFAULT_AXES = MappingProxyType({
    "mood": AxisConfig(0, 7200, -1, 1),
    "energy": AxisConfig(0.7, 14400, 0, 1),
    "stress": AxisConfig(0, 3600, 0, 1),
    "boredom": AxisConfig(0.2, 10800, 0, 1),
})
RELATION_AXES = frozenset({"closeness", "trust", "familiarity"})


@dataclass(frozen=True)
class PersonaState:
    at: datetime
    mood: float = 0
    energy: float = 0.7
    stress: float = 0
    boredom: float = 0.2
    closeness: float = 0
    trust: float = 0
    familiarity: float = 0

    def __post_init__(self):
        object.__setattr__(self, "at", aware(self.at))
        for name, axis in DEFAULT_AXES.items():
            number(getattr(self, name), axis.minimum, axis.maximum)
        for name in RELATION_AXES:
            number(getattr(self, name), 0, 1)


@dataclass(frozen=True)
class StateEvent:
    effects: Mapping[str, float]
    confidence: float = 1
    relevance: float = 1
    relationship_deltas: Mapping[str, float] | None = None
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self):
        number(self.confidence, 0, 1)
        number(self.relevance, 0, 1)
        for data, allowed in ((self.effects, DEFAULT_AXES),
                              (self.relationship_deltas or {}, RELATION_AXES)):
            if not isinstance(data, Mapping) or set(data) - set(allowed):
                raise StateError("unknown state axis")
            for value in data.values():
                number(value, -10, 10)
        if (not isinstance(self.evidence_refs, tuple) or len(self.evidence_refs) > 16
                or any(not isinstance(x, str) or not x or len(x) > 128 for x in self.evidence_refs)):
            raise StateError("invalid evidence references")
        if self.relationship_deltas and not self.evidence_refs:
            raise StateError("relationship updates require evidence references")
        object.__setattr__(self, "effects", MappingProxyType(dict(self.effects)))
        object.__setattr__(self, "relationship_deltas", MappingProxyType(dict(self.relationship_deltas or {})))


def default_state(at: datetime) -> PersonaState:
    return PersonaState(at=at)


def advance_state(state: PersonaState, at: datetime, *, event: StateEvent | None = None,
                  configs: Mapping[str, AxisConfig] | None = None) -> PersonaState:
    at = aware(at)
    elapsed = (at - state.at).total_seconds()
    if elapsed < 0:
        raise StateError("time must not run backwards")
    axes = dict(DEFAULT_AXES)
    if configs is not None:
        if set(configs) - set(axes) or any(not isinstance(c, AxisConfig) for c in configs.values()):
            raise StateError("invalid axis configuration")
        axes.update(configs)
    weight = event.confidence * event.relevance if event else 0
    values = {}
    for name, axis in axes.items():
        previous = getattr(state, name)
        value = axis.baseline + (previous - axis.baseline) * exp(-log(2) * elapsed / axis.half_life_seconds)
        if event:
            value += event.effects.get(name, 0) * weight
        values[name] = min(axis.maximum, max(axis.minimum, value))
    for name in RELATION_AXES:
        delta = (event.relationship_deltas.get(name, 0) * weight) if event else 0
        delta = min(0.02, max(-0.02, delta))
        values[name] = min(1, max(0, getattr(state, name) + delta))
    return PersonaState(at=at, **values)


def state_to_dict(state: PersonaState) -> dict:
    return {"version": 1, "at": state.at.isoformat(),
            **{f.name: getattr(state, f.name) for f in fields(state) if f.name != "at"}}


def state_from_dict(raw: dict) -> PersonaState:
    expected = {f.name for f in fields(PersonaState)} | {"version"}
    if not isinstance(raw, dict) or set(raw) != expected or type(raw.get("version")) is not int or raw["version"] != 1:
        raise StateError("unsupported state format")
    try:
        return PersonaState(at=datetime.fromisoformat(raw["at"]),
                            **{k: v for k, v in raw.items() if k not in {"at", "version"}})
    except (ValueError, TypeError, OverflowError):
        raise StateError("invalid serialized state") from None
