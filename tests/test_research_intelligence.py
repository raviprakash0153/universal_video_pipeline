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
