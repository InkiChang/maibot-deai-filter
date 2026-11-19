"""Offline tests for deterministic persona-state transitions."""
import unittest
from datetime import datetime, timedelta, timezone

from state_engine import (
    AxisConfig, PersonaState, StateEvent, StateError, advance_state,
    default_state, state_from_dict, state_to_dict,
)


class StateEngineTests(unittest.TestCase):
    def setUp(self):
        self.t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.state = default_state(self.t0)

    def test_decay_returns_toward_baseline(self):
        state = PersonaState(
            at=self.t0, mood=1.0, energy=0.2, stress=1.0, boredom=1.0,
            closeness=0.8, trust=0.8, familiarity=0.8,
        )
        after = advance_state(state, self.t0 + timedelta(hours=1))
        self.assertLess(after.mood, state.mood)
        self.assertGreater(after.energy, state.energy)
        self.assertLess(after.stress, state.stress)
        self.assertLess(after.boredom, state.boredom)
        self.assertEqual(after.closeness, state.closeness)

    def test_effect_is_weighted_by_confidence_and_relevance(self):
        event = StateEvent(effects={"mood": 0.5}, confidence=0.5, relevance=0.5)
        after = advance_state(self.state, self.t0, event=event)
        self.assertAlmostEqual(after.mood, 0.125)

    def test_effects_clamp_to_axis_range(self):
        event = StateEvent(effects={"mood": 9, "energy": -9}, confidence=1, relevance=1)
        after = advance_state(self.state, self.t0, event=event)
        self.assertEqual(after.mood, 1)
        self.assertEqual(after.energy, 0)

    def test_unknown_axis_rejected(self):
        with self.assertRaises(StateError):
            StateEvent(effects={"identity": 1}, confidence=1, relevance=1)

    def test_low_confidence_has_proportionally_small_effect(self):
        event = StateEvent(effects={"stress": 1}, confidence=0.2, relevance=1)
        after = advance_state(self.state, self.t0, event=event)
        self.assertAlmostEqual(after.stress, 0.2)

    def test_relationship_change_requires_evidence_and_rate_is_limited(self):
        event = StateEvent(effects={}, confidence=1, relevance=1,
                           relationship_deltas={"trust": 0.9}, evidence_refs=("msg:1",))
        after = advance_state(self.state, self.t0, event=event)
        self.assertEqual(after.trust, 0.02)
        no_evidence = advance_state(self.state, self.t0, event=None)
        self.assertEqual(no_evidence.trust, 0)

    def test_relationship_change_without_evidence_is_rejected(self):
        with self.assertRaises(StateError):
            StateEvent(effects={}, relationship_deltas={"trust": 0.1})

    def test_event_copies_and_freezes_input_mappings(self):
        effects = {"mood": 0.2}
        event = StateEvent(effects=effects)
        effects["mood"] = -0.2
        self.assertAlmostEqual(advance_state(self.state, self.t0, event=event).mood, 0.2)
        with self.assertRaises(TypeError):
            event.effects["mood"] = 0.5

    def test_invalid_configuration_mapping_is_rejected(self):
        with self.assertRaises(StateError):
            advance_state(self.state, self.t0, configs={"unknown": AxisConfig(0, 1, -1, 1)})

    def test_timezone_required_and_backwards_time_rejected(self):
        with self.assertRaises(StateError):
            advance_state(self.state, datetime(2026, 1, 1))
        with self.assertRaises(StateError):
            advance_state(self.state, self.t0 - timedelta(seconds=1))

    def test_custom_baseline_and_half_life_are_validated(self):
        config = {"mood": AxisConfig(baseline=-0.5, half_life_seconds=3600, minimum=-1, maximum=1)}
        state = PersonaState(at=self.t0, mood=1, energy=0.5, stress=0.2, boredom=0.3,
                             closeness=0, trust=0, familiarity=0)
        after = advance_state(state, self.t0 + timedelta(hours=1), configs=config)
        self.assertAlmostEqual(after.mood, 0.25)
        with self.assertRaises(StateError):
            AxisConfig(baseline=0, half_life_seconds=0, minimum=0, maximum=1)

    def test_serialization_roundtrip(self):
        raw = state_to_dict(self.state)
        restored = state_from_dict(raw)
        self.assertEqual(restored, self.state)

    def test_corrupt_serialized_state_rejected(self):
        raw = state_to_dict(self.state)
        raw["trust"] = 2
        with self.assertRaises(StateError):
            state_from_dict(raw)
        raw = state_to_dict(self.state)
        raw["at"] = "not a date"
        with self.assertRaises(StateError):
            state_from_dict(raw)


if __name__ == "__main__":
    unittest.main()
