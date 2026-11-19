"""Offline tests for fail-closed candidate intake."""
import unittest
from datetime import datetime, timezone

from candidate_intake import CandidateIntake
from memory_store import MemoryError, MemoryItem, MemoryStore, OwnerScope


class CandidateIntakeTests(unittest.TestCase):
    def setUp(self):
        self.scope = OwnerScope("owner-a", "session-a")
        self.now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        self.store = MemoryStore()
        self.verified = {("owner-a", "session-a", "msg:1"),
                         ("owner-a", "session-a", "msg:2")}
        self.calls = []
        def verify(scope, ref):
            self.calls.append((scope, ref))
            return (scope.owner_id, scope.session_id, ref) in self.verified
        self.intake = CandidateIntake(self.store, verify)

    def item(self, **kwargs):
        values = dict(scope=self.scope, domain="user_fact", text="prefers tea",
                      evidence_refs=("msg:1",), confidence=0.8,
                      sensitivity="normal", created_at=self.now, status="pending")
        values.update(kwargs)
        return MemoryItem(**values)

    def test_verified_candidate_enters_pending_without_auto_approval(self):
        item = self.item()
        self.assertEqual(self.intake.submit(item), item.item_id)
        self.assertEqual(self.store.get(self.scope, item.item_id).status, "pending")
        self.assertEqual(self.store.search(self.scope, "tea", self.now), [])

    def test_missing_evidence_is_rejected(self):
        item = self.item(evidence_refs=())
        self.assertIsNone(self.intake.submit(item))
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(self.calls, [])

    def test_every_reference_must_resolve_to_exact_scope(self):
        item = self.item(evidence_refs=("msg:1", "foreign:1"))
        self.assertIsNone(self.intake.submit(item))
        self.assertEqual(self.store.count(), 0)

    def test_verifier_must_return_exact_boolean_true(self):
        for result in (1, "true", object()):
            with self.subTest(result_type=type(result).__name__):
                intake = CandidateIntake(self.store, lambda scope, ref: result)
                self.assertIsNone(intake.submit(self.item(text=f"candidate {type(result).__name__}")))
        self.assertEqual(self.store.count(), 0)

    def test_untrusted_resolver_exception_fails_closed(self):
        intake = CandidateIntake(self.store, lambda scope, ref: (_ for _ in ()).throw(RuntimeError()))
        self.assertIsNone(intake.submit(self.item()))
        self.assertEqual(self.store.count(), 0)

    def test_non_pending_candidate_cannot_bypass_review(self):
        for status in ("approved", "quarantined", "rejected", "archived"):
            with self.subTest(status=status):
                self.assertIsNone(self.intake.submit(self.item(status=status, text="fact " + status)))
        self.assertEqual(self.store.count(), 0)

    def test_cross_scope_item_is_only_accepted_if_refs_verify_for_that_scope(self):
        foreign = OwnerScope("owner-b", "session-b")
        item = self.item(scope=foreign)
        self.assertIsNone(self.intake.submit(item))
        self.assertEqual(self.store.count(), 0)

    def test_duplicate_refs_are_verified_once_and_existing_dedup_is_used(self):
        first = self.item(text="prefers tea", evidence_refs=("msg:1",))
        second = self.item(text="prefers tea", evidence_refs=("msg:1", "msg:1", "msg:2"))
        first_id = self.intake.submit(first)
        second_id = self.intake.submit(second)
        self.assertEqual(first_id, second_id)
        stored = self.store.get(self.scope, first_id)
        self.assertEqual(stored.evidence_refs, ("msg:1", "msg:2"))
        self.assertEqual(self.store.count(), 1)

    def test_constructor_requires_store_and_verifier(self):
        with self.assertRaises(MemoryError):
            CandidateIntake(None, lambda scope, ref: True)
        with self.assertRaises(MemoryError):
            CandidateIntake(self.store, None)


if __name__ == "__main__":
    unittest.main()
