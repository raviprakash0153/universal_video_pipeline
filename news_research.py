"""Lightweight, replaceable news search and evidence assessment.

The default provider uses Google News RSS for discovery, then fetches article
pages for evidence. RSS snippets are never treated as verification evidence.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import json
import os
import re
import socket
import ssl
import http.client
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urljoin, urlparse, parse_qsl, urlencode, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen, HTTPCookieProcessor
from http.cookiejar import CookieJar
import xml.etree.ElementTree as ET


STATUSES = {"CONFIRMED", "DEVELOPING", "DISPUTED", "UNVERIFIED"}
OFFICIAL_DOMAINS = {"rockstargames.com", "take2games.com", "rockstarnewswire.com"}
TRUSTED_PUBLISHERS = {
    "bbc.com", "reuters.com", "apnews.com", "theguardian.com", "nytimes.com",
    "ign.com", "gamespot.com", "pcgamer.com", "eurogamer.net", "polygon.com",
    "kotaku.com", "venturebeat.com", "gamesindustry.biz", "theverge.com",
}
SEARCH_WAIT_SECONDS = 20  # Overall discovery deadline; individual HTTP calls use provider timeouts.
ARTICLE_WAIT_SECONDS = 35
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 8.0


def _diagnostic_get(url: str, *, connect_timeout: float, read_timeout: float) -> tuple[bytes, dict]:
    """Perform one measured HTTPS GET without logging request headers or query values."""
    result = {"dns_seconds": None, "dns_outcome": "not measured", "tcp_seconds": None,
              "tcp_outcome": "not attempted", "tls_seconds": None, "tls_outcome": "not attempted",
              "ttfb_seconds": None, "status": None, "content_type": None,
              "body_seconds": None, "response_bytes": 0, "exception": None}
    parsed = urlparse(url)
    host, path = parsed.hostname, parsed.path + (("?" + parsed.query) if parsed.query else "")
    sock = None
    try:
        began = time.monotonic()
        resolved = []
        resolver_error = []
        resolver_done = threading.Event()
        def resolve():
            try: resolved.extend(socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM))
            except Exception as exc: resolver_error.append(exc)
            finally: resolver_done.set()
        threading.Thread(target=resolve, name="news-diagnostic-dns", daemon=True).start()
        if not resolver_done.wait(connect_timeout):
            result["dns_seconds"] = time.monotonic() - began
            raise TimeoutError("DNS resolution exceeded diagnostic connect timeout")
        result["dns_seconds"] = time.monotonic() - began
        if resolver_error: raise resolver_error[0]
        addresses = resolved
        result["dns_outcome"] = f"resolved ({len(addresses)} address(es))"
        last_error = None
        began = time.monotonic()
        for family, socktype, proto, _, sockaddr in addresses:
            candidate = socket.socket(family, socktype, proto)
            candidate.settimeout(connect_timeout)
            try:
                candidate.connect(sockaddr); sock = candidate; break
            except OSError as exc:
                candidate.close(); last_error = exc
        result["tcp_seconds"] = time.monotonic() - began
        if sock is None:
            raise last_error or OSError("no usable address")
        result["tcp_outcome"] = "connected"
        began = time.monotonic()
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        result["tls_seconds"] = time.monotonic() - began
        result["tls_outcome"] = "handshake complete"
        sock.settimeout(read_timeout)
        sock.sendall((f"GET {path or '/'} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: UniversalVideoPipelineNews/2.0\r\nAccept: application/rss+xml, application/xml, text/xml\r\nConnection: close\r\n\r\n").encode("ascii"))
        response = http.client.HTTPResponse(sock)
        began = time.monotonic(); response.begin()
        result["ttfb_seconds"] = time.monotonic() - began
        result["status"] = response.status
        result["content_type"] = response.getheader("Content-Type")
        began = time.monotonic()
        body = response.read(2_000_000)
        result["body_seconds"] = time.monotonic() - began
        result["response_bytes"] = len(body)
        return body, result
    except Exception as exc:
        stage = ("DNS" if result["dns_outcome"] == "not measured" else
                 "TCP" if result["tcp_outcome"] == "not attempted" else
                 "TLS" if result["tls_outcome"] == "not attempted" else
                 "response/body")
        result["exception"] = {"stage": stage, "type": type(exc).__name__, "message": str(exc)[:300]}
        if stage == "DNS": result["dns_outcome"] = f"failed ({type(exc).__name__})"
        elif stage == "TCP": result["tcp_outcome"] = f"failed ({type(exc).__name__})"
        elif stage == "TLS": result["tls_outcome"] = f"failed ({type(exc).__name__})"
        return b"", result
    finally:
        if sock is not None:
            try: sock.close()
            except OSError: pass


def diagnose_discovery_providers(query: str, *, connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
                                 read_timeout: float = DEFAULT_READ_TIMEOUT) -> dict:
    """Probe each RSS discovery query independently; safe to invoke without Telegram."""
    providers = {
        "Google News RSS": query,
        "Official sites": f"{query} site:rockstargames.com OR site:rockstarnewswire.com OR site:take2games.com",
        "Independent publishers": f"{query} site:ign.com OR site:gamespot.com OR site:eurogamer.net OR site:pcgamer.com OR site:polygon.com OR site:reuters.com OR site:apnews.com",
    }
    reports = {}
    for name, term in providers.items():
        url = "https://news.google.com/rss/search?q=" + quote_plus(term) + "&hl=en-US&gl=US&ceid=US:en"
        body, measured = _diagnostic_get(url, connect_timeout=connect_timeout, read_timeout=read_timeout)
        parse_started = time.monotonic(); count = 0
        if body:
            try:
                root = ET.fromstring(body)
                count = sum(1 for node in root.findall("./channel/item") if node.findtext("title") and node.findtext("link"))
                measured["parse_outcome"] = "success"
            except Exception as exc:
                measured["parse_outcome"] = "failed"
                measured["parse_exception"] = {"type": type(exc).__name__, "message": str(exc)[:300]}
        else:
            measured["parse_outcome"] = "not run (no response body)"
        measured["parse_seconds"] = time.monotonic() - parse_started
        measured["results_extracted"] = count
        reports[name] = measured
    return reports


def diagnose_production_discovery(query: str, provider=None) -> dict:
    """Exercise the actual configured discovery methods, outside Telegram polling.

    Sequential calls separate provider behavior from executor contention. The
    concurrent pass submits the same three bound methods as MultiSourceNewsProvider.
    URLs and exception messages are intentionally omitted to avoid leaking keys.
    """
    provider = provider or MultiSourceNewsProvider()
    methods = [("Google News RSS (discovery)", provider.rss.search),
               ("Official sites (discovery)", provider.official.search),
               ("Independent publishers (discovery)", provider.publishers.search)]

    worker_starts = {}
    worker_lock = threading.Lock()
    def invoke(name, fn):
        started = time.monotonic()
        with worker_lock: worker_starts[name] = started
        try:
            rows = fn(query)
            details = getattr(getattr(provider, "rss", None), "_search_local", None)
            phase_timings = getattr(details, "last_search", None)
            return name, {"status": "completed", "result_count": len(rows),
                          "started_monotonic": started, "completed_monotonic": time.monotonic(),
                          "elapsed_seconds": time.monotonic() - started, "exception_type": None,
                          "provider_timings": dict(phase_timings) if phase_timings else None}
        except Exception as exc:
            http = {}
            if isinstance(exc, HTTPError):
                http = {"http_status": exc.code,
                        "content_type": exc.headers.get("Content-Type", "")[:120] if exc.headers else None}
            return name, {"status": "failed", "result_count": 0,
                          "started_monotonic": started, "completed_monotonic": time.monotonic(),
                          "elapsed_seconds": time.monotonic() - started,
                          "exception_type": type(exc).__name__,
                          "failure_stage": _provider_failure_stage(exc),
                          "safe_failure_reason": _safe_provider_failure(exc), **http}

    sequential = dict(invoke(name, fn) for name, fn in methods)
    with worker_lock: worker_starts.clear()
    pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="news-production-diagnostic")
    futures = {pool.submit(invoke, name, fn): name for name, fn in methods}
    concurrent = {}
    try:
        for future in as_completed(futures, timeout=SEARCH_WAIT_SECONDS):
            name, result = future.result()
            concurrent[name] = result
    except FuturesTimeoutError:
        pass
    finally:
        for future, name in futures.items():
            if name not in concurrent:
                started = worker_starts.get(name)
                concurrent[name] = {"status": "application deadline",
                                    "worker_state": "running" if started is not None else "queued/not started",
                                    "result_count": 0, "started_monotonic": started,
                                    "completed_monotonic": None, "elapsed_seconds": None,
                                    "exception_type": None}
        pool.shutdown(wait=False, cancel_futures=True)
    return {"sequential": sequential, "concurrent": concurrent,
            "application_deadline_seconds": SEARCH_WAIT_SECONDS, "executor_max_workers": 3,
            "submitted_task_count": len(methods), "shared_executor": True}


def _is_timeout_error(exc: BaseException) -> bool:
    return (isinstance(exc, (TimeoutError, socket.timeout)) or
            isinstance(exc, URLError) and isinstance(exc.reason, (TimeoutError, socket.timeout)))


def _provider_failure_stage(exc: BaseException) -> str:
    if isinstance(exc, HTTPError): return "HTTP response"
    if _is_timeout_error(exc): return "network timeout"
    if isinstance(exc, URLError): return "network connection or DNS"
    if isinstance(exc, (ET.ParseError, ValueError)): return "response parsing or provider data"
    return "provider execution"


def _safe_provider_failure(exc: BaseException) -> str:
    """Describe common failures without echoing URLs, headers, or credentials."""
    if isinstance(exc, HTTPError):
        if exc.code == 429: return "provider rate limited the request"
        if exc.code in {401, 403}: return "provider denied access to the request"
        return f"provider returned HTTP {exc.code}"
    if _is_timeout_error(exc): return "request exceeded the provider or application deadline"
    if isinstance(exc, URLError):
        reason = exc.reason
        if isinstance(reason, socket.gaierror): return "DNS lookup failed"
        if isinstance(reason, ssl.SSLError): return "TLS connection failed"
        if isinstance(reason, OSError): return "network connection failed"
        return "provider URL could not be reached"
    if isinstance(exc, ET.ParseError): return "provider response was malformed XML"
    if isinstance(exc, ValueError): return "provider returned invalid or incomplete data"
    return "provider failed; raw exception details suppressed"


def _safe_retry_after(headers) -> int | None:
    """Return a bounded Retry-After delay without exposing arbitrary header text."""
    if headers is None: return None
    try: value = headers.get("Retry-After")
    except Exception: return None
    if not isinstance(value, str): return None
    value = value.strip()
    try:
        seconds = int(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None: date = date.replace(tzinfo=timezone.utc)
            seconds = int((date - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
    return min(3600, max(0, seconds))


@dataclass
class Article:
    url: str
    publisher: str
    headline: str
    publication_date: str | None = None
    retrieved_at: str | None = None
    evidence: str | None = None
    accessible: bool = True
    original_source_url: str | None = None
    event_date: str | None = None
    rejection_reason: str | None = None
    http_status: int | None = None
    final_url: str | None = None
    redirect_count: int = 0
    content_type: str | None = None
    response_kind: str | None = None
    page_issue: str | None = None
    request_seconds: float = 0.0
    rss_url: str | None = None
    google_news_url: str | None = None
    extraction_method: str | None = None
    extracted_text_length: int = 0
    publisher_url: str | None = None
    platform: str = "web"
    author: str | None = None
    account_id: str | None = None
    content_id: str | None = None
    engagement: dict = field(default_factory=dict)
    authenticity: str = "unknown"
    relevance_score: float = 0.0
    limitations: list[str] = field(default_factory=list)
    is_repost: bool = False
    source_type: str = "INDEPENDENT REPORTING"
    reshared_at: str | None = None
    post_kind: str | None = None
    content_kind: str | None = None
    rss_metadata: dict = field(default_factory=dict)
    token_decoded: bool = False
    token_decode_error: str | None = None
    candidate_urls: list = field(default_factory=list)
    candidate_discovery: list = field(default_factory=list)
    candidate_verified: bool = False
    requested_url: str | None = None
    raw_article_urls: list = field(default_factory=list)
    resolver_diagnostics: dict = field(default_factory=dict)
    diagnostic_explicit: bool = False


@dataclass
class SourceRecord:
    """Normalized evidence returned by a replaceable platform provider."""
    platform: str
    original_url: str
    author: str | None = None
    account_id: str | None = None
    content_id: str | None = None
    published_at: str | None = None
    retrieved_at: str | None = None
    content: str = ""
    engagement: dict = field(default_factory=dict)
    authenticity: str = "unknown"
    relevance_score: float = 0.0
    evidence: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    is_repost: bool = False
    source_type: str = "SOCIAL MEDIA REPORT"
    reshared_at: str | None = None
    original_published_at: str | None = None
    post_kind: str = "original"


OFFICIAL_SOURCE_REGISTRY = {
    "rockstar": {"domains": ("rockstargames.com", "rockstarnewswire.com"),
                 "social_accounts": {"x": {"username": "RockstarGames", "id": None},
                                     "youtube": {"username": "RockstarGames", "id": None},
                                     "instagram": {"username": "rockstargames", "id": None}},
                 "reference": "https://www.rockstargames.com/contact links X, YouTube, and Instagram accounts."},
    "take_two": {"domains": ("take2games.com",), "social_accounts": {},
                  "reference": "First-party Take-Two domain; populate social account IDs only from links on that site."},
}


def verify_account(platform: str, account_id: str | None, username: str | None,
                   official_links: list[str] | None = None) -> tuple[str, list[str]]:
    """A badge, handle, or display name alone is not evidence of official identity."""
    service = platform.casefold()
    expected = OFFICIAL_SOURCE_REGISTRY["rockstar"]["social_accounts"].get(service)
    username_fold = (username or "").casefold().lstrip("@")
    if expected and username_fold == expected["username"].casefold():
        if service == "youtube" and not account_id:
            return "unknown_account", ["Official Rockstar username matches, but the channel ID was not resolved."]
        return "official_account", ["Identity matches the account linked by Rockstar's first-party contact page."]
    hosts = {_host(link) for link in (official_links or []) if link.startswith("https://")}
    link_match = any((service == "youtube" and host.endswith("youtube.com")) or
                     (service == "x" and host in {"x.com", "twitter.com"}) for host in hosts)
    if account_id and username and link_match:
        return "official_account", ["Account link was present on a first-party company page."]
    return "unknown_account", ["No sufficient first-party account identity evidence was available."]


def _json_request(url: str, *, headers: dict | None = None, timeout: float = 8) -> dict:
    req = Request(url, headers={"User-Agent": "UniversalVideoPipelineNews/2.0", **(headers or {})})
    with urlopen(req, timeout=min(max(timeout, 1), 15)) as response:
        return json.loads(response.read(2_000_000).decode("utf-8"))


class XAPIProvider:
    """X API v2 recent search; requires X_BEARER_TOKEN."""
    name = "x"
    def __init__(self, token: str | None = None, timeout: float = 8):
        self.token, self.timeout = token or os.getenv("X_BEARER_TOKEN"), timeout
    def search_records(self, query: str) -> list[SourceRecord]:
        if not self.token: raise RuntimeError("X unavailable: X_BEARER_TOKEN is not configured")
        params = "query=" + quote_plus(f'("{query}" OR GTA6)') + "&max_results=25&tweet.fields=created_at,public_metrics,referenced_tweets,author_id&expansions=author_id,referenced_tweets.id&user.fields=name,username,verified"
        data = _json_request("https://api.x.com/2/tweets/search/recent?" + params,
                             headers={"Authorization": "Bearer " + self.token}, timeout=self.timeout)
        users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
        original_tweets = {t["id"]: t for t in data.get("includes", {}).get("tweets", [])}
        rows = []
        for tweet in data.get("data", []):
            user = users.get(tweet.get("author_id"), {})
            repost = next((r for r in tweet.get("referenced_tweets", []) if r.get("type") == "retweeted"), None)
            reply = next((r for r in tweet.get("referenced_tweets", []) if r.get("type") == "replied_to"), None)
            quoted = next((r for r in tweet.get("referenced_tweets", []) if r.get("type") == "quoted"), None)
            original = original_tweets.get(repost.get("id"), {}) if repost else {}
            auth, identity_evidence = verify_account("x", tweet.get("author_id"), user.get("username"))
            rows.append(SourceRecord("X", f"https://x.com/{user.get('username','i')}/status/{tweet['id']}",
                user.get("name"), tweet.get("author_id"), tweet["id"], tweet.get("created_at"), _now(),
                original.get("text", tweet.get("text", "")), tweet.get("public_metrics", {}), auth, .5,
                ["Retrieved from X API v2.", *identity_evidence],
                [] if auth == "official_account" else ["Official identity is unverified."],
                bool(repost), reshared_at=tweet.get("created_at") if repost else None,
                original_published_at=original.get("created_at") if repost else tweet.get("created_at"),
                post_kind="repost" if repost else "reply" if reply else "quote" if quoted else "original"))
        return rows


class YouTubeDataAPIProvider:
    """YouTube Data API v3 search and detail lookup; requires YOUTUBE_DATA_API_KEY."""
    name = "youtube"
    def __init__(self, api_key: str | None = None, timeout: float = 8):
        self.api_key, self.timeout = api_key or os.getenv("YOUTUBE_DATA_API_KEY"), timeout
    def search_records(self, query: str) -> list[SourceRecord]:
        if not self.api_key: raise RuntimeError("YouTube unavailable: YOUTUBE_DATA_API_KEY is not configured")
        q = quote_plus(query + " GTA 6")
        found = _json_request(f"https://www.googleapis.com/youtube/v3/search?part=snippet&type=video&maxResults=5&q={q}&key={quote_plus(self.api_key)}", timeout=self.timeout)
        records = []
        hits = [(hit.get("id", {}).get("videoId"), hit.get("snippet", {})) for hit in found.get("items", [])]
        ids = [vid for vid, _ in hits if vid]
        details = _json_request(f"https://www.googleapis.com/youtube/v3/videos?part=snippet,statistics&id={quote_plus(','.join(ids))}&key={quote_plus(self.api_key)}", timeout=self.timeout).get("items", []) if ids else []
        stats_by_id = {item.get("id"): item.get("statistics", {}) for item in details}
        official_channels = _json_request(f"https://www.googleapis.com/youtube/v3/channels?part=id&forUsername=RockstarGames&key={quote_plus(self.api_key)}", timeout=self.timeout).get("items", [])
        official_channel_ids = {item.get("id") for item in official_channels}
        for vid, snip in hits:
            if not vid: continue
            stats = stats_by_id.get(vid, {})
            channel_id, channel = snip.get("channelId"), snip.get("channelTitle")
            auth, ev = (("official_account", ["Channel ID matches the RockstarGames channel resolved through YouTube Data API v3."])
                        if channel_id in official_channel_ids else verify_account("youtube", channel_id, channel))
            records.append(SourceRecord("YouTube", f"https://www.youtube.com/watch?v={vid}", channel, channel_id, vid,
                snip.get("publishedAt"), _now(), snip.get("title", "") + "\n" + snip.get("description", ""),
                {k: int(v) for k, v in stats.items() if k in {"viewCount", "likeCount", "commentCount"} and str(v).isdigit()},
                auth, .6, ["Metadata returned by YouTube Data API v3.", *ev],
                ["Official channel identity requires first-party outbound-link verification."]))
        return records


class InstagramProvider:
    """Supported Graph API surface only; never scrapes login-gated public search."""
    name = "instagram"
    def __init__(self, token: str | None = None, account_id: str | None = None):
        self.token = token or os.getenv("INSTAGRAM_ACCESS_TOKEN")
        self.account_id = account_id or os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID")
        self.api_version = os.getenv("INSTAGRAM_GRAPH_API_VERSION", "v27.0")
    def search_records(self, query: str) -> list[SourceRecord]:
        del query
        if not self.token or not self.account_id:
            raise RuntimeError("Instagram unavailable: Graph API token and professional account ID required; unrestricted public search is not offered")
        data = _json_request(f"https://graph.facebook.com/{quote_plus(self.api_version)}/{quote_plus(self.account_id)}/media?fields=id,caption,media_type,permalink,timestamp,like_count,comments_count&access_token={quote_plus(self.token)}")
        rows = []
        for m in data.get("data", []):
            text = m.get("caption", "")
            if not re.search(r"GTA\s*6|GTA\s*VI|Grand Theft Auto VI", text, re.I): continue
            rows.append(SourceRecord("Instagram", m.get("permalink", ""), self.account_id, self.account_id, m.get("id"),
                m.get("timestamp"), _now(), text, {k: m[k] for k in ("like_count", "comments_count") if k in m},
                "unknown", .5, ["Caption and permalink returned by authorized Instagram Graph API."],
                ["Only media exposed to the configured professional account is searched; not platform-wide search."]))
        return rows


@dataclass
class NewsItem:
    claim: str
    status: str
    sources: list[Article] = field(default_factory=list)
    confirmed: str = ""
    uncertain: str = ""
    disagreement: str = ""
    attribution: str = ""
    source_type: str = "INDEPENDENT REPORTING"


@dataclass
class NewsReport:
    topic: str
    researched_at: str
    summary: str
    items: list[NewsItem]
    metrics: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


class NewsProvider(Protocol):
    def search(self, query: str) -> list[Article]: ...
    def open(self, article: Article) -> Article: ...


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.ignore = 0
        self.meta: dict[str, str] = {}
        self.canonical: str | None = None
        self.article_parts: list[str] = []
        self.in_article = 0
        self.links: list[str] = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "noscript", "svg"}: self.ignore += 1
        if tag == "meta":
            key = attrs.get("property") or attrs.get("name")
            if key and attrs.get("content"): self.meta[key.casefold()] = attrs["content"]
        if tag == "a" and attrs.get("href", "").startswith("https://"):
            self.links.append(attrs["href"])
        if tag == "link" and "canonical" in (attrs.get("rel") or "").casefold().split():
            self.canonical = attrs.get("href")
        if tag in {"article", "main"}: self.in_article += 1
    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self.ignore: self.ignore -= 1
        if tag in {"article", "main"} and self.in_article: self.in_article -= 1
    def handle_data(self, data):
        if not self.ignore and data.strip():
            self.parts.append(data.strip())
            if self.in_article: self.article_parts.append(data.strip())


def _jsonld_article_bodies(html: str) -> list[str]:
    """Read public JSON-LD articleBody fields without executing page scripts."""
    bodies = []
    for match in re.finditer(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script\s*>', html, re.I | re.S):
        try: value = json.loads(match.group(1))
        except (ValueError, TypeError): continue
        stack = value if isinstance(value, list) else [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                body = item.get("articleBody")
                if isinstance(body, str) and body.strip(): bodies.append(body)
                graph = item.get("@graph")
                if isinstance(graph, list): stack.extend(graph)
    return bodies


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _publisher_host(host: str) -> str:
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in {"co.uk", "com.au", "co.jp", "co.in", "com.br"}:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) > 1 else host


def _known_publisher_host(publisher: str) -> str | None:
    known = {"pc guide": "pcguide.com", "ign": "ign.com", "gamespot": "gamespot.com",
             "pc gamer": "pcgamer.com", "eurogamer": "eurogamer.net", "polygon": "polygon.com",
             "reuters": "reuters.com", "associated press": "apnews.com", "bbc": "bbc.com",
             "the guardian": "theguardian.com", "new york times": "nytimes.com",
             "the verge": "theverge.com", "kotaku": "kotaku.com", "venturebeat": "venturebeat.com",
             "pinknews": "pinknews.co.uk", "push square": "pushsquare.com", "nme": "nme.com",
             "the independent": "independent.co.uk", "red bull": "redbull.com"}
    folded = publisher.casefold()
    return next((host for name, host in known.items() if name in folded), None)


def _iso_date(value: str | None) -> str | None:
    if not value: return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        try: return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError): return None


def _safe_url(url: str | None) -> str | None:
    if not url: return None
    parsed = urlparse(url)
    # Keep public RSS metadata useful while dropping query values that might be
    # tracking identifiers or credentials.
    query = urlencode([(key, "[redacted]") for key, _ in parse_qsl(parsed.query, keep_blank_values=True)])
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["): host = f"[{host}]"
    netloc = host + (f":{parsed.port}" if parsed.port else "")
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, query, ""))


def _protobuf_fields(data: bytes):
    """Yield wire-format protobuf fields, rejecting malformed/truncated input."""
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        number, wire = key >> 3, key & 7
        if number == 0: raise ValueError("invalid protobuf field number")
        if wire == 0:
            value, pos = _read_varint(data, pos)
        elif wire == 1:
            if pos + 8 > len(data): raise ValueError("truncated fixed64 field")
            value, pos = data[pos:pos + 8], pos + 8
        elif wire == 2:
            size, pos = _read_varint(data, pos)
            if pos + size > len(data): raise ValueError("truncated length-delimited field")
            value, pos = data[pos:pos + size], pos + size
        elif wire == 5:
            if pos + 4 > len(data): raise ValueError("truncated fixed32 field")
            value, pos = data[pos:pos + 4], pos + 4
        else: raise ValueError(f"unsupported protobuf wire type {wire}")
        yield number, wire, value


def _read_varint(data: bytes, pos: int):
    value = shift = 0
    while pos < len(data) and shift < 70:
        byte = data[pos]; pos += 1
        value |= (byte & 0x7f) << shift
        if not byte & 0x80: return value, pos
        shift += 7
    raise ValueError("invalid or truncated protobuf varint")


def _decode_google_token(url: str) -> tuple[str | None, str | None]:
    """Decode a URL only when it occurs in a valid protobuf length-delimited field."""
    if _host(url) != "news.google.com": return None, "stage=host-validation: not a Google News URL"
    import base64, binascii
    token = urlparse(url).path.rsplit("/", 1)[-1]
    if not token: return None, "stage=token-extraction: missing article token"
    try:
        payload = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError): return None, "stage=base64url-decode: token is not valid URL-safe base64"
    # Since July 2024 most Google News IDs decode to an opaque AU_yqL… value;
    # some generations wrap that value in protobuf field 4.
    if payload.startswith(b"AU_yqL"):
        return None, "stage=token-generation: current opaque AU_yqL token requires Google's public batchexecute resolver"
    try:
        fields = list(_protobuf_fields(payload))
    except ValueError as exc: return None, f"stage=protobuf-parse: {exc}"
    if any(wire == 2 and value.startswith(b"AU_yqL") for _, wire, value in fields):
        return None, "stage=token-generation: current opaque AU_yqL token requires Google's public batchexecute resolver"
    # Google token generations store their URL in different length-delimited
    # fields. Inspect only parsed byte fields, including nested protobuf fields.
    pending = [value for _, wire, value in fields if wire == 2]
    seen = set()
    while pending and len(seen) < 64:
        value = pending.pop(0)
        if value in seen: continue
        seen.add(value)
        text = value.decode("utf-8", errors="ignore")
        for match in re.findall(r"https://[^\x00-\x20\x7f\"'<>]+", text):
            candidate = match.rstrip(".,);]")
            parsed = urlparse(candidate)
            if (parsed.scheme == "https" and _host(candidate) not in {"news.google.com", "google.com"}
                    and parsed.path not in {"", "/"}):
                return candidate, None
        try: pending.extend(v for _, wire, v in _protobuf_fields(value) if wire == 2)
        except ValueError: pass
    return None, "stage=url-extraction: valid protobuf fields contain no publisher article HTTPS URL"


def _parse_batchexecute(body: bytes) -> str:
    """Parse Google's guarded, double-JSON batchexecute response strictly."""
    text = body.decode("utf-8", errors="strict")
    if text.startswith(")]}'"):
        parts = text.split("\n\n", 1)
        if len(parts) != 2: raise ValueError("malformed anti-hijacking response prefix")
        text = parts[1]
    frames = json.loads(text)
    if not isinstance(frames, list): raise ValueError("response frames are not a list")
    frame = next((row for row in frames if isinstance(row, list) and len(row) > 2 and
                  row[0] == "wrb.fr" and row[1] == "Fbv4je"), None)
    if frame is None or not isinstance(frame[2], str): raise ValueError("missing Fbv4je response frame")
    result = json.loads(frame[2])
    if not isinstance(result, list) or len(result) < 2 or result[0] != "garturlres" or not isinstance(result[1], str):
        raise ValueError("missing garturlres URL")
    return result[1]


