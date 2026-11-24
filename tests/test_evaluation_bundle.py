"""Structural tests over synthetic bundles; not real-world quality results."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evaluation_bundle import (
    BundleValidationError,
    SCHEMA_VERSION,
    BundleExistsError,
    dumps_bundle,
    loads_bundle,
    validate_bundle,
    write_bundle,
)


def synthetic_bundle(candidates=None):
    return {
        "schema_version": SCHEMA_VERSION,
        "capture": {"timestamp": "2026-01-02T03:04:05+05:30", "timezone": "Asia/Kolkata"},
        "project": {"code_version": "synthetic-test-version"},
        "scope": {"selected_categories": ["science"], "coverage_mode": "india_and_global"},
        "discovery": {"queries": [{"query": "synthetic query", "provider": "fixture"}], "metadata": {"run": "fixture"}},
        "candidates": candidates if candidates is not None else [{
            "candidate_id": "synthetic-1",
            "discovery_provenance": {"provider": "fixture", "discovered_geography": "india"},
            "pipeline_predictions": {"categories": ["science"], "geography": "india"},
            "sources": [{
                "discovery_source": "google_news_rss", "source_url": "https://feed.invalid/item",
                "resolved_url": "https://publisher.invalid/story", "publisher": {
                    "name": "Fixture Publisher", "identity": "publisher.invalid", "original_publisher": None,
                }, "retrieved_at": "2026-01-02T03:05:00+05:30", "http_outcome": "200",
                "extraction_outcome": "metadata_only", "evidence_reference": None,
            }],
            "outcomes": {"retrieval": "completed", "extraction": "metadata_only"},
            "annotations": {
                "annotator_id": "reviewer-a", "story_group_id": "story-1",
                "categories": {"science": "unknown"}, "geographic_relevance": "india",
                "primary_source": "unknown", "evidence_sufficiency": "insufficient",
                "corroboration": "unknown", "source_identity": "known",
                "syndication": "unknown", "assessability": "partially_assessable",
                "uncertainty": ["No article text retained"],
            },
        }],
        "evaluation": {"provenance": {"kind": "synthetic structural fixture"}, "limitations": ["No real-world evidence"]},
    }


class EvaluationBundleTests(unittest.TestCase):
    def test_valid_bundle_and_round_trip_serialization(self):
        original = synthetic_bundle()
        self.assertEqual(validate_bundle(original), original)
        self.assertEqual(loads_bundle(dumps_bundle(original)), original)

    def test_empty_bundle_is_valid(self):
        bundle = synthetic_bundle(candidates=[])
        self.assertEqual(validate_bundle(bundle)["candidates"], [])

    def test_invalid_and_unsupported_schema_versions_rejected(self):
        for version in ("2.0.0", "1.1.0", "1.0.1", "not-a-version", None):
            bundle = synthetic_bundle()
            bundle["schema_version"] = version
            with self.subTest(version=version), self.assertRaises(BundleValidationError):
                validate_bundle(bundle)

    def test_missing_required_fields_rejected(self):
        bundle = synthetic_bundle()
        del bundle["capture"]
        with self.assertRaisesRegex(BundleValidationError, "capture"):
            validate_bundle(bundle)

    def test_unknown_and_negative_annotation_states_are_distinct(self):
        bundle = synthetic_bundle()
        annotations = bundle["candidates"][0]["annotations"]
        annotations["categories"]["science"] = "not_relevant"
        validate_bundle(bundle)
        annotations["categories"]["science"] = "unknown"
        validate_bundle(bundle)
        annotations["categories"]["science"] = "no"
        with self.assertRaises(BundleValidationError):
            validate_bundle(bundle)

    def test_predictions_and_ground_truth_are_separate(self):
        bundle = synthetic_bundle()
        candidate = bundle["candidates"][0]
        self.assertIn("categories", candidate["pipeline_predictions"])
        self.assertIn("categories", candidate["annotations"])
        candidate["annotations"].pop("categories")
        with self.assertRaises(BundleValidationError):
            validate_bundle(bundle)

    def test_missing_source_metadata_is_explicit_and_permitted(self):
        bundle = synthetic_bundle()
        source = bundle["candidates"][0]["sources"][0]
        for key in ("source_url", "resolved_url", "publisher", "retrieved_at", "http_outcome", "extraction_outcome", "evidence_reference"):
            source[key] = None
        validate_bundle(bundle)
        del source["publisher"]
        with self.assertRaises(BundleValidationError):
            validate_bundle(bundle)

    def test_duplicate_and_syndicated_source_metadata(self):
        bundle = synthetic_bundle()
        candidate = bundle["candidates"][0]
        candidate["sources"].append({
            **copy.deepcopy(candidate["sources"][0]),
            "source_url": "https://reprint.invalid/copy",
            "resolved_url": "https://publisher.invalid/story",
            "publisher": {"name": "Reprint Outlet", "identity": "reprint.invalid", "original_publisher": "Fixture Publisher"},
        })
        candidate["annotations"]["syndication"] = "syndicated"
        validate_bundle(bundle)

    def test_google_rss_remains_discovery_provenance(self):
        bundle = synthetic_bundle()
        bundle["candidates"][0]["sources"][0]["evidence_reference"] = {"kind": "local_note", "location": "evidence/note.txt"}
        validate_bundle(bundle)
        source = bundle["candidates"][0]["sources"][0]
        self.assertEqual(source["discovery_source"], "google_news_rss")
        self.assertEqual(source["publisher"]["identity"], "publisher.invalid")

    def test_duplicate_candidate_ids_rejected(self):
        bundle = synthetic_bundle()
        bundle["candidates"].append(copy.deepcopy(bundle["candidates"][0]))
        with self.assertRaises(BundleValidationError):
            validate_bundle(bundle)

    def test_invalid_urls_and_google_rss_publisher_evidence_rejected(self):
        for url in ("http://", "https:///missing-host", "https://bad host/a", "https://example.com:99999/a", "ftp://example.com/a", "mailto:a@example.com"):
            for key in ("source_url", "resolved_url"):
                bundle = synthetic_bundle()
                bundle["candidates"][0]["sources"][0][key] = url
                with self.subTest(key=key, url=url), self.assertRaises(BundleValidationError):
                    validate_bundle(bundle)
        bundle = synthetic_bundle()
        bundle["candidates"][0]["sources"][0]["resolved_url"] = "https://news.google.com/rss/articles/123"
        with self.assertRaises(BundleValidationError):
            validate_bundle(bundle)

    def test_google_news_trailing_dot_rejected_as_url_and_publisher(self):
        bundle = synthetic_bundle()
        source = bundle["candidates"][0]["sources"][0]
        source["resolved_url"] = "https://news.google.com./rss/articles/123"
        with self.assertRaises(BundleValidationError):
            validate_bundle(bundle)
        source["resolved_url"] = "https://publisher.invalid/story"
        source["publisher"]["identity"] = "news.google.com."
        with self.assertRaises(BundleValidationError):
            validate_bundle(bundle)

    def test_single_trailing_dot_normalized_for_publisher_url_and_identity(self):
        bundle = synthetic_bundle()
        source = bundle["candidates"][0]["sources"][0]
        source["resolved_url"] = "https://publisher.invalid./story"
        source["publisher"]["identity"] = "publisher.invalid."

        self.assertEqual(validate_bundle(bundle), bundle)
        self.assertEqual(source["resolved_url"].split("/")[2].rstrip("."), source["publisher"]["identity"].rstrip("."))

    def test_malformed_publisher_hostnames_rejected(self):
        for hostname in ("publisher.invalid..", "publisher..invalid", "-bad.invalid", "bad_.invalid"):
            bundle = synthetic_bundle()
            bundle["candidates"][0]["sources"][0]["publisher"]["identity"] = hostname
            with self.subTest(hostname=hostname), self.assertRaises(BundleValidationError):
                validate_bundle(bundle)
        bundle = synthetic_bundle()
        bundle["candidates"][0]["sources"][0]["source_url"] = "https://publisher..invalid/story"
        with self.assertRaises(BundleValidationError):
            validate_bundle(bundle)

    def test_publisher_identity_types_and_states(self):
        for identity in (None, [], {}, "", "unknown"):
            bundle = synthetic_bundle()
            bundle["candidates"][0]["sources"][0]["publisher"]["identity"] = identity
            with self.subTest(identity=identity):
                if identity == "unknown":
                    validate_bundle(bundle)
                else:
                    with self.assertRaises(BundleValidationError):
                        validate_bundle(bundle)
        for member, value in (("name", []), ("original_publisher", {})):
            bundle = synthetic_bundle()
            bundle["candidates"][0]["sources"][0]["publisher"][member] = value
            with self.assertRaises(BundleValidationError):
                validate_bundle(bundle)

    def test_timestamp_policy_and_round_trip_preserve_text(self):
        for timestamp in ("2026-01-02T03:04:05", "not a timestamp", 123, []):
            bundle = synthetic_bundle()
            bundle["candidates"][0]["sources"][0]["retrieved_at"] = timestamp
            with self.subTest(timestamp=timestamp), self.assertRaises(BundleValidationError):
                validate_bundle(bundle)
        for timestamp in ("2026-99-90T99:99:99+00:00", "2026-01-02T03:04:05"):
            bundle = synthetic_bundle()
            bundle["capture"]["timestamp"] = timestamp
            with self.subTest(capture_timestamp=timestamp), self.assertRaises(BundleValidationError):
                validate_bundle(bundle)
        bundle = synthetic_bundle()
        timestamp = bundle["candidates"][0]["sources"][0]["retrieved_at"]
        self.assertEqual(loads_bundle(dumps_bundle(bundle))["candidates"][0]["sources"][0]["retrieved_at"], timestamp)

    def test_evidence_reference_paths_are_safe_and_never_opened(self):
        for location in ("/tmp/evidence.txt", "../secret", "evidence/../secret", "", "a//b", "C:/secret", "https://example.com/a", "mailto:notes", "file:notes", "https:example.com", "data:text/plain,hello", "a\\b"):
            bundle = synthetic_bundle()
            bundle["candidates"][0]["sources"][0]["evidence_reference"] = {"kind": "local_note", "location": location}
            with self.subTest(location=location), self.assertRaises(BundleValidationError):
                validate_bundle(bundle)
        bundle = synthetic_bundle()
        bundle["candidates"][0]["sources"][0]["evidence_reference"] = {"kind": "local_note", "location": "evidence/note.txt"}
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "note.txt").write_text("test fixture", encoding="utf-8")
            bundle["candidates"][0]["sources"][0]["evidence_reference"]["location"] = "note.txt"
            with patch.object(Path, "open", side_effect=AssertionError("validation opened a file")), patch.object(Path, "read_text", side_effect=AssertionError("validation read a file")):
                validate_bundle(bundle)

    def test_safe_write_refuses_existing_by_default_and_allows_explicit_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bundle.json"
            write_bundle(path, synthetic_bundle())
            original = path.read_text(encoding="utf-8")
            with self.assertRaises(BundleExistsError):
                write_bundle(path, synthetic_bundle())
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            replacement = synthetic_bundle()
            replacement["capture"]["timezone"] = "UTC"
            write_bundle(path, replacement, overwrite=True)
            self.assertEqual(loads_bundle(path.read_text(encoding="utf-8")), replacement)

    def test_write_destination_directory_and_invalid_destination_raise_validation_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(BundleValidationError):
                write_bundle(Path(directory), synthetic_bundle())
            with self.assertRaises(BundleValidationError):
                write_bundle(Path(directory), synthetic_bundle(), overwrite=True)
            with self.assertRaises(BundleValidationError):
                write_bundle(Path(directory) / "missing" / "bundle.json", synthetic_bundle())
            with self.assertRaises(BundleValidationError):
                write_bundle(None, synthetic_bundle())

    def test_unknown_and_not_assessable_annotations_round_trip(self):
        bundle = synthetic_bundle()
        annotations = bundle["candidates"][0]["annotations"]
        annotations["categories"]["science"] = "not_assessable"
        annotations["geographic_relevance"] = "unknown"
        annotations["primary_source"] = "not_assessable"
        annotations["evidence_sufficiency"] = "unknown"
        annotations["corroboration"] = "not_assessable"
        annotations["source_identity"] = "unknown"
        annotations["syndication"] = "not_assessable"
        annotations["assessability"] = "not_assessable"
        self.assertEqual(loads_bundle(dumps_bundle(bundle)), bundle)

    def test_annotation_prediction_separation_and_unknown_fields(self):
        bundle = synthetic_bundle()
        bundle["candidates"][0]["annotations"]["predicted_score"] = 0.8
        with self.assertRaises(BundleValidationError):
            validate_bundle(bundle)

    def test_unhashable_or_wrong_typed_values_at_nested_levels_are_bundle_errors(self):
        mutations = [
            lambda b: b.update(schema_version=[]),
            lambda b: b["capture"].update(timestamp=[]),
            lambda b: b["scope"].update(selected_categories={}),
            lambda b: b["scope"].update(coverage_mode=[]),
            lambda b: b["discovery"].update(queries=[[]]),
            lambda b: b["discovery"].update(metadata=[]),
            lambda b: b["candidates"][0].update(candidate_id=[]),
            lambda b: b["candidates"][0].update(discovery_provenance=[]),
            lambda b: b["candidates"][0].update(pipeline_predictions=[]),
            lambda b: b["candidates"][0].update(sources=[[]]),
            lambda b: b["candidates"][0]["sources"][0].update(discovery_source=[]),
            lambda b: b["candidates"][0]["sources"][0].update(source_url=[]),
            lambda b: b["candidates"][0]["sources"][0].update(publisher=[]),
            lambda b: b["candidates"][0]["sources"][0]["publisher"].update(name=[]),
            lambda b: b["candidates"][0]["sources"][0]["publisher"].update(identity=[]),
            lambda b: b["candidates"][0]["sources"][0].update(evidence_reference=[]),
            lambda b: b["candidates"][0].update(outcomes=[]),
            lambda b: b["candidates"][0].update(annotations=[]),
            lambda b: b["candidates"][0]["annotations"].update(categories=[]),
            lambda b: b["candidates"][0]["annotations"].update(geographic_relevance=[]),
            lambda b: b["candidates"][0]["annotations"].update(uncertainty=[{}]),
            lambda b: b.update(evaluation=[]),
            lambda b: b["evaluation"].update(provenance=[]),
            lambda b: b["evaluation"].update(limitations=[{}]),
        ]
        for mutate in mutations:
            bundle = synthetic_bundle()
            mutate(bundle)
            with self.subTest(mutate=mutate), self.assertRaises(BundleValidationError):
                validate_bundle(bundle)


if __name__ == "__main__":
    unittest.main()
