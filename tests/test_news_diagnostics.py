import socket
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import news_research as news


class _FakeSocket:
    def __init__(self, failure=None): self.failure = failure
    def settimeout(self, value): pass
    def connect(self, address):
        if self.failure: raise self.failure
    def close(self): pass
    def sendall(self, data): pass


class _FakeResponse:
    def __init__(self, status=200, body=b"<rss><channel></channel></rss>", failure=None):
        self.status, self.body, self.failure = status, body, failure
    def begin(self):
        if self.failure == "ttfb": raise TimeoutError("first byte timeout")
    def getheader(self, name): return "application/rss+xml"
    def read(self, limit):
        if self.failure == "body": raise TimeoutError("body timeout")
        return self.body[:limit]


class NewsDiagnosticTests(unittest.TestCase):
    def test_explicit_url_diagnostic_exposes_provider_results_without_inference(self):
        independent = "https://news.google.com/rss/articles/CBMiiAFBVV95cUxNdW9aZ3o1S3I4RVZfX016SXRVZkM1RER0VmY4Rm9udDZWM2lGUEcwSERkY0x3ejJNODNqd2tFQVhtRmUzdXV0VmE0TEZiZ2hXb0I4TVRNNTlKb0JITlRFbWpBX20tNW9wZVRxTHlIMi1oRUZWQ0RpMUU4UzVHbHJyUURQVlBfcFVN"
        redbull = "https://news.google.com/rss/articles/CBMiaEFVX3lxTE9kVjVUSDRhOFdEeDNEM0J3Wm9IdFNjWDNEdm1FQUdRMXZvUW9YaC1Vek5ZZHRuTmtvWkY0anVaV282aXVLMl9rTHd3bVpDM1lvZjIzQ2szVEtlLS1QaGxoZ2pwV3ZkZmZk"
        class Provider:
            def open(self, article):
                if article.publisher == "The Independent":
                    return news.Article(url=article.url, publisher=article.publisher, headline=article.headline,
                        publisher_url="https://www.independent.co.uk/story", evidence=None, accessible=False,
                        candidate_verified=False, rejection_reason="publisher_captcha", resolver_diagnostics={
                            "validated_publisher_url": "https://www.independent.co.uk/story",
                            "failure_stage": "article-retrieval", "failure": "publisher_captcha",
                            "signature_page_classification": "resolver attributes extracted"})
                return news.Article(url=article.url, publisher=article.publisher, headline=article.headline,
                    evidence=None, accessible=False, candidate_verified=False,
                    rejection_reason="google_rate_limited", resolver_diagnostics={
                        "failure": "google_rate_limited", "signature_page_classification": "google_rate_limited",
                        "retry_after_seconds": 3600, "batchexecute_request": "not started"})
        result = news.diagnose_explicit_google_urls([independent, redbull], Provider())
        first, second = result["articles"]
        self.assertEqual(first["validated_publisher_url"], "https://www.independent.co.uk/story")
        self.assertFalse(first["evidence_available"])
        self.assertFalse(first["candidate_verified"])
        self.assertEqual(first["failure_stage"], "article-retrieval")
        self.assertEqual(first["failure_reason"], "publisher_captcha")
        self.assertIsNone(second["validated_publisher_url"])
        self.assertFalse(second["evidence_available"])
        self.assertFalse(second["candidate_verified"])
        self.assertEqual(second["signature_page_classification"], "google_rate_limited")
        self.assertEqual(second["retry_after_seconds"], 3600)
        self.assertNotIn("batchexecute", second)

    def test_explicit_google_urls_are_isolated_and_safe(self):
        urls = [
            "https://news.google.com/rss/articles/CBMiiAFBVV95cUxNdW9aZ3o1S3I4RVZfX016SXRVZkM1RER0VmY4Rm9udDZWM2lGUEcwSERkY0x3ejJNODNqd2tFQVhtRmUzdXV0VmE0TEZiZ2hXb0I4TVRNNTlKb0JITlRFbWpBX20tNW9wZVRxTHlIMi1oRUZWQ0RpMUU4UzVHbHJyUURQVlBfcFVN?oc=5&secret=hide",
            "https://news.google.com/rss/articles/CBMiaEFVX3lxTE9kVjVUSDRhOFdEeDNEM0J3Wm9IdFNjWDNEdm1FQUdRMXZvUW9YaC1Vek5ZZHRuTmtvWkY0anVaV282aXVLMl9rTHd3bVpDM1lvZjIzQ2szVEtlLS1QaGxoZ2pwV3ZkZmZk?oc=5",
        ]
        class Provider:
            def __init__(self): self.calls = []
            def open(self, article):
                self.calls.append(article.url)
                if len(self.calls) == 1: raise TimeoutError("secret response detail")
                diag = {"signature_request": "started", "signature_response": "received",
                        "signature_extracted": True, "timestamp_extracted": True,
                        "batchexecute_request": "started", "batchexecute_response": "received",
                        "response_parsed": True, "url_validation": "accepted",
                        "publisher_domain_valid": True, "article_extraction": "article text extracted"}
                return news.Article(url=article.url, publisher=article.publisher, headline=article.headline,
                                    resolver_diagnostics=diag, http_status=200, response_kind="HTML",
                                    extraction_method="semantic article/main", extracted_text_length=245,
                                    accessible=True)
        provider = Provider()
        result = news.diagnose_explicit_google_urls(urls, provider)
        self.assertEqual(len(provider.calls), 2)
        first, second = result["articles"]
        self.assertEqual(first["failure_stage"], "article-diagnostic")
        self.assertEqual(first["safe_exception_type"], "TimeoutError")
        self.assertEqual(second["resolver"]["article_extraction"], "article text extracted")
        self.assertEqual(second["expected_publisher_domain"], "redbull.com")
        self.assertNotIn("secret response detail", repr(result))
        self.assertNotIn("hide", repr(result))
        self.assertNotIn("?oc=5", repr(result))

    def test_production_discovery_runs_bound_methods_sequentially_and_concurrently(self):
        class Discovery:
            def __init__(self, count): self.count, self.calls, self._timings = count, 0, {}
            def search(self, query):
                self.calls += 1
                return [object()] * self.count
        rss, official, publishers = Discovery(2), Discovery(1), Discovery(3)
        provider = news.MultiSourceNewsProvider([], rss, official, publishers)
        result = news.diagnose_production_discovery("topic", provider)
        expected = {"Google News RSS (discovery)": 2, "Official sites (discovery)": 1,
                    "Independent publishers (discovery)": 3}
        for mode in ("sequential", "concurrent"):
            self.assertEqual({name: item["result_count"] for name, item in result[mode].items()}, expected)
        self.assertEqual((rss.calls, official.calls, publishers.calls), (2, 2, 2))

    def test_production_diagnostic_records_sanitized_provider_failure_reason(self):
        class Discovery:
            _timings = {}
            def search(self, query): raise ValueError("secret in details")
        provider = news.MultiSourceNewsProvider([], Discovery(), Discovery(), Discovery())
        result = news.diagnose_production_discovery("topic", provider)
        item = result["sequential"]["Google News RSS (discovery)"]
        self.assertEqual(item["exception_type"], "ValueError")
        self.assertEqual(item["safe_failure_reason"], "provider returned invalid or incomplete data")
        self.assertEqual(item["failure_stage"], "response parsing or provider data")
        self.assertNotIn("secret", repr(result))

    def test_production_diagnostic_reports_http_status_without_body_or_headers(self):
        class Discovery:
            _timings = {}
            def search(self, query):
                raise HTTPError("https://news.google.com/", 429, "Too Many Requests", {"X-Secret": "credential"}, None)
        provider = news.MultiSourceNewsProvider([], Discovery(), Discovery(), Discovery())
        result = news.diagnose_production_discovery("topic", provider)
        item = result["sequential"]["Google News RSS (discovery)"]
        self.assertEqual(item["http_status"], 429)
        self.assertEqual(item["safe_failure_reason"], "provider rate limited the request")
        self.assertNotIn("safe_response_detail", item)
        self.assertNotIn("credential", repr(result))

    def test_concurrent_diagnostic_timestamps_started_workers_and_names_queued_tasks(self):
        class Discovery:
            _timings = {}
            def search(self, query): time.sleep(.04); return []
        provider = news.MultiSourceNewsProvider([], Discovery(), Discovery(), Discovery())
        with patch.object(news, "SEARCH_WAIT_SECONDS", .005):
            result = news.diagnose_production_discovery("topic", provider)
        for item in result["concurrent"].values():
            self.assertEqual(item["status"], "application deadline")
            self.assertIn(item["worker_state"], ("running", "queued/not started"))
            if item["worker_state"] == "running": self.assertIsNotNone(item["started_monotonic"])
            else: self.assertIsNone(item["started_monotonic"])
        self.assertEqual(result["executor_max_workers"], 3)
        self.assertEqual(result["submitted_task_count"], 3)

    def _probe(self, *, dns=None, tcp=None, tls=None, response=None):
        def getaddrinfo(*args, **kwargs):
            if dns: raise dns
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        sock = _FakeSocket(tcp)
        def wrap_socket(raw, server_hostname):
            if tls: raise tls
            return raw
        response = response or _FakeResponse()
        with patch.object(news.socket, "getaddrinfo", side_effect=getaddrinfo), \
             patch.object(news.socket, "socket", return_value=sock), \
             patch.object(news.ssl, "create_default_context") as context, \
             patch.object(news.http.client, "HTTPResponse", return_value=response):
            context.return_value.wrap_socket.side_effect = wrap_socket
            return news._diagnostic_get("https://example.test/rss", connect_timeout=.05, read_timeout=.05)

    def test_dns_failure_is_measured_and_classified(self):
        body, report = self._probe(dns=socket.gaierror("name lookup failed"))
        self.assertEqual(body, b"")
        self.assertEqual(report["dns_outcome"], "failed (gaierror)")
        self.assertEqual(report["exception"]["stage"], "DNS")
        self.assertIsNotNone(report["dns_seconds"])

    def test_slow_dns_is_bounded_and_not_mislabeled_as_tcp_failure(self):
        def slow(*args, **kwargs): time.sleep(.1); return []
        with patch.object(news.socket, "getaddrinfo", side_effect=slow):
            _, report = news._diagnostic_get("https://example.test/", connect_timeout=.01, read_timeout=.01)
        self.assertEqual(report["exception"]["stage"], "DNS")
        self.assertIn("TimeoutError", report["exception"]["type"])

    def test_tcp_tls_ttfb_and_body_failures_name_their_stage(self):
        for kwargs, stage in (({"tcp": TimeoutError("connect")}, "TCP"),
                              ({"tls": ssl_error()}, "TLS"),
                              ({"response": _FakeResponse(failure="ttfb")}, "response/body"),
                              ({"response": _FakeResponse(failure="body")}, "response/body")):
            with self.subTest(stage=stage):
                _, report = self._probe(**kwargs)
                self.assertEqual(report["exception"]["stage"], stage)

    def test_http_status_and_empty_body_are_reported_without_network_timeout(self):
        for status in (403, 429, 500, 200):
            body, report = self._probe(response=_FakeResponse(status=status, body=b""))
            self.assertEqual(report["status"], status)
            self.assertEqual(report["response_bytes"], 0)
            self.assertEqual(report["content_type"], "application/rss+xml")
            self.assertIsNone(report["exception"])

    def test_provider_diagnostic_parses_results_and_malformed_xml(self):
        xml = b"<rss><channel><item><title>News</title><link>https://news.google.com/a</link></item></channel></rss>"
        with patch.object(news, "_diagnostic_get", return_value=(xml, {"status": 200})):
            reports = news.diagnose_discovery_providers("topic", connect_timeout=1, read_timeout=1)
        self.assertEqual(len(reports), 3)
        self.assertTrue(all(report["results_extracted"] == 1 for report in reports.values()))
        with patch.object(news, "_diagnostic_get", return_value=(b"<rss", {"status": 200})):
            reports = news.diagnose_discovery_providers("topic", connect_timeout=1, read_timeout=1)
        self.assertTrue(all(report["parse_outcome"] == "failed" for report in reports.values()))

    def test_application_deadline_is_not_reported_as_network_timeout(self):
        class Slow:
            _timings = {}
            def search(self, query): time.sleep(.08); return []
        provider = news.MultiSourceNewsProvider([], Slow(), Slow(), Slow())
        with patch.object(news, "SEARCH_WAIT_SECONDS", .005): provider.search("topic")
        statuses = list(provider.availability.values())
        self.assertTrue(all(value.startswith("APPLICATION DEADLINE") for value in statuses))
        self.assertTrue(all("network outcome unknown" in value for value in statuses))

    def test_real_discovery_path_marks_network_stall_for_each_independent_provider(self):
        entered = []
        release = threading.Event()
        def blocked_request(_url):
            entered.append(time.monotonic())
            release.wait(.2)
            return b"<rss><channel></channel></rss>"
        provider = news.MultiSourceNewsProvider(rss=news.GoogleNewsRSSProvider())
        with patch.object(provider.rss, "_get", side_effect=blocked_request), \
             patch.object(news, "SEARCH_WAIT_SECONDS", .03):
            provider.search("GTA 6 news")
        release.set()
        self.assertEqual(len(entered), 3)
        discovery = {name: value for name, value in provider.discovery_diagnostics.items()
                     if name.endswith("(discovery)")}
        self.assertEqual(len(discovery), 3)
        for diagnostic in discovery.values():
            stages = [event["stage"] for event in diagnostic["stage_events"]]
            self.assertIn("task submitted", stages)
            self.assertIn("task started", stages)
            self.assertIn("network request started", stages)
            self.assertIn("application deadline", stages)
            self.assertNotIn("response received", stages)
            self.assertEqual(diagnostic["status"], "still running at overall deadline")

    def test_real_discovery_path_reports_response_parse_and_task_completion(self):
        xml = b"<rss><channel><item><title>News</title><link>https://news.google.com/a</link></item></channel></rss>"
        class Response:
            status = 200
            headers = {"Content-Type": "application/rss+xml"}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, _limit): return xml
        provider = news.MultiSourceNewsProvider(rss=news.GoogleNewsRSSProvider())
        with patch("news_research.urlopen", return_value=Response()):
            provider.search("GTA 6 news")
        for name, diagnostic in provider.discovery_diagnostics.items():
            if not name.endswith("(discovery)"):
                continue
            stages = [event["stage"] for event in diagnostic["stage_events"]]
            self.assertIn("network request started", stages)
            self.assertIn("response received", stages)
            self.assertIn("parsing completed", stages)
            self.assertIn("task completed", stages)
            self.assertEqual(diagnostic["status"], "completed")


def ssl_error():
    import ssl
    return ssl.SSLError("handshake failed")


if __name__ == "__main__": unittest.main()