def _resolve_google_token(url: str, expected_host: str, timeout: float = 8,
                          opener_factory=None, diagnostics: dict | None = None) -> tuple[str | None, str]:
    """Resolve one opaque ID with Google's public but undocumented RPC.

    Sends ordinary requests only. It does not seed consent cookies, spoof browser
    fingerprints, retry blocks, or attempt to pass access restrictions.
    """
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics.update({"signature_request": "not started", "signature_response": "not received",
                        "signature_page_classification": "not classified",
                        "signature_extracted": False, "timestamp_extracted": False,
                        "signature_timestamp_extraction": "not reached",
                        "batchexecute_request": "not started", "batchexecute_response": "not received",
                        "batchexecute_response_bytes": None, "response_parsed": False,
                        "publisher_url_extracted": False, "url_validation": "not reached",
                        "publisher_domain_valid": None, "article_extraction": "not reached"})
    started = time.monotonic()
    def finish(value, reason):
        diagnostics.setdefault("total_seconds", round(time.monotonic() - started, 3))
        diagnostics.setdefault("result", "resolved" if value else "unavailable")
        if reason:
            diagnostics.setdefault("failure", reason)
            diagnostics.setdefault("failure_reason", reason)
            if reason == "google_rate_limited":
                diagnostics.setdefault("failure_stage", "signature-fetch")
            else:
                stage = re.match(r"stage=([^:]+):", reason)
                if stage: diagnostics.setdefault("failure_stage", stage.group(1))
        return value, reason
    if not expected_host: return finish(None, "stage=publisher-validation: missing publisher URL/domain")
    if _host(url) != "news.google.com": return finish(None, "stage=host-validation: not a Google News URL")
    token_started = time.monotonic()
    article_id = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    if not article_id or article_id in {"articles", "read"}: return finish(None, "stage=token-extraction: missing token")
    diagnostics["token_extraction"] = "ok"
    diagnostics["token_extraction_seconds"] = round(time.monotonic() - token_started, 4)
    # Carry only cookies the ordinary first-party page response sets into RPC.
    # Do not manufacture cookies to skip consent or other interstitials.
    # No retries: the signature page and RPC are each one HTTP operation.  The
    # timeout is capped, and the small redirect limit bounds the number of hops.
    timeout = min(max(float(timeout), 0.1), 12.0)
    opener = (opener_factory() if opener_factory is not None else
              build_opener(_LimitedRedirects(2), HTTPCookieProcessor(CookieJar())))
    try:
        diagnostics["signature_request"] = "started"
        req = Request("https://news.google.com/rss/articles/" + article_id,
                      headers={"User-Agent": "UniversalVideoPipelineNews/1.0", "Accept": "text/html"})
        stage_started = time.monotonic()
        with opener.open(req, timeout=timeout) as response:
            diagnostics["signature_http_status"] = getattr(response, "status", 200)
            diagnostics["signature_response"] = "received"
            html = response.read(2_000_000).decode("utf-8", errors="replace")
            diagnostics["signature_page_bytes"] = len(html)
            diagnostics["signature_page_seconds"] = round(time.monotonic() - stage_started, 3)
    except HTTPError as exc:
        diagnostics["signature_response"] = "HTTP error"
        diagnostics["signature_http_status"] = exc.code
        diagnostics["signature_request_seconds"] = round(time.monotonic() - stage_started, 3)
        if exc.code == 429:
            diagnostics["signature_page_classification"] = "google_rate_limited"
            retry_after = _safe_retry_after(exc.headers)
            if retry_after is not None: diagnostics["retry_after_seconds"] = retry_after
            diagnostics["batchexecute_request"] = "not started"
            return finish(None, "google_rate_limited")
        return finish(None, f"stage=signature-fetch: HTTP {exc.code}")
    except (OSError, URLError, TimeoutError, http.client.HTTPException) as exc:
        diagnostics["signature_response"] = "no response"
        diagnostics["safe_exception_type"] = type(exc).__name__
        diagnostics["safe_exception_detail"] = "bounded request timed out" if _is_timeout_error(exc) else "network request failed; raw message suppressed"
        diagnostics["signature_request_seconds"] = round(time.monotonic() - stage_started, 3)
        return finish(None, "stage=signature-fetch: timeout" if _is_timeout_error(exc) else f"stage=signature-fetch: {type(exc).__name__}")
    if _page_issue(html):
        diagnostics["signature_page_classification"] = "access restriction/interstitial"
        return finish(None, "stage=signature-fetch: access restriction/interstitial; not bypassed")
    sg = re.search(r'data-n-a-sg=["\']([^"\']+)', html)
    ts = re.search(r'data-n-a-ts=["\'](\d+)', html)
    diagnostics["signature_extracted"] = bool(sg)
    diagnostics["timestamp_extracted"] = bool(ts)
    diagnostics["signature_timestamp_extraction"] = "complete" if sg and ts else "incomplete"
    if not sg or not ts:
        diagnostics["signature_page_classification"] = "resolver attributes missing"
        return finish(None, "stage=signature-fetch: required signature/timestamp missing")
    diagnostics["signature_page_classification"] = "resolver attributes extracted"
    diagnostics["signature_timestamp_extraction"] = "complete"
    inner = ["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
            None, None, None, None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
            article_id, int(ts.group(1)), sg.group(1)]
    payload = urlencode({"f.req": json.dumps([[ ["Fbv4je", json.dumps(inner, separators=(",", ":")) ] ]])}).encode()
    req = Request("https://news.google.com/_/DotsSplashUi/data/batchexecute?rpcids=Fbv4je", data=payload,
                  headers={"User-Agent": "UniversalVideoPipelineNews/1.0",
                           "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                           "Origin": "https://news.google.com", "Referer": "https://news.google.com/",
                           "X-Same-Domain": "1"}, method="POST")
    try:
        diagnostics["batchexecute_request"] = "started"
        stage_started = time.monotonic()
        with opener.open(req, timeout=timeout) as response:
            diagnostics["batchexecute_http_status"] = getattr(response, "status", 200)
            diagnostics["batchexecute_response"] = "received"
            if diagnostics["batchexecute_http_status"] != 200: return finish(None, f"stage=batchexecute: HTTP {diagnostics['batchexecute_http_status']}")
            response_body = response.read(1_000_000)
            diagnostics["batchexecute_response_bytes"] = len(response_body)
            resolved = _parse_batchexecute(response_body)
            diagnostics["response_parsed"] = True
            diagnostics["batchexecute_seconds"] = round(time.monotonic() - stage_started, 3)
            diagnostics["publisher_url_extracted"] = True
    except HTTPError as exc:
        diagnostics["batchexecute_response"] = "HTTP error"
        diagnostics["batchexecute_http_status"] = exc.code
        diagnostics["batchexecute_seconds"] = round(time.monotonic() - stage_started, 3)
        return finish(None, f"stage=batchexecute: HTTP {exc.code}")
    except (OSError, URLError, TimeoutError, http.client.HTTPException) as exc:
        diagnostics["batchexecute_response"] = "no response"
        diagnostics["safe_exception_type"] = type(exc).__name__
        diagnostics["safe_exception_detail"] = "bounded request timed out" if _is_timeout_error(exc) else "network request failed; raw message suppressed"
        diagnostics["batchexecute_seconds"] = round(time.monotonic() - stage_started, 3)
        return finish(None, "stage=batchexecute: timeout" if _is_timeout_error(exc) else f"stage=batchexecute: {type(exc).__name__}")
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        diagnostics["batchexecute_response"] = "received"
        diagnostics["response_parsed"] = False
        diagnostics["batchexecute_seconds"] = round(time.monotonic() - stage_started, 3)
        diagnostics["safe_exception_type"] = type(exc).__name__
        diagnostics["safe_exception_detail"] = "response could not be parsed; raw response suppressed"
        return finish(None, f"stage=batchexecute-parse: {type(exc).__name__}")
    parsed = urlparse(resolved)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        diagnostics["url_validation"] = "rejected"
        return finish(None, "stage=url-validation: result is not a valid HTTPS URL")
    host = _publisher_host(_host(resolved))
    if host in {"google.com", "news.google.com"} or host != _publisher_host(expected_host):
        diagnostics["publisher_domain_valid"] = False
        diagnostics["url_validation"] = "publisher domain rejected"
        return finish(None, f"stage=publisher-validation: resolved host does not match {expected_host}")
    if parsed.path in {"", "/"}:
        diagnostics["publisher_domain_valid"] = True
        diagnostics["url_validation"] = "homepage rejected"
        return finish(None, "stage=url-validation: publisher homepage rejected")
    diagnostics["publisher_domain_valid"] = True
    diagnostics["url_validation"] = "accepted"
    # This is the only point at which the diagnostic may expose a publisher URL:
    # the resolver has checked HTTPS, article path, and the expected domain.
    diagnostics["validated_publisher_url"] = resolved
    return finish(resolved, "")


