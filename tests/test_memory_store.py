"""Offline tests for scoped, bounded short-term memory storage."""
import unittest
from datetime import datetime, timedelta, timezone

from memory_store import (
    MemoryItem, MemoryStore, MemoryError, OwnerScope, search_score,
)


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
        self.scope = OwnerScope("owner-1", "session-1")
        self.store = MemoryStore(max_items=10)

    def item(self, text="prefers concise answers", **kw):
        values = {
            "scope": self.scope,
            "domain": "user_fact",
            "text": text,
            "evidence_refs": ("msg:1",),
            "confidence": 0.9,
            "sensitivity": "low",
            "created_at": self.now,
            "expires_at": self.now + timedelta(days=30),
            "importance": 0.8,
            "status": "pending",
        }
        values.update(kw)
        return MemoryItem(**values)

    def test_scope_isolation_in_search_and_removal(self):
        other = OwnerScope("owner-2", "session-1")
        item = self.item()
        other_item = self.item(scope=other, text="other person's fact", evidence_refs=("msg:2",))
        self.store.add(item)
        self.store.add(other_item)
        self.store.approve(self.scope, item.item_id, authorized_domains={"user_fact"})
        self.store.approve(other, other_item.item_id, authorized_domains={"user_fact"})
        self.assertEqual([x.item_id for x in self.store.search(self.scope, "concise", self.now)], [item.item_id])
        self.assertEqual(self.store.remove(self.scope, item.item_id), 1)
        self.assertEqual(len(self.store.search(other, "fact", self.now)), 1)

    def test_archived_state_cannot_be_injected_through_add(self):
        forged = self.item(status="archived")
        with self.assertRaises(MemoryError):
            self.store.add(forged)
        self.assertEqual(self.store.count(), 0)

    def test_evidence_quotes_only_live_on_pending_candidates_and_are_cleared_on_approval(self):
        pending = self.item(evidence_quotes=("我喜欢喝茶",))
        self.store.add(pending)
        self.assertEqual(self.store.get(self.scope, pending.item_id).evidence_quotes, ("我喜欢喝茶",))
        with self.assertRaises(MemoryError):
            self.item(status="approved", evidence_quotes=("quote",))
        with self.assertRaises(MemoryError):
            self.item(evidence_refs=("msg:1", "msg:2"), evidence_quotes=("quote",))
        self.assertTrue(self.store.approve(self.scope, pending.item_id,
                                           authorized_domains={"user_fact"}))
        approved = self.store.get(self.scope, pending.item_id)
        self.assertEqual(approved.evidence_quotes, ())
        self.assertNotIn("evidence_quotes", self.store.export(self.scope, self.now)[0])

    def test_candidates_are_pending_until_explicit_approval(self):
        candidate = self.item(status="pending")
        self.store.add(candidate)
        self.assertEqual(self.store.search(self.scope, "concise", self.now), [])
        self.assertFalse(self.store.approve(self.scope, candidate.item_id, authorized_domains=set()))
        self.assertTrue(self.store.approve(self.scope, candidate.item_id, authorized_domains={"user_fact"}))
        self.assertEqual(len(self.store.search(self.scope, "concise", self.now)), 1)

    def test_conflicting_fact_key_quarantines_both_and_exposes_conflict(self):
        original = self.item(text="lives in Paris", conflict_key="residence")
        self.store.add(original)
        self.assertTrue(self.store.approve(
            self.scope, original.item_id, authorized_domains={"user_fact"}))
        conflict = self.item(text="lives in Rome", conflict_key="residence",
                             evidence_refs=("msg:2",))
        self.store.add(conflict)
        self.assertEqual(self.store.get(self.scope, original.item_id).status, "quarantined")
        self.assertEqual(self.store.get(self.scope, conflict.item_id).status, "quarantined")
        self.assertEqual({item.item_id for item in self.store.find_conflicts(
            self.scope, "residence")}, {original.item_id, conflict.item_id})
        self.assertEqual(self.store.search(self.scope, "lives", self.now), [])
        self.assertFalse(self.store.approve(
            self.scope, conflict.item_id, authorized_domains={"user_fact"}))

    def test_unverified_candidate_cannot_quarantine_approved_fact(self):
        fact = self.item(text="lives in Paris", conflict_key="residence")
        self.store.add(fact)
        self.assertTrue(self.store.approve(
            self.scope, fact.item_id, authorized_domains={"user_fact"}))
        unsupported = self.item(text="lives in Rome", conflict_key="residence",
                                evidence_refs=())
        self.store.add(unsupported)
        self.assertEqual(self.store.get(self.scope, fact.item_id).status, "approved")
        self.assertEqual(self.store.get(self.scope, unsupported.item_id).status, "pending")
        self.assertEqual(self.store.find_conflicts(self.scope, "residence"), [])

    def test_conflicting_quarantined_item_cannot_be_approved(self):
        paris = self.item(text="lives in Paris", conflict_key="residence")
        self.store.add(paris)
        self.assertTrue(self.store.approve(
            self.scope, paris.item_id, authorized_domains={"user_fact"}))
        rome = self.item(text="lives in Rome", conflict_key="residence",
                         evidence_refs=("msg:2",))
        self.store.add(rome)
        self.assertFalse(self.store.approve(
            self.scope, rome.item_id, authorized_domains={"user_fact"}))
        self.assertFalse(self.store.approve(
            self.scope, paris.item_id, authorized_domains={"user_fact"}))

    def test_conflicts_do_not_cross_session_or_domain(self):
        self.store.add(self.item(text="lives in Paris", conflict_key="residence"))
        other_session = OwnerScope("owner-1", "session-2")
        other_domain = self.item(text="likes tea", domain="topic", conflict_key="residence")
        self.store.add(self.item(scope=other_session, text="lives in Rome",
                                 conflict_key="residence", evidence_refs=("msg:2",)))
        self.store.add(other_domain)
        self.assertEqual(self.store.get(self.scope,
                                      self.item(text="lives in Paris", conflict_key="residence").item_id).status,
                         "pending")
        self.assertEqual(self.store.get(other_session,
                                      self.item(scope=other_session, text="lives in Rome",
                                                conflict_key="residence").item_id).status,
                         "pending")
        self.assertEqual(self.store.find_conflicts(self.scope, "residence"), [])

    def test_correction_changing_conflict_key_reconciles_old_group(self):
        paris = self.item(text="lives in Paris", conflict_key="residence")
        rome = self.item(text="lives in Rome", conflict_key="residence",
                         evidence_refs=("msg:2",))
        self.store.add(paris)
        self.store.add(rome)
        self.assertEqual(self.store.get(self.scope, paris.item_id).status, "quarantined")
        revised = self.item(text="lives in Madrid", conflict_key=None,
                            evidence_refs=("msg:3",))
        corrected = self.store.correct(self.scope, rome.item_id, revised)
        self.assertEqual(corrected.status, "pending")
        self.assertEqual(self.store.get(self.scope, paris.item_id).status, "pending")
        self.assertEqual(self.store.find_conflicts(self.scope, "residence"), [])

    def test_correction_quarantines_conflicting_replacement_and_existing_fact(self):
        existing = self.item(text="lives in Paris", conflict_key="residence")
        replaced = self.item(text="temporary city", conflict_key=None)
        self.store.add(existing)
        self.store.add(replaced)
        self.assertTrue(self.store.approve(
            self.scope, existing.item_id, authorized_domains={"user_fact"}))
        corrected = self.item(item_id="", text="lives in Rome", conflict_key="residence",
                              evidence_refs=("msg:3",))
        result = self.store.correct(self.scope, replaced.item_id, corrected)
        self.assertEqual(result.status, "quarantined")
        self.assertEqual(self.store.get(self.scope, existing.item_id).status, "quarantined")
        self.assertEqual({item.text for item in self.store.find_conflicts(
            self.scope, "residence")}, {"lives in Paris", "lives in Rome"})

    def test_conflict_detection_requires_explicit_key_and_distinct_content(self):
        no_key_a = self.item(text="lives in Paris")
        no_key_b = self.item(text="lives in Rome", evidence_refs=("msg:2",))
        self.store.add(no_key_a)
        self.store.add(no_key_b)
        self.assertEqual(self.store.get(self.scope, no_key_a.item_id).status, "pending")
        self.assertEqual(self.store.get(self.scope, no_key_b.item_id).status, "pending")

        keyed_a = self.item(text="prefers tea", conflict_key="drink")
        keyed_b = self.item(text="prefers tea", conflict_key="drink", evidence_refs=("msg:3",))
        self.store.add(keyed_a)
        self.store.add(keyed_b)
        self.assertEqual(self.store.get(self.scope, keyed_a.item_id).status, "pending")
        self.assertEqual(self.store.get(self.scope, keyed_b.item_id).status, "pending")

    def test_purging_expired_conflict_resolves_remaining_item_to_pending(self):
        paris = self.item(text="lives in Paris", conflict_key="residence")
        self.store.add(paris)
        self.assertTrue(self.store.approve(
            self.scope, paris.item_id, authorized_domains={"user_fact"}))
        rome = self.item(text="lives in Rome", conflict_key="residence",
                         evidence_refs=("msg:2",),
                         created_at=self.now - timedelta(days=2),
                         expires_at=self.now - timedelta(seconds=1))
        self.store.add(rome)
        self.assertEqual(self.store.get(self.scope, paris.item_id).status, "quarantined")
        self.assertEqual(self.store.purge_expired(self.now), 1)
        self.assertEqual(self.store.get(self.scope, paris.item_id).status, "pending")
        self.assertEqual(self.store.find_conflicts(self.scope, "residence"), [])

    def test_removing_conflict_resolves_remaining_item_to_pending(self):
        paris = self.item(text="lives in Paris", conflict_key="residence")
        self.store.add(paris)
        self.assertTrue(self.store.approve(
            self.scope, paris.item_id, authorized_domains={"user_fact"}))
        rome = self.item(text="lives in Rome", conflict_key="residence",
                         evidence_refs=("msg:2",))
        self.store.add(rome)
        self.assertEqual(self.store.get(self.scope, paris.item_id).status, "quarantined")
        self.assertEqual(self.store.remove(self.scope, rome.item_id), 1)
        self.assertEqual(self.store.get(self.scope, paris.item_id).status, "pending")
        self.assertEqual(self.store.find_conflicts(self.scope, "residence"), [])

    def test_conflict_keys_are_validated(self):
        for value in ("", " " * 2, 42):
            with self.assertRaises(MemoryError):
                self.item(conflict_key=value)

    def test_dedup_does_not_auto_approve_pending_candidate(self):
        pending = self.item(status="pending")
        duplicate = self.item(evidence_refs=("msg:2",))
        self.store.add(pending)
        self.store.add(duplicate)
        stored = self.store.get(self.scope, pending.item_id)
        self.assertEqual(stored.status, "pending")
        self.assertEqual(self.store.search(self.scope, "concise", self.now), [])

    def test_dedup_does_not_mutate_nonpending_records(self):
        for status in ("approved", "quarantined", "rejected"):
            with self.subTest(status=status):
                scope = OwnerScope("owner-1", f"session-{status}")
                original = self.item(scope=scope, status="pending" if status == "approved" else status)
                self.store.add(original)
                if status == "approved":
                    self.store.approve(scope, original.item_id, authorized_domains={"user_fact"})
                original = self.store.get(scope, original.item_id)
                duplicate = self.item(scope=scope, evidence_refs=("msg:2",), confidence=1.0)
                self.store.add(duplicate)
                stored = self.store.get(scope, original.item_id)
                self.assertEqual(stored.status, status)
                self.assertEqual(stored.evidence_refs, original.evidence_refs)
                self.assertEqual(stored.confidence, original.confidence)

    def test_only_pending_candidates_can_be_approved(self):
        for status in ("quarantined", "rejected"):
            candidate = self.item(text=f"{status} memory", status=status)
            self.store.add(candidate)
            self.assertFalse(self.store.approve(
                self.scope, candidate.item_id, authorized_domains={"user_fact"}))
            self.assertEqual(self.store.get(self.scope, candidate.item_id).status, status)

    def test_approval_rejects_malformed_authorization_domains(self):
        candidate = self.item()
        self.store.add(candidate)
        with self.assertRaises(MemoryError):
            self.store.approve(self.scope, candidate.item_id, authorized_domains="user_fact")
        with self.assertRaises(MemoryError):
            self.store.approve(self.scope, candidate.item_id, authorized_domains={"not-a-domain"})

    def test_sensitive_or_unverified_memories_not_returned_by_default(self):
        sensitive = self.item(sensitivity="sensitive")
        unverified = self.item(text="unverified claim", status="quarantined", evidence_refs=())
        self.store.add(sensitive)
        self.store.add(unverified)
        self.assertFalse(self.store.approve(self.scope, sensitive.item_id,
                                            authorized_domains={"user_fact"}))
        self.assertFalse(self.store.approve(self.scope, unverified.item_id,
                                            authorized_domains={"user_fact"}))
        self.assertEqual(self.store.search(self.scope, "concise unverified", self.now,
                                           include_sensitive=True), [])

    def test_expired_items_are_not_returned_and_can_be_purged(self):
        expired = self.item(created_at=self.now - timedelta(days=2),
                            expires_at=self.now - timedelta(seconds=1))
        self.store.add(expired)
        self.assertEqual(self.store.search(self.scope, "concise", self.now), [])
        self.assertEqual(self.store.purge_expired(self.now), 1)
        self.assertEqual(self.store.count(), 0)

    def test_hash_dedup_ignores_case_whitespace_and_duplicate_refs(self):
        first = self.item(text="User likes tea", evidence_refs=("msg:1", "msg:1"))
        second = self.item(text=" user   likes TEA ", evidence_refs=("msg:2",))
        self.assertEqual(self.store.add(first), first.item_id)
        self.assertEqual(self.store.add(second), first.item_id)
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.get(self.scope, first.item_id).evidence_refs, ("msg:1", "msg:2"))

    def test_dedup_keeps_earliest_expiry_and_never_extends_retention(self):
        first = self.item(expires_at=self.now + timedelta(days=2))
        later = self.item(expires_at=self.now + timedelta(days=8), evidence_refs=("msg:2",))
        self.store.add(first)
        self.store.add(later)
        self.assertEqual(self.store.get(self.scope, first.item_id).expires_at, first.expires_at)

        permanent = self.item(expires_at=None, evidence_refs=("msg:3",))
        self.store.add(permanent)
        self.assertEqual(self.store.get(self.scope, first.item_id).expires_at, first.expires_at)

    def test_search_orders_by_relevance_freshness_and_confidence(self):
        strong = self.item(text="tea preference", confidence=1.0, importance=1.0)
        weak = self.item(text="tea preference", confidence=0.4, importance=0.4,
                         created_at=self.now - timedelta(days=20),
                         expires_at=self.now + timedelta(days=10), evidence_refs=("msg:2",))
        self.store.add(weak)
        self.store.add(strong)
        self.store.approve(self.scope, weak.item_id, authorized_domains={"user_fact"})
        self.store.approve(self.scope, strong.item_id, authorized_domains={"user_fact"})
        results = self.store.search(self.scope, "tea preference", self.now)
        self.assertEqual(results[0].item_id, strong.item_id)

    def test_capacity_evicts_lowest_value_within_scope_only(self):
        store = MemoryStore(max_items=2)
        low = self.item(text="low", importance=0.1, evidence_refs=("msg:1",))
        high = self.item(text="high", importance=0.9, evidence_refs=("msg:2",))
        newest = self.item(text="new", importance=0.8, evidence_refs=("msg:3",))
        store.add(low)
        store.add(high)
        store.add(newest)
        self.assertIsNone(store.get(self.scope, low.item_id))
        self.assertIsNotNone(store.get(self.scope, high.item_id))
        self.assertIsNotNone(store.get(self.scope, newest.item_id))

    def test_capacity_eviction_reconciles_conflict_group(self):
        store = MemoryStore(max_items=2)
        stable = self.item(text="stable baseline", importance=0.0,
                           conflict_key=None)
        store.add(stable)
        paris = self.item(text="lives in Paris", conflict_key="residence",
                          importance=0.5)
        store.add(paris)
        rome = self.item(text="lives in Rome", conflict_key="residence",
                         importance=1.0, evidence_refs=("msg:2",))
        store.add(rome)
        self.assertEqual(store.count(), 2)
        # The baseline is the lowest-value record; the remaining conflict pair
        # must remain quarantined and discoverable.
        self.assertIsNone(store.get(self.scope, stable.item_id))
        self.assertEqual({item.text for item in store.find_conflicts(
            self.scope, "residence")}, {"lives in Paris", "lives in Rome"})

    def test_capacity_evicts_pending_before_approved_memory(self):
        store = MemoryStore(max_items=1)
        approved = self.item(text="approved high value", importance=0.1)
        store.add(approved)
        self.assertTrue(store.approve(self.scope, approved.item_id,
                                      authorized_domains={"user_fact"}))
        pending = self.item(text="pending candidate", importance=1.0,
                            evidence_refs=("msg:2",))
        store.add(pending)
        self.assertIsNotNone(store.get(self.scope, approved.item_id))
        self.assertIsNone(store.get(self.scope, pending.item_id))

    def test_invalid_scope_evidence_and_numeric_fields_rejected(self):
        for bad in [
            lambda: OwnerScope("", "session"),
            lambda: self.item(status="approved", evidence_refs=()),
            lambda: self.item(confidence=float("nan")),
            lambda: self.item(importance=2),
            lambda: self.item(evidence_refs="msg:1"),
            lambda: self.item(evidence_refs=None),
            lambda: self.item(created_at=datetime.now()),
            lambda: self.item(domain="unknown"),
            lambda: self.item(domain=None),
            lambda: self.item(status=None),
            lambda: self.item(sensitivity=None),
        ]:
            with self.subTest(bad=bad), self.assertRaises(MemoryError):
                bad()

    def test_chinese_substring_query_matches_chinese_memory(self):
        item = self.item(text="用户喜欢喝乌龙茶", evidence_refs=("msg:1",))
        self.store.add(item)
        self.assertTrue(self.store.approve(self.scope, item.item_id,
                                           authorized_domains={"user_fact"}))
        results = self.store.search(self.scope, "喜欢喝茶", self.now)
        self.assertEqual([result.item_id for result in results], [item.item_id])

    def test_search_can_filter_by_domain(self):
        fact = self.item(text="likes tea")
        topic = self.item(domain="topic", text="tea discussion", evidence_refs=("msg:2",))
        self.store.add(fact)
        self.store.add(topic)
        self.store.approve(self.scope, fact.item_id, authorized_domains={"user_fact"})
        self.store.approve(self.scope, topic.item_id, authorized_domains={"topic"})
        self.assertEqual([x.item_id for x in self.store.search(
            self.scope, "tea", self.now, domain="topic")], [topic.item_id])
        with self.assertRaises(MemoryError):
            self.store.search(self.scope, "tea", self.now, domain="unknown")

    def test_export_contains_only_approved_nonsensitive_items_in_scope(self):
        approved = self.item()
        pending = self.item(text="pending note", evidence_refs=("msg:2",))
        other_scope = OwnerScope("owner-2", "session-1")
        foreign = self.item(scope=other_scope, text="foreign tea", evidence_refs=("msg:3",))
        sensitive = self.item(text="sensitive tea", sensitivity="sensitive", evidence_refs=("msg:4",))
        for item in (approved, pending, foreign, sensitive):
            self.store.add(item)
        self.store.approve(self.scope, approved.item_id, authorized_domains={"user_fact"})
        exported = self.store.export(self.scope, self.now)
        self.assertEqual([row["item_id"] for row in exported], [approved.item_id])
        self.assertEqual(exported[0]["scope"], {"owner_id": "owner-1", "session_id": "session-1"})
        self.assertNotIn("sensitive tea", repr(exported))

    def test_correction_replaces_item_as_pending(self):
        original = self.item()
        self.store.add(original)
        self.assertTrue(self.store.approve(self.scope, original.item_id,
                                           authorized_domains={"user_fact"}))
        corrected = self.item(text="prefers short answers", evidence_refs=("msg:2",))
        result = self.store.correct(self.scope, original.item_id, corrected)
        self.assertEqual(result.status, "pending")
        self.assertIsNone(self.store.get(self.scope, original.item_id))
        self.assertIsNotNone(self.store.get(self.scope, corrected.item_id))
        self.assertEqual(self.store.search(self.scope, "short answers", self.now), [])

    def test_correction_rejects_cross_scope_and_preserves_earliest_expiry(self):
        original = self.item(expires_at=self.now + timedelta(days=2))
        self.store.add(original)
        foreign = self.item(scope=OwnerScope("owner-2", "session-1"),
                            text="revised fact", evidence_refs=("msg:2",),
                            expires_at=self.now + timedelta(days=20))
        with self.assertRaises(MemoryError):
            self.store.correct(self.scope, original.item_id, foreign)
        revised = self.item(text="revised fact", evidence_refs=("msg:2",),
                            expires_at=self.now + timedelta(days=20))
        self.store.correct(self.scope, original.item_id, revised)
        self.assertEqual(self.store.get(self.scope, revised.item_id).expires_at,
                         original.expires_at)

    def test_closed_store_clears_data_and_rejects_further_operations(self):
        item = self.item()
        self.store.add(item)
        self.store.close()
        self.store.close()
        self.assertEqual(self.store._items, {})
        operations = {
            "count": self.store.count,
            "add": lambda: self.store.add(item),
            "get": lambda: self.store.get(self.scope, item.item_id),
            "search": lambda: self.store.search(self.scope, "concise", self.now),
            "approve": lambda: self.store.approve(
                self.scope, item.item_id, authorized_domains={"user_fact"}),
            "export": lambda: self.store.export(self.scope, self.now),
            "correct": lambda: self.store.correct(self.scope, item.item_id, item),
            "remove": lambda: self.store.remove(self.scope, item.item_id),
            "purge_expired": lambda: self.store.purge_expired(self.now),
        }
        for name, operation in operations.items():
            with self.subTest(operation=name), self.assertRaisesRegex(MemoryError, "closed"):
                operation()

    def test_importance_adaptive_decay_preserves_baseline_and_ranks_labeled_fixture(self):
        from math import exp
        baseline = self.item(text="likes tea", importance=0.5,
                             created_at=self.now - timedelta(days=30))
        low = self.item(text="likes tea occasionally", importance=0.0,
                        created_at=self.now - timedelta(days=30))
        high = self.item(text="likes tea every morning", importance=1.0,
                         created_at=self.now - timedelta(days=30))
        self.assertAlmostEqual(search_score(baseline, "tea", self.now),
                               0.9 * exp(-1.0) * 0.75)
        self.assertLess(search_score(low, "tea", self.now),
                        search_score(baseline, "tea", self.now))
        self.assertGreater(search_score(high, "tea", self.now),
                           search_score(baseline, "tea", self.now))
        # Synthetic labeled preference fixture: higher labeled importance ranks first.
        self.store.add(low); self.store.add(high)
        self.assertTrue(self.store.approve(self.scope, low.item_id,
                                           authorized_domains={"user_fact"}))
        self.assertTrue(self.store.approve(self.scope, high.item_id,
                                           authorized_domains={"user_fact"}))
        self.assertEqual([item.item_id for item in self.store.search(
            self.scope, "tea", self.now, limit=2)], [high.item_id, low.item_id])

    def test_archive_and_restore_require_reapproval_and_hide_from_export(self):
        archived = self.item(text="likes tea", importance=0.1,
                             created_at=self.now - timedelta(days=100),
                             expires_at=self.now + timedelta(days=20))
        self.store.add(archived)
        self.assertTrue(self.store.approve(self.scope, archived.item_id,
                                           authorized_domains={"user_fact"}))
        self.assertEqual(self.store.archive_low_importance(self.scope, self.now), 1)
        self.assertEqual(self.store.get(self.scope, archived.item_id).status, "archived")
        self.assertEqual(self.store.search(self.scope, "tea", self.now), [])
        self.assertEqual(self.store.export(self.scope, self.now), [])
        self.assertTrue(self.store.restore(self.scope, archived.item_id))
        self.assertEqual(self.store.get(self.scope, archived.item_id).status, "pending")
        self.assertFalse(self.store.restore(self.scope, archived.item_id))
        self.assertTrue(self.store.approve(self.scope, archived.item_id,
                                           authorized_domains={"user_fact"}))

    def test_archive_skips_expired_sensitive_conflicting_or_recent_items(self):
        eligible_expiry = self.now + timedelta(days=1)
        items = [
            self.item(text="expired old", importance=0.1,
                      created_at=self.now-timedelta(days=100), expires_at=self.now),
            self.item(text="sensitive old", importance=0.1, sensitivity="sensitive",
                      created_at=self.now-timedelta(days=100), expires_at=eligible_expiry),
            self.item(text="keyed old", importance=0.1, conflict_key="k",
                      created_at=self.now-timedelta(days=100), expires_at=eligible_expiry),
            self.item(text="recent low", importance=0.1,
                      created_at=self.now-timedelta(days=10), expires_at=eligible_expiry),
            self.item(text="important old", importance=0.8,
                      created_at=self.now-timedelta(days=100), expires_at=eligible_expiry),
            self.item(text="eligible quiet interest", importance=0.1,
                      created_at=self.now-timedelta(days=100), expires_at=eligible_expiry),
        ]
        for item in items:
            self.store.add(item)
            if item.sensitivity != "sensitive":
                self.store.approve(self.scope, item.item_id,
                                   authorized_domains={"user_fact"})
        self.assertEqual(self.store.archive_low_importance(
            self.scope, self.now, threshold=0.05), 0)
        self.assertEqual(self.store.purge_expired(self.now), 1)
        self.assertEqual(self.store.archive_low_importance(
            self.scope, self.now, threshold=0.1, min_age_days=90), 1)
        self.assertEqual(self.store.get(self.scope, items[1].item_id).status, "pending")
        self.assertEqual(self.store.get(self.scope, items[2].item_id).status, "approved")
        self.assertEqual(self.store.get(self.scope, items[3].item_id).status, "approved")
        self.assertEqual(self.store.get(self.scope, items[4].item_id).status, "approved")

    def test_archive_parameters_are_validated(self):
        for kwargs in ({"threshold": True}, {"threshold": 2},
                       {"min_age_days": True}, {"min_age_days": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MemoryError):
                self.store.archive_low_importance(self.scope, self.now, **kwargs)

    def test_search_rejects_malformed_query_and_sensitive_flag(self):
        item = self.item(text="sensitive concise", sensitivity="sensitive")
        self.store.add(item)
        self.store.approve(self.scope, item.item_id, authorized_domains={"user_fact"})
        for query in (None, 42):
            with self.assertRaises(MemoryError):
                self.store.search(self.scope, query, self.now)
            with self.assertRaises(MemoryError):
                search_score(item, query, self.now)
        for flag in ("yes", 1):
            with self.assertRaises(MemoryError):
                self.store.search(self.scope, "sensitive", self.now, include_sensitive=flag)
        self.assertEqual(self.store.search(
            self.scope, "sensitive", self.now, include_sensitive=False), [])

    def test_search_score_rejects_empty_query_and_future_item(self):
        item = self.item()
        self.assertEqual(search_score(item, "", self.now), 0)
        with self.assertRaises(MemoryError):
            search_score(item, "concise", self.now - timedelta(days=1))


if __name__ == "__main__":
    unittest.main()
