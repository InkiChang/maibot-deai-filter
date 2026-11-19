"""Offline policy/schema tests for short SubQ decisions."""
import json
import unittest

from subq_schema import POLICIES, SubQDecision, SubQSchemaError, parse_decision


class SubQSchemaTests(unittest.TestCase):
    def setUp(self):
        self.refs = {"msg:123", "memory.fact_1"}

    def payload(self, **overrides):
        obj = {"action": "reply_now", "reason_code": "clear_intent",
               "confidence": 0.75, "context_refs": ["msg:123"], "ttl": 60}
        obj.update(overrides)
        return json.dumps(obj, allow_nan=True)

    def test_valid_task_specific_decisions(self):
        cases = {
            "reply_ambiguity": ("reply_now", "clear_intent", 60),
            "life_share": ("share", "relevant", 3600),
            "memory_candidate": ("keep_candidate", "supported", 86400),
            "plan_conflict": ("adjust", "conflict", 10),
        }
        for task, (action, reason, ttl) in cases.items():
            with self.subTest(task=task):
                result = parse_decision(self.payload(action=action, reason_code=reason, ttl=ttl),
                                        task=task, allowed_context_refs=self.refs)
                self.assertIsInstance(result, SubQDecision)
                self.assertEqual(result.action, action)
                self.assertEqual(result.context_refs, ("msg:123",))

    def test_rejects_unknown_task_and_forbidden_action_or_reason(self):
        self.assertEqual(set(POLICIES), {"reply_ambiguity", "life_share", "memory_candidate", "plan_conflict"})
        for task, changes in (("reply_ambiguity", {"action": "send_message"}),
                              ("life_share", {"action": "execute"}),
                              ("memory_candidate", {"action": "write_database"}),
                              ("plan_conflict", {"reason_code": "clear_intent"})):
            with self.subTest(task=task):
                with self.assertRaises(SubQSchemaError):
                    parse_decision(self.payload(**changes), task=task, allowed_context_refs=self.refs)
        with self.assertRaises(SubQSchemaError):
            parse_decision(self.payload(), task="arbitrary", allowed_context_refs=self.refs)

    def test_exact_fields_only(self):
        for raw in (self.payload(extra="x"), json.dumps({"action": "reply_now"}),
                    self.payload().replace('"ttl": 60', '"ttl": 60, "action": "reply_now"')):
            with self.subTest(raw=raw):
                with self.assertRaises(SubQSchemaError):
                    parse_decision(raw, task="reply_ambiguity", allowed_context_refs=self.refs)

    def test_confidence_is_finite_number_not_bool_and_in_range(self):
        for value in (-0.01, 1.01, True, "0.5", float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(SubQSchemaError):
                    parse_decision(self.payload(confidence=value), task="reply_ambiguity",
                                   allowed_context_refs=self.refs)

    def test_context_refs_must_be_explicitly_authorized_unique_and_bounded(self):
        for refs in (["not-allowed"], ["msg:123", "msg:123"], ["bad ref"], ["x"] * 17):
            with self.subTest(refs=refs):
                with self.assertRaises(SubQSchemaError):
                    parse_decision(self.payload(context_refs=refs), task="reply_ambiguity",
                                   allowed_context_refs=self.refs)
        with self.assertRaises(SubQSchemaError):
            parse_decision(self.payload(), task="reply_ambiguity", allowed_context_refs={"bad ref"})

    def test_ttl_integer_and_task_maximum(self):
        for ttl in (-1, 3601, True, 1.5):
            with self.subTest(ttl=ttl):
                with self.assertRaises(SubQSchemaError):
                    parse_decision(self.payload(ttl=ttl), task="reply_ambiguity",
                                   allowed_context_refs=self.refs)

    def test_rejects_duplicate_keys_nonfinite_constants_bad_json_and_size(self):
        cases = ['{"action":"reply_now","action":"defer"}',
                 self.payload().replace('0.75', 'NaN'), "not json", " " * 8193]
        for raw in cases:
            with self.subTest(prefix=raw[:40]):
                with self.assertRaises(SubQSchemaError):
                    parse_decision(raw, task="reply_ambiguity", allowed_context_refs=self.refs)

    def test_no_model_text_or_unknown_reference_is_interpreted_as_command(self):
        raw = self.payload(context_refs=["msg:123"], action="reply_now")
        decision = parse_decision(raw, task="reply_ambiguity", allowed_context_refs=self.refs)
        self.assertEqual(decision.action, "reply_now")
        self.assertFalse(hasattr(decision, "reply_text"))
        self.assertFalse(hasattr(decision, "write_memory"))


if __name__ == "__main__":
    unittest.main()
