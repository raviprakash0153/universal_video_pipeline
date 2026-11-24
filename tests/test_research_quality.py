import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import research_cli
import research_intelligence as ri
import research_quality as rq


class ResearchQualityTests(unittest.TestCase):
    def test_synthetic_fixtures_cover_all_categories_and_known_labels(self):
        rows = rq.synthetic_fixtures()
        self.assertEqual(set(ri.CATEGORIES), {category for x in rows for category in x["expected_categories"]})
        data = rq.evaluate(rows)
        self.assertEqual(data["topics"], len(rows))
        self.assertEqual(data["classification"]["status"], "measured on labelled fixtures")
        self.assertEqual(data["geography"]["india_provenance"] > 0, True)
        self.assertEqual(data["geography"]["global_provenance"] > 0, True)
        predictions = {c for row in rows for c in row["relevant_categories"]}
        self.assertTrue(predictions & set(ri.CATEGORIES))
        self.assertTrue(any(set(row["expected_categories"]) - set(row["relevant_categories"]) for row in rows))

    def test_unrequested_and_empty_categories_are_not_failures(self):
        data = rq.evaluate([], requested_categories={"science"})
        self.assertTrue(data["category_coverage"]["science"]["requested"])
        self.assertTrue(data["category_coverage"]["science"]["no_results"])
        self.assertIsNone(data["category_coverage"]["politics"]["no_results"])
        self.assertEqual(data["classification"]["status"], "unavailable")

    def test_unknown_publisher_and_syndication_are_explicit(self):
        data = rq.evaluate(rq.synthetic_fixtures())
        self.assertGreater(data["publishers"]["unknown_identity_articles"], 0)
        self.assertGreater(data["publishers"]["approximate_synthetic_syndicated_article_rows"], 0)
        self.assertGreater(data["publishers"]["approximate_synthetic_duplicate_headline_groups"], 0)

    def test_historical_reads_snapshots_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research.db"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE research_topics(run_id TEXT, topic_id TEXT, snapshot_json TEXT)")
            db.execute("INSERT INTO research_topics VALUES(?,?,?)", ("run", "one", json.dumps({"headline":"Saved", "category":"science", "articles":[]})))
            db.commit()
            before = db.execute("SELECT * FROM research_topics").fetchall()
            db.close()
            rows, runs = rq.load_history(path)
            result = rq.evaluate(rows, source="historical")
            db = sqlite3.connect(path)
            self.assertEqual(db.execute("SELECT * FROM research_topics").fetchall(), before)
            self.assertEqual(runs, ["run"])
            self.assertEqual(result["classification"]["status"], "unavailable")
            db.close()

    def test_cli_normal_diagnostic_and_history_modes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(research_cli.main(["evaluate-quality"]), 0)
        self.assertIn("(synthetic)", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(research_cli.main(["evaluate-quality", "--diagnostic"]), 0)
        self.assertIn("Warning:", out.getvalue())
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(research_cli.main(["evaluate-quality", "--history", "--db", str(Path(tmp)/"empty.db")]), 0)
            self.assertIn("historical (no snapshots)", out.getvalue())
            self.assertNotIn("(synthetic)", out.getvalue())

    def test_score_order_and_missing_components_are_reported(self):
        data = rq.evaluate(rq.synthetic_fixtures())
        self.assertTrue(data["ranking"]["score_ordered"])
        self.assertTrue(data["ranking"]["production_ranking_determinism"])
        self.assertIn("evidence", data["ranking"]["score_components_missing"])

    def test_coverage_modes_and_shortlist_constraints_are_reported(self):
        rows = rq.synthetic_fixtures()
        for mode in ("india", "global", "india_and_global"):
            result = rq.evaluate(rows, coverage=mode)
            self.assertEqual(result["geography"]["requested_mode"], mode)
            self.assertGreaterEqual(sum(result["geography"]["shortlist_distribution"].values()), 1)
        self.assertTrue(rq.evaluate(rows)["geography"]["balancing_adjustments"])

    def test_mixed_category_and_precedence_labels_are_counted(self):
        rows = rq.synthetic_fixtures()
        data = rq.evaluate(rows)
        self.assertIn("politics", next(t for t in rows if t["topic_id"] == "mixed")["relevant_categories"])
        self.assertEqual(next(t for t in rows if t["topic_id"] == "tech-false-positive")["category"], "politics")
        self.assertGreaterEqual(data["classification"]["false_positives"], 0)
        self.assertIn("CONFLICTING", data["verification"]["statuses"])
        self.assertGreater(data["verification"]["evidence"]["inaccessible"], 0)

    def test_classifier_predictions_never_fall_back_to_expected_labels(self):
        metrics = rq._class_metrics([
            {"expected_categories": ["science"], "relevant_categories": ["sports"]},
            {"expected_categories": ["science"], "relevant_categories": []},
        ])
        self.assertEqual((metrics["true_positives"], metrics["false_positives"], metrics["false_negatives"]), (0, 1, 2))
        self.assertEqual(metrics["precision"], 0)
        self.assertEqual(metrics["recall"], 0)

    def test_micro_precision_recall_f1_and_zero_denominators(self):
        perfect = rq._class_metrics([{"expected_categories": ["science", "sports"], "relevant_categories": ["science", "sports"]}])
        self.assertEqual((perfect["precision"], perfect["recall"], perfect["f1_score"]), (1, 1, 1))
        mixed = rq._class_metrics([{"expected_categories": ["science"], "relevant_categories": ["sports"]}])
        self.assertEqual((mixed["true_positives"], mixed["false_positives"], mixed["false_negatives"]), (0, 1, 1))
        no_prediction = rq._class_metrics([{"expected_categories": ["science"], "relevant_categories": []}])
        self.assertIsNone(no_prediction["precision"])
        self.assertEqual(no_prediction["recall"], 0)
        no_labels = rq._class_metrics([{"expected_categories": [], "relevant_categories": []}])
        self.assertIsNone(no_labels["precision"])
        self.assertIsNone(no_labels["recall"])
        false_positive_only = rq._class_metrics([{"expected_categories": [], "relevant_categories": ["sports"]}])
        self.assertEqual(false_positive_only["precision"], 0)
        self.assertIsNone(false_positive_only["recall"])
        self.assertIsNone(false_positive_only["f1_score"])

    def test_missing_history_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.db"
            self.assertEqual(rq.load_history(path), ([], []))
            self.assertFalse(path.exists())

    def test_report_labels_synthetic_fixture_counts(self):
        output = rq.report(rq.evaluate(rq.synthetic_fixtures()))
        self.assertIn("Synthetic fixture rows", output)
        self.assertNotIn("Candidates", output)

    def test_determinism_check_rejects_order_dependent_results(self):
        rows = [{"id": "a"}, {"id": "b"}]
        self.assertFalse(rq._ranking_is_deterministic(lambda values: [x["id"] for x in values], rows))


if __name__ == "__main__":
    unittest.main()
