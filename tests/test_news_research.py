import unittest
import base64
import io
import time
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

from news_research import Article, GoogleNewsRSSProvider, format_report, research_news
from command_engine import InputType, Intent, process_command
import telegram_text_dry_run as telegram


class FakeProvider:
    def __init__(self, articles=(), fail=False): self.articles, self.fail, self.queries, self.opened = list(articles), fail, [], []
    def search(self, query):
        self.queries.append(query)
        if self.fail: raise OSError("network error")
        return list(self.articles)
    def open(self, article):
        self.opened.append(article.url)
        return Article(article.url, article.publisher, article.headline, article.publication_date,
                       "2026-11-23T00:00:00+00:00", article.evidence or "Article body supports reported details.", True,
                       article.original_source_url)


def article(host, title, publisher=None, date="2026-11-22T10:00:00+00:00", evidence="Article text"):
    return Article(f"https://{host}/story", publisher or host, title, date, evidence=evidence)


class NewsResearchTests(unittest.TestCase):
    def test_google_resolver_timeout_http_network_and_malformed_failures_are_bounded(self):
        from news_research import _resolve_google_token
        url = "https://news.google.com/rss/articles/opaque"
        class StalledResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, _): raise TimeoutError("mock stalled body")
        class Opener:
            def __init__(self, result): self.result, self.timeouts = result, []
            def open(self, req, timeout):
                self.timeouts.append(timeout)
                if isinstance(self.result, Exception): raise self.result
                return self.result
        cases = [
            (StalledResponse(), "timeout"),
            (HTTPError(url, 429, "", Message(), None), "google_rate_limited"),
            (HTTPError(url, 503, "", Message(), None), "HTTP 503"),
            (URLError("offline"), "URLError"),
            (type("Bad", (), {"status": 200, "__enter__": lambda s: s,
                "__exit__": lambda *a: None, "read": lambda s, n: b"<html>broken"})(), "signature/timestamp missing"),
        ]
        for result, reason in cases:
            opener = Opener(result)
            with self.subTest(reason=reason):
                resolved, failure = _resolve_google_token(url, "ign.com", timeout=99,
                    opener_factory=lambda: opener)
                self.assertIsNone(resolved)
                self.assertIn(reason, failure)
                self.assertEqual(opener.timeouts, [12.0])

    def test_google_resolver_timeout_policy_has_no_retry_and_caps_redirects(self):
        from news_research import _resolve_google_token, _LimitedRedirects
        opener = type("Failing", (), {"open": lambda self, req, timeout: (_ for _ in ()).throw(TimeoutError())})()
        _resolve_google_token("https://news.google.com/rss/articles/x", "ign.com",
                              opener_factory=lambda: opener)
        self.assertEqual(_LimitedRedirects(2).maximum, 2)
        provider = GoogleNewsRSSProvider(timeout=99, retries=8)
        self.assertEqual(provider.timeout, 15)
        self.assertEqual(provider.retries, 1)  # resolver itself never uses provider retries

    def test_redirected_resolver_response_can_timeout_without_retry(self):
        from news_research import _resolve_google_token
        class RedirectedAndStalled:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, _): raise TimeoutError("stalled after redirect")
        class Opener:
            calls = 0
            def open(self, req, timeout):
                self.calls += 1
                return RedirectedAndStalled()
        opener = Opener()
        result = _resolve_google_token("https://news.google.com/rss/articles/x", "ign.com",
                                       opener_factory=lambda: opener)
        self.assertIsNone(result[0])
        self.assertEqual(result[1], "stage=signature-fetch: timeout")
        self.assertEqual(opener.calls, 1)

    def test_unresolved_discovery_preserves_existing_article_url_and_continues(self):
        token = "https://news.google.com/rss/articles/opaque"
        preserved = Article(token, "IGN", "First story", original_source_url="https://ign.com/news/first-story")
        unresolved = Article(token + "2", "IGN", "Second story")
        class Provider(FakeProvider):
            timeout = 8
            def resolve_discovery_candidate(self, item):
                if item.headline == "Second story":
                    item.resolver_diagnostics = {"result": "unavailable", "failure": "stage=signature-fetch: timeout"}
                return item
        provider = Provider([preserved, unresolved])
        report = research_news("IGN news", provider)
        self.assertEqual(len(provider.queries), 2)
        self.assertEqual(preserved.original_source_url, "https://ign.com/news/first-story")
        self.assertEqual(report.metrics["google_url_resolution"]["attempted"], 1)
        self.assertEqual(report.metrics["google_url_resolution"]["policy"],
                         "maximum five relevance-ranked discovery resolutions per query; no retries")

    def test_resolver_exception_does_not_stop_processing_later_articles(self):
        items = [Article("https://news.google.com/rss/articles/one", "IGN", "First"),
                 Article("https://news.google.com/rss/articles/two", "IGN", "Second")]
        class Provider(FakeProvider):
            def resolve_discovery_candidate(self, item):
                if item.headline == "First": raise OSError("mock connection failure")
                item.resolver_diagnostics = {"result": "unavailable", "failure": "stage=resolver: mocked"}
                return item
        report = research_news("stories", Provider(items))
        self.assertEqual(report.metrics["results_received"], 2)
        self.assertEqual(report.metrics["google_url_resolution"]["attempted"], 2)

    def test_discovery_resolver_work_is_capped_per_run(self):
        items = [Article(f"https://news.google.com/rss/articles/{i}", "IGN", f"Story {i}")
                 for i in range(7)]
        class Provider(FakeProvider):
            def __init__(self, articles): super().__init__(articles); self.resolved = 0
            def resolve_discovery_candidate(self, item): self.resolved += 1; return item
        provider = Provider(items)
        report = research_news("stories", provider)
        self.assertEqual(provider.resolved, 5)
        self.assertEqual(report.metrics["google_url_resolution"]["skipped_budget"], 2)

    def test_resolver_budget_prioritizes_query_relevant_records_over_feed_order(self):
        headlines = ["Unrelated sports roundup", "Quantum computing breakthrough announced",
                     "New quantum computing processor tested", "Quantum computing research advances",
                     "Quantum computing chip reaches milestone", "Quantum computing lab reports result",
                     "Quantum computing system demonstrates speedup"]
        items = [Article(f"https://news.google.com/rss/articles/{i}", "News", headline)
                 for i, headline in enumerate(headlines)]
        class Provider(FakeProvider):
            def __init__(self, articles): super().__init__(articles); self.resolved_headlines = []
            def resolve_discovery_candidate(self, item):
                self.resolved_headlines.append(item.headline)
                return item
        provider = Provider(items)
        report = research_news("quantum computing", provider, latest=False)
        self.assertEqual(len(provider.resolved_headlines), 5)
        self.assertNotIn("Unrelated sports roundup", provider.resolved_headlines)
        self.assertEqual(report.metrics["google_url_resolution"]["skipped_budget"], 2)

    def test_discovery_url_validation_accepts_unusual_publisher_article_paths(self):
        from news_research import _article_url_rejection
        fixtures = {
            "Reuters": "https://www.reuters.com/world/us/nvidia-reinstates-jensen-huang-2026-11-24/",
            "AP News": "https://apnews.com/article/indonesia-factory-fire-rescue-abc123?utm_source=rss",
            "ScienceDaily": "https://www.sciencedaily.com/releases/2026/11/261124123456.htm/",
            "Science News": "https://www.sciencenews.org/article/milky-way-galactic-shock-wave-map",
            "query": "https://example.com/story/123?output=1&ref=rss",
            "odd structure": "https://example.com/index.php?story=123&section=world",
            "redirect target": "https://example.com/article/final-story/",
        }
        for publisher, url in fixtures.items():
            with self.subTest(publisher=publisher):
                self.assertIsNone(_article_url_rejection(Article(url, publisher, "A sufficiently descriptive headline")))

    def test_discovery_url_validation_rejects_homepages_and_listing_pages(self):
        from news_research import _article_url_rejection
        fixtures = (
            ("https://www.reuters.com/", "Reuters"),
            ("https://apnews.com", "Associated Press"),
            ("https://www.sciencedaily.com/", "ScienceDaily"),
            ("https://www.sciencenews.org/", "Science News"),
            ("https://www.ndtv.com/", "NDTV"),
            ("https://example.com/search?q=fire", "Example News"),
            ("https://example.com/tag/science/", "Example News"),
            ("https://example.com/category/world/", "Example News"),
            ("https://example.com/news/", "Example News"),
        )
        for url, publisher in fixtures:
            with self.subTest(url=url):
                self.assertIsNotNone(_article_url_rejection(Article(url, publisher, "A sufficiently descriptive headline")))
        self.assertIsNotNone(_article_url_rejection(Article("https://news.google.com/rss/articles/token", "Reuters",
            "A sufficiently descriptive headline", publisher_url="https://www.reuters.com/")))

    def test_discovery_url_validation_rejects_invalid_and_non_https(self):
        from news_research import _article_url_rejection
        for url in ("http://example.com/story", "javascript:alert(1)", "https://user:pass@example.com/story"):
            with self.subTest(url=url):
                self.assertEqual(_article_url_rejection(Article(url, "Example", "A descriptive article headline")),
                                 "invalid or non-HTTPS article URL")

    def test_valid_original_article_url_takes_precedence_over_rss_publisher_homepage(self):
        from news_research import _article_url_rejection
        item = Article("https://news.google.com/rss/articles/token", "Reuters", "A descriptive article headline",
                       original_source_url="https://reuters.com/article/story-123",
                       publisher_url="https://www.reuters.com/")
        self.assertIsNone(_article_url_rejection(item))

    def test_real_rss_discovery_resolves_opaque_wrapper_before_quality_filter(self):
        """Exercise RSS parsing, the production discovery filter, and provider.open dispatch."""
        from news_research import MultiSourceNewsProvider
        article_url = "https://www.reuters.com/world/us/science-report-story-123"
        xml = (b'<rss><channel><item><title>Researchers announce a major science discovery - Reuters</title>'
               b'<link>https://news.google.com/rss/articles/AU_yqLopaque</link>'
               b'<pubDate>Mon, 23 Nov 2026 10:00:00 GMT</pubDate>'
               b'<source url="https://www.reuters.com/">Reuters</source></item></channel></rss>')
        response = type("Response", (), {"read": lambda self, n: xml, "__enter__": lambda self: self,
                                          "__exit__": lambda *args: None})()
        class Provider(GoogleNewsRSSProvider):
            def open(self, article):
                self.opened = article
                return Article(article.original_source_url, article.publisher, article.headline,
                    article.publication_date, evidence="Researchers announced this major science discovery.", accessible=True,
                    original_source_url=article.original_source_url)
        rss = Provider()
        multi = MultiSourceNewsProvider(providers=[], rss=rss)
        with patch("news_research.urlopen", return_value=response), \
             patch("news_research._resolve_google_token", return_value=(article_url, "")) as resolve:
            report = research_news("science discovery", multi, latest=False)
        self.assertEqual(resolve.call_count, 1)  # deduplicated result reaches production filter once
        self.assertEqual(rss.opened.original_source_url, article_url)
        self.assertEqual(report.metrics["accessible_articles"], 1)
        self.assertEqual(report.metrics["articles_rejected"], [])

    def test_rejected_record_diagnostics_show_safe_url_validation_inputs(self):
        class WrapperOnly(FakeProvider):
            def resolve_discovery_candidate(self, article): return article
        item = Article("https://news.google.com/rss/articles/opaque", "Reuters",
            "Researchers announce a major science discovery", publisher_url="https://www.reuters.com/")
        report = research_news("science discovery", WrapperOnly([item]), latest=False)
        diagnostic = report.metrics["url_validation_diagnostics"][0]
        self.assertEqual(diagnostic["publisher"], "Reuters")
        self.assertEqual(diagnostic["original_source_url"], None)
        self.assertEqual(diagnostic["publisher_url"], "https://www.reuters.com/")
        self.assertIn("homepage", diagnostic["reason"])

    def test_batchexecute_response_is_decoded_as_two_json_layers(self):
        from news_research import _parse_batchexecute
        result = _parse_batchexecute(b')]}\'\n\n[["wrb.fr","Fbv4je","[\\"garturlres\\",\\"https://ign.com/articles/story\\",1]",null]]')
        self.assertEqual(result, "https://ign.com/articles/story")

    def test_batchexecute_rejects_malformed_and_null_payloads(self):
        from news_research import _parse_batchexecute
        for data in (b"not json", b'[["wrb.fr","Fbv4je",null]]', b'[]'):
            with self.subTest(data=data), self.assertRaises((ValueError, TypeError)):
                _parse_batchexecute(data)

    def test_google_resolver_validates_https_domain_and_article_path(self):
        from news_research import _resolve_google_token
        self.assertEqual(_resolve_google_token("https://news.google.com/rss/articles/x", ""),
                         (None, "stage=publisher-validation: missing publisher URL/domain"))
        # All rejection cases are checked before any HTTP request.
        class Response:
            status = 200
            def __init__(self, data): self.data = data
            def read(self, _): return self.data
            def __enter__(self): return self
            def __exit__(self, *args): pass
        class Opener:
            def __init__(self, target): self.target = target
            def open(self, req, timeout):
                if req.get_method() == "GET": return Response(b'<i data-n-a-sg="sig" data-n-a-ts="123">')
                inner = json.dumps(["garturlres", self.target, 1])
                data = json.dumps([["wrb.fr", "Fbv4je", inner]])
                return Response((")]}\'\n\n" + data).encode())
        import json
        for target, expected in (("https://ign.com/", "stage=url-validation: publisher homepage rejected"),
                                 ("https://other.example/story", "stage=publisher-validation: resolved host does not match ign.com"),
                                 ("http://ign.com/articles/a", "stage=url-validation: result is not a valid HTTPS URL")):
            got = _resolve_google_token("https://news.google.com/rss/articles/opaque", "ign.com",
                                        opener_factory=lambda t=target: Opener(t))
            self.assertEqual(got, (None, expected))

    def test_google_resolver_reports_missing_token_timeout_and_http_error(self):
        from news_research import _resolve_google_token
        self.assertIn("missing token", _resolve_google_token("https://news.google.com/rss/articles/", "ign.com")[1])
        class TimeoutOpener:
            def open(self, *args, **kwargs): raise TimeoutError()
        self.assertEqual(_resolve_google_token("https://news.google.com/rss/articles/x", "ign.com",
                         opener_factory=TimeoutOpener)[1], "stage=signature-fetch: timeout")
        class ErrorOpener:
            def open(self, *args, **kwargs): raise HTTPError("https://news.google.com/", 503, "", Message(), None)
        self.assertEqual(_resolve_google_token("https://news.google.com/rss/articles/x", "ign.com",
                         opener_factory=ErrorOpener)[1], "stage=signature-fetch: HTTP 503")

    def test_google_resolver_reports_missing_signature_and_batchexecute_http_error(self):
        from news_research import _resolve_google_token
        class Response:
            status = 200
            def __init__(self, body): self.body = body
            def read(self, _): return self.body
            def __enter__(self): return self
            def __exit__(self, *args): pass
        class MissingSignature:
            def open(self, req, timeout): return Response(b"<html>ordinary page without resolver attributes</html>")
        diagnostics = {}
        self.assertEqual(_resolve_google_token("https://news.google.com/rss/articles/x", "ign.com",
                         opener_factory=MissingSignature, diagnostics=diagnostics)[1],
                         "stage=signature-fetch: required signature/timestamp missing")
        self.assertFalse(diagnostics["signature_extracted"])
        self.assertEqual(diagnostics["signature_request"], "started")
        self.assertEqual(diagnostics["signature_response"], "received")
        self.assertEqual(diagnostics["signature_timestamp_extraction"], "incomplete")
        self.assertEqual(diagnostics["batchexecute_request"], "not started")
        self.assertEqual(diagnostics["signature_page_classification"], "resolver attributes missing")
        class BatchexecuteError:
            def open(self, req, timeout):
                if req.get_method() == "GET": return Response(b'<i data-n-a-sg="sig" data-n-a-ts="123">')
                raise HTTPError(req.full_url, 429, "", Message(), None)
        diagnostics = {}
        self.assertEqual(_resolve_google_token("https://news.google.com/rss/articles/x", "ign.com",
                         opener_factory=BatchexecuteError, diagnostics=diagnostics)[1],
                         "stage=batchexecute: HTTP 429")
        self.assertEqual(diagnostics["batchexecute_http_status"], 429)

    def test_independent_resolver_then_publisher_captcha_preserves_validated_url(self):
        import json
        publisher_url = "https://www.independent.co.uk/news/story.html"
        rpc = json.dumps(["garturlres", publisher_url, 1])
        framed = b')]}\'\n\n' + json.dumps([["wrb.fr", "Fbv4je", rpc]]).encode()
        class Response:
            status = 200
            def __init__(self, body, url): self.body, self.url = body, url
            headers = Message()
            def read(self, _): return self.body
            def geturl(self): return self.url
            def info(self): return Message()
            def __enter__(self): return self
            def __exit__(self, *args): pass
        class Opener:
            def __init__(self): self.calls = []
            def open(self, req, timeout):
                self.calls.append(req.get_method() + " " + req.full_url)
                if req.get_method() == "GET" and "rss/articles" in req.full_url:
                    return Response(b'<i data-n-a-sg="sig" data-n-a-ts="123">', req.full_url)
                if req.get_method() == "POST": return Response(framed, req.full_url)
                return Response(b"<html><title>CAPTCHA</title> Please complete the CAPTCHA</html>", publisher_url)
        opener = Opener()
        with patch("news_research.build_opener", return_value=opener):
            result = GoogleNewsRSSProvider().open(Article(
                "https://news.google.com/rss/articles/opaque", "The Independent", "Independent report"))
        self.assertEqual(opener.calls[0].split()[0], "GET")
        self.assertEqual(opener.calls[1].split()[0], "POST")
        self.assertIn("independent.co.uk", opener.calls[2])
        self.assertEqual(result.publisher_url, publisher_url)
        self.assertEqual(result.original_source_url, publisher_url)
        self.assertEqual(result.rejection_reason, "publisher_captcha")
        self.assertFalse(result.accessible)
        self.assertIsNone(result.evidence)
        self.assertFalse(result.candidate_verified)

    def test_red_bull_signature_429_is_classified_and_stops_before_batchexecute(self):
        from news_research import _resolve_google_token
        err = HTTPError("https://news.google.com/rss/articles/x", 429, "", {"Retry-After": "999999"}, None)
        class RateLimited:
            def __init__(self): self.calls = 0
            def open(self, req, timeout):
                self.calls += 1
                raise err
        opener, diagnostics = RateLimited(), {}
        result, reason = _resolve_google_token("https://news.google.com/rss/articles/x", "redbull.com",
                                                opener_factory=lambda: opener, diagnostics=diagnostics)
        self.assertIsNone(result)
        self.assertEqual(reason, "google_rate_limited")
        self.assertEqual(opener.calls, 1)
        self.assertEqual(diagnostics["signature_page_classification"], "google_rate_limited")
        self.assertEqual(diagnostics["retry_after_seconds"], 3600)
        self.assertEqual(diagnostics["batchexecute_request"], "not started")

    def test_resolved_url_is_retained_only_after_domain_validation(self):
        from news_research import _resolve_google_token
        import json
        class Response:
            status = 200
            def __init__(self, body): self.body = body
            def read(self, _): return self.body
            def __enter__(self): return self
            def __exit__(self, *args): pass
        class Resolver:
            def __init__(self, target): self.target = target
            def open(self, req, timeout):
                if req.get_method() == "GET": return Response(b'<i data-n-a-sg="s" data-n-a-ts="1">')
                rpc = json.dumps(["garturlres", self.target, 1])
                return Response(b')]}\'\n\n' + json.dumps([["wrb.fr", "Fbv4je", rpc]]).encode())
        valid = _resolve_google_token("https://news.google.com/rss/articles/x", "redbull.com",
                                      opener_factory=lambda: Resolver("https://redbull.com/story"))
        invalid = _resolve_google_token("https://news.google.com/rss/articles/x", "redbull.com",
                                        opener_factory=lambda: Resolver("https://evil.example/story"))
        self.assertEqual(valid[0], "https://redbull.com/story")
        self.assertIsNone(invalid[0])
        self.assertIn("does not match", invalid[1])

    def test_exact_live_token_fixtures_report_signature_and_rpc_parse_stages(self):
        from news_research import _resolve_google_token
        urls = (
            "https://news.google.com/rss/articles/CBMic0FVX3lxTE1IM3EzdFh2TDAzLVNIR2R6SDhfa0M0NU1GQkF1QmJOcGFtNF9WcmtZY3RwU29KSkNqcVlmY0FHRUwtNXFENzdTYk9fQ0pYTnFfaFdONGlsanNQaEdmb0VUdUNtQ3dkaG9QWVJCdlJvT2tpZGM?oc=5",
            "https://news.google.com/rss/articles/CBMimwFBVV95cUxQc0RzSjJZQ29Ldjd3R19sakRmSHk0emZ3TmVHNXN5U25pSURPV1dHdEFka1dHY0VyRVpKNzktdGN6aFZOcUdoQThHb3gwYXk3c0tzYk9aYWdjYjhmX1MxNS0yVnVWRDY5OE8tZV9SZzdHNGFxUk5tY1FKamN1dTN1TXQyQ2tkanJVRzJjbFIwNzhQY2JWUkJGVEpmYw?oc=5",
            "https://news.google.com/rss/articles/CBMisAFBVV95cUxNS3JlcTNuT3FGTl93bEQ4M0VDdklhM1B6Z0xCT3E5allSdVZRSTBBLTdBQ0tiUnEyT0gwbWF3ajY2NnNfUm5HalppV0I4cHNOdXlCVHZtUXVOMWFPSjdtUVRJV21iQ1g0M2hRZGh5cTBGdTAtNnJMdDBmNV9TNW1iVjJjeUxvUmpjaTFPLTZIMG5iSlpxVTF4X0VldGhfbWkza1JfdFVkdllKRlRwcS1HNA?oc=5",
        )
        class Response:
            status = 200
            def __init__(self, body): self.body = body
            def read(self, _): return self.body
            def __enter__(self): return self
            def __exit__(self, *args): pass
        for url in urls:
            with self.subTest(url=url):
                class MissingSignature:
                    def open(self, req, timeout):
                        return Response(b"<html>ordinary page</html>")
                diagnostics = {}
                result, reason = _resolve_google_token(url, "example.com", opener_factory=MissingSignature,
                                                      diagnostics=diagnostics)
                self.assertIsNone(result)
                self.assertEqual(reason, "stage=signature-fetch: required signature/timestamp missing")
                self.assertEqual(diagnostics["signature_http_status"], 200)
                self.assertFalse(diagnostics["timestamp_extracted"])

                class MalformedRPC:
                    def open(self, req, timeout):
                        return Response(b'<i data-n-a-sg="sig" data-n-a-ts="123">' if req.get_method() == "GET"
                                        else b"not-json")
                diagnostics = {}
                result, reason = _resolve_google_token(url, "example.com", opener_factory=MalformedRPC,
                                                      diagnostics=diagnostics)
                self.assertIsNone(result)
                self.assertTrue(reason.startswith("stage=batchexecute-parse:"))
                self.assertTrue(diagnostics["signature_extracted"])
                self.assertEqual(diagnostics["batchexecute_response_bytes"], 8)
                self.assertFalse(diagnostics["response_parsed"])
                self.assertEqual(diagnostics["batchexecute_request"], "started")
                self.assertEqual(diagnostics["batchexecute_response"], "received")
                self.assertEqual(diagnostics["signature_timestamp_extraction"], "complete")

    def test_article_timeout_preserves_completed_results_and_marks_timeout(self):
        class SlowProvider(FakeProvider):
            def open(self, item):
                if item.url.endswith("slow"):
                    time.sleep(.15)
                    return super().open(item)
                return super().open(item)
        provider = SlowProvider([article("ign.com", "Studio announces an update"),
                                 Article("https://reuters.com/slow", "Reuters", "Studio confirmed an update")])
        with patch("news_research.ARTICLE_WAIT_SECONDS", .03):
            report = research_news("Studio", provider)
        self.assertTrue(report.items)
        self.assertEqual(report.metrics["timed_out_articles"], 1)
        self.assertTrue(any(attempt["status"] == "TIMEOUT" for attempt in report.metrics["article_attempts"]))

    def test_several_article_timeouts_are_all_reported(self):
        class MixedProvider(FakeProvider):
            def open(self, item):
                if "slow" in item.url:
                    time.sleep(.15)
                    return super().open(item)
                return super().open(item)
        provider = MixedProvider([Article(f"https://ign.com/slow{i}", "IGN", f"Studio announces update {i}")
                                  for i in range(3)])
        with patch("news_research.ARTICLE_WAIT_SECONDS", .02):
            report = research_news("Studio", provider)
        self.assertEqual(report.metrics["timed_out_articles"], 3)

    def test_all_article_timeouts_return_clear_no_evidence_report_without_waiting(self):
        class HungProvider(FakeProvider):
            def open(self, item):
                time.sleep(.5)
                return super().open(item)
        provider = HungProvider([article("ign.com", "Studio announces an update")])
        started = time.monotonic()
        with patch("news_research.ARTICLE_WAIT_SECONDS", .02):
            report = research_news("Studio", provider)
        self.assertLess(time.monotonic() - started, .2)
        self.assertFalse(report.items)
        self.assertIn("timed out", report.summary)
        self.assertEqual(report.metrics["article_attempts"][0]["status"], "TIMEOUT")

    def test_provider_open_unexpected_exception_is_reported_not_raised(self):
        class BrokenProvider(FakeProvider):
            def open(self, item): raise ValueError("unexpected")
        report = research_news("Studio", BrokenProvider([article("ign.com", "Studio announces update")]))
        self.assertFalse(report.items)
        self.assertIn("could not be retrieved", report.summary)

    def test_telegram_reply_and_polling_survive_article_timeout(self):
        class HungProvider(FakeProvider):
            def open(self, item):
                time.sleep(.5)
                return super().open(item)
        provider = HungProvider([article("ign.com", "Studio announces an update")])
        update = {"update_id": 992, "message": {"text": "Fetch news about Studio",
                  "chat": {"id": 1, "type": "private"}, "from": {"id": 1}}}
        with patch("news_research.ARTICLE_WAIT_SECONDS", .02), patch.object(telegram, "_telegram_request") as send:
            telegram._reply_to_update("token", update, "1", set(), provider)
        self.assertIn("timed out", send.call_args.args[2]["text"])

    def test_executor_shutdown_is_non_waiting_after_timeout(self):
        class HungProvider(FakeProvider):
            def open(self, item):
                time.sleep(.5)
                return super().open(item)
        with patch("news_research.ARTICLE_WAIT_SECONDS", .02), patch("news_research.ThreadPoolExecutor.shutdown") as shutdown:
            report = research_news("Studio", HungProvider([article("ign.com", "Studio announces update")]))
        self.assertTrue(report.metrics["timed_out_articles"])
        self.assertTrue(shutdown.call_args.kwargs["wait"] is False)

    def test_google_news_rss_redirect_extracts_original_publisher_article(self):
        provider = GoogleNewsRSSProvider()
        xml = b'''<rss><channel><item><title>GTA 6 announced</title><link>https://news.google.com/rss/articles/abc</link><pubDate>Mon, 23 Nov 2026 10:00:00 GMT</pubDate><source>Google News</source></item></channel></rss>'''
        response = type("Response", (), {"read": lambda self, n: xml, "__enter__": lambda self: self,
                                          "__exit__": lambda *args: None})()
        with patch("news_research.urlopen", return_value=response):
            item = provider.search("GTA 6")
        self.assertEqual(item[0].url, "https://news.google.com/rss/articles/abc")
        self.assertEqual(item[0].publisher, "Google News")

    def test_rss_source_name_and_url_and_date_are_preserved_with_decoded_article_url(self):
        target = "https://ign.com/articles/gta-6"
        token = base64.urlsafe_b64encode(b"\x12" + bytes([len(target)]) + target.encode()).decode().rstrip("=")
        google = f"https://news.google.com/rss/articles/{token}"
        xml = (f'<rss><channel><item><title>GTA 6 update - IGN</title><link>{google}</link>'
               '<pubDate>Mon, 23 Nov 2026 10:00:00 GMT</pubDate>'
               '<source url="https://ign.com">IGN</source></item></channel></rss>').encode()
        response = type("Response", (), {"read": lambda self, n: xml, "__enter__": lambda self: self,
                                          "__exit__": lambda *args: None})()
        with patch("news_research.urlopen", return_value=response): item = GoogleNewsRSSProvider().search("GTA 6")
        self.assertEqual(item[0].publisher, "IGN")
        self.assertEqual(item[0].original_source_url, target)
        self.assertEqual(item[0].publication_date, "2026-11-23T10:00:00+00:00")

    def test_rss_article_level_publisher_url_is_used_when_present(self):
        target = "https://publisher.example/story/gta-update"
        xml = (f'<rss><channel><item><title>GTA update changes release plans</title>'
               '<link>https://news.google.com/rss/articles/id</link>'
               f'<source url="{target}">Publisher</source></item></channel></rss>').encode()
        response = type("Response", (), {"read": lambda self, n: xml, "__enter__": lambda self: self,
                                          "__exit__": lambda *args: None})()
        with patch("news_research.urlopen", return_value=response): item = GoogleNewsRSSProvider().search("GTA")
        self.assertEqual(item[0].original_source_url, target)

    def test_real_feed_shaped_metadata_is_preserved_and_homepage_is_not_article_url(self):
        xml = b'''<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>
          <title>GTA 6 could be playable on PC by next week, say modders - PC Guide</title>
          <link>https://news.google.com/rss/articles/abc</link><description>&lt;a href="https://www.pcguide.com/news/gta-6-pc/"&gt;story&lt;/a&gt;</description>
          <source url="https://www.pcguide.com/">PC Guide</source>
          <content:encoded>&lt;p&gt;Publisher article link&lt;/p&gt;</content:encoded>
          <guid isPermaLink="false">opaque-guid</guid></item></channel></rss>'''
        response = type("Response", (), {"read": lambda self, n: xml, "__enter__": lambda self: self,
                                          "__exit__": lambda *args: None})()
        with patch("news_research.urlopen", return_value=response):
            item = GoogleNewsRSSProvider().search("GTA 6")[0]
        self.assertEqual(item.publisher_url, "https://www.pcguide.com/")
        self.assertEqual(item.original_source_url, "https://www.pcguide.com/news/gta-6-pc/")
        self.assertIn("encoded", item.rss_metadata)
        self.assertIn("guid", item.rss_metadata)

    def test_invalid_google_token_has_explicit_decoder_reason(self):
        from news_research import _decode_google_token
        self.assertEqual(_decode_google_token("https://news.google.com/rss/articles/%not-base64"),
                         (None, "stage=base64url-decode: token is not valid URL-safe base64"))

    def test_valid_protobuf_token_without_url_is_explicitly_unresolved(self):
        from news_research import _decode_google_token
        token = base64.urlsafe_b64encode(b"\x08\x01").decode().rstrip("=")
        self.assertEqual(_decode_google_token("https://news.google.com/rss/articles/" + token),
                         (None, "stage=url-extraction: valid protobuf fields contain no publisher article HTTPS URL"))

    def test_current_opaque_google_token_is_distinguished_from_invalid_protobuf(self):
        from news_research import _decode_google_token
        token = base64.urlsafe_b64encode(b"AU_yqLopaque-current-id").decode().rstrip("=")
        resolved, reason = _decode_google_token("https://news.google.com/rss/articles/" + token)
        self.assertIsNone(resolved)
        self.assertIn("current opaque AU_yqL token", reason)

    def test_rss_discovery_does_not_resolve_each_opaque_article_token(self):
        from unittest.mock import patch
        opaque = base64.urlsafe_b64encode(b"AU_yqLopaque-current-id").decode().rstrip("=")
        items = "".join(f"<item><title>GTA 6 update {i}</title><link>https://news.google.com/rss/articles/{opaque}</link><source url=\"https://ign.com\">IGN</source></item>" for i in range(100))
        xml = f"<rss><channel>{items}</channel></rss>".encode()
        provider = GoogleNewsRSSProvider()
        with patch.object(provider, "_get", return_value=xml), \
             patch("news_research._resolve_google_token", side_effect=AssertionError("discovery must not resolve")):
            started = time.monotonic()
            rows = provider.search("GTA 6")
        self.assertEqual(len(rows), 100)
        self.assertLess(time.monotonic() - started, .5)
        timing = provider._timings["last_search"]
        self.assertEqual(timing["per_result_url_resolution_seconds"], 0.0)
        self.assertEqual(timing["result_count"], 100)
        self.assertIsNone(timing["dns_seconds"])

    def test_telegram_google_news_tokens_are_exact_current_opaque_regressions(self):
        from news_research import _decode_google_token
        urls = (
            "https://news.google.com/rss/articles/CBMic0FVX3lxTE1IM3EzdFh2TDAzLVNIR2R6SDhfa0M0NU1GQkF1QmJOcGFtNF9WcmtZY3RwU29KSkNqcVlmY0FHRUwtNXFENzdTYk9fQ0pYTnFfaFdONGlsanNQaEdmb0VUdUNtQ3dkaG9QWVJCdlJvT2tpZGM?oc=5",
            "https://news.google.com/rss/articles/CBMimwFBVV95cUxQc0RzSjJZQ29Ldjd3R19sakRmSHk0emZ3TmVHNXN5U25pSURPV1dHdEFka1dHY0VyRVpKNzktdGN6aFZOcUdoQThHb3gwYXk3c0tzYk9aYWdjYjhmX1MxNS0yVnVWRDY5OE8tZV9SZzdHNGFxUk5tY1FKamN1dTN1TXQyQ2tkanJVRzJjbFIwNzhQY2JWUkJGVEpmYw?oc=5",
            "https://news.google.com/rss/articles/CBMisAFBVV95cUxNS3JlcTNuT3FGTl93bEQ4M0VDdklhM1B6Z0xCT3E5allSdVZRSTBBLTdBQ0tiUnEyT0gwbWF3ajY2NnNfUm5HalppV0I4cHNOdXlCVHZtUXVOMWFPSjdtUVRJV21iQ1g0M2hRZGh5cTBGdTAtNnJMdDBmNV9TNW1iVjJjeUxvUmpjaTFPLTZIMG5iSlpxVTF4X0VldGhfbWkza1JfdFVkdllKRlRwcS1HNA?oc=5",
        )
        for url in urls:
            with self.subTest(url=url):
                resolved, reason = _decode_google_token(url)
                self.assertIsNone(resolved)
                self.assertEqual(reason, "stage=token-generation: current opaque AU_yqL token requires Google's public batchexecute resolver")

    def test_open_sends_supplied_opaque_token_to_batchexecute_after_signature_page(self):
        import json
        from news_research import _resolve_google_token
        urls = (
            "https://news.google.com/rss/articles/CBMic0FVX3lxTE1IM3EzdFh2TDAzLVNIR2R6SDhfa0M0NU1GQkF1QmJOcGFtNF9WcmtZY3RwU29KSkNqcVlmY0FHRUwtNXFENzdTYk9fQ0pYTnFfaFdONGlsanNQaEdmb0VUdUNtQ3dkaG9QWVJCdlJvT2tpZGM?oc=5",
            "https://news.google.com/rss/articles/CBMimwFBVV95cUxQc0RzSjJZQ29Ldjd3R19sakRmSHk0emZ3TmVHNXN5U25pSURPV1dHdEFka1dHY0VyRVpKNzktdGN6aFZOcUdoQThHb3gwYXk3c0tzYk9aYWdjYjhmX1MxNS0yVnVWRDY5OE8tZV9SZzdHNGFxUk5tY1FKamN1dTN1TXQyQ2tkanJVRzJjbFIwNzhQY2JWUkJGVEpmYw?oc=5",
            "https://news.google.com/rss/articles/CBMisAFBVV95cUxNS3JlcTNuT3FGTl93bEQ4M0VDdklhM1B6Z0xCT3E5allSdVZRSTBBLTdBQ0tiUnEyT0gwbWF3ajY2NnNfUm5HalppV0I4cHNOdXlCVHZtUXVOMWFPSjdtUVRJV21iQ1g0M2hRZGh5cTBGdTAtNnJMdDBmNV9TNW1iVjJjeUxvUmpjaTFPLTZIMG5iSlpxVTF4X0VldGhfbWkza1JfdFVkdllKRlRwcS1HNA?oc=5",
        )
        class Response:
            status = 200
            def __init__(self, body): self.body = body
            def read(self, _): return self.body
            def __enter__(self): return self
            def __exit__(self, *args): pass
        class Opener:
            def __init__(self): self.methods = []
            def open(self, req, timeout):
                self.methods.append(req.get_method())
                if req.get_method() == "GET": return Response(b'<i data-n-a-sg="sig" data-n-a-ts="123">')
                body = json.dumps([["wrb.fr", "Fbv4je", json.dumps(["garturlres", "https://ign.com/articles/story", 1])]])
                return Response(body.encode())
        for url in urls:
            opener = Opener(); diagnostics = {}
            with self.subTest(url=url):
                resolved, error = _resolve_google_token(url, "ign.com", opener_factory=lambda: opener,
                                                        diagnostics=diagnostics)
                self.assertEqual(resolved, "https://ign.com/articles/story")
                self.assertEqual(error, "")
                self.assertEqual(opener.methods, ["GET", "POST"])
                self.assertEqual(diagnostics["batchexecute_http_status"], 200)

    def test_open_invokes_opaque_resolver_only_for_selected_article(self):
        article_record = Article(
            "https://news.google.com/rss/articles/CBMic0FVX3lxTE1IM3EzdFh2TDAzLVNIR2R6SDhfa0M0NU1GQkF1QmJOcGFtNF9WcmtZY3RwS29KSkNqcVlmY0FHRUwtNXFENzdTYk9fQ0pYTnFfaFdONGlsanNQaEdmb0VUdUNtQ3dkaG9QWVJCdlJvT2tpZGM?oc=5",
            "BBC", "GTA 6 update")
        provider = GoogleNewsRSSProvider()
        page = b"<html><body><article><p>GTA 6 update provides new information for fans and players around the world.</p></article></body></html>"
        provider._fetch = lambda url: {"body": page, "final_url": url, "status": 200,
            "content_type": "text/html", "redirect_count": 0, "request_seconds": 0.01}
        with patch("news_research._resolve_google_token", return_value=("https://bbc.com/news/story", "")) as resolve:
            result = provider.open(article_record)
        resolve.assert_called_once()
        self.assertEqual(result.requested_url, "https://bbc.com/news/story")
        self.assertTrue(result.token_decoded)

    def test_google_token_rejects_non_article_and_non_https_urls_inside_valid_protobuf(self):
        from news_research import _decode_google_token
        for target in ("https://ign.com/", "http://ign.com/articles/story", "https://news.google.com/article/x"):
            token = base64.urlsafe_b64encode(b"\x12" + bytes([len(target)]) + target.encode()).decode().rstrip("=")
            self.assertIsNone(_decode_google_token("https://news.google.com/rss/articles/" + token)[0])

    def test_exact_headline_fallback_rejects_homepage_wrong_domain_and_unrelated_title(self):
        from news_research import _fallback_rss_candidates
        xml = b'''<rss><channel>
          <item><title>GTA 6 report</title><link>https://www.pcguide.com/</link></item>
          <item><title>GTA 6 report - PC Guide</title><link>https://unrelated.example/story</link></item>
          <item><title>Another GTA 6 story - PC Guide</title><link>https://www.pcguide.com/news/other/</link></item>
          <item><title>GTA 6 report - PC Guide</title><link>https://www.pcguide.com/news/gta-6-report/</link></item>
          </channel></rss>'''
        article = Article("https://news.google.com/rss/articles/id", "PC Guide", "GTA 6 report",
                          publisher_url="https://www.pcguide.com/")
        candidates, why = _fallback_rss_candidates(article, lambda _: xml)
        self.assertEqual(candidates, ["https://www.pcguide.com/news/gta-6-report/"])
        self.assertTrue(why)

    def test_no_fallback_candidate_and_inaccessible_search_are_reported(self):
        from news_research import _fallback_rss_candidates
        article = Article("https://news.google.com/rss/articles/id", "PC Guide", "GTA 6 report",
                          publisher_url="https://www.pcguide.com/")
        candidates, why = _fallback_rss_candidates(article, lambda _: b"<rss><channel/></rss>")
        self.assertEqual(candidates, [])
        self.assertTrue(any("no verified publisher article candidate found" in reason for reason in why))
        candidates, why = _fallback_rss_candidates(article, lambda _: (_ for _ in ()).throw(TimeoutError()))
        self.assertEqual(candidates, [])
        self.assertIn("public search RSS unavailable: TimeoutError", why)

    def test_bing_apiclick_wrapper_resolves_only_verified_publisher_domain(self):
        from urllib.parse import quote
        from news_research import _fallback_rss_candidates
        headline = "GTA 6 report confirms a major update"
        wrapped = "https://www.bing.com/news/apiclick.aspx?url=" + quote(
            "https://www.pcguide.com/news/gta-6-report/", safe="")
        xml = ("<rss><channel><item><title>" + headline + " - PC Guide</title><link>" + wrapped +
               "</link></item></channel></rss>").encode()
        article = Article("https://news.google.com/rss/articles/unresolved", "PC Guide", headline,
                          publisher_url="https://www.pcguide.com/")
        candidates, reasons = _fallback_rss_candidates(article, lambda _: xml)
        self.assertEqual(candidates, ["https://www.pcguide.com/news/gta-6-report/"])
        self.assertTrue(any("exact headline" in reason for reason in reasons))

    def test_bing_fallback_knows_live_report_publishers_and_constructs_query(self):
        from urllib.parse import parse_qs, urlparse
        from news_research import _fallback_rss_candidates
        for publisher, domain in (("PinkNews", "pinknews.co.uk"), ("Push Square", "pushsquare.com"), ("NME", "nme.com")):
            captured = []
            article = Article("https://news.google.com/rss/articles/token", publisher,
                              "Exact report headline", publisher_url="https://news.google.com/")
            feed = (f"<rss><channel><item><title>Exact report headline - {publisher}</title>"
                    f"<link>https://www.{domain}/news/exact-report/</link></item></channel></rss>").encode()
            candidates, _ = _fallback_rss_candidates(article, lambda url: captured.append(url) or feed)
            self.assertEqual(len(captured), 1)
            self.assertEqual(candidates, [f"https://www.{domain}/news/exact-report/"])
            parsed = urlparse(captured[0])
            self.assertEqual(parsed.netloc, "www.bing.com")
            self.assertEqual(parse_qs(parsed.query)["q"], ['"Exact report headline" ' + publisher])

    def test_exact_fallback_article_is_verified_by_publisher_page_headline(self):
        headline = "GTA 6 report confirms a major update"
        feed = ("<rss><channel><item><title>" + headline + " - PC Guide</title>"
                "<link>https://www.pcguide.com/news/gta-6-report/</link></item></channel></rss>").encode()
        body = ("<html><head><meta property='og:title' content='" + headline +
                "'></head><body><article><p>GTA 6 report confirms a major update for players. " +
                "The story describes new details and the publisher's reporting in context.</p></article></body></html>").encode()
        provider = GoogleNewsRSSProvider()
        provider._get = lambda _: feed
        provider._fetch = lambda url: {"body": body, "final_url": url, "status": 200,
            "content_type": "text/html", "redirect_count": 0, "request_seconds": 0.01}
        article = Article("https://news.google.com/rss/articles/unresolved", "PC Guide", headline,
                          publisher_url="https://www.pcguide.com/")
        result = provider.open(article)
        self.assertTrue(result.candidate_verified)
        self.assertEqual(result.requested_url, "https://www.pcguide.com/news/gta-6-report/")
        self.assertIn("Bing News RSS", result.candidate_discovery[0] if result.candidate_discovery else "")

    def test_open_follows_redirect_extracts_metadata_text_and_canonical_url(self):
        provider = GoogleNewsRSSProvider()
        html = b'''<html><head><meta property="og:title" content="Rockstar announced GTA 6 update"><meta property="article:published_time" content="2026-11-22T12:00:00Z"><link href="https://rockstargames.com/news/gta-update" rel="canonical"></head><body><article><p>Rockstar announced GTA 6 update for fans, with new details about the upcoming game and its planned release schedule.</p></article></body></html>'''
        headers = Message(); headers["Content-Type"] = "text/html"
        response = type("Response", (), {"read": lambda self, n: html, "geturl": lambda self: "https://newswire.example/redirect",
                                          "headers": headers, "status": 200,
                                          "__enter__": lambda self: self, "__exit__": lambda *args: None})()
        def make_opener(counter):
            counter.count = 1
            return type("Opener", (), {"open": lambda self, *args, **kwargs: response})()
        with patch("news_research.build_opener", side_effect=make_opener):
            result = provider.open(Article("https://news.google.com/rss/articles/x", "Google News", "GTA 6"))
        self.assertEqual(result.url, "https://rockstargames.com/news/gta-update")
        self.assertEqual(result.publisher, "rockstargames.com")
        self.assertIn("Rockstar announced", result.evidence)
        self.assertEqual(result.publication_date, "2026-11-22T12:00:00+00:00")
        self.assertEqual((result.http_status, result.final_url, result.redirect_count),
                         (200, "https://newswire.example/redirect", 1))
        from news_research import _article_url_rejection
        self.assertIsNone(_article_url_rejection(result))

    def _response(self, body, *, status=200, url="https://ign.com/story", content_type="text/html; charset=utf-8"):
        headers = Message(); headers["Content-Type"] = content_type
        return type("Response", (), {"read": lambda self, n: body, "geturl": lambda self: url,
            "status": status, "headers": headers, "__enter__": lambda self: self,
            "__exit__": lambda *args: None})()

    def test_html_article_page_is_extracted_and_reports_http_metadata(self):
        body = b'<html><head><link rel="canonical" href="https://ign.com/canonical"></head><body><article><p>GTA 6 update reveals new information for fans and players around the world.</p></article></body></html>'
        response = self._response(body, url="https://ign.com/redirected")
        with patch("news_research.build_opener", return_value=type("Opener", (), {"open": lambda *_, **__: response})()):
            result = GoogleNewsRSSProvider().open(Article("https://ign.com/start", "IGN", "GTA 6 update"))
        self.assertTrue(result.accessible)
        self.assertEqual(result.url, "https://ign.com/canonical")
        self.assertEqual((result.http_status, result.response_kind), (200, "HTML"))

    def test_jsonld_article_body_is_extracted_and_diagnostics_are_recorded(self):
        text = ("Rockstar announced a GTA 6 release update and explained the studio's plans. "
                "The article describes what players can expect from the upcoming game.")
        body = ('<html><head><script type="application/ld+json">' +
                __import__("json").dumps({"@type": "NewsArticle", "articleBody": text}) +
                '</script></head><body></body></html>').encode()
        response = self._response(body)
        with patch("news_research.build_opener", return_value=type("Opener", (), {"open": lambda *_, **__: response})()):
            result = GoogleNewsRSSProvider().open(Article("https://ign.com/story", "IGN", "GTA 6 release update"))
        self.assertTrue(result.accessible)
        self.assertEqual(result.extraction_method, "json-ld articleBody")
        self.assertGreaterEqual(result.extracted_text_length, 60)

    def test_semantic_article_text_is_preferred_over_page_chrome(self):
        text = "GTA 6 update: Rockstar explained the new release plans for players. " * 2
        response = self._response(("<html><body><nav>GTA 6 navigation links</nav><article><p>" + text +
                                   "</p></article></body></html>").encode())
        with patch("news_research.build_opener", return_value=type("Opener", (), {"open": lambda *_, **__: response})()):
            result = GoogleNewsRSSProvider().open(Article("https://ign.com/story", "IGN", "GTA 6 release plans"))
        self.assertTrue(result.accessible)
        self.assertEqual(result.extraction_method, "semantic article/main")
        self.assertIn("Rockstar explained", result.evidence)

    def test_empty_or_irrelevant_body_is_not_article_evidence(self):
        for body in (b"<html><body></body></html>",
                     b"<html><article>Completely unrelated discussion about gardening and flowers. "
                     b"Long text with no relevant facts for this reported video game story.</article></html>"):
            with self.subTest(body=body):
                response = self._response(body)
                with patch("news_research.build_opener", return_value=type("Opener", (), {"open": lambda *_, **__: response})()):
                    result = GoogleNewsRSSProvider().open(Article("https://ign.com/story", "IGN", "GTA 6 release plans"))
                self.assertFalse(result.accessible)
                self.assertIsNone(result.evidence)

    def test_google_rss_decoded_publisher_url_is_the_fetch_target(self):
        body = b"<html><body><p>GTA 6 update reveals new information for fans and players around the world.</p></body></html>"
        response = self._response(body, url="https://ign.com/articles/gta-6")
        opened = []
        def open_target(self, request, **kwargs):
            opened.append(request.full_url)
            return response
        article_record = Article("https://news.google.com/rss/articles/current-id", "IGN", "GTA 6 update",
                                 original_source_url="https://ign.com/articles/gta-6")
        with patch("news_research.build_opener", return_value=type("Opener", (), {"open": open_target})()):
            result = GoogleNewsRSSProvider().open(article_record)
        self.assertEqual(opened, ["https://ign.com/articles/gta-6"])
        self.assertEqual(result.publisher, "ign.com")

    def test_non_html_is_rejected_with_exact_reason(self):
        response = self._response(b'{"error":"no article"}', content_type="application/json")
        with patch("news_research.build_opener", return_value=type("Opener", (), {"open": lambda *_, **__: response})()):
            result = GoogleNewsRSSProvider().open(Article("https://ign.com/story", "IGN", "GTA 6 update"))
        self.assertFalse(result.accessible)
        self.assertEqual(result.rejection_reason, "expected HTML article page, received JSON")

    def test_http_403_and_429_are_reported_without_retry_or_bypass(self):
        for status in (403, 429):
            with self.subTest(status=status):
                err = HTTPError("https://ign.com/story", status, "blocked", {"Content-Type": "text/html"}, io.BytesIO(b"<html>Access denied</html>"))
                with patch("news_research.build_opener", return_value=type("Opener", (), {"open": lambda *_, **__: (_ for _ in ()).throw(err)})()):
                    result = GoogleNewsRSSProvider().open(Article("https://ign.com/story", "IGN", "GTA 6"))
                self.assertEqual(result.http_status, status)
                self.assertIn(f"HTTP {status}", result.rejection_reason)

    def test_captcha_and_consent_pages_are_rejected(self):
        for text, expected in (("Please complete CAPTCHA", "CAPTCHA page"),
                               ("Before you continue to Google consent.google.com", "consent page")):
            with self.subTest(text=text):
                response = self._response(f"<html><body>{text}</body></html>".encode())
                with patch("news_research.build_opener", return_value=type("Opener", (), {"open": lambda *_, **__: response})()):
                    result = GoogleNewsRSSProvider().open(Article("https://ign.com/story", "IGN", "GTA 6"))
                self.assertEqual(result.rejection_reason,
                                 "publisher_captcha" if expected == "CAPTCHA page" else expected)

    def test_extraction_rejection_reason_is_specific(self):
        response = self._response(b"<html><body><p>Navigation and cookie settings only.</p></body></html>")
        with patch("news_research.build_opener", return_value=type("Opener", (), {"open": lambda *_, **__: response})()):
            result = GoogleNewsRSSProvider().open(Article("https://ign.com/story", "IGN", "GTA 6 announcement"))
        self.assertFalse(result.accessible)
        self.assertIn("no sufficiently long text", result.rejection_reason)

    def test_page_without_article_evidence_is_inaccessible(self):
        provider = GoogleNewsRSSProvider()
        response = type("Response", (), {"read": lambda self, n: b"<html><body>Cookie settings and navigation.</body></html>",
                                          "geturl": lambda self: "https://ign.com/story", "__enter__": lambda self: self,
                                          "__exit__": lambda *args: None})()
        with patch("news_research.urlopen", return_value=response):
            result = provider.open(Article("https://news.google.com/x", "Google News", "GTA 6 announcement"))
        self.assertFalse(result.accessible)
        self.assertIsNone(result.evidence)

    def test_latest_prioritizes_recent_articles_and_omits_historical_results(self):
        old = Article("https://ign.com/old", "IGN", "GTA 6 announced historical detail", "2023-12-04T00:00:00+00:00", evidence="Article text")
        recent = Article("https://ign.com/new", "IGN", "GTA 6 announced new update", "2026-11-22T00:00:00+00:00", evidence="Article text")
        report = research_news("latest GTA 6 news", FakeProvider([old, recent]))
        self.assertEqual([s.headline for i in report.items for s in i.sources], [recent.headline])

    def test_official_announcement_and_rumor_are_classified_separately(self):
        sources = [article("rockstargames.com", "Rockstar announced GTA 6 update", "Rockstar Games"),
                   article("ign.com", "Rumor: GTA 6 leaked release date", "IGN")]
        report = research_news("GTA 6", FakeProvider(sources))
        self.assertEqual(report.items[0].status, "CONFIRMED")
        self.assertEqual(report.items[0].attribution, "Official announcement")
        self.assertEqual(report.items[1].status, "UNVERIFIED")
        self.assertEqual(report.items[1].attribution, "Rumor/leak")

    def test_claims_keep_different_evidence_statuses(self):
        report = research_news("GTA 6", FakeProvider([
            article("rockstargames.com", "Rockstar announced trailer", "Rockstar Games"),
            article("random.example", "GTA 6 leak claims map details"),
        ]))
        self.assertEqual([i.status for i in report.items], ["CONFIRMED", "UNVERIFIED"])

    def test_timeout_is_bounded_and_retry_count_limited(self):
        provider = GoogleNewsRSSProvider(timeout=80, retries=8)
        self.assertEqual(provider.timeout, 15)
        self.assertEqual(provider.retries, 1)
        with patch("news_research.urlopen", side_effect=URLError("offline")) as request, patch("news_research.time.sleep"):
            with self.assertRaises(URLError): provider._get("https://example.com")
        self.assertEqual(request.call_count, 2)

    def test_partial_results_keep_accessible_publisher(self):
        class Partial(FakeProvider):
            def open(self, a):
                if "bad.example" in a.url: return Article(a.url, a.publisher, a.headline, evidence=None, accessible=False)
                return super().open(a)
        report = research_news("GTA 6", Partial([article("ign.com", "GTA 6 announced update"),
                                                 article("bad.example", "GTA 6 details leaked")]))
        self.assertEqual(report.metrics["accessible_articles"], 1)
        self.assertTrue(any(s.accessible for i in report.items for s in i.sources))

    def test_total_execution_time_is_reported(self):
        report = research_news("GTA 6", FakeProvider([article("ign.com", "GTA 6 announced update")]))
        self.assertGreaterEqual(report.metrics["total_elapsed_seconds"], 0)
        self.assertIn("total", format_report(report))

    def test_timing_labels_separate_wall_clock_and_summed_requests(self):
        report = research_news("GTA 6", FakeProvider([article("ign.com", "GTA 6 announced update")]))
        self.assertIn("article retrieval wall", format_report(report))
        self.assertIn("request durations summed", format_report(report))
        self.assertIn("extraction wall", format_report(report))
        self.assertIn("total wall", format_report(report))

    def test_latest_request_with_missing_dates_does_not_claim_latest(self):
        report = research_news("latest GTA 6 news", FakeProvider([article("ign.com", "GTA 6 announced update", date=None)]))
        rendered = format_report(report)
        self.assertNotIn("Latest relevant developments", rendered)
        self.assertIn("publication dates were unavailable", rendered)

    def test_no_accessible_pages_do_not_become_empty_article_entries(self):
        class Offline(FakeProvider):
            def open(self, a): return Article(a.url, a.publisher, a.headline, accessible=False, evidence=None)
        report = research_news("GTA 6", Offline([article("bad.example", "GTA 6 announcement")]))
        self.assertEqual(report.items, [])
        self.assertIn("could not be retrieved", report.summary)
        self.assertIn("bad.example", format_report(report))  # diagnostic attempt only, not a cited article

    def test_publisher_homepage_is_rejected_as_non_article_with_reason(self):
        provider = FakeProvider([Article("https://www.ndtv.com/", "NDTV", "NDTV reports breaking news today",
            "2026-11-22T10:00:00+00:00")])
        report = research_news("breaking news", provider, latest=False)
        self.assertEqual(report.items, [])
        self.assertEqual(report.metrics["articles_rejected"][0]["stage"], "discovery-quality")
        self.assertIn("homepage", report.metrics["articles_rejected"][0]["rejection_reason"])

    def test_inaccessible_evidence_is_not_upgraded_to_confirmed(self):
        class Inaccessible(FakeProvider):
            def open(self, article):
                return Article(article.url, article.publisher, article.headline,
                    article.publication_date, accessible=False, rejection_reason="HTTP 403: access denied")
        report = research_news("specific claim", Inaccessible([Article(
            "https://reuters.com/article/123", "Reuters", "Officials confirm specific claim",
            "2026-11-22T10:00:00+00:00")]), latest=False)
        self.assertEqual(report.items, [])
        self.assertEqual(report.metrics["verification_outcomes"], {"UNABLE_TO_VERIFY": 1})
        self.assertTrue(any("403" in reason for reason in report.metrics["verification_failure_reasons"]))

    def test_discovery_commands_in_english_hinglish_and_devanagari(self):
        cases = ["Fetch the latest news about GTA 6", "GTA 6 ki latest news batao",
                 "GTA 6 की ताज़ा खबरें बताओ।", "Nintendo ki latest news batao"]
        for phrase in cases:
            with self.subTest(phrase=phrase):
                job = process_command(phrase)
                self.assertEqual(job.intent, Intent.NEWS_DISCOVERY)
                self.assertEqual(job.topic_or_claim, "Nintendo" if phrase.startswith("Nintendo") else "GTA 6")
                self.assertNotIn("Which output format", " ".join(job.clarification_needed))

    def test_specific_claim_is_retained_for_verification(self):
        job = process_command("Verify this news I heard: GTA 6 is delayed again")
        self.assertEqual(job.intent, Intent.NEWS_VERIFICATION)
        self.assertIn("GTA 6 is delayed again", job.topic_or_claim)
        provider = FakeProvider([article("ign.com", "GTA 6 delayed again", "IGN")])
        report = research_news(job.topic_or_claim, provider, verify_claim=True)
        self.assertEqual(report.items[0].claim, job.topic_or_claim)
        self.assertTrue(provider.queries[0].startswith(job.topic_or_claim))

    def test_unrelated_primary_announcement_does_not_confirm_specific_claim(self):
        provider = FakeProvider([article("rockstargames.com", "Rockstar announced a new trailer", "Rockstar Games")])
        report = research_news("GTA 6 is delayed again", provider, verify_claim=True)
        self.assertEqual(report.items[0].status, "UNVERIFIED")
        self.assertIn("specific claim", report.items[0].confirmed)

    def test_official_announcement_question_is_verification(self):
        job = process_command("Is this GTA 6 announcement official?")
        self.assertEqual(job.intent, Intent.NEWS_VERIFICATION)
        self.assertIn("GTA 6 announcement", job.topic_or_claim)

    def test_primary_sources_are_prioritized_and_can_confirm(self):
        provider = FakeProvider([
            article("ign.com", "Rockstar announced a release date", "IGN"),
            article("rockstargames.com", "Rockstar announced a release date", "Rockstar Games"),
        ])
        report = research_news("GTA 6", provider)
        self.assertTrue(provider.opened[0].endswith("rockstargames.com/story"))
        self.assertEqual(report.items[0].status, "CONFIRMED")

    def test_duplicate_syndicated_headlines_are_deduplicated(self):
        provider = FakeProvider([article("reuters.com", "Studio announces release date", "Reuters"),
                                 article("apnews.com", "Studio announces release date", "AP")])
        report = research_news("Studio", provider)
        self.assertEqual(len(report.items), 1)
        self.assertEqual(len(report.items[0].sources), 1)

    def test_near_duplicate_syndicated_headlines_are_not_independent_sources(self):
        provider = FakeProvider([article("reuters.com", "Studio announces release date for highly anticipated game", "Reuters"),
                                 article("apnews.com", "Studio announces the release date for highly anticipated game", "AP")])
        report = research_news("Studio", provider)
        self.assertEqual(len(report.items[0].sources), 1)

    def test_missing_publication_date_is_reported(self):
        report = research_news("Studio", FakeProvider([article("ign.com", "Studio announces update", date=None)]))
        self.assertIn("publication date unavailable", format_report(report))

    def test_conflicting_reliable_sources_are_disputed(self):
        provider = FakeProvider([article("reuters.com", "Studio confirmed the game is delayed", "Reuters"),
                                 article("bbc.com", "Studio denied delay and says date unchanged", "BBC")])
        report = research_news("Studio delay", provider, verify_claim=True)
        self.assertEqual(report.items[0].status, "DISPUTED")

    def test_rumors_and_leaks_remain_unverified(self):
        report = research_news("GTA 6", FakeProvider([article("ign.com", "Rumor: GTA 6 leaked release date", "IGN")]))
        self.assertEqual(report.items[0].status, "UNVERIFIED")
        self.assertEqual(report.items[0].attribution, "Rumor/leak")

    def test_developing_requires_no_primary_or_isolated_reporting(self):
        report = research_news("Studio", FakeProvider([article("ign.com", "Studio confirmed an update", "IGN")]))
        self.assertEqual(report.items[0].status, "DEVELOPING")

    def test_four_statuses_are_representable(self):
        cases = [
            ([article("rockstargames.com", "Rockstar confirmed GTA 6 delayed", "Rockstar Games")], "CONFIRMED"),
            ([article("ign.com", "GTA 6 announced delay update", "IGN")], "DEVELOPING"),
            ([article("reuters.com", "GTA 6 confirmed delay", "Reuters"), article("bbc.com", "GTA 6 denied delay", "BBC")], "DISPUTED"),
            ([article("random.example", "Someone claims an update")], "UNVERIFIED"),
        ]
        for sources, expected in cases:
            with self.subTest(status=expected):
                self.assertEqual(research_news("GTA 6 delay", FakeProvider(sources), verify_claim=True).items[0].status, expected)

    def test_empty_results_and_provider_failure_are_transparent(self):
        empty = research_news("nothing", FakeProvider())
        failed = research_news("nothing", FakeProvider(fail=True))
        self.assertFalse(empty.items)
        self.assertIn("No search results", empty.summary)
        self.assertIn("search failed", failed.summary)

    def test_telegram_formats_research_and_asks_for_output_format(self):
        provider = FakeProvider([article("ign.com", "Studio announced an update", "IGN")])
        text = telegram.build_reply("Fetch news about Studio", news_provider=provider)
        self.assertIn("News research: Studio", text)
        self.assertIn("DEVELOPING", text)
        self.assertIn("https://ign.com/story", text)
        self.assertNotIn("Which output format do you want", text)

    def test_telegram_preserves_latest_intent_after_topic_parsing(self):
        old = Article("https://ign.com/old", "IGN", "GTA 6 historical report", "2023-12-04T00:00:00+00:00", evidence="Article text")
        recent = Article("https://ign.com/new", "IGN", "GTA 6 announced update", "2026-11-22T00:00:00+00:00", evidence="Article text")
        reply = telegram.build_reply("Fetch the latest news about GTA 6", news_provider=FakeProvider([old, recent]))
        self.assertIn("GTA 6 announced update", reply)
        self.assertNotIn("GTA 6 historical report", reply)
        self.assertIn("Latest relevant developments", reply)

    def test_voice_news_uses_same_research_integration(self):
        provider = FakeProvider([article("ign.com", "Studio announced an update", "IGN")])
        update = {"update_id": 991, "message": {"voice": {"file_id": "f"},
                  "chat": {"id": 1, "type": "private"}, "from": {"id": 1}}}
        with patch.object(telegram, "_download_voice", return_value=__import__("pathlib").Path("voice.oga")), \
             patch.object(telegram, "transcribe_voice", return_value="Fetch news about Studio"), \
             patch.object(telegram, "_telegram_request") as request:
            telegram._reply_to_update("token", update, "1", set(), provider)
        self.assertIn("Recognized transcript", request.call_args.args[2]["text"])
        self.assertIn("News research: Studio", request.call_args.args[2]["text"])

    def test_news_path_never_calls_video_pipeline(self):
        provider = FakeProvider([article("ign.com", "Studio announced an update", "IGN")])
        with patch("video_pipeline.process_video", side_effect=AssertionError("must not execute")) as video:
            telegram.build_reply("Fetch news about Studio", news_provider=provider)
        video.assert_not_called()


if __name__ == "__main__": unittest.main()