def _google_link_target(url: str) -> str | None:
    return _decode_google_token(url)[0]


def _rss_item_metadata(node) -> dict:
    """Retain the complete, bounded item structure with query values redacted."""
    def clean(value: str) -> str:
        return re.sub(r"https?://[^\s\"'<>]+", lambda match: _safe_url(match.group()) or match.group(),
                      value)[:4000]
    metadata = {}
    for child in list(node):
        key = child.tag.rsplit("}", 1)[-1]
        value = {"qualified_name": child.tag, "text": clean((child.text or "").strip()),
                 "attributes": {k: (clean(v) if "http://" in v or "https://" in v else v[:500])
                                for k, v in child.attrib.items()}}
        metadata.setdefault(key, []).append(value)
    return metadata


def _metadata_article_urls(metadata: dict) -> list[str]:
    import html
    found = []
    serialized = json.dumps(metadata, ensure_ascii=False)
    for url in re.findall(r'https?://[^\s\"\'<>]+', html.unescape(serialized)):
        url = url.rstrip(".,);]\\")
        if urlparse(url).scheme == "https" and _host(url) not in {"news.google.com", "google.com"} and urlparse(url).path not in {"", "/"}:
            if url not in found: found.append(url)
    return found[:20]


def _headline_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _same_headline(candidate: str, expected: str, publisher: str) -> bool:
    candidate = re.sub(r"\s*[-|]\s*" + re.escape(publisher) + r"\s*$", "", candidate, flags=re.I)
    return _headline_key(candidate) == _headline_key(expected)


