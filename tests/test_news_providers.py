import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from news_research import (
    Article, InstagramProvider, MultiSourceNewsProvider, SourceRecord,
    YouTubeDataAPIProvider, XAPIProvider, _deduplicate, research_news,
    verify_account,
)


class _RSS:
    _timings = {"request_sum": 0, "redirect_count": 0, "extract": 0}
    def search(self, query): return []
    def open(self, article): return article


class _Social:
    name = "x"
    def search_records(self, query):
        return [SourceRecord("X", "https://x.com/test/status/42", "test", "id42", "42",
                             "2026-11-22T10:00:00Z", "2026-11-23T01:00:00Z", "GTA 6 rumor claim", {},
                             "unknown", .7, ["API result"], ["Unverified author"])]


class _RateLimited:
    name = "youtube"
    def search_records(self, query): raise HTTPError("https://api.example/search", 429, "rate limited", {}, None)


class _SocialOk(_Social):
    def __init__(self, name): self.name = name; self.calls = 0
    def search_records(self, query): self.calls += 1; return super().search_records(query)


class _Discovery:
    def __init__(self): self.calls = 0; self._timings = {}
    def search(self, query): self.calls += 1; return []
    def open(self, article): return article


class _SlowDiscovery(_Discovery):
    def __init__(self, delay, error=None): super().__init__(); self.delay, self.error = delay, error
    def search(self, query):
        import time
        self.calls += 1
        time.sleep(self.delay)
        if self.error: raise self.error
        return []


