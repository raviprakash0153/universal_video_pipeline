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
