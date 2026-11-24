"""Offline evaluation tests use explicitly synthetic bundle fixtures."""
import copy
import tempfile
import unittest
from pathlib import Path

from evaluation_bundle import BundleValidationError, dumps_bundle
from offline_research_evaluation import evaluate_bundle, evaluate_paths, dumps_report
from tests.test_evaluation_bundle import synthetic_bundle


class OfflineResearchEvaluationTests(unittest.TestCase):
    def test_valid_bundle_uses_independent_category_labels_and_denominators(self):
        b = synthetic_bundle()
        ann = b["candidates"][0]["annotations"]
        ann["categories"] = {"science": "relevant", "technology_ai": "not_relevant", "business_economy": "unknown", "sports": "not_assessable"}
        b["candidates"][0]["pipeline_predictions"]["categories"] = ["science"]
        result = evaluate_bundle(b)
        micro = result["category_metrics"]["micro"]
        self.assertEqual((micro["denominator"], micro["true_positive"], micro["false_negative"]), (2, 1, 0))
        self.assertEqual((micro["precision_denominator"], micro["recall_denominator"]), (1, 1))
        self.assertEqual(result["annotation_coverage"]["unknown_or_not_assessable_category_labels"], 2)
        self.assertEqual(result["category_annotation_counts"]["business_economy"]["unknown"], 1)
        self.assertEqual(result["category_annotation_counts"]["sports"]["not_assessable"], 1)
        fields = result["annotation_coverage"]["other_annotation_field_states"]
        self.assertEqual((fields["geographic_relevance"]["unknown"], fields["geographic_relevance"]["not_assessable"]), (0, 0))

    def test_prediction_is_not_used_as_ground_truth(self):
        b = synthetic_bundle()
        c = b["candidates"][0]
        c["pipeline_predictions"]["categories"] = ["science", "made_up"]
        c["annotations"]["categories"] = {"science": "not_relevant"}
        m = evaluate_bundle(b)["category_metrics"]["science"]
        self.assertEqual(m["false_positive"], 1)
        self.assertEqual(m["true_positive"], 0)

    def test_missing_prediction_is_unavailable_not_a_false_negative(self):
        b = synthetic_bundle()
        candidate = b["candidates"][0]
        candidate["annotations"]["categories"] = {"science": "relevant"}
        candidate["pipeline_predictions"].pop("categories")
        result = evaluate_bundle(b)
        metric = result["category_metrics"]["science"]
        self.assertEqual(metric["denominator"], 0)
        self.assertEqual(metric["prediction_unavailable_count"], 1)
        self.assertEqual(metric["false_negative"], 0)

    def test_missing_annotation_and_empty_bundle_are_explicit(self):
        b = synthetic_bundle()
        b["candidates"][0]["annotations"]["categories"] = {}
        result = evaluate_bundle(b)
        self.assertIsNone(result["category_metrics"]["micro"]["precision"])
        empty = evaluate_bundle(synthetic_bundle(candidates=[]))
        self.assertEqual(empty["candidate_count"], 0)
        self.assertEqual(empty["category_metrics"]["micro"]["denominator"], 0)
        self.assertEqual(empty["ranking_quality"]["status"], "unavailable: schema has no independent graded relevance labels")

    def test_insufficient_sample_is_labeled_descriptive_only(self):
        bundle = synthetic_bundle()
        bundle["candidates"][0]["annotations"]["categories"]["science"] = "relevant"
        result = evaluate_bundle(bundle)
        self.assertIn("descriptive only", result["category_metrics"]["micro"]["status"])
        self.assertEqual(result["category_metrics"]["micro"]["denominator"], 1)
        self.assertEqual(result["annotation_coverage"]["candidate_category_slots"], 1)

    def test_multiple_bundles_and_deterministic_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i in range(2):
                path = Path(tmp) / f"{i}.json"
                path.write_text(dumps_bundle(synthetic_bundle()), encoding="utf-8")
                paths.append(path)
            first = evaluate_paths(paths)
            second = evaluate_paths(paths)
            self.assertEqual(first["bundle_count"], 2)
            self.assertEqual(dumps_report(first), dumps_report(second))

    def test_invalid_bundle_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(BundleValidationError):
                evaluate_paths([path])

    def test_duplicate_evaluation_only_when_prediction_groups_exist(self):
        b = synthetic_bundle()
        b["candidates"][0]["pipeline_predictions"]["story_group_id"] = "story-1"
        other = copy.deepcopy(b["candidates"][0])
        other["candidate_id"] = "synthetic-2"
        b["candidates"].append(other)
        self.assertEqual(evaluate_bundle(b)["duplicate_handling"]["grouping_consistency"], 1.0)


if __name__ == "__main__":
    unittest.main()