class NewsProviderTests(unittest.TestCase):
    def test_official_identity_needs_first_party_link(self):
        self.assertEqual(verify_account("x", "42", "GTA6Rockstar")[0], "unknown_account")
        self.assertEqual(verify_account("x", "42", "RockstarGames")[0], "official_account")
        result, _ = verify_account("x", "42", "RockstarGames", ["https://x.com/rockstargames"])
        self.assertEqual(result, "official_account")

    def test_x_api_normalizes_original_post_url_and_metadata(self):
        payload = {"data": [{"id": "99", "author_id": "u1", "text": "GTA 6 update", "created_at": "2026-11-22T00:00:00Z", "public_metrics": {"like_count": 3}}],
                   "includes": {"users": [{"id": "u1", "username": "writer", "name": "Writer"}]}}
        with patch("news_research._json_request", return_value=payload) as request:
            rows = XAPIProvider("secret").search_records("GTA 6")
        self.assertEqual(rows[0].original_url, "https://x.com/writer/status/99")
        self.assertEqual(rows[0].account_id, "u1")
        self.assertEqual(rows[0].published_at, "2026-11-22T00:00:00Z")
        self.assertEqual(rows[0].engagement["like_count"], 3)
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer secret")
        self.assertNotIn("secret", repr(rows[0]))

    def test_youtube_api_combines_search_details_and_marks_identity_unknown(self):
        with patch("news_research._json_request", side_effect=[
            {"items": [{"id": {"videoId": "v1"}, "snippet": {"channelId": "c1", "channelTitle": "Rockstar Games", "title": "GTA 6 trailer", "description": "desc", "publishedAt": "2026-11-22T00:00:00Z"}}]},
            {"items": [{"id": "v1", "statistics": {"viewCount": "123"}}]},
            {"items": [{"id": "official-channel"}]},
        ]): rows = YouTubeDataAPIProvider("key").search_records("GTA 6")
        self.assertEqual(rows[0].original_url, "https://www.youtube.com/watch?v=v1")
        self.assertEqual(rows[0].account_id, "c1")
        self.assertEqual(rows[0].engagement["viewCount"], 123)
        self.assertEqual(rows[0].authenticity, "unknown_account")

    def test_instagram_unavailable_without_supported_credentials(self):
        with self.assertRaisesRegex(RuntimeError, "unrestricted public search"):
            InstagramProvider(token="", account_id="").search_records("GTA 6")

    def test_instagram_graph_results_keep_caption_permalink_and_timestamp(self):
        with patch("news_research._json_request", return_value={"data": [{"id": "m1", "caption": "GTA 6 post", "permalink": "https://instagram.com/p/m1/", "timestamp": "2026-11-22T00:00:00Z", "like_count": 4}]}):
            rows = InstagramProvider("token", "account").search_records("GTA 6")
        self.assertEqual(rows[0].content_id, "m1")
        self.assertEqual(rows[0].original_url, "https://instagram.com/p/m1/")
        self.assertEqual(rows[0].engagement["like_count"], 4)

    def test_provider_api_failure_is_partial_and_reported(self):
        provider = MultiSourceNewsProvider([_Social()], _RSS())
        report = research_news("GTA 6", provider)
        self.assertTrue(report.items)
        self.assertEqual(report.metrics["platforms"]["x"], "checked (1 result(s))")
        self.assertIn("Google News RSS (discovery)", report.metrics["platforms"])

    def test_rate_limited_provider_is_reported_without_blocking_other_sources(self):
        provider = MultiSourceNewsProvider([_Social(), _RateLimited()], _RSS())
        report = research_news("GTA 6", provider)
        self.assertTrue(report.items)
        self.assertIn("HTTP 429 rate limited", report.metrics["platforms"]["youtube"])

    def test_all_configured_platforms_and_discovery_sources_are_invoked(self):
        socials = [_SocialOk(name) for name in ("x", "youtube", "instagram")]
        rss, official, publishers = _Discovery(), _Discovery(), _Discovery()
        provider = MultiSourceNewsProvider(socials, rss, official, publishers)
        provider.search("GTA 6")
        self.assertEqual([p.calls for p in socials], [1, 1, 1])
        self.assertEqual((rss.calls, official.calls, publishers.calls), (1, 1, 1))

    def test_discovery_diagnostics_cover_success_partial_and_timeout_shapes(self):
        import time
        from unittest.mock import patch
        cases = [((.005, .005, .005), 3), ((.2, .005, .005), 2),
                 ((.2, .2, .005), 1), ((.2, .2, .2), 0)]
        for delays, successes in cases:
            with self.subTest(delays=delays):
                rss, official, publishers = [_SlowDiscovery(d) for d in delays]
                provider = MultiSourceNewsProvider([], rss, official, publishers)
                with patch("news_research.SEARCH_WAIT_SECONDS", .03):
                    started = time.monotonic()
                    provider.search("topic")
                    self.assertLess(time.monotonic() - started, .15)
                completed = sum(x.get("status") == "completed" for x in provider.discovery_diagnostics.values())
                self.assertEqual(completed, successes)
                self.assertEqual(len(provider.discovery_diagnostics), 3)
                self.assertTrue(all("exception" in x and "results_parsed" in x
                                    for x in provider.discovery_diagnostics.values()))

    def test_discovery_http_dns_and_malformed_feed_errors_are_specific_and_safe(self):
        from unittest.mock import patch
        from urllib.error import HTTPError, URLError
        import socket
        for exc, expected in ((HTTPError("https://example.com", 503, "no", {}, None), "HTTPError (HTTP 503)"),
                              (URLError(socket.gaierror("dns secret")), "URLError"),
                              (ValueError("malformed XML"), "ValueError")):
            with self.subTest(exc=type(exc).__name__):
                provider = MultiSourceNewsProvider([], _Discovery(), _Discovery(), _Discovery())
                with patch.object(provider.rss, "search", side_effect=exc):
                    provider.search("topic")
                diag = provider.discovery_diagnostics["Google News RSS (discovery)"]
                self.assertEqual(diag["exception"], expected)
                self.assertNotIn("secret", repr(diag))

    def test_dns_provider_failure_has_safe_stage_and_actionable_reason(self):
        from urllib.error import URLError
        import socket
        provider = MultiSourceNewsProvider([], _Discovery(), _Discovery(), _Discovery())
        with patch.object(provider.official, "search", side_effect=URLError(socket.gaierror("secret host details"))):
            provider.search("topic")
        diag = provider.discovery_diagnostics["Official sites (discovery)"]
        self.assertEqual(diag["failure_stage"], "network connection or DNS")
        self.assertEqual(diag["safe_exception_detail"], "DNS lookup failed")
        self.assertNotIn("secret", str(diag))

    def test_missing_platform_credentials_are_reported_not_configured(self):
        rss = _Discovery()
        provider = MultiSourceNewsProvider(
            [XAPIProvider(""), YouTubeDataAPIProvider(""), InstagramProvider("", "")], rss,
            _Discovery(), _Discovery())
        provider.search("GTA 6")
        for platform in ("x", "youtube", "instagram"):
            self.assertEqual(provider.availability[platform], "NOT CONFIGURED")
        self.assertIn("discovery only", provider.availability["Google News RSS (discovery)"])

    def test_social_claim_cannot_become_confirmed_and_repost_carries_flag(self):
        record = SourceRecord("X", "https://x.com/a/status/1", "a", "a1", "1", "2026-11-22T00:00:00Z",
                              content="GTA 6 official release date leak rumored", is_repost=True)
        article = Article(record.original_url, "a", record.content, record.published_at, evidence=record.content,
                          platform="X", is_repost=True)
        report = research_news("GTA 6 release date", type("P", (), {"search": lambda self, q: [article], "open": lambda self, a: a})(), verify_claim=True)
        self.assertEqual(report.items[0].status, "UNVERIFIED")
        self.assertTrue(report.items[0].sources[0].is_repost)

    def test_original_urls_are_deduplicated_without_counting_reposts_twice(self):
        items = [Article("https://x.com/a/status/1", "a", "same rumor", platform="X"),
                 Article("https://x.com/a/status/1?ref=share", "a", "same rumor", platform="X")]
        self.assertEqual(len(_deduplicate(items)), 1)


if __name__ == "__main__": unittest.main()
