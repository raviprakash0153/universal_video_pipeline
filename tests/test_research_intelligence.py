import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pipeline_store
import research_intelligence as ri
import telegram_text_dry_run as telegram
from news_research import Article, NewsItem


class IntelligenceTests(unittest.TestCase):
    def test_shortlist_decisions_capture_caps_balance_and_final_order(self):
        def topic(name, relevance="global", publisher=None, score=10):
            articles = ([{"publisher": publisher}] if publisher else [])
            return ri.Topic(name, name, "summary", "science", relevance, articles=articles,
                            score=score, relevant_categories=["science"])

        topics = [topic("g1", "global", "shared", 20), topic("g2", "global", "shared", 19),
                  topic("i1", "india", "india outlet", 18), topic("g3", "global", "shared", 17)]
        topics.extend(topic(f"other{i}", score=16-i) for i in range(8))
        events = []
        selected, _ = ri._build_shortlist(topics, "india_and_global", events)
        self.assertEqual(len(selected), 10)
        self.assertEqual(sorted(e["selection_order"] for e in events if e["selected"] == "yes"),
                         list(range(1, len(selected) + 1)))
        self.assertEqual([e["topic_id"] for e in sorted(
            (e for e in events if e["selected"] == "yes"), key=lambda e: e["selection_order"])],
            [t.topic_id for t in selected])
        g3 = next(e for e in events if e["topic_id"] == "g3")
        self.assertEqual(g3["publisher_cap_affected_eligibility"], "yes")
        self.assertEqual(g3["selected"], "no")
        self.assertEqual(g3["decision_reason"], "excluded_by_publisher_cap")
        self.assertEqual(next(e for e in events if e["topic_id"] == "i1")[
            "geography_balance_affected_selection"], "yes")

    def test_shortlist_missing_identity_and_geography_are_not_fabricated(self):
        topic = ri.Topic("unknown", "headline", "summary", "science", None,
                         articles=[{}], score=1, relevant_categories=["science"])
        events = []
        ri._build_shortlist([topic], "global", events)
        row = events[0]
        self.assertEqual(row["publisher_identities"], "unavailable")
        self.assertEqual(row["geography_classification"], "unavailable")
        self.assertEqual(row["publisher_cap_affected_eligibility"], "no")

    def test_unavailable_publishers_use_explicit_shared_fallback_bucket(self):
        topics = [ri.Topic(f"unknown{i}", f"story {i}", "s", "science", "global",
                           articles=[{}], score=20-i) for i in range(3)]
        known = ri.Topic("known", "known story", "s", "science", "global",
                         articles=[{"publisher": "Actual Outlet"}], score=10)
        events = []
        ri._build_shortlist(topics + [known], "global", events)
        unknown_rows = [row for row in events if row["topic_id"].startswith("unknown")]
        self.assertTrue(all(row["publisher_identity_status"] == "unavailable" for row in unknown_rows))
        self.assertTrue(all(row["publisher_cap_bucket_status"] == "shared_unknown_fallback"
                            for row in unknown_rows))
        self.assertTrue(all(row["effective_publisher_cap_buckets"] == ["unknown"]
                            for row in unknown_rows))
        known_row = next(row for row in events if row["topic_id"] == "known")
        self.assertEqual(known_row["publisher_identity_status"], "available")
        self.assertEqual(known_row["effective_publisher_cap_buckets"], ["actual outlet"])

    def test_cap_temporarily_blocked_but_geography_is_final_reason(self):
        topics = [ri.Topic("g1", "g1", "s", "science", "global", articles=[{"publisher": "Same"}], score=30),
                  ri.Topic("g2", "g2", "s", "science", "global", articles=[{"publisher": "Same"}], score=29),
                  ri.Topic("blocked", "blocked", "s", "science", "global", articles=[{"publisher": "Same"}], score=28)]
        topics.extend(ri.Topic(f"i{i}", f"i{i}", "s", "science", "india",
                               articles=[{"publisher": f"India {i}"}], score=20-i) for i in range(10))
        topics.extend(ri.Topic(f"g{i}", f"g{i}", "s", "science", "global",
                               articles=[{"publisher": f"Other {i}"}], score=10-i) for i in range(4))
        events = []
        ri._build_shortlist(topics, "india_and_global", events)
        row = next(row for row in events if row["topic_id"] == "blocked")
        self.assertEqual(row["publisher_cap_affected_eligibility"], "yes")
        self.assertEqual(row["publisher_cap_final_exclusion"], "no")
        self.assertEqual(row["decision_reason"], "geography_balance_exclusion")

    def test_cap_exclusion_is_final_and_identifies_known_or_unknown_bucket(self):
        topics = [ri.Topic(f"same{i}", f"same{i}", "s", "science", "global",
                           articles=[{"publisher": "Known"}], score=20-i) for i in range(3)]
        topics.extend(ri.Topic(f"alt{i}", f"alt{i}", "s", "science", "global",
                               articles=[{"publisher": f"Alt {i}"}], score=10-i) for i in range(9))
        events = []
        selected, _ = ri._build_shortlist(topics, "global", events)
        rows = {row["topic_id"]: row for row in events}
        self.assertEqual(rows["same2"]["publisher_cap_affected_eligibility"], "yes")
        self.assertEqual(rows["same2"]["publisher_cap_final_exclusion"], "yes")
        self.assertEqual(rows["same2"]["decision_reason"], "excluded_by_publisher_cap")
        self.assertEqual(rows["same2"]["publisher_cap_exclusion_type"], "known_shared_publisher")
        self.assertEqual({row["topic_id"] for row in events if row["selected"] == "yes"},
                         {topic.topic_id for topic in selected})

    def test_cap_relaxation_reconsiders_temporarily_blocked_topic(self):
        topics = [ri.Topic(f"same{i}", f"same{i}", "s", "science", "global",
                           articles=[{}], score=10-i) for i in range(4)]
        events = []
        selected, reasons = ri._build_shortlist(topics, "global", events)
        row = next(row for row in events if row["topic_id"] == "same2")
        self.assertIn("same2", {topic.topic_id for topic in selected})
        self.assertEqual(row["publisher_cap_affected_eligibility"], "yes")
        self.assertEqual(row["publisher_cap_final_exclusion"], "no")
        self.assertEqual(row["decision_reason"], "selected_by_rank_and_constraints")
        self.assertEqual(row["publisher_cap_exclusion_type"], "not_final_excluded_by_cap")
        self.assertIn("publisher limit relaxed: no alternative eligible story", reasons)

    def test_shortlist_limit_exclusion_has_explicit_reason(self):
        topics = [ri.Topic(str(i), f"story {i}", "summary", "science", "global",
                           score=20-i, relevant_categories=["science"]) for i in range(11)]
        events = []
        selected, _ = ri._build_shortlist(topics, "global", events)
        excluded = next(row for row in events if row["topic_id"] == "10")
        self.assertEqual(len(selected), 10)
        self.assertEqual(excluded["decision_reason"], "shortlist_limit_reached")
        self.assertEqual(excluded["selected"], "no")

    def test_tied_cap_exclusion_uses_decision_flow_and_diagnostics_are_observational(self):
        topics = [
            ri.Topic("shared1", "shared1", "s", "science", "global",
                     articles=[{"publisher": "Shared"}], score=10),
            ri.Topic("shared2", "shared2", "s", "science", "global",
                     articles=[{"publisher": "Shared"}], score=9),
            ri.Topic("cap_tie", "cap tie", "s", "science", "global",
                     articles=[{"publisher": "Shared"}], score=2),
        ] + [ri.Topic(f"other{i}", f"other{i}", "s", "science", "global",
                      articles=[{"publisher": f"Other{i}"}], score=9-i)
             for i in range(8)]

        plain, _ = ri._build_shortlist(topics, "global")
        events = []
        diagnosed, _ = ri._build_shortlist(topics, "global", events)
        self.assertEqual([(t.topic_id, t.score) for t in diagnosed],
                         [(t.topic_id, t.score) for t in plain])
        rows = {row["topic_id"]: row for row in events}
        self.assertEqual(min(t.score for t in diagnosed), rows["cap_tie"]["ranking_score"])
        self.assertEqual(rows["cap_tie"]["selected"], "no")
        self.assertEqual(rows["cap_tie"]["publisher_cap_final_exclusion"], "yes")
        self.assertEqual(rows["cap_tie"]["decision_reason"], "excluded_by_publisher_cap")
        self.assertEqual({row["topic_id"] for row in events if row["selected"] == "yes"},
                         {t.topic_id for t in diagnosed})

    def test_mixed_publisher_metadata_reports_known_and_unknown_bucket_with_parity(self):
        mixed = ri.Topic("mixed", "mixed", "s", "science", "global",
                         articles=[{"publisher": "Known Outlet"}, {}], score=20)
        topics = [mixed] + [ri.Topic(f"alt{i}", f"alt{i}", "s", "science", "global",
                                     articles=[{"publisher": f"Alt{i}"}], score=19-i)
                            for i in range(10)]
        plain, _ = ri._build_shortlist(topics, "global")
        events = []
        diagnosed, _ = ri._build_shortlist(topics, "global", events)
        self.assertEqual([(t.topic_id, t.score) for t in diagnosed],
                         [(t.topic_id, t.score) for t in plain])
        rows = {row["topic_id"]: row for row in events}
        row = rows["mixed"]
        self.assertEqual(row["publisher_identities"], ["known outlet"])
        self.assertEqual(row["publisher_identity_status"], "available")
        self.assertEqual(row["effective_publisher_cap_buckets"], ["known outlet", "unknown"])
        self.assertEqual(row["publisher_cap_bucket_status"],
                         "mixed_known_and_shared_unknown_fallback")
        self.assertEqual(row["publisher_article_identities"], [
            {"article_index": 0, "publisher_identity": "known outlet",
             "identity_status": "known_identity", "effective_publisher_cap_bucket": "known outlet",
             "bucket_status": "known_identity"},
            {"article_index": 1, "publisher_identity": "unavailable",
             "identity_status": "unavailable", "effective_publisher_cap_bucket": "unknown",
             "bucket_status": "shared_unknown_fallback"},
        ])
        self.assertEqual({r["topic_id"] for r in events if r["selected"] == "yes"},
                         {t.topic_id for t in diagnosed})

    def test_mixed_topic_excluded_by_unknown_fallback_bucket_with_parity(self):
        topics = [ri.Topic(f"unknown{i}", f"unknown{i}", "s", "science", "global",
                           articles=[{}], score=30-i) for i in range(2)]
        mixed = ri.Topic("mixed_blocked", "mixed", "s", "science", "global",
                         articles=[{"publisher": "Known Outlet"}, {}], score=27)
        topics.append(mixed)
        topics.extend(ri.Topic(f"alternative{i}", f"alternative{i}", "s", "science", "global",
                               articles=[{"publisher": f"Alternative{i}"}], score=20-i)
                      for i in range(9))

        plain, _ = ri._build_shortlist(topics, "global")
        events = []
        diagnosed, _ = ri._build_shortlist(topics, "global", events)
        self.assertEqual([(t.topic_id, t.score) for t in diagnosed],
                         [(t.topic_id, t.score) for t in plain])
        rows = {row["topic_id"]: row for row in events}
        row = rows["mixed_blocked"]
        self.assertEqual(row["selected"], "no")
        self.assertEqual(row["publisher_cap_final_exclusion"], "yes")
        self.assertEqual(row["decision_reason"], "excluded_by_publisher_cap")
        self.assertEqual(row["publisher_cap_exclusion_type"],
                         "unavailable_identity_shared_fallback")
        self.assertEqual(row["publisher_identities"], ["known outlet"])
        self.assertEqual(row["publisher_identity_status"], "available")
        self.assertEqual(row["effective_publisher_cap_buckets"], ["known outlet", "unknown"])
        self.assertEqual(row["publisher_article_identities"], [
            {"article_index": 0, "publisher_identity": "known outlet",
             "identity_status": "known_identity", "effective_publisher_cap_bucket": "known outlet",
             "bucket_status": "known_identity"},
            {"article_index": 1, "publisher_identity": "unavailable",
             "identity_status": "unavailable", "effective_publisher_cap_bucket": "unknown",
             "bucket_status": "shared_unknown_fallback"},
        ])

    def test_topic_blocked_by_multiple_known_publisher_caps_with_parity(self):
        topics = [
            ri.Topic("a1", "a1", "s", "science", "global",
                     articles=[{"publisher": "Publisher A"}], score=30),
            ri.Topic("a2", "a2", "s", "science", "global",
                     articles=[{"publisher": "Publisher A"}], score=29),
            ri.Topic("b1", "b1", "s", "science", "global",
                     articles=[{"publisher": "Publisher B"}], score=28),
            ri.Topic("b2", "b2", "s", "science", "global",
                     articles=[{"publisher": "Publisher B"}], score=27),
            ri.Topic("blocked_multi", "blocked", "s", "science", "global",
                     articles=[{"publisher": "Publisher A"}, {"publisher": "Publisher B"}], score=26),
        ] + [ri.Topic(f"alternative{i}", f"alternative{i}", "s", "science", "global",
                      articles=[{"publisher": f"Alternative {i}"}], score=20-i)
             for i in range(8)]

        plain, _ = ri._build_shortlist(topics, "global")
        events = []
        diagnosed, _ = ri._build_shortlist(topics, "global", events)
        expected = [(t.topic_id, t.score) for t in plain]
        self.assertEqual([(t.topic_id, t.score) for t in diagnosed], expected)
        self.assertTrue({"a1", "a2", "b1", "b2"}.issubset({t.topic_id for t in diagnosed}))
        rows = {row["topic_id"]: row for row in events}
        row = rows["blocked_multi"]
        self.assertEqual(row["selected"], "no")
        self.assertEqual(row["publisher_cap_final_exclusion"], "yes")
        self.assertEqual(row["decision_reason"], "excluded_by_publisher_cap")
        self.assertEqual(row["publisher_cap_exclusion_type"], "multiple_blocking_buckets")
        self.assertEqual(row["publisher_identities"], ["publisher a", "publisher b"])
        self.assertEqual(row["effective_publisher_cap_buckets"], ["publisher a", "publisher b"])
        self.assertEqual(row["publisher_article_identities"], [
            {"article_index": 0, "publisher_identity": "publisher a",
             "identity_status": "known_identity", "effective_publisher_cap_bucket": "publisher a",
             "bucket_status": "known_identity"},
            {"article_index": 1, "publisher_identity": "publisher b",
             "identity_status": "known_identity", "effective_publisher_cap_bucket": "publisher b",
             "bucket_status": "known_identity"},
        ])
        self.assertEqual([(t.topic_id, t.score) for t in diagnosed],
                         [(t.topic_id, t.score) for t in plain])
        self.assertEqual([row["topic_id"] for row in sorted(
            (r for r in events if r["selected"] == "yes"), key=lambda r: r["selection_order"])],
            [t.topic_id for t in diagnosed])

    def test_discover_shortlist_diagnostics_match_results_and_category_exclusions(self):
        from types import SimpleNamespace
        topics = [ri.Topic("chosen", "Science discovery today", "s", "science", "global",
                           articles=[{"publisher": "Outlet"}], score=7, relevant_categories=["science"]),
                  ri.Topic("category-out", "Other", "s", "science", "global", score=4,
                           relevant_categories=["business_economy"])]
        def run(enabled):
            ledger = {} if enabled else None
            with patch.object(ri, "research_news", return_value=SimpleNamespace(items=[], metrics={"results_received": 0})), \
                 patch.object(ri, "cluster_report_items", return_value=[ri.Topic(**vars(t)) for t in topics]):
                result = ri.discover_topics(categories={"science": ("science",)}, coverage="global",
                                            diagnostics=ledger)
            return result[1:], ledger
        (diagnostic_topics, _), ledger = run(True)
        (plain_topics, _), _ = run(False)
        self.assertEqual([(t.topic_id, t.score) for t in diagnostic_topics],
                         [(t.topic_id, t.score) for t in plain_topics])
        chosen = next(row for row in ledger["shortlist_decisions"] if row["topic_id"] == "chosen")
        self.assertEqual(chosen["selected"], "yes")
        self.assertEqual(chosen["selection_order"], 1)
        excluded = next(row for row in ledger["shortlist_decisions"] if row["topic_id"] == "category-out")
        self.assertEqual(excluded["decision_reason"], "no_relevant_category_selected")

    def test_association_reference_rejects_internal_whitespace(self):
        for url in ("https://valid.test/a b", "https://valid.test/a?b c",
                    "https://valid.test/a\tb", "https://valid.test/a\nb",
                    "https://valid.test/a\u00a0b"):
            with self.subTest(url=url):
                self.assertIsNone(ri._association_reference(url))

    def test_association_reference_accepts_percent_encoded_and_surrounding_whitespace(self):
        self.assertEqual(ri._association_reference("https://valid.test/a%20b"),
                         "https://valid.test/a%20b")
        self.assertEqual(ri._association_reference(" \t https://valid.test/story\n "),
                         "https://valid.test/story")

    def test_diagnostic_processing_failure_does_not_change_research_results(self):
        from types import SimpleNamespace

        topic = ri.Topic("stable", "Scientists announce climate research discovery", "summary",
                         "science", "global", score=11, relevant_categories=["science"])
        class BrokenMetrics(dict):
            def get(self, key, default=None):
                if key == "article_attempts":
                    raise RuntimeError("diagnostic-only failure")
                return super().get(key, default)
        report = SimpleNamespace(items=[], metrics=BrokenMetrics(results_received=0))
        def run(enabled):
            with patch.object(ri, "research_news", return_value=report), \
                 patch.object(ri, "cluster_report_items", return_value=[ri.Topic(**vars(topic))]):
                ledger = {} if enabled else None
                result = ri.discover_topics(categories={"science": ("climate",)}, coverage="global",
                                            diagnostics=ledger)
            return result[1:], ledger
        enabled, ledger = run(True)
        disabled, _ = run(False)
        signature = lambda result: ([(t.topic_id, t.score, t.category, t.relevant_categories,
                                      t.verification_status) for t in result[0]], result[1])
        self.assertEqual(signature(enabled), signature(disabled))
        self.assertEqual(ledger["diagnostics_status"], "incomplete")
        self.assertEqual(ledger["diagnostic_failure"], "RuntimeError")

    def test_final_topic_ledger_is_unique_and_candidate_verification_is_reconciled(self):
        from types import SimpleNamespace

        first = ri.Topic("same", "Scientists announce climate research discovery", "s", "science", "global",
                         claims=[{"status": "CONFIRMED"}], verification_status="CONFIRMED",
                         relevant_categories=["science"], articles=[{"url": "https://x.test/a"}])
        second = ri.Topic("same", first.headline, "s", "science", "global",
                          claims=[{"status": "DISPUTED"}], verification_status="DISPUTED",
                          relevant_categories=["science"], articles=[{"url": "https://x.test/b"}])
        attempt = lambda url: {"rss_url": url, "requested_url": url, "candidate_verified": True}
        reports = [SimpleNamespace(items=[], metrics={"results_received": 1, "article_attempts": [attempt("https://x.test/a")]}),
                   SimpleNamespace(items=[], metrics={"results_received": 1, "article_attempts": [attempt("https://x.test/b")]})]
        with patch.object(ri, "research_news", side_effect=reports), \
             patch.object(ri, "cluster_report_items", side_effect=[[first], [second]]):
            ledger = {}
            _, topics, _ = ri.discover_topics(categories={"science": ("one", "two")}, queries_per_category=2,
                                               coverage="global", diagnostics=ledger)
        self.assertEqual(len(ledger["topics"]), len({row["topic_id"] for row in ledger["topics"]}))
        final = topics[0]
        row = next(row for row in ledger["topics"] if row["topic_id"] == final.topic_id)
        self.assertEqual(len(row["query_ids"]), 2)
        self.assertEqual(row["verification_status"], final.verification_status)
        for candidate in ledger["candidates"]:
            self.assertEqual(candidate["topic_verification"], [{"topic_id": final.topic_id,
                             "verification_status": final.verification_status}])
            self.assertEqual(candidate["claim_verification"], final.verification_status)

    def test_candidates_distinguish_unmatched_and_ambiguous_evidence(self):
        from types import SimpleNamespace

        url = "https://shared.test/story"
        attempts = [{"rss_url": url, "requested_url": url, "extraction_method": "readability"},
                    {"rss_url": url, "requested_url": url, "extraction_method": "readability"},
                    {"rss_url": "https://other.test/story", "extraction_method": "readability"}]
        article = Article(url, "Outlet", "Scientists announce climate research discovery", evidence="evidence",
                          rss_url=url)
        report = SimpleNamespace(items=[NewsItem(article.headline, "DEVELOPING", [article])], metrics={
            "results_received": 3, "article_attempts": attempts})
        topic = ri.Topic("t", article.headline, "s", "science", "global", articles=[{"url": url}],
                         relevant_categories=["science"])
        with patch.object(ri, "research_news", return_value=report), \
             patch.object(ri, "cluster_report_items", return_value=[topic]):
            ledger = {}
            ri.discover_topics(provider=object(), categories={"science": ("science",)}, coverage="global",
                               diagnostics=ledger)
        self.assertEqual([c["evidence"] for c in ledger["candidates"]], ["ambiguous", "ambiguous", "not_accepted"])
        self.assertEqual([c["association_state"] for c in ledger["candidates"]],
                         ["ambiguous", "ambiguous", "unmatched"])
        self.assertTrue(any(r["type"] == "ambiguous_evidence_attribution" for r in ledger["relationships"]))

    def test_ledger_evidence_uses_accepted_evidence_not_extracted_length(self):
        from types import SimpleNamespace

        attempts = [
            {"rss_url": "https://x.test/length", "extracted_text_length": 80, "extraction_method": "readability"},
            {"rss_url": "https://x.test/fail", "page_issue": "timeout"},
            {"rss_url": "https://x.test/accepted", "extracted_text_length": 20, "extraction_method": "readability"},
            {"rss_url": "https://x.test/unknown"},
        ]
        items = [NewsItem("Accepted headline", "DEVELOPING", [Article("https://x.test/accepted", "Outlet", "Accepted headline",
            evidence="accepted article evidence", rss_url="https://x.test/accepted")])]
        report = SimpleNamespace(items=items, metrics={"results_received": 4, "article_attempts": attempts})
        with patch.object(ri, "research_news", return_value=report), \
             patch.object(ri, "cluster_report_items", return_value=[]):
            ledger = {}
            ri.discover_topics(provider=object(), categories={"science": ("science",)}, coverage="global", diagnostics=ledger)
        self.assertEqual([c["evidence"] for c in ledger["candidates"]],
                         ["not_accepted", "unavailable", "accepted", "unavailable"])
        self.assertEqual([c["publisher_extraction"] for c in ledger["candidates"]],
                         ["success", "failed", "success", "unavailable"])

    def test_topic_ledger_is_refreshed_after_cross_query_merge_and_rescore(self):
        from types import SimpleNamespace

        first = ri.Topic("same", "Scientists announce climate research discovery", "s", "science", "global",
                         claims=[{"status": "CONFIRMED"}], verification_status="CONFIRMED", score=10,
                         relevant_categories=["science"])
        second = ri.Topic("same", "Scientists announce climate research discovery", "s", "science", "global",
                          claims=[{"status": "DISPUTED"}], verification_status="DISPUTED", score=90,
                          relevant_categories=["science"], articles=[{"url": "https://x.test/a", "evidence": "e"}])
        reports = [SimpleNamespace(items=[], metrics={"results_received": 1}),
                   SimpleNamespace(items=[], metrics={"results_received": 1})]
        with patch.object(ri, "research_news", side_effect=reports), \
             patch.object(ri, "cluster_report_items", side_effect=[[first], [second]]):
            ledger = {}
            _, topics, _ = ri.discover_topics(categories={"science": ("one", "two")}, queries_per_category=2,
                                               coverage="global", diagnostics=ledger)
        merged = topics[0]
        rows = [row for row in ledger["topics"] if row["topic_id"] == merged.topic_id]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["score"], merged.score)
            self.assertEqual(row["verification_status"], merged.verification_status)
            self.assertEqual(row["relevant_categories"], merged.relevant_categories)
        self.assertTrue(any(row["type"] == "cross_query_merge" for row in ledger["relationships"]))

    def test_cross_query_merge_diagnostic_failure_does_not_change_results(self):
        from types import SimpleNamespace

        original = ri.Topic("same", "Scientists announce climate research discovery", "summary",
                            "science", "global", score=10, relevant_categories=["science"])
        reports = [SimpleNamespace(items=[], metrics={"results_received": 1}),
                   SimpleNamespace(items=[], metrics={"results_received": 1})]

        def run(diagnostics_enabled):
            topics = [ri.Topic(**vars(original)), ri.Topic(**vars(original))]
            ledger = {} if diagnostics_enabled else None
            with patch.object(ri, "research_news", side_effect=reports.copy()), \
                 patch.object(ri, "cluster_report_items", side_effect=[[topics[0]], [topics[1]]]):
                if diagnostics_enabled:
                    record = ri._record_diagnostic_relationship
                    def fail_cross_query(ledger_arg, relationship):
                        if relationship["type"] == "cross_query_merge":
                            raise RuntimeError("diagnostic recording failed")
                        return record(ledger_arg, relationship)
                    with patch.object(ri, "_record_diagnostic_relationship", side_effect=fail_cross_query):
                        result = ri.discover_topics(categories={"science": ("one", "two")},
                            queries_per_category=2, coverage="global", diagnostics=ledger)
                else:
                    result = ri.discover_topics(categories={"science": ("one", "two")},
                        queries_per_category=2, coverage="global")
            return result, ledger

        observed, ledger = run(True)
        baseline, _ = run(False)
        self.assertIsInstance(observed, tuple)
        self.assertEqual(len(observed), 3)
        self.assertEqual(len(baseline), 3)
        signature = lambda result: (
            [(t.topic_id, t.score, t.category, t.relevant_categories, t.verification_status) for t in result[1]],
            {key: {name: value for name, value in status.items() if name != "metrics"}
             for key, status in result[2].items()})
        self.assertEqual(signature(observed), signature(baseline))
        self.assertTrue(all(status["status"] == "ok" for status in observed[2].values()))
        self.assertEqual(ledger["diagnostics_status"], "incomplete")
        self.assertEqual(ledger["diagnostic_failure"], "RuntimeError")

    def test_url_collision_keeps_all_candidate_references_as_ambiguous(self):
        from types import SimpleNamespace

        url = "https://shared.test/story"
        attempts = [{"rss_url": url, "requested_url": url, "extraction_method": "readability"},
                    {"rss_url": url, "requested_url": url, "extraction_method": "readability"}]
        article = Article(url, "Outlet", "Scientists announce climate research discovery", evidence="evidence",
                          rss_url=url)
        report = SimpleNamespace(items=[NewsItem(article.headline, "DEVELOPING", [article])], metrics={
            "results_received": 2, "article_attempts": attempts})
        topic = ri.Topic("t", article.headline, "s", "science", "global",
                         articles=[{"url": url}], relevant_categories=["science"])
        with patch.object(ri, "research_news", return_value=report), \
             patch.object(ri, "cluster_report_items", return_value=[topic]):
            ledger = {}
            ri.discover_topics(provider=object(), categories={"science": ("science",)}, coverage="global", diagnostics=ledger)
        association = next(r for r in ledger["relationships"] if r["type"] == "ambiguous_candidate_association")
        self.assertEqual(len(association["candidate_ids"]), 2)
        self.assertEqual({c["candidate_id"] for c in ledger["candidates"] if "t" in c["topic_ids"]},
                         set(association["candidate_ids"]))
        self.assertTrue(all(c["headline_match_verification"] == "unavailable" for c in ledger["candidates"]))

    def test_candidate_association_is_unique_when_supported_and_omitted_when_unknown(self):
        from types import SimpleNamespace

        candidate_url = "https://known.test/story"
        attempts = [{"rss_url": candidate_url, "requested_url": candidate_url}]
        article = Article(candidate_url, "Outlet", "Scientists announce climate research discovery", evidence="e")
        report = SimpleNamespace(items=[NewsItem(article.headline, "DEVELOPING", [article])], metrics={
            "results_received": 1, "article_attempts": attempts})

        def run(topic_url):
            topic = ri.Topic("t", article.headline, "s", "science", "global",
                             articles=[{"url": topic_url}], relevant_categories=["science"])
            with patch.object(ri, "research_news", return_value=report), \
                 patch.object(ri, "cluster_report_items", return_value=[topic]):
                ledger = {}
                ri.discover_topics(provider=object(), categories={"science": ("science",)},
                                   coverage="global", diagnostics=ledger)
            return ledger

        unique = run(candidate_url)
        self.assertEqual(unique["candidates"][0]["topic_ids"], ["t"])
        self.assertFalse(any(r["type"] == "ambiguous_candidate_association" for r in unique["relationships"]))
        unknown = run("https://unmapped.test/story")
        self.assertIsNotNone(unknown["candidates"][0]["candidate_ordinal"])
        self.assertTrue(unknown["candidates"][0]["association_attempted"])
        self.assertEqual(unknown["candidates"][0]["association_state"], "unmatched")
        self.assertEqual(unknown["candidates"][0]["topic_ids"], [])
        self.assertFalse(any(r["type"] == "ambiguous_candidate_association" for r in unknown["relationships"]))

    def test_candidate_association_attempted_requires_usable_references(self):
        from types import SimpleNamespace

        url = "https://association.test/story"

        def run(attempts, topic_urls=()):
            articles = [{"url": topic_url} for topic_url in topic_urls]
            topics = [ri.Topic(f"topic-{index}", "Scientists announce climate research discovery", "s",
                               "science", "global", articles=[article], relevant_categories=["science"])
                      for index, article in enumerate(articles)]
            report = SimpleNamespace(items=[], metrics={"results_received": len(attempts),
                                                        "article_attempts": attempts})
            with patch.object(ri, "research_news", return_value=report), \
                 patch.object(ri, "cluster_report_items", return_value=topics):
                ledger = {}
                ri.discover_topics(provider=object(), categories={"science": ("science",)},
                                   coverage="global", diagnostics=ledger)
            return ledger["candidates"]

        matched = run([{"rss_url": url, "requested_url": url}], [url])
        self.assertEqual((matched[0]["association_attempted"], matched[0]["association_state"],
                          matched[0]["topic_ids"]), (True, "matched", ["topic-0"]))

        ambiguous = run([{"rss_url": url}, {"requested_url": url}], [url])
        self.assertEqual([(candidate["association_attempted"], candidate["association_state"],
                           candidate["topic_ids"]) for candidate in ambiguous],
                         [(True, "ambiguous", ["topic-0"]), (True, "ambiguous", ["topic-0"])])

        unmatched = run([{"rss_url": url}], ["https://elsewhere.test/story"])
        self.assertEqual((unmatched[0]["association_attempted"], unmatched[0]["association_state"],
                          unmatched[0]["topic_ids"]), (True, "unmatched", []))

        no_usable_reference = run([{"candidate_verified": True}])
        self.assertEqual((no_usable_reference[0]["association_attempted"],
                          no_usable_reference[0]["association_state"], no_usable_reference[0]["topic_ids"]),
                         (False, "unavailable", []))

        invalid_values = ["", "   ", "not a URL", "ftp://association.test/story",
                          "https:///story", "https://bad host/story", "http://[invalid]/",
                          [], {}, 17]
        invalid_attempts = [{field: value} for field in
                            ("rss_url", "requested_url", "final_url", "original_publisher_url")
                            for value in invalid_values]
        invalid_candidates = run(invalid_attempts)
        self.assertTrue(all(not candidate["association_attempted"] and
                            candidate["association_state"] == "unavailable"
                            for candidate in invalid_candidates))

        normalized = run([{"rss_url": " HTTPS://ASSOCIATION.TEST/story "}],
                         ["https://association.test/story"])
        self.assertEqual((normalized[0]["association_attempted"], normalized[0]["association_state"],
                          normalized[0]["topic_ids"]), (True, "matched", ["topic-0"]))

        for topic_url in ("HTTPS://ASSOCIATION.TEST/story", "  https://association.test/story  "):
            with self.subTest(topic_url=topic_url):
                topic_normalized = run([{"rss_url": url, "requested_url": url}], [topic_url])
                self.assertEqual((topic_normalized[0]["association_attempted"],
                                  topic_normalized[0]["association_state"],
                                  topic_normalized[0]["topic_ids"]),
                                 (True, "matched", ["topic-0"]))

        association_info_absent = run([])
        self.assertEqual(association_info_absent, [])

    def test_topic_association_falls_back_to_usable_url_safely(self):
        from types import SimpleNamespace

        requested = "https://requested.test/story"
        fallback = "https://fallback.test/story"
        attempts = [{"rss_url": requested}, {"rss_url": fallback}]
        report = SimpleNamespace(items=[], metrics={"results_received": 2, "article_attempts": attempts})

        def run(requested_url, url, active_report=report):
            topic = ri.Topic("association-topic", "Scientists announce climate research discovery", "s",
                             "science", "global", articles=[{"requested_url": requested_url, "url": url}],
                             relevant_categories=["science"])
            with patch.object(ri, "research_news", return_value=active_report), \
                 patch.object(ri, "cluster_report_items", return_value=[topic]):
                ledger = {}
                ri.discover_topics(provider=object(), categories={"science": ("science",)},
                                   coverage="global", diagnostics=ledger)
            return ledger["candidates"]

        invalid_requested = run("not a URL", fallback)
        self.assertEqual([candidate["topic_ids"] for candidate in invalid_requested],
                         [[], ["association-topic"]])

        valid_requested = run(requested, fallback)
        self.assertEqual([candidate["topic_ids"] for candidate in valid_requested],
                         [["association-topic"], []])

        unavailable_report = SimpleNamespace(items=[], metrics={
            "results_received": 1, "article_attempts": [{"candidate_verified": True}]})
        for requested_url, url in (("ftp://requested.test/story", "https://bad host/story"), (None, None)):
            with self.subTest(requested_url=requested_url, url=url):
                no_usable_url = run(requested_url, url, unavailable_report)
                self.assertEqual([(candidate["association_state"], candidate["topic_ids"])
                                  for candidate in no_usable_url], [("unavailable", [])])

        malformed_requested = run({"unexpected": "value"}, fallback)
        self.assertEqual([candidate["topic_ids"] for candidate in malformed_requested],
                         [[], ["association-topic"]])

    def test_opt_in_candidate_ledger_tracks_query_provenance_without_changing_outputs(self):
        class Provider:
            def search(self, query):
                headline = "Scientists announce new climate research discovery"
                return [Article("https://research.test/story", "Research Desk", headline,
                                "2026-11-22T10:00:00+00:00")]
            def open(self, article):
                return Article(article.url, article.publisher, article.headline, article.publication_date,
                               evidence="Accessible research details.")
        options = dict(provider=Provider(), categories={"science": ("science",)}, coverage="india_and_global")
        _, baseline_topics, baseline_status = ri.discover_topics(**options)
        ledger = {}
        _, observed_topics, observed_status = ri.discover_topics(**options, diagnostics=ledger)
        signature = lambda topics, statuses: (
            [(t.topic_id, t.score, t.category, t.relevance, t.verification_status) for t in topics],
            {k: (v["status"], v.get("topics_before_clustering"), v.get("topics_after_clustering"))
             for k, v in statuses.items()})
        self.assertEqual(signature(baseline_topics, baseline_status), signature(observed_topics, observed_status))
        self.assertEqual([(q["source_category"], q["geography_intent"]) for q in ledger["queries"]],
                         [("science", "global"), ("science", "india")])
        self.assertEqual([q["generated_query"] for q in ledger["queries"]], ["science", "science India"])
        self.assertEqual([q["candidate_count_unit"] for q in ledger["queries"]], ["article_candidates"] * 2)
        self.assertTrue(all(c["query_id"] for c in ledger["candidates"]))

    def test_candidate_ledger_is_deterministic_and_marks_failed_and_empty_queries(self):
        from types import SimpleNamespace

        def run(ledger):
            reports = {
                "empty": SimpleNamespace(items=[], metrics={"results_received": 0, "article_attempts": []}),
                "broken": SimpleNamespace(items=[], metrics={"results_received": 0, "article_attempts": [],
                    "search_errors": [{"failure_stage": "search", "exception_type": "RuntimeError"}]}),
            }
            def research(query, **_kwargs):
                return reports[query]
            with patch.object(ri, "research_news", side_effect=research):
                ri.discover_topics(provider=object(), categories={"science": ("empty", "broken")},
                                   queries_per_category=2, coverage="global", diagnostics=ledger)

        first, second = {}, {}
        run(first)
        run(second)
        self.assertEqual(first, second)
        self.assertEqual([row["discovery_status"] for row in first["queries"]], ["success", "failed"])
        self.assertEqual(first["queries"][0]["candidate_count"], 0)
        self.assertEqual(first["queries"][1]["candidate_count"], 0)
        self.assertEqual(first["queries"][1]["candidate_count_unit"], "article_candidates")

    def test_technology_category_recall_and_precision(self):
        positive = {
            "Processor maker announces new server processor": "technology_ai",
            "Semiconductor company expands chip production": "technology_ai",
            "Data center operator adds robotics to network": "technology_ai",
            "Software firm reports cybersecurity breach": "technology_ai",
            "Artificial intelligence system improves cloud computing": "technology_ai",
        }
        for headline, expected in positive.items():
            with self.subTest(headline=headline):
                self.assertEqual(ri._category_for(headline), expected)
        for headline in ("Food processor recalled after safety complaints",
                         "Technology changes the way artists tell stories"):
            with self.subTest(headline=headline):
                self.assertNotEqual(ri._category_for(headline), "technology_ai")

    def test_selected_categories_keep_mixed_topics_but_exclude_unselected_only(self):
        class Provider:
            def search(self, query):
                headline = "Scientists use AI software to study climate patterns"
                return [Article("https://research.test/story", "Research Desk", headline,
                                "2026-11-22T10:00:00+00:00")]
            def open(self, article):
                return Article(article.url, article.publisher, article.headline, article.publication_date,
                               evidence="Accessible research details.")
        _, science_topics, _ = ri.discover_topics(provider=Provider(), categories={"science": ("science",)}, coverage="global")
        self.assertEqual(len(science_topics), 1)
        self.assertIn("technology_ai", science_topics[0].relevant_categories)
        _, gaming_topics, _ = ri.discover_topics(provider=Provider(), categories={"gaming": ("gaming",)}, coverage="global")
        self.assertEqual(gaming_topics, [])

    def test_india_global_shortlist_balances_available_geographies(self):
        topics = [ri.Topic(str(i), f"Story {i}", "s", "science", geo, score=100-i,
                           articles=[{"publisher": f"Outlet {i}"}])
                  for i, geo in enumerate(("global", "global", "global", "india", "india"))]
        selected, reasons = ri._build_shortlist(topics, "india_and_global")
        self.assertIn("india", {topic.relevance for topic in selected})
        self.assertIn("global", {topic.relevance for topic in selected})
        self.assertIn("geography balancing changed shortlist", reasons)

    def test_one_geography_only_is_not_fabricated_or_excluded(self):
        topics = [ri.Topic(str(i), f"Story {i}", "s", "science", "india", score=10-i) for i in range(3)]
        selected, reasons = ri._build_shortlist(topics, "india_and_global")
        self.assertEqual(selected, topics)
        self.assertEqual(reasons, [])

    def test_publisher_limit_prefers_alternatives_and_relaxes_when_needed(self):
        topics = [ri.Topic(str(i), f"Story {i}", "s", "science", "global", score=110-i,
                           articles=[{"publisher": "Same"}]) for i in range(11)]
        topics.extend(ri.Topic(f"alt{i}", f"Alternative {i}", "s", "science", "global", score=80-i,
                               articles=[{"publisher": f"Other {i}"}]) for i in range(10))
        selected, reasons = ri._build_shortlist(topics, "global")
        self.assertTrue(any(t.topic_id.startswith("alt") for t in selected))
        self.assertIn("publisher concentration limit changed shortlist", reasons)
        self.assertLessEqual(sum(t.articles[0]["publisher"] == "Same" for t in selected), 2)
        only_same, fallback = ri._build_shortlist(topics[:4], "global")
        self.assertEqual(len(only_same), 4)
        self.assertIn("publisher limit relaxed: no alternative eligible story", fallback)

    def test_syndicated_copies_share_publisher_identity(self):
        original = {"publisher": "Reuters", "url": "https://reuters.test/a",
                    "original_source_url": "https://reuters.test/a"}
        copy = {"publisher": "Local News", "url": "https://local.test/a",
                "original_source_url": "https://reuters.test/a"}
        self.assertEqual(ri._publisher_identity(original), ri._publisher_identity(copy))

    def test_zero_result_diagnostics_identify_distinct_pipeline_stages(self):
        status = {"stage_counts": {"discovered_articles": 0, "accepted_articles": 0,
                    "clustered_topics": 0, "final_ranked_shortlist": 0}, "metrics": {}}
        self.assertEqual(ri._zero_result_cause(status, []), "discovery returned no articles")
        status["stage_counts"]["discovered_articles"] = 2
        status["metrics"] = {"articles_rejected": [{"stage": "article-extraction"}]}
        self.assertIn("not evidence of irrelevance", ri._zero_result_cause(status, []))
        status["stage_counts"]["accepted_articles"] = 1
        status["stage_counts"]["clustered_topics"] = 1
        self.assertEqual(ri._zero_result_cause(status, [ri.Topic("x", "x", "s", "gaming", "global")]),
                         "topics were relevant only to unselected categories")

    def test_query_diagnostics_attribute_exclusions_merges_and_shortlist_locally(self):
        from types import SimpleNamespace

        # Both synthetic queries produce one deterministic cluster. A's topic
        # is excluded; B's eligible topic ranks outside the mocked shortlist.
        topics = {
            "science:query A": [ri.Topic("a", "A story", "s", "science", "global",
                                          score=1, relevant_categories=["sports"])],
            "science:query B": [ri.Topic("b", "B story", "s", "science", "global",
                                          score=1, relevant_categories=["science"])],
        }
        reports = {key: SimpleNamespace(items=[], metrics={"results_received": 1,
                    "accessible_articles": 1, "articles_rejected": []}) for key in topics}
        call = {"index": 0}

        def fake_research_news(query, **kwargs):
            key = "science:query A" if "query A" in query else "science:query B"
            return reports[key]

        def fake_cluster(items, *, category, relevance):
            call["index"] += 1
            key = "science:query A" if call["index"] == 1 else "science:query B"
            return topics[key]

        with patch.object(ri, "research_news", side_effect=fake_research_news), \
             patch.object(ri, "cluster_report_items", side_effect=fake_cluster), \
             patch.object(ri, "_build_shortlist", return_value=([], [])):
            # A's topic is ineligible, B's topic is eligible but not selected.
            _, _, statuses = ri.discover_topics(
                categories={"science": ("query A", "query B")}, queries_per_category=2, coverage="global")

        a = statuses["science:query A"]
        b = statuses["science:query B"]
        self.assertEqual(a["stage_counts"]["final_selected_category_exclusions"], 1)
        self.assertEqual(b["stage_counts"]["final_selected_category_exclusions"], 0)
        self.assertEqual(a["zero_result_cause"], "topics were relevant only to unselected categories")
        self.assertIn("shortlist", b["zero_result_cause"])
        self.assertEqual(b["stage_counts"]["cross_query_merges"], 0)
        self.assertEqual(a["stage_counts"]["cross_query_merges"], 0)

    def test_cross_query_merges_are_charged_only_to_query_with_duplicate(self):
        from types import SimpleNamespace

        duplicate = lambda: ri.Topic("same", "Shared story", "s", "science", "global",
                                     score=1, relevant_categories=["science"])
        reports = [SimpleNamespace(items=[], metrics={"results_received": 1,
                   "accessible_articles": 1, "articles_rejected": []}) for _ in range(3)]
        produced = [[duplicate()], [duplicate()],
                    [ri.Topic("other", "Other story", "s", "science", "global",
                              score=1, relevant_categories=["science"])]]
        with patch.object(ri, "research_news", side_effect=reports), \
             patch.object(ri, "cluster_report_items", side_effect=produced):
            _, _, statuses = ri.discover_topics(
                categories={"science": ("first", "second", "third")}, queries_per_category=3, coverage="global")
        counts = [status["stage_counts"]["cross_query_merges"] for status in statuses.values()]
        self.assertEqual(counts, [0, 1, 0])

    def test_cross_run_novelty_matches_headline_rewrite_and_keeps_evidence_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "research.db"
            old = ri.Topic("old", "Nvidia unveils Blackwell AI chip architecture",
                "summary", "technology_ai", "global", [{"url": "https://wire.test/a",
                "original_source_url": "https://wire.test/a", "publisher": "Wire",
                "headline": "Nvidia unveils Blackwell AI chip architecture",
                "publication_date": "2026-11-20T00:00:00Z", "evidence": "Earlier details",
                "primary": False}], [{"claim": "claim", "status": "DEVELOPING"}], "DEVELOPING")
            ri.persist_research("old", [old], {}, db_path=db_path)
            db = pipeline_store.connect(db_path)
            db.execute("update research_runs set created_at=?", ("2026-11-23T00:00:00+00:00",)); db.commit(); db.close()
            current = ri.Topic("current", "Nvidia reveals Blackwell architecture for AI processors",
                "summary", "technology_ai", "global", [{"url": "https://wire.test/b",
                "original_source_url": "https://wire.test/b", "publisher": "Wire",
                "publication_date": "2026-11-24T00:00:00Z", "evidence": "Updated article",
                "primary": False}], [{"claim": "new claim", "status": "CONFIRMED"}], "CONFIRMED")
            before = (json.dumps(current.claims), current.verification_status, json.dumps(current.articles))
            ri.classify_novelty([current], db_path=db_path)
            self.assertEqual(current.novelty_status, "PREVIOUSLY_COVERED")
            self.assertTrue(current.previous_coverage)
            self.assertEqual(before, (json.dumps(current.claims), current.verification_status, json.dumps(current.articles)))

    def test_cross_run_generic_or_short_similar_headlines_do_not_match(self):
        for old_head, new_head in (("Officials announce new policy today", "Officials announce new tax today"),
                                   ("Breaking news", "Breaking news")):
            with self.subTest(old=old_head), tempfile.TemporaryDirectory() as directory:
                db_path = Path(directory) / "research.db"
                ri.persist_research("old", [ri.Topic("old", old_head, "s", "politics", "global")], {}, db_path=db_path)
                db = pipeline_store.connect(db_path)
                db.execute("update research_runs set created_at=?", ("2026-11-23T00:00:00+00:00",)); db.commit(); db.close()
                current = ri.Topic("new", new_head, "s", "politics", "global")
                ri.classify_novelty([current], db_path=db_path)
                self.assertEqual(current.novelty_status, "NEW")

    def test_minor_rewrite_and_newer_syndicated_copy_are_not_meaningful_updates(self):
        previous = [{"headline": "Officials announce major ceasefire agreement",
            "articles": [{"url": "https://wire.test/a", "original_source_url": "https://wire.test/a",
                          "publication_date": "2026-11-20T00:00:00Z", "primary": True,
                          "evidence": "Officials announce agreement"}]}]
        topic = ri.Topic("t", "Officials announce historic major ceasefire agreement",
            "s", "politics", "global", [{"url": "https://copy.test/a",
            "original_source_url": "https://wire.test/a", "publication_date": "2026-11-24T00:00:00Z",
            "primary": True, "evidence": "Officials announce agreement"}])
        self.assertFalse(ri._has_newer_coverage(topic, previous))

    def test_lookback_excludes_old_future_invalid_and_malformed_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "research.db"
            ri.persist_research("old", [ri.Topic("old", "Researchers discover distant exoplanet",
                "s", "science", "global")], {}, db_path=db_path)
            db = pipeline_store.connect(db_path)
            db.execute("update research_runs set created_at=?", ("2026-11-01T00:00:00Z",))
            db.execute("insert into research_runs values(?,?,?)", ("future", "2026-11-25T00:00:00Z", "{}"))
            db.execute("insert into research_topics values(?,?,?,?,?,?,?,?,?,?,?)", ("future:t", "t", "future",
                "Researchers discover distant exoplanet", "s", "science", "global", "UNVERIFIED", 0, "{}",
                json.dumps({"headline": "Researchers discover distant exoplanet", "articles": []})))
            db.execute("insert into research_runs values(?,?,?)", ("bad", "not-a-date", "{}"))
            db.execute("insert into research_topics values(?,?,?,?,?,?,?,?,?,?,?)", ("bad:t", "t", "bad",
                "Researchers discover distant exoplanet", "s", "science", "global", "UNVERIFIED", 0, "{}", "[]"))
            db.commit(); db.close()
            current = ri.Topic("new", "Researchers discover distant exoplanet", "s", "science", "global")
            ri.classify_novelty([current], db_path=db_path)
            self.assertEqual(current.novelty_status, "NEW")

    def test_story_clustering_deduplicates_articles_and_keeps_distinct_publishers(self):
        a = Article("https://one.test/a", "One", "India launches new lunar mission today", evidence="details")
        b = Article("https://two.test/b", "Two", "India launches new lunar mission today", evidence="details")
        c = Article("https://three.test/c", "Three", "India announces new tax policy today", evidence="details")
        topics = ri.cluster_report_items([NewsItem(a.headline, "DEVELOPING", [a]),
                                          NewsItem(b.headline, "DEVELOPING", [b]),
                                          NewsItem(c.headline, "UNVERIFIED", [c])])
        self.assertEqual(len(topics), 2)
        lunar = next(t for t in topics if "lunar" in t.headline)
        self.assertEqual(len(lunar.articles), 2)
        self.assertEqual(len({x["publisher"] for x in lunar.articles}), 2)

    def test_unrelated_headline_not_clustered_by_one_shared_word(self):
        items = [NewsItem("Apple launches new device in India", "DEVELOPING"),
                 NewsItem("India reports severe monsoon flooding", "DEVELOPING")]
        self.assertEqual(len(ri.cluster_report_items(items)), 2)

    def test_event_headline_rewrites_cluster_but_distinct_nvidia_developments_do_not(self):
        a = Article("https://one.test/nvidia-a", "One", "Nvidia unveils Blackwell AI chip architecture",
                    evidence="Company announced the Blackwell architecture.")
        b = Article("https://two.test/nvidia-a", "Two", "Nvidia reveals Blackwell architecture for AI chips",
                    evidence="Details on Blackwell architecture.")
        c = Article("https://three.test/nvidia-b", "Three", "Nvidia earnings rise after data center sales",
                    evidence="Quarterly financial report.")
        topics = ri.cluster_report_items([NewsItem(a.headline, "DEVELOPING", [a]),
            NewsItem(b.headline, "DEVELOPING", [b]), NewsItem(c.headline, "DEVELOPING", [c])])
        self.assertEqual(len(topics), 2)
        blackwell = next(topic for topic in topics if "Blackwell" in topic.headline)
        self.assertEqual(len(blackwell.articles), 2)

    def test_war_and_creator_summit_subject_classification(self):
        self.assertEqual(ri._category_for("Ukraine war escalates as military launches airstrikes"), "international_affairs")
        self.assertEqual(ri._category_for("Creators summit gathers influencers for annual convention"), "explainers")
        self.assertEqual(ri._category_for("Creator economy platform expands monetization business"), "business_economy")
        self.assertIn("science", ri._categories_for("Scientists discover a new species"))
        self.assertIn("technology_ai", ri._categories_for("Scientists use AI technology to study climate"))

    def test_political_and_legal_stories_override_incidental_science_and_ai_terms(self):
        for headline in (
            "Trump and the Justice Department review AI research funding",
            "US Justice Department sues technology company over scientific data",
            "Government opens investigation into AI used by federal agencies",
        ):
            self.assertEqual(ri._category_for(headline), "politics", headline)

    def test_gaming_classification_does_not_leak_from_science_selected_run(self):
        self.assertEqual(ri._category_for("Grand Theft Auto VI release date announced"), "gaming")
        class Provider:
            def search(self, query):
                return [Article("https://game.test/story", "Game Desk",
                                "Grand Theft Auto VI release date announced", "2026-11-22T10:00:00+00:00")]
            def open(self, article):
                return Article(article.url, article.publisher, article.headline, article.publication_date,
                               evidence="Accessible report with details.")
        _, topics, _ = ri.discover_topics(provider=Provider(), categories={"science": ("science",)}, coverage="global")
        self.assertEqual(topics, [])

    def test_science_and_technology_headlines_are_retained_and_classified(self):
        for headline, expected in (("Researchers discover new exoplanet using telescope", "science"),
                                   ("Technology company unveils new artificial intelligence chip", "technology_ai")):
            article = Article("https://publisher.test/story", "Publisher", headline, evidence="Accessible details.")
            topic = ri.cluster_report_items([NewsItem(headline, "DEVELOPING", [article])], category="entertainment")[0]
            self.assertEqual(topic.category, expected)

    def test_science_and_technology_queries_produce_topics_when_articles_are_current_and_accessible(self):
        class Provider:
            def search(self, query):
                if "science" in query:
                    headline, host = "Researchers discover new exoplanet around nearby star", "science.test"
                else:
                    headline, host = "Technology firm announces artificial intelligence chip", "tech.test"
                return [Article(f"https://{host}/story", host, headline, "2026-11-22T10:00:00+00:00")]
            def open(self, article):
                return Article(article.url, article.publisher, article.headline, article.publication_date,
                               evidence="Accessible report with supporting details.")
        for category in ("science", "technology_ai"):
            _, topics, statuses = ri.discover_topics(provider=Provider(), categories={category: ri.CATEGORIES[category]},
                                                      coverage="global")
            self.assertTrue(topics, (category, statuses))
            self.assertEqual(topics[0].category, category)

    def test_unverified_topic_is_not_recommendation_eligible_or_briefed(self):
        article = Article("https://publisher.test/story", "Publisher", "Unconfirmed claim reaches social media",
                          evidence="Unverified report.")
        topic = ri.cluster_report_items([NewsItem(article.headline, "UNVERIFIED", [article])])[0]
        self.assertFalse(topic.recommendation_eligible)
        self.assertIn("No topics currently meet", ri.format_briefing("run", [topic]))

    def test_empty_and_low_quality_shortlists_stay_empty(self):
        topic = ri.Topic("weak", "Weak claim", "No support", "science", "global", [], [], "UNABLE_TO_VERIFY")
        ri.score_topic(topic)
        self.assertFalse(topic.recommendation_eligible)
        self.assertIn("No topics currently meet", ri.format_briefing("run", [topic]))

    def test_india_global_cross_query_topic_keeps_both_relevance_and_categories(self):
        item = NewsItem("Scientists discover new ocean species", "DEVELOPING", [Article(
            "https://science.test/story", "Science Desk", "Scientists discover new ocean species", evidence="Details.")])
        class Provider:
            def __init__(self): self.calls = []
            def search(self, query): self.calls.append(query); return [Article(
                "https://science.test/story", "Science Desk", item.claim, "2026-11-22T00:00:00+00:00")]
            def open(self, article): return Article(article.url, article.publisher, article.headline,
                article.publication_date, evidence="Accessible details.")
        provider = Provider()
        _, topics, _ = ri.discover_topics(provider=provider, categories={"science": ("science",)}, coverage="india_and_global")
        self.assertEqual(provider.calls, ["science news", "science official announcement",
                                          "science India news", "science India official announcement"])
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0].relevance, "india_and_global")

    def test_independent_sources_corroborate_but_reposts_do_not(self):
        a = Article("https://one.test/a", "One", "New telescope finds distant planet today",
                    evidence="page evidence", original_source_url="https://one.test/a")
        b = Article("https://two.test/b", "Two", "New telescope finds distant planet today",
                    evidence="page evidence", original_source_url="https://two.test/b")
        item = NewsItem(a.headline, "DEVELOPING", [a, b])
        topic = ri.cluster_report_items([item])[0]
        self.assertEqual(topic.verification_status, "CORROBORATED")
        repost = Article("https://syndicated.test/a", "Syndicated", a.headline,
                         evidence="same origin", original_source_url="https://one.test/a")
        topic = ri.cluster_report_items([NewsItem(a.headline, "DEVELOPING", [a, repost])])[0]
        self.assertEqual(topic.verification_status, "DEVELOPING")
        self.assertEqual(len(topic.articles), 2)  # retain both articles as evidence records

    def test_syndicated_copy_keeps_its_article_url_and_original_origin(self):
        original = Article("https://reuters.com/world/story", "Reuters", "Officials announce ceasefire agreement", evidence="report text",
                           original_source_url="https://reuters.com/world/story")
        copy = Article("https://local.example/news/123", "Local News", original.headline, evidence="republished report text",
                       original_source_url="https://reuters.com/world/story")
        topic = ri.cluster_report_items([NewsItem(original.headline, "DEVELOPING", [original, copy])])[0]
        self.assertEqual(len(topic.articles), 2)
        self.assertEqual({row["url"] for row in topic.articles}, {original.url, copy.url})
        self.assertEqual(topic.verification_status, "DEVELOPING")

    def test_ranking_uses_no_fabricated_engagement_and_penalizes_uncertainty(self):
        topic = ri.Topic("x", "Headline", "Summary", "science", "india", [], [], "UNVERIFIED")
        ranked = ri.score_topic(topic, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertFalse(ranked.score_breakdown["engagement_metrics_available"])
        self.assertIsNone(ranked.score_breakdown["interest"])
        self.assertEqual(ranked.score_breakdown["uncertainty_multiplier"], .65)

    def test_naive_publication_timestamp_is_treated_as_utc(self):
        topic = ri.Topic("t", "Headline", "Summary", "science", "global", [
            {"publication_date": "2026-01-01T00:00:00"}], [], "CONFIRMED")
        self.assertEqual(ri.score_topic(topic, now=datetime(2026, 1, 1, 1, tzinfo=timezone.utc)).score_breakdown["freshness"],
                         100 - (1 / 72 * 100))

    def test_accessible_evidence_improves_score_and_conflict_is_not_confirmed(self):
        uncertain = ri.score_topic(ri.Topic("u", "u", "s", "science", "global", [], [], "UNVERIFIED"))
        confirmed = ri.score_topic(ri.Topic("c", "c", "s", "science", "global", [], [], "CONFIRMED"))
        self.assertGreater(confirmed.score, uncertain.score)
        t = ri.Topic("c", "headline", "summary", "science", "global", [], [], "CONFLICTING")
        ri.score_topic(t)
        self.assertEqual(t.verification_status, "CONFLICTING")

    def test_sqlite_run_topics_and_selection_snapshot_are_persisted_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "research.db"
            topic = ri.Topic("topic-one", "Story", "Summary", "science", "india", [],
                             [{"claim": "A fact", "status": "DEVELOPING"}], "DEVELOPING", 40)
            ri.persist_research("run-1", [topic], {"rss": {"status": "ok"}}, db_path=db_path)
            ri.persist_research("run-1", [topic], {"rss": {"status": "ok"}}, db_path=db_path)
            db = pipeline_store.connect(db_path)
            self.assertEqual(db.execute("select count(*) from research_runs").fetchone()[0], 1)
            self.assertEqual(db.execute("select count(*) from research_topics").fetchone()[0], 1)
            snapshot = json.loads(db.execute("select snapshot_json from research_topics").fetchone()[0])
            self.assertEqual(snapshot["claims"][0]["claim"], "A fact")
            db.close()

    def test_telegram_briefing_is_selection_prompt(self):
        self.assertIn("Choose a topic number", ri.format_briefing("run", []))

    def test_custom_topic_is_saved_to_snapshot_without_claiming_research(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "research.db"
            topic = ri.Topic("t1", "Story", "Summary", "science", "global", [], [], "DEVELOPING")
            ri.persist_research("r", [topic], {}, db_path=db_path)
            briefing_id = "b1"
            with patch.object(telegram, "_research_db", side_effect=lambda: pipeline_store.connect(db_path)):
                db = pipeline_store.connect(db_path)
                from datetime import timedelta
                now = datetime.now(timezone.utc)
                payload = {"user_id": "123", "topics": [telegram.asdict_topic(topic)]}
                db.execute("insert into telegram_briefings values(?,?,?,?,?)",
                           (briefing_id, "r", now.isoformat(), json.dumps(payload),
                            (now + timedelta(hours=1)).isoformat()))
                db.commit(); db.close()
                answer = telegram._topic_selection_or_brief("My custom science topic", "123")
            self.assertIn("marked unresearched", answer)
            db = pipeline_store.connect(db_path)
            row = db.execute("select custom_topic,snapshot_json from topic_selections").fetchone()
            self.assertEqual(row["custom_topic"], "My custom science topic")
            self.assertEqual(json.loads(row["snapshot_json"])["verification_status"], "UNRESEARCHED")
            db.close()

    def test_numbered_selection_saves_immutable_snapshot_and_rejects_other_user(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "research.db"
            topic = ri.Topic("topic-1", "Selected story", "Evidence summary", "science", "india", [],
                             [{"claim": "supported detail", "evidence_urls": ["https://publisher.test/a"],
                               "status": "DEVELOPING"}], "DEVELOPING", 52)
            ri.persist_research("run", [topic], {}, db_path=db_path)
            with patch.object(telegram, "_research_db", side_effect=lambda: pipeline_store.connect(db_path)):
                db = pipeline_store.connect(db_path)
                from datetime import timedelta
                now = datetime.now(timezone.utc)
                db.execute("insert into telegram_briefings values(?,?,?,?,?)",
                           ("brief", "run", now.isoformat(),
                            json.dumps({"user_id": "123", "topics": [telegram.asdict_topic(topic)]}),
                            (now + timedelta(hours=1)).isoformat()))
                db.commit(); db.close()
                self.assertIn("another authorized account", telegram._topic_selection_or_brief("1", "456"))
                answer = telegram._topic_selection_or_brief("1", "123")
            self.assertIn("Saved topic 1", answer)
            db = pipeline_store.connect(db_path)
            saved = json.loads(db.execute("select snapshot_json from topic_selections").fetchone()[0])
            self.assertEqual(saved["claims"][0]["evidence_urls"], ["https://publisher.test/a"])
            db.close()


if __name__ == "__main__":
    unittest.main()