def _fallback_rss_candidates(article: Article, fetch_xml) -> tuple[list[str], list[str]]:
    """Use public Bing News RSS only for exact-title, same-publisher candidates."""
    source_host = _host(article.publisher_url or "")
    expected = source_host if source_host and source_host not in {"google.com", "news.google.com"} else (
        _known_publisher_host(article.publisher) or "")
    if not expected: return [], ["publisher domain could not be established"]
    query = f'"{article.headline}" {article.publisher}'
    rss_url = "https://www.bing.com/news/search?q=" + quote_plus(query) + "&format=rss"
    try: root = ET.fromstring(fetch_xml(rss_url))
    except Exception as exc: return [], [f"public search RSS unavailable: {type(exc).__name__}"]
    candidates, discovery = [], []
    nodes = root.findall("./channel/item")
    discovery.append(f"Bing News RSS returned {len(nodes)} results")
    if root.tag.rsplit("}", 1)[-1].casefold() not in {"rss", "feed"}:
        return [], [f"Bing News RSS returned malformed/unexpected XML root: {root.tag.rsplit('}', 1)[-1]}"]
    if not nodes:
        discovery.append("Bing News RSS response contained zero RSS items")
    for node in nodes:
        title, link = node.findtext("title") or "", node.findtext("link") or ""
        # Bing News RSS commonly wraps the publisher URL in an apiclick URL.
        # Unwrap only explicit URL parameters, then apply the same strict host,
        # headline, HTTPS and article-path checks as for a direct link.
        possible = [link]
        try:
            for key, value in parse_qsl(urlparse(link).query, keep_blank_values=False):
                if key.casefold() in {"url", "u", "target"} and value.startswith("https://"):
                    possible.append(value)
        except ValueError:
            pass
        if not _same_headline(title, article.headline, article.publisher):
            discovery.append("rejected unrelated headline from Bing News RSS")
            continue
        accepted = None
        for candidate in possible:
            host = _host(candidate)
            if urlparse(candidate).scheme != "https" or not host:
                continue
            if _publisher_host(host) != _publisher_host(expected):
                continue
            if urlparse(candidate).path in {"", "/"}:
                continue
            accepted = candidate
            break
        if not accepted and not any(_publisher_host(_host(c)) == _publisher_host(expected) for c in possible):
            discovery.append("rejected wrong-domain headline match from Bing News RSS")
            continue
        if not accepted:
            discovery.append("rejected publisher homepage or non-article URL")
            continue
        if accepted not in candidates:
            candidates.append(accepted)
            discovery.append("exact headline and publisher-domain match via Bing News RSS")
    if not candidates: discovery.append("no verified publisher article candidate found; Bing fallback did not resolve this item")
    return candidates, discovery


