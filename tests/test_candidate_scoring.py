"""Offline tests for deterministic behavior-candidate scoring."""
import unittest

from candidate_scoring import Candidate, ScoringContext, score_candidates, choose_candidate


class CandidateScoringTests(unittest.TestCase):
    def test_positive_factors_and_risks_are_weighted(self):
        candidate = Candidate(
            candidate_id="continue_topic",
            factors={"personality": 0.8, "relationship": 0.5, "state": 1.0,
                     "continuity": 0.6, "relevance": 0.9},
            risks={"conflict": 0.2, "repetition": 0.1, "boundary": 0.0, "intrusion": 0.1},
        )
        result = score_candidates([candidate])[0]
        expected = (0.8 * 0.2 + 0.5 * 0.2 + 1.0 * 0.2 + 0.6 * 0.2 + 0.9 * 0.2
                    - 0.2 * 0.3 - 0.1 * 0.2 - 0.1 * 0.2)
        self.assertAlmostEqual(result.score, expected)
        self.assertEqual(result.candidate_id, "continue_topic")

    def test_missing_factors_and_risks_are_neutral_zero(self):
        result = score_candidates([Candidate("minimal")])[0]
        self.assertEqual(result.score, 0)

    def test_candidates_returned_in_stable_score_then_id_order(self):
        items = [Candidate("z", factors={"relevance": 0.5}),
                 Candidate("a", factors={"relevance": 0.5}),
                 Candidate("top", factors={"relevance": 1})]
        self.assertEqual([x.candidate_id for x in score_candidates(items)], ["top", "a", "z"])

    def test_hard_boundary_risk_can_veto_candidate(self):
        items = [Candidate("risky", factors={"relevance": 1}, risks={"boundary": 1}),
                 Candidate("safe", factors={"relevance": 0.5})]
        choice = choose_candidate(items, ScoringContext(minimum_score=0.05, minimum_margin=0.02))
        self.assertEqual(choice.candidate_id, "safe")

    def test_low_score_abstains(self):
        items = [Candidate("weak", factors={"relevance": 0.1})]
        choice = choose_candidate(items, ScoringContext(minimum_score=0.1))
        self.assertIsNone(choice.candidate_id)
        self.assertEqual(choice.reason, "below_minimum")

    def test_close_high_impact_candidates_request_arbitration(self):
        items = [Candidate("a", factors={"relevance": 0.8}, impact=0.9),
                 Candidate("b", factors={"relevance": 0.78}, impact=0.9)]
        context = ScoringContext(minimum_score=0.1, minimum_margin=0.1, arbitration_impact=0.7)
        result = choose_candidate(items, context)
        self.assertIsNone(result.candidate_id)
        self.assertTrue(result.needs_arbitration)
        self.assertEqual(result.reason, "close_high_impact")

    def test_close_low_impact_candidates_choose_stably_without_arbitration(self):
        items = [Candidate("b", factors={"relevance": 0.8}, impact=0.2),
                 Candidate("a", factors={"relevance": 0.8}, impact=0.2)]
        context = ScoringContext(minimum_score=0.1, minimum_margin=0.1, arbitration_impact=0.7)
        result = choose_candidate(items, context)
        self.assertEqual(result.candidate_id, "a")
        self.assertFalse(result.needs_arbitration)

    def test_rejects_unknown_fields_nonfinite_values_and_duplicate_ids(self):
        for bad in [
            lambda: Candidate("x", factors={"unknown": 1}),
            lambda: Candidate("x", factors={"relevance": float("nan")}),
            lambda: Candidate("x", risks={"intrusion": -0.1}),
            lambda: Candidate("x", impact=2),
        ]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                bad()
        with self.assertRaises(ValueError):
            score_candidates([Candidate("same"), Candidate("same")])


if __name__ == "__main__":
    unittest.main()