class _CountRedirects(HTTPRedirectHandler):
    def __init__(self): super().__init__(); self.count = 0
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _LimitedRedirects(HTTPRedirectHandler):
    """Keep resolver redirect chains finite; urllib otherwise allows 10 hops."""
    def __init__(self, maximum: int = 2):
        super().__init__(); self.maximum = maximum; self.count = 0
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        if self.count > self.maximum:
            raise HTTPError(newurl, code, "Google resolver redirect limit exceeded", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _response_kind(content_type: str, body: bytes) -> str:
    ctype = (content_type or "").casefold()
    sample = body.lstrip()[:100].lower()
    if "html" in ctype or sample.startswith((b"<!doctype html", b"<html")): return "HTML"
    if "xml" in ctype or sample.startswith((b"<?xml", b"<rss")): return "XML"
    if "json" in ctype or sample.startswith((b"{", b"[")): return "JSON"
    return "other"


def _page_issue(text: str, status: int | None = None) -> str | None:
    low = text.casefold()
    for needle, label in (("captcha", "CAPTCHA page"), ("unusual traffic", "bot-blocking page"),
                          ("verify you are human", "bot-blocking page"), ("access denied", "access-denied page"),
                          ("consent.google", "consent page"), ("before you continue to", "consent page"),
                          ("enable javascript and cookies", "bot-blocking page")):
        if needle in low: return label
    if status in (403, 429): return f"HTTP {status} access/rate limit response"
    return None


class GoogleNewsRSSProvider:
    """No-key RSS discovery plus bounded public article retrieval.

    Publisher URLs are resolved in this order: article-level RSS source URL,
    a literal URL in Google's encoded article token, then ordinary HTTPS
    redirects and publisher links in the Google response. The last fallback is
    a normal public-page GET; it does not attempt to pass access controls.
    """
    def __init__(self, timeout: float = 8, retries: int = 0, user_agent: str = "UniversalVideoPipelineNews/1.0"):
        self.timeout, self.retries, self.user_agent = min(max(timeout, 1), 15), min(max(0, retries), 1), user_agent
        self._timings = {"search": [], "results": 0, "request_wall": 0.0, "request_sum": 0.0,
                         "redirect_count": 0, "fetch": 0.0, "extract": 0.0,
                         "extract_started": None, "extract_ended": None}
        self._timing_lock = threading.Lock()
        self._search_local = threading.local()

    def _emit_search_stage(self, stage: str, **details) -> None:
        """Send safe stage events to the active caller, when diagnostics are enabled."""
        callback = getattr(self._search_local, "stage_callback", None)
        if callback:
            callback(stage, **details)

    def _get(self, url: str) -> bytes:
        if urlparse(url).scheme != "https": raise ValueError("Only HTTPS news URLs are allowed")
        for attempt in range(self.retries + 1):
            try:
                req = Request(url, headers={"User-Agent": self.user_agent})
                with urlopen(req, timeout=self.timeout) as response:
                    headers = getattr(response, "headers", {})
                    self._emit_search_stage("response received", status=getattr(response, "status", None),
                                            content_type=headers.get("Content-Type", "")[:120])
                    body = response.read(2_000_000)
                    self._emit_search_stage("response body downloaded", response_bytes=len(body))
                    return body
            except (OSError, URLError, TimeoutError):
                if attempt == self.retries: raise
                time.sleep(min(0.25 * (attempt + 1), 1))
        return b""

    def _fetch(self, url: str):
        if urlparse(url).scheme != "https": raise ValueError("Only HTTPS article URLs are allowed")
        for attempt in range(self.retries + 1):
            redirects = _CountRedirects()
            opener = build_opener(redirects)
            started = time.monotonic()
            try:
                req = Request(url, headers={"User-Agent": self.user_agent})
                with opener.open(req, timeout=self.timeout) as response:
                    final_url = response.geturl()
                    if urlparse(final_url).scheme != "https": raise ValueError("Insecure article redirect")
                    body = response.read(2_000_000)
                    return {"body": body, "final_url": final_url, "status": getattr(response, "status", 200),
                            "content_type": response.headers.get("Content-Type", ""),
                            "redirect_count": redirects.count, "request_seconds": time.monotonic() - started}
            except HTTPError as exc:
                return {"body": exc.read(2_000_000), "final_url": exc.geturl(), "status": exc.code,
                        "content_type": exc.headers.get("Content-Type", ""), "redirect_count": redirects.count,
                        "request_seconds": time.monotonic() - started}
            except (OSError, URLError, TimeoutError):
                if attempt == self.retries: raise
                time.sleep(min(0.25 * (attempt + 1), 1))

    def search(self, query: str) -> list[Article]:
        started = time.monotonic()
        self._emit_search_stage("network request started")
        url_started = time.monotonic()
        url = "https://news.google.com/rss/search?q=" + quote_plus(query) + "&hl=en-US&gl=US&ceid=US:en"
        url_seconds = time.monotonic() - url_started
        request_started = time.monotonic()
        body = self._get(url)
        request_seconds = time.monotonic() - request_started
        self._emit_search_stage("network request completed", response_bytes=len(body), request_seconds=request_seconds)
        parse_started = time.monotonic()
        root = ET.fromstring(body)
        parse_seconds = time.monotonic() - parse_started
        found = []
        normalize_started = time.monotonic()
        for node in root.findall("./channel/item"):
            headline = node.findtext("title") or ""
            link = node.findtext("link") or ""
            if not headline or not link: continue
            source = node.find("source")
            publisher = source.text.strip() if source is not None and source.text else _publisher_host(_host(link))
            source_url = source.attrib.get("url") if source is not None else None
            metadata = _rss_item_metadata(node)
            decoded, decode_error = _decode_google_token(link)
            raw_urls = _metadata_article_urls(metadata)
            source_article = source_url if source_url and urlparse(source_url).path not in {"", "/"} else None
            metadata_article = next((u for u in raw_urls if _publisher_host(_host(u)) == _publisher_host(_host(source_url or u))), None)
            expected_host = (_publisher_host(_host(source_url)) if source_url and _host(source_url) not in {"google.com", "news.google.com"}
                             else _known_publisher_host(publisher))
            # Discovery only: guarded network resolution belongs to open(), when an
            # article is selected for retrieval. Resolving every feed item here made
            # a 100-result search perform up to 200 serial HTTP requests.
            if decoded and expected_host and _publisher_host(_host(decoded)) != expected_host:
                decode_error = f"publisher validation failed: decoded host does not match {expected_host}"
                decoded = None
            original = (source_url if source_url and urlparse(source_url).scheme == "https" and
                        urlparse(source_url).path not in {"", "/"} and
                        _host(source_url) not in {"news.google.com", "google.com"} else None) or metadata_article or decoded
            found.append(Article(link, publisher, headline,
                                 _iso_date(node.findtext("pubDate")), original_source_url=original, rss_url=link,
                                 google_news_url=link, publisher_url=source_url, rss_metadata=metadata,
                                 token_decoded=bool(decoded), token_decode_error=decode_error,
                                 raw_article_urls=raw_urls))
        normalize_seconds = time.monotonic() - normalize_started
        self._emit_search_stage("parsing completed", result_count=len(found), parse_seconds=parse_seconds,
                                normalization_seconds=normalize_seconds)
        with self._timing_lock:
            self._timings["search"].append(time.monotonic() - started)
            self._timings["results"] += len(found)
            phase_timings = {"request_url_construction_seconds": url_seconds,
                "dns_seconds": None, "tcp_seconds": None, "tls_seconds": None,
                "first_response_byte_seconds": None, "response_body_download_seconds": None,
                "urllib_request_seconds": request_seconds, "redirect_count": None, "rss_parse_seconds": parse_seconds,
                "result_normalization_seconds": normalize_seconds, "per_result_url_resolution_seconds": 0.0,
                "result_count": len(found), "total_provider_seconds": time.monotonic() - started,
                "network_phase_note": "urllib does not expose DNS/TCP/TLS/TTFB/body/redirect subphases; urllib_request_seconds includes the request and body read"}
            self._timings["last_search"] = phase_timings
            self._search_local.last_search = phase_timings
        return found

    def open(self, article: Article) -> Article:
        request = {}
        fetch_started = time.monotonic()
        resolver_diagnostics = {}
        try:
            target = article.original_source_url or _google_link_target(article.url)
            if not target and _host(article.url) == "news.google.com":
                expected_host = (_publisher_host(_host(article.publisher_url))
                                 if article.publisher_url and _host(article.publisher_url) not in {"google.com", "news.google.com"}
                                 else _known_publisher_host(article.publisher))
                if expected_host:
                    target, resolver_reason = _resolve_google_token(
                        article.url, expected_host, timeout=self.timeout, diagnostics=resolver_diagnostics)
                    article.token_decode_error = resolver_reason or None
                    article.token_decoded = bool(target)
                    # _resolve_google_token returns a target only after HTTPS,
                    # publisher-domain, and article-path validation.
                    if target:
                        article.publisher_url = target
                        article.original_source_url = target
                else:
                    resolver_diagnostics.update(result="unavailable", failure="stage=publisher-validation: expected publisher domain unavailable")
                if article.diagnostic_explicit and not target:
                    resolver_diagnostics["article_extraction"] = "not reached: publisher URL unresolved"
                    return Article(**{**asdict(article), "retrieved_at": _now(), "evidence": None,
                        "accessible": False, "rejection_reason": resolver_diagnostics.get("failure"),
                        "google_news_url": article.url, "resolver_diagnostics": resolver_diagnostics})
            target_candidates, discovery = list(article.candidate_urls), list(article.candidate_discovery)
            if target and _host(article.url) == "news.google.com":
                target_candidates.append(target)
                discovery.append("Google News batchexecute resolution" if resolver_diagnostics else "Google News article token")
            if article.original_source_url and article.original_source_url not in target_candidates:
                target_candidates.append(article.original_source_url)
                discovery.append("Google News article token" if article.token_decoded else "RSS item metadata")
            used_search_fallback = False
            if not target and _host(article.url) == "news.google.com":
                target_candidates, discovery = _fallback_rss_candidates(article, self._get)
                used_search_fallback = bool(target_candidates)
                target = target_candidates[0] if target_candidates else article.url
            if not target: target = article.url
            request = self._fetch(target)
            raw, final_url = request["body"], request["final_url"]
            page = raw.decode("utf-8", errors="replace")
            # Some current Google News article tokens do not expose the target
            # in the RSS URL. A normal GET may still redirect or contain a
            # publisher article link; follow only ordinary HTTPS links.
            if _host(final_url) == "news.google.com" and request["status"] < 400:
                probe = _TextExtractor()
                try: probe.feed(page)
                except Exception: pass
                choices = []
                for link in ([probe.meta.get("og:url")] if probe.meta.get("og:url") else []) + probe.links:
                    if not link or urlparse(link).scheme != "https" or _host(link) in {"news.google.com", "google.com"}: continue
                    if link not in choices: choices.append(link)
                if choices:
                    target = choices[0]
                    request = self._fetch(target)
                    raw, final_url = request["body"], request["final_url"]
                    page = raw.decode("utf-8", errors="replace")
        except Exception as exc:
            elapsed = time.monotonic() - fetch_started
            resolver_diagnostics["article_extraction"] = "not reached"
            resolver_diagnostics["failure_stage"] = "article-retrieval"
            resolver_diagnostics["safe_exception_type"] = type(exc).__name__
            resolver_diagnostics["safe_exception_detail"] = "bounded request timed out" if _is_timeout_error(exc) else "article request failed; raw message suppressed"
            reason = ("TIMEOUT (provider network request exceeded its bounded timeout)"
                      if _is_timeout_error(exc) else f"request failed: {type(exc).__name__}")
            with self._timing_lock:
                self._timings["request_wall"] += elapsed
                self._timings["request_sum"] += elapsed
            return Article(**{**asdict(article), "retrieved_at": _now(), "evidence": None, "accessible": False,
                              "rejection_reason": reason, "request_seconds": elapsed, "requested_url": locals().get("target"),
                              "candidate_urls": locals().get("target_candidates", []),
                              "candidate_discovery": locals().get("discovery", []),
                              "resolver_diagnostics": resolver_diagnostics})
        elapsed = request["request_seconds"]
        with self._timing_lock:
            self._timings["request_wall"] += time.monotonic() - fetch_started
            self._timings["request_sum"] += elapsed
            self._timings["redirect_count"] += request["redirect_count"]
            self._timings["fetch"] += elapsed
        extract_started = time.monotonic()
        resolver_diagnostics["article_extraction"] = "started"
        kind = _response_kind(request["content_type"], raw)
        issue = _page_issue(page, request["status"])
        resolved_original = article.original_source_url or (target if target != article.url and _host(target) != "news.google.com" else None)
        base = {"http_status": request["status"], "final_url": final_url,
                "redirect_count": request["redirect_count"], "content_type": request["content_type"],
                "response_kind": kind, "page_issue": issue, "request_seconds": elapsed,
                "google_news_url": article.google_news_url or (article.url if _host(article.url) == "news.google.com" else None),
                "original_source_url": resolved_original, "candidate_urls": target_candidates,
                "candidate_discovery": discovery, "requested_url": target,
                "candidate_verified": article.candidate_verified,
                "resolver_diagnostics": resolver_diagnostics,
                "token_decoded": article.token_decoded,
                "token_decode_error": article.token_decode_error}
        if request["status"] >= 400:
            resolver_diagnostics["article_extraction"] = "not attempted: HTTP error"
            resolver_diagnostics["failure_stage"] = "article-retrieval"
            return Article(**{**asdict(article), **base, "evidence": None, "accessible": False,
                              "rejection_reason": f"HTTP {request['status']}: {issue or 'response rejected'}"})
        if kind != "HTML":
            resolver_diagnostics["article_extraction"] = "not attempted: non-HTML response"
            resolver_diagnostics["failure_stage"] = "article-retrieval"
            return Article(**{**asdict(article), **base, "evidence": None, "accessible": False,
                              "rejection_reason": f"expected HTML article page, received {kind}"})
        if issue:
            resolver_diagnostics["article_extraction"] = "not attempted: page issue"
            resolver_diagnostics["failure_stage"] = "article-retrieval"
            return Article(**{**asdict(article), **base, "evidence": None, "accessible": False,
                              "rejection_reason": "publisher_captcha" if issue == "CAPTCHA page" else issue})
        parser = _TextExtractor()
        try: parser.feed(page)
        except Exception: pass
        meta = parser.meta
        headline = meta.get("og:title") or meta.get("twitter:title") or article.headline
        observed_headline = meta.get("og:title") or meta.get("twitter:title")
        candidate_verified = bool(resolved_original and observed_headline and
                                  _same_headline(observed_headline, article.headline, article.publisher))
        if used_search_fallback and not candidate_verified:
            return Article(**{**asdict(article), **base, "evidence": None, "accessible": False,
                              "candidate_verified": False,
                              "rejection_reason": "publisher candidate did not verify against the exact article headline"})
        date = (meta.get("article:published_time") or meta.get("datepublished") or
                meta.get("date") or meta.get("pubdate") or article.publication_date)
        # Keep text from the article page that overlaps the headline; page chrome
        # and the RSS summary alone cannot support a verification decision.
        structured = _jsonld_article_bodies(page)
        semantic = parser.article_parts
        method = "json-ld articleBody" if structured else "semantic article/main" if semantic else "readable page text"
        raw_candidates = structured or semantic or parser.parts
        text_candidates = [re.sub(r"\s+", " ", part).strip() for part in raw_candidates if len(part.strip()) >= 45]
        terms = {word.casefold() for word in re.findall(r"[A-Za-z0-9]{4,}", headline)
                 if word.casefold() not in {"this", "that", "with", "from", "after", "about", "game"}}
        scored = sorted(((sum(term in part.casefold() for term in terms), part) for part in text_candidates), reverse=True)
        threshold = max(2, min(4, (len(terms) + 2) // 3)) if len(terms) > 1 else 1
        matching = [part for score, part in scored if score >= threshold]
        evidence = " ".join(matching[:8])[:3500] if matching else None
        if evidence and len(evidence) < 60: evidence = None
        canonical_url = urljoin(final_url, parser.canonical) if parser.canonical else final_url
        if _host(canonical_url) in {"news.google.com", "google.com"}:
            canonical_url = final_url
        final_host = _host(canonical_url) or _host(final_url)
        publisher = article.publisher
        if _host(final_url) != "news.google.com" and final_host not in {"news.google.com", "google.com"}:
            publisher = _publisher_host(final_host)
        extract_ended = time.monotonic()
        with self._timing_lock:
            self._timings["extract"] += extract_ended - extract_started
            previous_start = self._timings.get("extract_started")
            previous_end = self._timings.get("extract_ended")
            self._timings["extract_started"] = min(previous_start, extract_started) if previous_start is not None else extract_started
            self._timings["extract_ended"] = max(previous_end, extract_ended) if previous_end is not None else extract_ended
        if not evidence:
            resolver_diagnostics["article_extraction"] = "no usable article text"
            resolver_diagnostics["failure_stage"] = "article-extraction"
            reason = ("HTML contained no sufficiently long text relevant to headline terms" if text_candidates
                      else "HTML contained no sufficiently long text or substantial article text")
            base.update(extraction_method=method, extracted_text_length=len(" ".join(text_candidates)),
                        candidate_verified=candidate_verified or article.candidate_verified)
            return Article(url=canonical_url, publisher=publisher, headline=headline[:300],
                           publication_date=_iso_date(date) or article.publication_date,
                           retrieved_at=_now(), evidence=None, accessible=False, rejection_reason=reason, **base)
        base.update(extraction_method=method, extracted_text_length=len(evidence),
                    candidate_verified=candidate_verified or article.candidate_verified)
        resolver_diagnostics["article_extraction"] = "article text extracted"
        base["original_source_url"] = resolved_original or (final_url if _host(final_url) not in {"news.google.com", "google.com"} else None)
        return Article(url=canonical_url, publisher=publisher, headline=headline[:300],
                       publication_date=_iso_date(date) or article.publication_date,
                       retrieved_at=_now(), evidence=evidence, accessible=True, **base)

    def resolve_discovery_candidate(self, article: Article) -> Article:
        """Resolve opaque Google News links before discovery quality filtering."""
        if article.original_source_url and _host(article.original_source_url) not in {"google.com", "news.google.com"}:
            return article
        if _host(article.url) != "news.google.com":
            return article
        target = _google_link_target(article.url)
        expected = (_publisher_host(_host(article.publisher_url))
                    if article.publisher_url and _host(article.publisher_url) not in {"google.com", "news.google.com"}
                    else _known_publisher_host(article.publisher))
        reason = None
        if target and expected and _publisher_host(_host(target)) != expected:
            target, reason = None, "resolved publisher domain did not match feed publisher"
        diagnostics = {"google_news_url": _safe_url(article.url),
                      "publisher_url": _safe_url(article.publisher_url)}
        if not target and expected:
            target, reason = _resolve_google_token(article.url, expected, timeout=self.timeout,
                                                   diagnostics=diagnostics)
        diagnostics.update(candidate_selected_for_validation=_safe_url(target), failure=reason if not target else None)
        article.resolver_diagnostics = {**article.resolver_diagnostics, **diagnostics}
        if target:
            article.original_source_url = target
            article.token_decoded = True
            article.token_decode_error = None
        return article


def diagnose_explicit_google_urls(urls: list[str], provider: GoogleNewsRSSProvider | None = None) -> dict:
    """Diagnose supplied Google News article URLs independently, without discovery/ranking."""
    provider = provider or GoogleNewsRSSProvider()
    results = []
    for url in urls:
        host = _host(url)
        token = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        known = {
            "CBMiiAFBVV95cUxNdW9aZ3o1S3I4RVZfX016SXRVZkM1RER0VmY4Rm9udDZWM2lGUEcwSERkY0x3ejJNODNqd2tFQVhtRmUzdXV0VmE0TEZiZ2hXb0I4TVRNNTlKb0JITlRFbWpBX20tNW9wZVRxTHlIMi1oRUZWQ0RpMUU4UzVHbHJyUURQVlBfcFVN": ("The Independent", "independent.co.uk"),
            "CBMiaEFVX3lxTE9kVjVUSDRhOFdEeDNEM0J3Wm9IdFNjWDNEdm1FQUdRMXZvUW9YaC1Vek5ZZHRuTmtvWkY0anVaV282aXVLMl9rTHd3bVpDM1lvZjIzQ2szVEtlLS1QaGxoZ2pwV3ZkZmZk": ("Red Bull", "redbull.com"),
        }
        publisher, expected = known.get(token, ("unknown", None))
        diag = {"input_url": _safe_url(url), "publisher": publisher,
                "expected_publisher_domain": expected, "failure_stage": None,
                "validated_publisher_url": None, "evidence_available": False,
                "candidate_verified": False, "failure_reason": None,
                "signature_page_classification": None, "retry_after_seconds": None}
        if host != "news.google.com" or urlparse(url).scheme != "https" or not expected:
            diag.update(failure_stage="input-validation", failure="URL is not one of the two configured HTTPS Google News article URLs")
            results.append(diag)
            continue
        try:
            article = provider.open(Article(url=url, publisher=publisher, headline=publisher,
                                            google_news_url=url, diagnostic_explicit=True))
            diag["resolver"] = article.resolver_diagnostics
            resolver = article.resolver_diagnostics
            diag["validated_publisher_url"] = _safe_url(resolver.get("validated_publisher_url"))
            diag["evidence_available"] = article.evidence is not None
            diag["candidate_verified"] = bool(article.candidate_verified)
            diag["failure_stage"] = resolver.get("failure_stage") or resolver.get("failure")
            diag["failure_reason"] = article.rejection_reason or resolver.get("failure")
            for field in ("signature_page_classification", "retry_after_seconds"):
                diag[field] = resolver.get(field)
            diag["article_retrieval"] = {"status": article.http_status, "response_kind": article.response_kind,
                "final_url": _safe_url(article.final_url), "redirect_count": article.redirect_count,
                "content_type": article.content_type, "extraction_method": article.extraction_method,
                "extracted_text_length": article.extracted_text_length,
                "article_extraction": article.resolver_diagnostics.get("article_extraction", "not reached"),
                "failure": article.rejection_reason}
        except Exception as exc:
            diag["failure_stage"] = "article-diagnostic"
            diag["safe_exception_type"] = type(exc).__name__
            diag["safe_exception_detail"] = "diagnostic raised an exception; raw message suppressed"
            diag["failure"] = "diagnostic failed; exception message suppressed"
        results.append(diag)
    return {"mode": "explicit-google-news-url-diagnostic", "articles": results}


class OfficialAnnouncementsProvider:
    """First-party announcement discovery, with page retrieval delegated to the original host."""
    name = "official"
    def __init__(self, rss: GoogleNewsRSSProvider | None = None): self.rss = rss or GoogleNewsRSSProvider()
    def search(self, query: str) -> list[Article]:
        return self.rss.search(f'{query} site:rockstargames.com OR site:rockstarnewswire.com OR site:take2games.com')
    def open(self, article: Article) -> Article: return self.rss.open(article)


class IndependentPublisherProvider:
    """Established gaming and general-news publisher discovery and original-page retrieval."""
    name = "publishers"
    def __init__(self, rss: GoogleNewsRSSProvider | None = None): self.rss = rss or GoogleNewsRSSProvider()
    def search(self, query: str) -> list[Article]:
        return self.rss.search(f'{query} site:ign.com OR site:gamespot.com OR site:eurogamer.net OR site:pcgamer.com OR site:polygon.com OR site:reuters.com OR site:apnews.com')
    def open(self, article: Article) -> Article: return self.rss.open(article)


class MultiSourceNewsProvider:
    """Bounded multi-platform adapter; Google News discovers links but does not verify them."""
    def __init__(self, providers: list[RecordProvider] | None = None, rss: GoogleNewsRSSProvider | None = None,
                 official=None, publishers=None):
        self.record_providers = providers if providers is not None else [XAPIProvider(), YouTubeDataAPIProvider(), InstagramProvider()]
        self.rss = rss or GoogleNewsRSSProvider()
        self.official = official or OfficialAnnouncementsProvider(self.rss)
        self.publishers = publishers or IndependentPublisherProvider(self.rss)
        self.availability: dict[str, str] = {}
        self.discovery_diagnostics: dict[str, dict] = {}
        self.discovery_executor: dict[str, object] = {}
        self._records: dict[str, SourceRecord] = {}
        self._timings = self.rss._timings

    def search(self, query: str) -> list[Article]:
        search_started = time.monotonic()
        found: list[Article] = []
        self.availability = {}
        for provider in self.record_providers:
            configured = (bool(provider.token) if isinstance(provider, XAPIProvider) else
                          bool(provider.api_key) if isinstance(provider, YouTubeDataAPIProvider) else
                          bool(provider.token and provider.account_id)
                          if isinstance(provider, InstagramProvider) else True)
            if not configured:
                self.availability[provider.name] = "NOT CONFIGURED"
        # RSS can find candidate links, but its feed entries are never evidence.
        self.availability["Google News RSS (discovery)"] = "discovery only"
        self.discovery_diagnostics = {}
        self.discovery_executor = {}
        worker_count = max(1, len(self.record_providers) + 3)
        pool = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="news-search")
        try:
            tasks = [(p.name, p.search_records) for p in self.record_providers]
            tasks += [("Google News RSS (discovery)", self.rss.search),
                      ("Official sites (discovery)", self.official.search),
                      ("Independent publishers (discovery)", self.publishers.search)]
            # Each worker returns its own timing record. A still-running worker cannot
            # mutate provider state or the report after the deadline snapshot is made.
            worker_starts = {}
            worker_lock = threading.Lock()
            def event_sink(name):
                def emit(stage, **details):
                    with worker_lock:
                        diagnostic = self.discovery_diagnostics.setdefault(name, {
                            "status": "running", "stage_events": []})
                        diagnostic["stage_events"].append({"stage": stage,
                            "elapsed_seconds": round(time.monotonic() - search_started, 6), **details})
                return emit
            def invoke(name, fn):
                began = time.monotonic()
                with worker_lock:
                    worker_starts[name] = began
                    prior_events = self.discovery_diagnostics.get(name, {}).get("stage_events", [])
                    self.discovery_diagnostics[name] = {"status": "running", "stage_events": [*prior_events, {
                        "stage": "task started", "elapsed_seconds": round(began - search_started, 6)}]}
                rss = getattr(fn, "__self__", None)
                if not isinstance(rss, GoogleNewsRSSProvider):
                    rss = getattr(getattr(fn, "__self__", None), "rss", None)
                if isinstance(rss, GoogleNewsRSSProvider):
                    rss._search_local.stage_callback = event_sink(name)
                try:
                    rows = fn(query)
                    completed = time.monotonic()
                    diagnostic = {"status": "completed", "started_monotonic": began, "completed_monotonic": completed,
                                        "elapsed_seconds": time.monotonic() - began,
                                        "http_status": None, "response_size": None,
                                        "results_parsed": len(rows), "exception": None,
                                        "stage_events": self.discovery_diagnostics.get(name, {}).get("stage_events", [])}
                    diagnostic["stage_events"].append({"stage": "task completed",
                        "elapsed_seconds": round(completed - search_started, 6), "result_count": len(rows)})
                    return name, rows, diagnostic
                except Exception as exc:
                    code = getattr(exc, "code", None)
                    completed = time.monotonic()
                    diagnostic = {"status": "failed", "started_monotonic": began, "completed_monotonic": completed,
                                       "elapsed_seconds": time.monotonic() - began,
                                       "http_status": code, "response_size": None,
                                       "results_parsed": 0,
                                       "exception": f"{type(exc).__name__} (HTTP {code})" if code else type(exc).__name__,
                                       "failure_stage": _provider_failure_stage(exc),
                                       "safe_exception_detail": _safe_provider_failure(exc),
                                       "stage_events": self.discovery_diagnostics.get(name, {}).get("stage_events", [])}
                    diagnostic["stage_events"].append({"stage": "task failed",
                        "elapsed_seconds": round(completed - search_started, 6), "exception": diagnostic["exception"]})
                    return name, exc, diagnostic
                finally:
                    if isinstance(rss, GoogleNewsRSSProvider):
                        try: del rss._search_local.stage_callback
                        except AttributeError: pass
            for name, fn in tasks:
                del fn
                self.discovery_diagnostics[name] = {"status": "submitted", "stage_events": [{
                    "stage": "task submitted", "elapsed_seconds": round(time.monotonic() - search_started, 6)}]}
            futures = {pool.submit(invoke, name, fn): name for name, fn in tasks}
            done = set()
            try:
                remaining = max(0.0, SEARCH_WAIT_SECONDS - (time.monotonic() - search_started))
                for future in as_completed(futures, timeout=remaining):
                    done.add(future)
                    name = futures[future]
                    try:
                        name, rows, diagnostic = future.result()
                        self.discovery_diagnostics[name] = diagnostic
                        if isinstance(rows, BaseException): raise rows
                        self.availability[name] = (f"checked ({len(rows)} result(s); discovery only)"
                                                   if name.endswith("discovery)")
                                                   else f"checked ({len(rows)} result(s))")
                        if name.endswith("discovery)"):
                            for a in rows:
                                a.platform = "web-discovery"
                                found.append(a)
                        else:
                            for rec in rows:
                                if not rec.original_url.startswith("https://"): continue
                                self._records[rec.original_url] = rec
                                found.append(Article(rec.original_url, rec.author or name, (rec.content.splitlines() or [""])[0][:300],
                                    rec.original_published_at or rec.published_at, rec.retrieved_at, rec.content or None, bool(rec.content),
                                    platform=rec.platform, author=rec.author, account_id=rec.account_id,
                                    content_id=rec.content_id, engagement=rec.engagement, authenticity=rec.authenticity,
                                    relevance_score=rec.relevance_score, limitations=rec.limitations,
                                    is_repost=rec.is_repost, source_type=rec.source_type,
                                    reshared_at=rec.reshared_at, post_kind=rec.post_kind,
                                    content_kind=("rumor/leak/speculation" if re.search(r"\b(rumou?r|leak|speculation|alleged)\b", rec.content, re.I)
                                                  else "commentary" if rec.platform == "YouTube" and not rec.authenticity == "official_account"
                                                  else "official trailer/announcement" if rec.authenticity == "official_account" and re.search(r"\b(trailer|announce|update)\b", rec.content, re.I)
                                                  else "community discussion")))
                    except Exception as exc:
                        code = getattr(exc, "code", None)
                        message = str(exc).casefold()
                        missing = ("not configured" in message or
                                   ("required" in message and any(word in message for word in ("token", "key", "account id"))))
                        issue = f"HTTP {code} rate limited" if code == 429 else f"HTTP {code} access denied" if code in {401, 403} else type(exc).__name__
                        self.availability[name] = ("NOT CONFIGURED" if missing else
                                                   f"TIMEOUT ({issue})" if _is_timeout_error(exc) else
                                                   f"UNAVAILABLE ({issue})")
            except FuturesTimeoutError:
                pass
            for future, name in futures.items():
                if future not in done:
                    cancelled = future.cancel()
                    prior = self.discovery_diagnostics.get(name, {})
                    self.discovery_diagnostics[name] = {**prior, "started_monotonic": worker_starts.get(name), "completed_monotonic": None,
                        "elapsed_seconds": time.monotonic() - worker_starts.get(name, search_started),
                        "http_status": None, "response_size": None,
                        "results_parsed": 0, "exception": None,
                        "status": "cancelled before start" if cancelled else "still running at overall deadline"}
                    self.discovery_diagnostics[name]["stage_events"].append({"stage": "application deadline",
                        "elapsed_seconds": round(time.monotonic() - search_started, 6),
                        "worker_state": "cancelled before start" if cancelled else "still running"})
                    self.availability[name] = (f"APPLICATION DEADLINE ({SEARCH_WAIT_SECONDS}s; "
                                               f"{'cancelled before start' if cancelled else 'still running'}; network outcome unknown)")
            for name, diagnostic in self.discovery_diagnostics.items():
                diagnostic.setdefault("status", "completed")
        finally:
            # Running calls may finish later; their results are never collected or
            # written into availability. Network calls themselves have deadlines.
            pool.shutdown(wait=False, cancel_futures=True)
        self.discovery_executor = {"max_workers": worker_count, "submitted_task_count": len(tasks),
                                   "shared_executor": True}
        return found

    def open(self, article: Article) -> Article:
        if article.platform != "web-discovery": return article
        opened = (self.official if any((_host(article.url) == d or _host(article.url).endswith("." + d)) for d in OFFICIAL_DOMAINS)
                  else self.publishers).open(article)
        opened.platform = "web"
        host = _host(opened.url)
        if any(host == d or host.endswith("." + d) for d in OFFICIAL_DOMAINS):
            opened.source_type = "OFFICIAL ANNOUNCEMENT"
            opened.authenticity = "official_domain"
        elif _is_reputable(opened):
            opened.source_type = "INDEPENDENT REPORTING"
            opened.authenticity = "established_publisher"
        return opened

    def resolve_discovery_candidate(self, article: Article) -> Article:
        if article.platform == "web-discovery":
            return self.rss.resolve_discovery_candidate(article)
        return article


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_primary(article: Article) -> bool:
    if article.is_repost: return False
    if article.authenticity == "official_account" and article.platform.casefold() in {"x", "youtube", "instagram"}:
        return True
    if article.platform not in {"web", "web-discovery"}: return False
    host = _host(article.url)
    publisher = article.publisher.casefold()
    return (any(host == domain or host.endswith("." + domain) for domain in OFFICIAL_DOMAINS)
            or any(name in publisher for name in ("rockstar games", "take-two interactive")))


def _is_reputable(article: Article) -> bool:
    host = _host(article.url)
    publisher = article.publisher.casefold()
    return (_is_primary(article) or any(host == domain or host.endswith("." + domain) for domain in TRUSTED_PUBLISHERS)
            or any(name in publisher for name in ("reuters", "associated press", "bbc", "ign", "gamespot", "eurogamer", "polygon", "pc gamer")))


def _deduplicate(articles: list[Article], *, dedupe_titles: bool = True) -> list[Article]:
    result, seen_urls, seen_titles = [], set(), []
    def priority(article):
        try: published = datetime.fromisoformat((article.publication_date or "").replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError): published = 0
        return (not _is_primary(article), -published)
    for article in sorted(articles, key=priority):
        url_key = article.url.split("?")[0].rstrip("/").casefold()
        title_key = set(re.findall(r"[a-z0-9]+", article.headline.casefold()))
        syndicated_match = any(
            len(title_key & prior) >= 4 and len(title_key & prior) / max(1, len(title_key | prior)) >= .78
            for prior in seen_titles
        ) if dedupe_titles and title_key else False
        if url_key in seen_urls or syndicated_match: continue
        seen_urls.add(url_key)
        if title_key: seen_titles.append(title_key)
        result.append(article)
    return result


def _article_url_rejection(article: Article) -> str | None:
    """Reject feed entries that point at publisher landing pages, not stories."""
    # RSS `source url` is often the publisher's homepage, while the same item
    # carries its story URL in `original_source_url`. Check all known candidates
    # and accept if any is a safe HTTPS article URL.
    candidates = list(dict.fromkeys(u for u in (article.original_source_url, article.publisher_url, article.url)
                                    if u and _host(u) not in {"news.google.com", "google.com"}))
    failures = []
    for raw in candidates:
        parsed = urlparse(raw)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            failures.append("invalid or non-HTTPS article URL")
            continue
        path = re.sub(r"/+", "/", parsed.path or "/").casefold().rstrip("/")
        if not path:
            failures.append("publisher homepage is not an individual article")
            continue
        # Only reject unmistakable utility routes or exact section roots. Many
        # publishers put article IDs/slugs under /news, /science, etc.
        if path in {"/news", "/world", "/business", "/technology", "/tech", "/science",
                    "/entertainment", "/latest", "/search", "/tag", "/tags", "/category", "/categories"}:
            failures.append("publisher category or landing page is not an individual article")
            continue
        if re.search(r"/(?:search|tag|tags|category|categories)(?:/|$)", path):
            failures.append("publisher search, tag, or category page is not an individual article")
            continue
        if re.search(r"/(?:news|world|business|technology|tech|science|entertainment|latest)/(?:page/)?\d+/?$", path):
            failures.append("publisher paginated landing page is not an individual article")
            continue
        break
    else:
        # Prefer a homepage explanation when no article candidate was present.
        return next((reason for reason in failures if "homepage" in reason),
                    failures[0] if failures else "invalid or non-HTTPS article URL")
    title = re.sub(r"\s+", " ", (article.headline or "")).strip()
    if len(re.findall(r"\w+", title)) < 2 or title.casefold() in {
        "home", "latest news", "news", "top stories", "breaking news", "business news", "world news"
    }:
        return "headline is generic or incomplete"
    if not article.publisher or article.publisher.casefold() in {"", "unknown", "news"}:
        return "publisher name is missing or generic"
    return None


def _classify(articles: list[Article], is_rumor: bool = False) -> tuple[str, str, str, str]:
    usable = [a for a in articles if a.accessible and a.evidence]
    primary = [a for a in usable if _is_primary(a)]
    trusted = [a for a in usable if _is_reputable(a)]
    # Republished copies that point to the same original article count once.
    independent_domains = set()
    seen_origins = set()
    for article in trusted:
        origin = article.original_source_url or article.publisher_url or article.url
        origin_host = _publisher_host(_host(origin))
        publisher_host = _publisher_host(_host(article.url))
        identity = origin.casefold().rstrip("/")
        if identity in seen_origins:
            continue
        seen_origins.add(identity)
        independent_domains.add(origin_host or publisher_host)
    positive = [a for a in trusted if re.search(r"\b(confirmed|announced|will|released|launch|date|delayed|delay|official|revealed|plans?)\b", a.headline, re.I)]
    negative = [a for a in trusted if re.search(r"\b(not|no longer|denied|false|debunk|untrue|unchanged)\b", a.headline, re.I)]
    if positive and negative:
        status, disagreement = "DISPUTED", "Reliable sources make materially different claims; the available pages do not resolve the conflict."
    elif is_rumor:
        status, disagreement = "UNVERIFIED", "The item is described as a rumor or leak and has no confirming primary statement."
    elif primary:
        status, disagreement = "CONFIRMED", "An accessible primary source supports the reported statement."
    elif positive and len(independent_domains) >= 2:
        status, disagreement = "DEVELOPING", "Multiple independent reputable reports support the claim, but no primary confirmation was found."
    elif positive:
        status, disagreement = "DEVELOPING", "Credible reporting exists; independent corroboration or primary confirmation is limited."
    else:
        status, disagreement = "UNVERIFIED", "Accessible reliable evidence sufficient to verify this claim was not found."
    confirmed = ("The source confirms only the announcement as stated; broader implications may remain uncertain." if primary
                 else "No primary confirmation found." if usable else
                 "No accessible evidence supporting this specific claim was found.")
    uncertain = "Rumors, interpretation, and details beyond the cited reporting remain unconfirmed."
    return status, confirmed, uncertain, disagreement


def _supports_requested_claim(article: Article, claim: str) -> bool:
    stop = {"about", "again", "announcement", "announced", "claim", "news", "this", "that",
            "what", "with", "from", "official", "please", "heard", "game", "the", "is", "was"}
    terms = {term.casefold() for term in re.findall(r"[^\W_]+", claim, re.UNICODE)
             if len(term) >= 3 and term.casefold() not in stop}
    if not terms:
        return False
    text = f"{article.headline} {article.evidence or ''}".casefold()
    def matches(term):
        variants = {term}
        if term.endswith("ed") and len(term) > 4: variants.add(term[:-2])
        if term.endswith("y") and len(term) > 4: variants.add(term[:-1] + "ied")
        return any(variant in text for variant in variants)
    matched = sum(matches(term) for term in terms)
    required = min(2, len(terms))
    return article.accessible and bool(article.evidence) and matched >= required


def research_news(topic_or_claim: str, provider: NewsProvider | None = None, *, verify_claim: bool = False,
                  latest: bool | None = None) -> NewsReport:
    """Search sources, inspect pages, deduplicate and return conservative assessments."""
    topic = (topic_or_claim or "").strip()
    timestamp = _now()
    started = time.monotonic()
    metrics = {"rss_search_seconds": 0.0, "results_received": 0, "redirect_resolution_seconds": 0.0,
               "article_fetch_seconds": 0.0, "article_extraction_seconds": 0.0,
               "accessible_articles": 0, "total_elapsed_seconds": 0.0, "latest_requested": False}
    if not topic:
        return NewsReport("(unspecified)", timestamp, "No topic or claim was supplied.", [], metrics)
    provider = provider or MultiSourceNewsProvider()
    timing_provider = provider.rss if isinstance(provider, MultiSourceNewsProvider) else provider
    if isinstance(timing_provider, GoogleNewsRSSProvider):
        with timing_provider._timing_lock:
            timing_provider._timings.update(search=[], results=0, request_wall=0.0, request_sum=0.0,
                                             redirect_count=0, fetch=0.0, extract=0.0,
                                             extract_started=None, extract_ended=None)
    query = topic if verify_claim else f"{topic} news"
    search_started = time.monotonic()
    searches = []
    search_failed = False
    search_errors = []
    terms = (query,) if isinstance(provider, MultiSourceNewsProvider) else (query, f"{topic} official announcement")
    for term in terms:
        try: searches.append(provider.search(term))
        except Exception as exc:
            searches.append([])
            search_failed = True
            search_errors.append({"query": term, "exception_type": type(exc).__name__,
                                  "failure_stage": _provider_failure_stage(exc),
                                  "reason": _safe_provider_failure(exc)})
    metrics["search_errors"] = search_errors
    if not any(searches):
        metrics["rss_search_seconds"] = time.monotonic() - search_started
        metrics["total_elapsed_seconds"] = time.monotonic() - started
        availability = getattr(provider, "availability", {})
        metrics["platforms"] = availability
        if isinstance(provider, MultiSourceNewsProvider):
            metrics["discovery_diagnostics"] = {k: dict(v) for k, v in provider.discovery_diagnostics.items()}
            metrics["discovery_executor"] = dict(provider.discovery_executor)
        metrics["provider_errors"] = {name: value for name, value in metrics.get("discovery_diagnostics", {}).items()
                                       if value.get("status") != "completed" or value.get("exception")}
        summary = ("News search failed; article evidence could not be retrieved. Please try again later."
                   if search_failed else "No search results were returned; no claim could be verified.")
        return NewsReport(topic, timestamp, summary, [], metrics)
    metrics["rss_search_seconds"] = time.monotonic() - search_started
    discovered = _deduplicate([article for group in searches for article in group],
                              dedupe_titles=not verify_claim and not isinstance(provider, MultiSourceNewsProvider))
    metrics["results_received"] = len(discovered)
    rejected_discovery = []
    accepted_discovery = []
    url_diagnostics = []
    resolver_candidates = 0
    resolver_skipped = 0
    # Resolution is slow and only needed for opaque Google News URLs. Select a
    # bounded shortlist using headline relevance and source/date quality before
    # spending that budget; feed order is not a useful priority signal.
    unresolved = [a for a in discovered if _host(a.url) == "news.google.com" and not
        (a.original_source_url and _host(a.original_source_url) not in {"google.com", "news.google.com"})]
    query_terms = {term for term in re.findall(r"[a-z0-9]{3,}", topic.casefold())
                   if term not in {"news", "latest", "today", "india", "global", "official"}}
    def resolution_priority(article):
        headline_terms = set(re.findall(r"[a-z0-9]{3,}", article.headline.casefold()))
        overlap = len(query_terms & headline_terms)
        dated = bool(article.publication_date)
        try:
            if not dated:
                raise ValueError("missing publication date")
            published = datetime.fromisoformat(article.publication_date.replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            age = max(0.0, (datetime.now(timezone.utc) - published.astimezone(timezone.utc)).total_seconds())
        except (ValueError, TypeError):
            age = float("inf")
        return (-overlap, not dated, age, not _is_primary(article), not _is_reputable(article), article.headline.casefold())
    selected_for_resolution = {id(a) for a in sorted(unresolved, key=resolution_priority)[:5]}
    for article in discovered:
        resolver = getattr(provider, "resolve_discovery_candidate", None)
        needs_resolution = (_host(article.url) == "news.google.com" and not
            (article.original_source_url and _host(article.original_source_url) not in {"google.com", "news.google.com"}))
        if resolver:
            if needs_resolution and id(article) not in selected_for_resolution:
                resolver_skipped += 1
                article.resolver_diagnostics = {**article.resolver_diagnostics,
                    "result": "unavailable", "failure": "stage=resolver-budget: per-run resolution limit reached",
                    "failure_stage": "resolver-budget"}
            else:
                if needs_resolution: resolver_candidates += 1
                try:
                    article = resolver(article)
                except Exception as exc:
                    article.resolver_diagnostics = {**article.resolver_diagnostics,
                        "failure_stage": "google-news-resolution", "safe_exception_type": type(exc).__name__}
        reason = _article_url_rejection(article)
        if reason:
            rejected_discovery.append({"headline": article.headline, "publisher": article.publisher,
                                      "url": _safe_url(article.url), "rejection_reason": reason,
                                      "stage": "discovery-quality"})
            if len(url_diagnostics) < 5:
                validation_candidate = next((u for u in (article.original_source_url, article.publisher_url, article.url)
                    if u and _host(u) not in {"google.com", "news.google.com"}), None)
                url_diagnostics.append({"publisher": article.publisher, "headline": article.headline[:180],
                    "original_source_url": _safe_url(article.original_source_url),
                    "publisher_url": _safe_url(article.publisher_url),
                    "resolved_final_url": _safe_url(article.final_url),
                    "candidate_selected_for_validation": _safe_url(validation_candidate),
                    "stage": "discovery-quality", "reason": reason,
                    "resolution_failure": article.resolver_diagnostics.get("failure")})
        else:
            accepted_discovery.append(article)
    discovered = accepted_discovery
    metrics["articles_rejected"] = rejected_discovery
    resolver_failures = Counter(article.resolver_diagnostics.get("failure") for article in discovered
                                if article.resolver_diagnostics.get("failure"))
    metrics["google_url_resolution"] = {"attempted": resolver_candidates, "skipped_budget": resolver_skipped,
                                         "failures": dict(resolver_failures),
                                         "per_request_timeout_seconds": (min(timing_provider.timeout, 12)
                                            if hasattr(timing_provider, "timeout") else None),
                                         "policy": "maximum five relevance-ranked discovery resolutions per query; no retries"}
    metrics["url_validation_diagnostics"] = url_diagnostics
    metrics["platforms"] = getattr(provider, "availability", {"Google News RSS": "checked (discovery only)"})
    if isinstance(provider, MultiSourceNewsProvider):
        metrics["discovery_diagnostics"] = {k: dict(v) for k, v in provider.discovery_diagnostics.items()}
        metrics["discovery_executor"] = dict(provider.discovery_executor)
    metrics["provider_errors"] = {name: value for name, value in metrics.get("discovery_diagnostics", {}).items()
                                   if value.get("status") != "completed" or value.get("exception")}
    latest = (bool(re.search(r"\b(latest|today|recent|current|newest)\b", topic, re.I))
              if latest is None else latest) and not verify_claim
    metrics["latest_requested"] = latest
    reference_time = datetime.now(timezone.utc)
    def rank(a):
        date = a.publication_date
        age = 10**9
        if date:
            try: age = (reference_time - datetime.fromisoformat(date.replace("Z", "+00:00"))).total_seconds()
            except ValueError: pass
        stale = (not date) or age > 90 * 86400
        return ((stale, age, not _is_primary(a), not _is_reputable(a)) if latest else
                (not _is_primary(a), not _is_reputable(a), age))
    stale_rejected = []
    if latest:
        for article in discovered:
            if not article.publication_date or rank(article)[0]:
                stale_rejected.append({"headline": article.headline, "publisher": article.publisher,
                    "url": _safe_url(article.url), "rejection_reason": "publication date missing, malformed, or older than 90 days",
                    "stage": "freshness-filter"})
        discovered = [a for a in discovered if a.publication_date and not rank(a)[0]]
    metrics["articles_rejected"].extend(stale_rejected)
    discovered = sorted(discovered, key=rank)[:12]
    inspected = []
    # A small worker pool bounds latency while avoiding excessive publisher load.
    inspect_started = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="news-article")
    futures = {pool.submit(provider.open, article): article for article in discovered}
    completed = set()
    try:
        for future in as_completed(futures, timeout=ARTICLE_WAIT_SECONDS):
            completed.add(future)
            source = futures[future]
            try:
                inspected.append(future.result())
            except Exception as exc:
                inspected.append(Article(**{**asdict(source), "retrieved_at": _now(), "evidence": None,
                    "accessible": False, "rejection_reason": f"provider.open raised {type(exc).__name__}"}))
    except FuturesTimeoutError:
        pass
    finally:
        for future, source in futures.items():
            if future in completed:
                continue
            future.cancel()
            # Future.cancel cannot stop a running thread. Its eventual result is
            # intentionally detached from this report and represented as TIMEOUT.
            inspected.append(Article(**{**asdict(source), "retrieved_at": _now(), "evidence": None,
                "accessible": False, "rejection_reason": f"TIMEOUT (article retrieval exceeded {ARTICLE_WAIT_SECONDS}s)"}))
        pool.shutdown(wait=False, cancel_futures=True)
    inspect_wall = time.monotonic() - inspect_started
    timings = getattr(timing_provider, "_timings", {})
    metrics["article_request_wall_seconds"] = inspect_wall
    metrics["article_request_seconds_sum"] = float(timings.get("request_sum", timings.get("fetch", 0)))
    metrics["redirect_count"] = int(timings.get("redirect_count", 0))
    metrics["article_fetch_seconds"] = inspect_wall
    metrics["redirect_resolution_seconds"] = 0.0
    metrics["article_extraction_seconds"] = float(timings.get("extract", 0))
    extract_start, extract_end = timings.get("extract_started"), timings.get("extract_ended")
    metrics["article_extraction_wall_seconds"] = max(0.0, extract_end - extract_start) if extract_start is not None and extract_end is not None else 0.0
    metrics["article_attempts"] = [{"headline": a.headline, "publisher": a.publisher,
        "original_publisher_url": a.original_source_url or a.publisher_url,
        "rss_source_name": a.publisher, "rss_metadata": a.rss_metadata,
        "rss_url_fields": [key for key, values in a.rss_metadata.items()
                            if any(("http://" in str(entry) or "https://" in str(entry))
                                   for entry in values)],
        "rss_article_url_present": bool(a.raw_article_urls or
            (a.publisher_url and urlparse(a.publisher_url).path not in {"", "/"})), "raw_article_urls": a.raw_article_urls,
        "token_decoded": a.token_decoded,
        "token_decode_error": a.token_decode_error,
        "resolver_diagnostics": a.resolver_diagnostics,
        "candidate_urls": a.candidate_urls or ([a.original_source_url] if a.original_source_url else []),
        "candidate_discovery": a.candidate_discovery, "candidate_verified": a.candidate_verified,
        "verified_publisher_url": a.original_source_url if a.candidate_verified else None,
        "requested_url": a.requested_url,
        "publication_date": a.publication_date, "rss_url": a.rss_url or a.url,
        "google_news_url": a.google_news_url or (a.url if _host(a.url) == "news.google.com" else None),
        "http_status": a.http_status, "final_url": a.final_url, "final_hostname": _host(a.final_url or ""),
        "redirect_count": a.redirect_count, "content_type": a.content_type, "response_kind": a.response_kind,
        "extraction_method": a.extraction_method, "extracted_text_length": a.extracted_text_length,
        "page_issue": a.page_issue, "rejection_reason": a.rejection_reason,
        "status": "TIMEOUT" if (a.rejection_reason or "").startswith("TIMEOUT") else None} for a in inspected]
    metrics["timed_out_articles"] = sum(bool(a.rejection_reason and a.rejection_reason.startswith("TIMEOUT"))
                                        for a in inspected)
    metrics["latest_established"] = bool(latest and any(a.publication_date and not rank(a)[0] for a in inspected))
    metrics["accessible_articles"] = sum(bool(a.accessible and a.evidence) for a in inspected)
    metrics["articles_extracted"] = metrics["accessible_articles"]
    metrics["articles_rejected"].extend({"headline": a.headline, "publisher": a.publisher,
        "url": _safe_url(a.url), "rejection_reason": a.rejection_reason or a.page_issue or "no usable article evidence",
        "stage": "article-extraction"} for a in inspected if not (a.accessible and a.evidence))
    if inspected and not metrics["accessible_articles"]:
        metrics["total_elapsed_seconds"] = time.monotonic() - started
        metrics["verification_outcomes"] = {"UNABLE_TO_VERIFY": 1}
        metrics["verification_failure_reasons"] = [a.rejection_reason or a.page_issue or "no accessible article evidence"
                                                    for a in inspected]
        summary = ("Article research timed out; no usable publisher evidence was retrieved. Please try again later."
                   if metrics["timed_out_articles"] else
                   "Article evidence could not be retrieved; no usable publisher pages were accessible.")
        return NewsReport(topic, timestamp, summary, [], metrics)
    if not inspected:
        metrics["total_elapsed_seconds"] = time.monotonic() - started
        metrics["verification_outcomes"] = {"UNABLE_TO_VERIFY": 1}
        metrics["verification_failure_reasons"] = ([row["rejection_reason"] for row in metrics["articles_rejected"]]
            or ["no article candidates were available for inspection"])
        summary = ("No recent relevant reporting was accessible; older results are excluded." if latest and metrics["results_received"]
                   else "No search results were returned; no claim could be verified." if not metrics["results_received"]
                   else "Article evidence could not be retrieved; no usable publisher pages were accessible.")
        return NewsReport(topic, timestamp, summary, [], metrics)
    # Search results describe separate articles, not necessarily independent claims.
    # Keep one item centered on a requested claim; roundup results are grouped by headline.
    groups: dict[str, list[Article]] = {}
    if latest:
        inspected.sort(key=rank)
        fresh = [a for a in inspected if a.publication_date and rank(a)[0] is False]
        inspected = fresh
    for article in inspected:
        key = "requested-claim" if verify_claim else re.sub(r"[^a-z0-9]+", " ", article.headline.casefold()).strip()
        groups.setdefault(key, []).append(article)
    items = []
    for group in groups.values():
        if not verify_claim and len(items) >= 5: break
        rumor = bool(re.search(r"\b(rumou?r|leak|alleged|unconfirmed|speculation)\b", " ".join(a.headline + " " + (a.evidence or "") for a in group), re.I))
        evidence_group = ([a for a in group if _supports_requested_claim(a, topic)]
                          if verify_claim else group)
        status, confirmed, uncertain, disagreement = _classify(evidence_group, rumor)
        claim = topic if verify_claim else group[0].headline
        # If multiple outlets all cite the same named original source, count the source once.
        unique = {}
        for article in group:
            key = article.original_source_url or article.url
            unique.setdefault(key, article)
        official = any(_is_primary(a) for a in evidence_group if a.accessible and a.evidence)
        social = any(a.platform not in {"web", "web-discovery"} for a in evidence_group)
        legacy_attribution = "Rumor/leak" if rumor else ("Official announcement" if official else
                              "Social media report" if social else "Independent reporting")
        label = ("RUMOR / LEAK" if rumor else "OFFICIAL ANNOUNCEMENT" if official else
                 "SOCIAL MEDIA REPORT" if social else "INDEPENDENT REPORTING")
        items.append(NewsItem(claim, status, list(unique.values()), confirmed, uncertain, disagreement,
                              legacy_attribution, label))
    statuses = {item.status for item in items}
    metrics["verification_outcomes"] = dict(Counter(item.status for item in items))
    metrics["verification_failure_reasons"] = [item.disagreement for item in items
        if item.status in {"UNABLE_TO_VERIFY", "UNVERIFIED", "DISPUTED"}]
    summary = (f"Partial research returned: {metrics['timed_out_articles']} article retrieval(s) timed out; only retrieved evidence is assessed." if metrics["timed_out_articles"]
               else "Article evidence could not be retrieved; no usable publisher pages were accessible." if not metrics["accessible_articles"]
               else "No recent relevant reporting was accessible; older results are excluded." if latest and not items
               else "No accessible evidence supported a reliable conclusion." if statuses <= {"UNVERIFIED"}
               else f"Found {len(items)} item(s); statuses: {', '.join(sorted(statuses))}. Treat developing reports as provisional.")
    metrics["total_elapsed_seconds"] = time.monotonic() - started
    return NewsReport(topic, timestamp, summary, items, metrics)


def format_report(report: NewsReport, *, include_diagnostics: bool = False) -> str:
    lines = [f"News research: {report.topic}", f"Researched: {report.researched_at}", report.summary]
    if report.metrics.get("platforms"):
        lines.append("Platforms checked: " + "; ".join(f"{name}: {state}" for name, state in report.metrics["platforms"].items()))
    if not report.items: lines.append("No usable article evidence was retrieved.")
    latest = report.metrics.get("latest_requested", bool(re.search(r"\b(latest|today|recent|current|newest)\b", report.topic, re.I)))
    lines.extend(["", "Latest relevant developments" if report.metrics.get("latest_established", latest) else "Reported developments"])
    section_titles = {"Official announcement": "Official announcements", "Independent reporting": "Independent reporting",
                      "Social media report": "Social media reports", "Rumor/leak": "Rumors and leaks", "Reported claim": "Other claims"}
    last_section = None
    for item in report.items:
        if item.attribution != last_section:
            lines.extend(["", section_titles.get(item.attribution, "Reported claims")])
            last_section = item.attribution
        lines.extend(["", f"{item.status} — {item.source_type}: {item.claim}", f"Confirmed: {item.confirmed}", f"Uncertain: {item.uncertain}"])
        if item.disagreement: lines.append(f"Source disagreement: {item.disagreement}")
        for source in item.sources[:4]:
            date = source.publication_date or "publication date unavailable"
            event_date = source.event_date or "not established"
            retrieved = source.retrieved_at or report.researched_at
            evidence = source.evidence[:320].strip() if source.evidence else ("page inaccessible; not used as evidence" if not source.accessible else "no usable page text")
            reshared = f"; reshared: {source.reshared_at}" if source.reshared_at else ""
            kind = f"; post: {source.post_kind}" if source.post_kind else ""
            media_kind = f"; content: {source.content_kind}" if source.content_kind else ""
            lines.append(f"• {source.platform} — {source.publisher} | Published: {date}{reshared}{kind}{media_kind}; event date: {event_date}; retrieved: {retrieved}\n  {source.headline}\n  {source.url}\n  Evidence: {evidence}")
            if source.limitations: lines.append("  Limitations: " + "; ".join(source.limitations[:2]))
    if report.metrics:
        lines.append(f"\nSearch wall {report.metrics.get('rss_search_seconds', 0):.1f}s; {report.metrics.get('results_received', 0)} results; article retrieval wall {report.metrics.get('article_request_wall_seconds', report.metrics.get('article_fetch_seconds', 0)):.1f}s; request durations summed {report.metrics.get('article_request_seconds_sum', 0):.1f}s; redirects followed {report.metrics.get('redirect_count', 0)}; extraction wall {report.metrics.get('article_extraction_wall_seconds', 0):.1f}s (CPU summed {report.metrics.get('article_extraction_seconds', 0):.1f}s); accessible {report.metrics.get('accessible_articles', 0)}; total wall {report.metrics.get('total_elapsed_seconds', 0):.1f}s.")
        if report.metrics.get("article_attempts"):
            for attempt in report.metrics.get("article_attempts", []):
                final = _safe_url(attempt.get("final_url")) or "unresolved"
                detail = (f"HTTP {attempt['http_status']}" if attempt.get("http_status") is not None else "no HTTP response")
                detail += f"; {attempt.get('response_kind') or 'unknown body'}; "
                detail += attempt.get("rejection_reason") or f"extracted {attempt.get('extracted_text_length', 0)} chars"
                lines.append(f"Attempt: {attempt.get('headline', '')[:110]} | publisher {attempt.get('publisher', 'unknown')} "
                             f"({attempt.get('original_publisher_url') or 'original URL undiscovered'}) | "
                             f"Google News {attempt.get('google_news_url') or 'not used'} | "
                             f"{attempt.get('publication_date') or 'date unavailable'} | {detail} | final {final}; "
                             f"redirects {attempt.get('redirect_count', 0)}; {attempt.get('content_type') or 'content type unknown'}; "
                             f"extraction {attempt.get('extraction_method') or 'none'}")
                candidate_info = ", ".join(_safe_url(url) or "[invalid URL]"
                                             for url in attempt.get("candidate_urls", [])[:3]) or "none"
                fallback_count = len(attempt.get("candidate_urls", []))
                resolution = (f"RSS article URL {'present' if attempt.get('rss_article_url_present') else 'absent'}; "
                              f"token {'decoded' if attempt.get('token_decoded') else 'not decoded'}")
                if attempt.get("token_decode_error"): resolution += f" ({attempt['token_decode_error']})"
                lines.append(f"  Resolution: {resolution}; fallback candidates {fallback_count}; {candidate_info}; "
                             f"verified {attempt.get('candidate_verified', False)}; requested {_safe_url(attempt.get('requested_url')) or 'unknown'}")
                resolver = attempt.get("resolver_diagnostics") or {}
                if include_diagnostics and resolver:
                    safe_keys = ("token_extraction", "token_extraction_seconds", "signature_request", "signature_response",
                                 "signature_http_status",
                                 "retry_after_seconds",
                                 "signature_request_seconds", "signature_page_bytes", "signature_page_seconds",
                                 "signature_page_classification", "signature_extracted", "timestamp_extracted",
                                 "signature_timestamp_extraction", "batchexecute_request", "batchexecute_response",
                                 "batchexecute_http_status", "batchexecute_response_bytes", "batchexecute_seconds",
                                 "response_parsed", "publisher_url_extracted", "url_validation",
                                 "publisher_domain_valid", "article_extraction", "failure_stage",
                                 "safe_exception_type", "safe_exception_detail", "total_seconds", "failure")
                    stages = "; ".join(f"{key}={resolver[key]}" for key in safe_keys if key in resolver)
                    lines.append("  Google URL resolver: " + stages)
                lines.append(f"  Final verified publisher URL: {_safe_url(attempt.get('verified_publisher_url')) or 'none (UNVERIFIED)'}")
                if attempt.get("candidate_discovery"):
                    lines.append("  Candidate checks: " + "; ".join(attempt["candidate_discovery"][:8]))
                if attempt.get("rss_metadata"):
                    metadata = json.dumps(attempt["rss_metadata"], ensure_ascii=False, separators=(",", ":"))[:550]
                    lines.append(f"  Raw RSS item metadata (sanitized): {metadata}")
    if latest and not report.metrics.get("latest_established", False):
        dated = any(a.get("publication_date") for a in report.metrics.get("article_attempts", []))
        limitation = ("dated results were older than 90 days, so recent coverage could not be established."
                      if dated else "publication dates were unavailable, so recency could not be established.")
        lines.append("Freshness limitation: " + limitation)
    return "\n".join(lines)[:3900]
