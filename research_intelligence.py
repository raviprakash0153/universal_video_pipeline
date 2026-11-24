"""Local, deterministic topic discovery, clustering, ranking and persistence.

Google News is used only to discover candidate publisher pages. The existing
``research_news`` workflow inspects publisher pages and supplies conservative
claim assessments; ranking never upgrades verification status.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import ipaddress
import json
import re
from urllib.parse import urlparse, urlunparse

import pipeline_store
from news_research import MultiSourceNewsProvider, research_news


CATEGORIES = {
    "technology_ai": ("technology", "artificial intelligence", "AI"),
    "business_economy": ("business", "economy", "markets"),
    "science": ("science", "discovery", "research"),
    "entertainment": ("entertainment", "celebrity"),
    "gaming": ("gaming", "video games"),
    "sports": ("sports",), "politics": ("politics", "government"),
    "international_affairs": ("world", "international affairs"),
    "explainers": ("interesting stories", "explainer"),
    "viral_trends": ("viral internet trends",),
    "consumer_technology": ("consumer technology", "smartphones"),
    "public_interest": ("public interest", "health climate"),
}
DEFAULT_WEIGHTS = {"freshness": .20, "interest": .10, "relevance": .15,
                   "evidence": .25, "originality": .10, "low_saturation": .10,
                   "feasibility": .10}
_STOP = {"with", "from", "that", "this", "have", "after", "into", "over", "will",
         "says", "said", "about", "their", "your", "what", "when", "where", "than",
         "india", "today", "new", "launches", "announces", "reports", "report", "reveals", "update"}
DEFAULT_NOVELTY_LOOKBACK_DAYS = 7
SHORTLIST_SIZE = 10
PUBLISHER_SHORTLIST_LIMIT = 2


def _association_reference(value):
    """Return a normalized usable absolute HTTP(S) URL, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if any(character.isspace() for character in value):
        return None
    try:
        parsed = urlparse(value)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
            return None
        hostname = parsed.hostname
        if not hostname:
            return None
        # Accessing port validates malformed port syntax; hostname checks below
        # reject whitespace and malformed DNS labels without doing DNS lookups.
        _ = parsed.port
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            if parsed.netloc.rsplit("@", 1)[-1].startswith("["):
                return None
            host = hostname.rstrip(".").encode("idna").decode("ascii")
            labels = host.split(".")
            if (not host or len(host) > 253 or any(
                    not label or len(label) > 63 or
                    not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?", label)
                    for label in labels)):
                return None
        return urlunparse((parsed.scheme.casefold(), parsed.netloc.casefold(),
                           parsed.path, parsed.params, parsed.query, parsed.fragment))
    except (TypeError, ValueError, UnicodeError):
        return None


@dataclass
class Topic:
    topic_id: str
    headline: str
    summary: str
    category: str
    relevance: str
    articles: list[dict] = field(default_factory=list)
    claims: list[dict] = field(default_factory=list)
    verification_status: str = "UNVERIFIED"
    score: float = 0.0
    score_breakdown: dict = field(default_factory=dict)
    score_explanation: str = ""
    relevant_categories: list[str] = field(default_factory=list)
    recommendation_eligible: bool = False
    recommendation_reason: str = "Not assessed"
    novelty_status: str = "NEW"
    previous_coverage: list[dict] = field(default_factory=list)


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.casefold()) if w not in _STOP}


def _cluster_key(headline: str) -> str:
    return " ".join(sorted(_tokens(headline)))


def _similar(a: str, b: str) -> bool:
    left, right = _tokens(a), _tokens(b)
    if not left or not right:
        return False
    overlap = len(left & right) / min(len(left), len(right))
    # Require a substantial shared phrase-sized anchor; generic single-token
    # matches can never join otherwise unrelated reports.
    # Two substantive shared anchors capture headline rewrites while a single
    # entity (for example Nvidia) cannot merge separate developments.
    # Headline verbs and common technology descriptors often vary between
    # publishers ("unveils" / "reveals", "chip" / "processor"). Normalize a
    # small set of event language while keeping entity plus event anchors.
    aliases = {"unveils": "announces", "reveals": "announces", "announced": "announces",
               "launches": "announces", "launched": "announces", "chip": "processor",
               "chips": "processor", "architecture": "design"}
    left = {aliases.get(token, token) for token in left}
    right = {aliases.get(token, token) for token in right}
    overlap = len(left & right) / min(len(left), len(right))
    shared = left & right
    # Two-word headlines and generic overlaps are too ambiguous for cross-run
    # identity. Keep within-run clustering behavior stable; apply this stricter
    # rule only to historical comparisons below.
    return overlap >= .50 and len(shared) >= 2


def _article_dict(article) -> dict:
    return {"url": article.url,
            "original_source_url": article.original_source_url or article.publisher_url,
            "requested_url": getattr(article, "requested_url", None) or article.url,
            "final_url": getattr(article, "final_url", None),
            "redirect_count": getattr(article, "redirect_count", 0),
            "publisher": article.publisher, "headline": article.headline,
            "publication_date": article.publication_date, "retrieved_at": article.retrieved_at,
            "evidence": article.evidence, "accessible": article.accessible,
            "engagement": dict(getattr(article, "engagement", {}) or {}),
            "primary": bool(getattr(article, "source_type", "").upper().startswith("OFFICIAL") or
                            "OFFICIAL" in getattr(article, "source_type", "").upper()),
            "issue": article.rejection_reason or article.page_issue}


def cluster_report_items(items, *, category="uncategorized", relevance="global") -> list[Topic]:
    """Group only close headline matches, retaining each distinct publisher URL."""
    groups: list[list] = []
    for item in items:
        target = next((g for g in groups if _similar(item.claim, g[0].claim)), None)
        if target is None:
            groups.append([item])
        else:
            target.append(item)
    result = []
    for group in groups:
        articles_by_url = {}
        for item in group:
            for article in item.sources:
                row = _article_dict(article)
                articles_by_url.setdefault(row["url"], row)
        headline = group[0].claim
        digest = sha256(_cluster_key(headline).encode()).hexdigest()[:20]
        statuses = {item.status.upper() for item in group}
        sources_with_evidence = [a for item in group for a in item.sources if a.accessible and a.evidence]
        if "DISPUTED" in statuses:
            status = "CONFLICTING"
        elif not sources_with_evidence:
            status = "UNABLE_TO_VERIFY"
        elif "CONFIRMED" in statuses:
            status = "CONFIRMED"
        elif "DEVELOPING" in statuses:
            # Distinct publisher pages count only when their original source URLs
            # differ. Syndicated copies are already folded onto their origin.
            origins = {}
            for article in sources_with_evidence:
                origin = article.original_source_url or article.publisher_url or article.url
                origin_host = urlparse(origin).hostname or ""
                publisher_host = urlparse(article.url).hostname or ""
                # Syndicated articles that identify a common original report are
                # one evidence source even when republished on different domains.
                origins.setdefault(origin_host or publisher_host, origin)
            status = "CORROBORATED" if len(origins) > 1 else "DEVELOPING"
        else:
            status = "UNVERIFIED"
        claims = [{"claim": i.claim, "status": i.status, "confirmed": i.confirmed,
                   "uncertain": i.uncertain, "disagreement": i.disagreement,
                   "evidence_urls": [_article_dict(a)["url"] for a in i.sources if a.evidence]} for i in group]
        primary_category = _category_for(headline) or category
        classified = _categories_for(headline)
        if _category_for(headline):
            classified.add(primary_category)
        relevant = sorted(classified or ({category} if category != "uncategorized" else set()))
        result.append(Topic("topic-" + digest, headline, group[0].confirmed or group[0].uncertain or
                            "Publisher pages were inspected; see claim evidence and uncertainty below.",
                            primary_category, relevance, list(articles_by_url.values()), claims, status,
                            relevant_categories=relevant))
    return result


def score_topic(topic: Topic, *, weights: dict | None = None, now: datetime | None = None) -> Topic:
    """Heuristic 0–100 score. No engagement/search metrics are fabricated."""
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    now = now or datetime.now(timezone.utc)
    dates = []
    for row in topic.articles:
        try:
            published = datetime.fromisoformat((row.get("publication_date") or "").replace("Z", "+00:00"))
            dates.append(published.replace(tzinfo=timezone.utc) if published.tzinfo is None else published)
        except ValueError: pass
    age_hours = min((max(0.0, (now - d.astimezone(timezone.utc)).total_seconds() / 3600)
                     for d in dates), default=720)
    metrics_present = any(row.get("engagement") for row in topic.articles)
    breakdown = {"freshness": max(0, 100 - min(100, age_hours / 72 * 100)),
                 "interest": 50 if metrics_present else None,
                 "relevance": 85 if topic.relevance == "india_and_global" else 70 if topic.relevance == "india" else 55,
                 "evidence": {"CONFIRMED": 100, "CORROBORATED": 85, "DEVELOPING": 55,
                              "UNVERIFIED": 20, "DISPUTED": 10, "CONFLICTING": 10,
                              "UNABLE_TO_VERIFY": 15}.get(topic.verification_status.upper(), 15),
                 "originality": max(10, 80 - min(70, max(0, len(topic.articles)-1)*15)),
                 "low_saturation": max(10, 100 - min(90, max(0, len(topic.articles)-1)*12)),
                 "feasibility": 70 if any(row.get("accessible") and row.get("evidence") for row in topic.articles) else 15}
    score = sum((breakdown[k] or 0) * float(weights.get(k, 0)) for k in breakdown)
    if topic.verification_status.upper() in {"UNVERIFIED", "DISPUTED", "CONFLICTING", "UNABLE_TO_VERIFY"}:
        score *= .65
    topic.score = round(score, 1)
    eligible = topic.verification_status.upper() in {"CONFIRMED", "CORROBORATED"} and any(
        row.get("accessible") and row.get("evidence") for row in topic.articles)
    topic.recommendation_eligible = eligible
    topic.recommendation_reason = ("Accessible primary evidence or independent corroboration supports this topic."
                                   if eligible else
                                   f"Not eligible for a factual publishing recommendation: verification is {topic.verification_status}; accessible supporting evidence is required.")
    topic.score_breakdown = {**breakdown, "engagement_metrics_available": metrics_present,
                             "weights": weights, "uncertainty_multiplier": .65 if topic.verification_status.upper() in
                             {"UNVERIFIED", "DISPUTED", "CONFLICTING", "UNABLE_TO_VERIFY"} else 1.0}
    topic.score_explanation = ("Evidence quality and recency drive the score; audience interest is a heuristic "
                              + ("with available signals." if metrics_present else "; audience metrics are unavailable and contribute no score.")
                              + f" {len(topic.articles)} distinct article URL(s) indicate coverage saturation.")
    return topic


def _category_for(topic: str) -> str:
    tokens = _tokens(topic)
    text = topic.casefold()
    # High precision overrides prevent entity/wording collisions from sending
    # conflicts to entertainment or generic creators' events to business.
    if re.search(r"\b(war|ukraine|russia|airstrike|invasion|military|missile|troops|ceasefire|geopolit|sanctions|diplomatic|gaza|israel|iran|nato|conflict|bombing|soldiers)\b", text):
        return "international_affairs"
    if re.search(r"\b(creator economy|creator business|creator monetization|influencer marketing|creator platform|creator fund)\b", text):
        return "business_economy"
    # Political/legal entities and proceedings are strong subject signals. Their
    # articles often mention AI, scientific agencies, or technology in passing.
    if re.search(r"\b(trump|justice department|department of justice|\bdoj\b|white house|congress|senate|parliament|government|president|minister|election|court|lawsuit|prosecutor|attorney general|federal judge|supreme court)\b", text):
        return "politics"
    if re.search(r"\b(summit|conference|convention)\b", text) and re.search(r"\b(creators?|influencers?)\b", text):
        return "explainers"
    scores = {}
    for category, terms in CATEGORIES.items():
        matches = set().union(*(_tokens(term) for term in terms)) & tokens
        if category == "technology_ai" and matches <= {"technology"}:
            matches = set()
        if matches:
            scores[category] = len(matches)
    for category, pattern in {
        "technology_ai": r"\b(ai|artificial intelligence|machine learning|deep learning|software|cybersecurity|cyber security|processor|processors|chip|chips|semiconductor|data cent(?:er|re)s?|robotics?|automation|cloud computing|operating system|database|networking|quantum computing)\b",
        "science": r"\b(science|scientists?|study|research(?:er)?s?|discover(?:y|ies|s|ed)?|climate|space|planet|species)\b",
        "gaming": r"\b(game|gaming|xbox|playstation|nintendo|steam|gta|grand theft auto|video game)\b",
        "business_economy": r"\b(stocks?|market|economy|earnings|revenue|trade|jobs|inflation)\b",
        "sports": r"\b(match|tournament|league|championship|football|cricket|tennis)\b",
        "politics": r"\b(election|parliament|government|minister|president|policy|vote)\b",
        "entertainment": r"\b(film|movie|actor|actress|music|singer|celebrity|box office)\b",
    }.items():
        if re.search(pattern, text): scores[category] = scores.get(category, 0) + 2
    if re.search(r"\bprocessors?\b", text) and not re.search(
            r"\b(computer|computing|intel|amd|chip|semiconductor|data center|server|silicon)\b", text):
        scores["technology_ai"] = max(0, scores.get("technology_ai", 0) - 2)
        if not scores["technology_ai"]:
            scores.pop("technology_ai", None)
    # A gaming title or platform is an explicit signal; a generic mention of
    # "game" in technology coverage is not enough to override stronger topics.
    return max(scores, key=scores.get) if scores else None


def _categories_for(topic: str) -> set[str]:
    """Return all clearly relevant categories while _category_for picks one primary."""
    text = topic.casefold()
    result = set()
    for category in CATEGORIES:
        if category == "international_affairs" and re.search(
                r"\b(war|ukraine|russia|airstrike|invasion|military|missile|troops|ceasefire|geopolit|sanctions|diplomatic|gaza|israel|iran|nato|conflict|bombing|soldiers|international|world)\b", text):
            result.add(category)
        elif category == "science" and re.search(r"\b(science|scientists?|study|research|discovery|discover|space|climate|species|planet)\b", text):
            result.add(category)
        elif category == "technology_ai" and re.search(r"\b(ai|artificial intelligence|machine learning|deep learning|software|cybersecurity|cyber security|processor|processors|chip|chips|semiconductor|data cent(?:er|re)s?|robotics?|automation|cloud computing|operating system|database|networking|quantum computing)\b", topic, re.I):
            if not (re.search(r"\bprocessors?\b", text) and not re.search(
                    r"\b(computer|computing|intel|amd|chip|semiconductor|data center|server|silicon)\b", text)):
                result.add(category)
        elif category == "business_economy" and re.search(r"\b(business|economy|market|earnings|revenue|jobs|inflation|creator economy|monetization)\b", text):
            result.add(category)
        elif category == "entertainment" and re.search(r"\b(film|movie|actor|actress|music|singer|celebrity|box office)\b", text):
            result.add(category)
        elif category == "gaming" and re.search(r"\b(game|gaming|xbox|playstation|nintendo|steam|gta|grand theft auto|video game)\b", text):
            result.add(category)
    return result


def _publisher_identity(article: dict) -> str:
    """Use the original outlet when known, collapsing syndicated copies."""
    origin = article.get("original_source_url")
    host = (urlparse(origin).hostname or "").casefold() if origin else ""
    if host:
        return host.removeprefix("www.")
    publisher = re.sub(r"\s+", " ", (article.get("publisher") or "").casefold()).strip()
    return publisher or (urlparse(article.get("url") or "").hostname or "unknown").removeprefix("www.")


def _diagnostic_publisher_identities(topic: Topic):
    """Expose only publisher identities supported by topic article metadata."""
    identities = []
    for article in topic.articles:
        origin = article.get("original_source_url")
        host = (urlparse(origin).hostname or "").casefold() if origin else ""
        publisher = re.sub(r"\s+", " ", (article.get("publisher") or "").casefold()).strip()
        article_host = (urlparse(article.get("url") or "").hostname or "").removeprefix("www.")
        identity = host.removeprefix("www.") or publisher or article_host
        if identity and identity not in identities:
            identities.append(identity)
    return identities or "unavailable"


def _diagnostic_publisher_identity_details(topic: Topic):
    """Describe known publisher identities separately from algorithm buckets."""
    identities = _diagnostic_publisher_identities(topic)
    available = identities != "unavailable"
    article_identities = []
    effective_buckets = []
    for index, article in enumerate(topic.articles):
        identity = _publisher_identity(article)
        known = identity != "unknown"
        article_identities.append({
            "article_index": index,
            "publisher_identity": identity if known else "unavailable",
            "identity_status": "known_identity" if known else "unavailable",
            "effective_publisher_cap_bucket": identity,
            "bucket_status": "known_identity" if known else "shared_unknown_fallback",
        })
        if identity not in effective_buckets:
            effective_buckets.append(identity)
    return {
        "publisher_identity_status": "available" if available else "unavailable",
        "effective_publisher_cap_buckets": effective_buckets,
        "publisher_cap_bucket_status": (
            "mixed_known_and_shared_unknown_fallback" if available and "unknown" in effective_buckets
            else "known_identity" if available
            else "shared_unknown_fallback"
        ),
        "publisher_article_identities": article_identities,
    }


def _build_shortlist(topics: list[Topic], coverage: str, diagnostic_events=None) -> tuple[list[Topic], list[str]]:
    """Apply deterministic geography and publisher limits to the ranked top ten."""
    pool = list(topics)
    if not pool:
        return [], []
    reasons = []
    target = min(SHORTLIST_SIZE, len(pool))
    both_geos = coverage == "india_and_global" and {t.relevance for t in pool} >= {"india", "global"}
    geo_counts = {"india": 0, "global": 0}
    publisher_counts = {}
    selected = []
    remaining = list(pool)
    publisher_changed = False
    cap_affected = set()
    cap_relaxed = set()
    balance_affected = set()
    while remaining and len(selected) < target:
        slots = target - len(selected)
        eligible = [t for t in remaining if all(
            publisher_counts.get(_publisher_identity(a), 0) < PUBLISHER_SHORTLIST_LIMIT
            for a in t.articles)]
        if diagnostic_events is not None:
            for topic in remaining:
                if topic not in eligible:
                    cap_affected.add(topic.topic_id)
        if not eligible:
            if diagnostic_events is not None:
                cap_relaxed.update(topic.topic_id for topic in remaining)
            eligible = remaining
            if "publisher limit relaxed: no alternative eligible story" not in reasons:
                reasons.append("publisher limit relaxed: no alternative eligible story")
        if both_geos and slots >= 2:
            need = {geo for geo in ("india", "global") if geo_counts[geo] == 0}
            choices = [t for t in eligible if t.relevance in need]
            if not choices:
                choices = [t for t in remaining if t.relevance in need]
                if diagnostic_events is not None:
                    cap_relaxed.update(topic.topic_id for topic in choices if topic not in eligible)
                if choices and "publisher limit relaxed to include available geography" not in reasons:
                    reasons.append("publisher limit relaxed to include available geography")
            if choices:
                if diagnostic_events is not None and choices[0] is not eligible[0]:
                    balance_affected.add(choices[0].topic_id)
                    for topic in eligible:
                        if topic not in choices and pool.index(topic) < pool.index(choices[0]):
                            balance_affected.add(topic.topic_id)
                if "geography balancing changed shortlist" not in reasons:
                    reasons.append("geography balancing changed shortlist")
                chosen = choices[0]
            else:
                chosen = eligible[0]
        else:
            chosen = eligible[0]
        if any(t not in eligible and t.score > chosen.score for t in remaining):
            publisher_changed = True
        selected.append(chosen)
        remaining.remove(chosen)
        if chosen.relevance in geo_counts:
            geo_counts[chosen.relevance] += 1
        for identity in {_publisher_identity(a) for a in chosen.articles}:
            publisher_counts[identity] = publisher_counts.get(identity, 0) + 1
    if diagnostic_events is not None:
        try:
            selected_order = {topic.topic_id: index + 1 for index, topic in enumerate(selected)}
            final_cap_blocking_buckets = {
                topic.topic_id: {
                    _publisher_identity(article) for article in topic.articles
                    if publisher_counts.get(_publisher_identity(article), 0) >= PUBLISHER_SHORTLIST_LIMIT
                }
                for topic in remaining if topic.topic_id not in cap_relaxed
            }
            final_cap_blocked = {
                topic_id for topic_id, buckets in final_cap_blocking_buckets.items() if buckets
            }
            for rank, topic in enumerate(pool, 1):
                picked = topic.topic_id in selected_order
                final_cap_exclusion = (not picked and topic.topic_id in final_cap_blocked and
                                       topic.topic_id not in balance_affected and
                                       len(selected) >= target)
                diagnostic_events.append({
                    "topic_id": topic.topic_id, "candidate_topic_ref": topic.topic_id,
                    "shortlist_eligibility": "eligible", "category": topic.category,
                    "geography_classification": topic.relevance or "unavailable",
                    "publisher_identities": _diagnostic_publisher_identities(topic),
                    **_diagnostic_publisher_identity_details(topic),
                    "publisher_cap_affected_eligibility": ("yes" if topic.topic_id in cap_affected else "no"),
                    "publisher_cap_final_exclusion": "yes" if final_cap_exclusion else "no",
                    "geography_balance_affected_selection": ("yes" if topic.topic_id in balance_affected else "no"),
                    "selected": "yes" if picked else "no",
                    "decision_reason": ("selected_by_rank_and_constraints" if picked else
                                        "geography_balance_exclusion" if topic.topic_id in balance_affected else
                                        "excluded_by_publisher_cap" if final_cap_exclusion else
                                        "shortlist_limit_reached"),
                    "publisher_cap_exclusion_type": (
                        "multiple_blocking_buckets" if final_cap_exclusion and
                        len(final_cap_blocking_buckets.get(topic.topic_id, ())) > 1
                        else "unavailable_identity_shared_fallback" if final_cap_exclusion and
                        final_cap_blocking_buckets.get(topic.topic_id) == {"unknown"}
                        else "known_shared_publisher" if final_cap_exclusion
                        else "not_final_excluded_by_cap"
                    ),
                    "ranking_score": topic.score, "ranking_order": rank,
                    "selection_order": selected_order.get(topic.topic_id),
                    "constraints": {"shortlist_size": SHORTLIST_SIZE,
                                    "publisher_limit": PUBLISHER_SHORTLIST_LIMIT,
                                    "coverage_mode": coverage,
                                    "geography_balance_applicable": bool(both_geos)},
                })
        except Exception:
            # Recording is observational only and must never affect selection.
            pass
    if publisher_changed:
        reasons.append("publisher concentration limit changed shortlist")
    return selected, reasons


def _record_diagnostic_relationship(ledger: dict, relationship: dict) -> None:
    ledger["relationships"].append(relationship)


def discover_topics(*, provider=None, categories: dict | None = None, queries_per_category=1,
                    weights: dict | None = None, coverage="india_and_global", progress=None,
                    diagnostics: dict | None = None) -> tuple[str, list[Topic], dict]:
    """Run configurable free-source queries then inspect and rank candidate stories."""
    provider = provider or MultiSourceNewsProvider()
    selected = categories or CATEGORIES
    run_id = sha256(datetime.now(timezone.utc).isoformat().encode()).hexdigest()[:24]
    topics, statuses = [], {}
    topic_queries = {}
    topic_origins = {}
    query_merge_counts = {}
    ledger = {"schema_version": 1, "queries": [], "candidates": [], "topics": [],
              "relationships": []} if diagnostics is not None else None
    query_rows = {}
    diagnostics_failure = None
    for category, queries in selected.items():
        for query in list(queries)[:queries_per_category]:
            # Separate India and worldwide discovery so a strong global result
            # cannot hide locally relevant developments (or the reverse).
            geos = ((query, "global"), (query + " India", "india")) if coverage == "india_and_global" else (
                ((query + " India", "india"),) if coverage == "india" else ((query, "global"),))
            for geo_query, relevance in geos:
                key = category + ":" + geo_query
                query_id = "query-" + sha256(f"{len(query_rows)}\0{category}\0{geo_query}\0{relevance}".encode()).hexdigest()[:16]
                if ledger is not None:
                    query_row = {"query_id": query_id, "source_category": category,
                                 "generated_query": geo_query, "geography_intent": relevance,
                                 "discovery_status": "not_attempted", "candidate_count": None,
                                 "candidate_count_unit": "article_candidates"}
                    ledger["queries"].append(query_row)
                    query_rows[key] = query_row
                try:
                    report = research_news(geo_query, provider=provider, latest=True)
                    before = len(report.items)
                    discovered_topics = cluster_report_items(report.items, category=category, relevance=relevance)
                    discovered_topics = [score_topic(topic, weights=weights) for topic in discovered_topics]
                    metrics = report.metrics
                    try:
                        candidate_ids_by_url = {}
                        if ledger is not None:
                            query_row = query_rows[key]
                            search_errors = metrics.get("search_errors", [])
                            query_failed = bool(search_errors) and not metrics.get("results_received", 0)
                            query_row["discovery_status"] = "failed" if query_failed else "success"
                            if query_failed:
                                query_row["discovery_failure_reason"] = "search_failed_without_candidates"
                            query_row["candidate_count"] = metrics.get("results_received")
                            query_row["returned_news_item_count"] = len(report.items)
                            query_row["returned_news_item_count_unit"] = "news_items"
                            attempts = metrics.get("article_attempts", [])
                            # Article attempts intentionally omit full text. Match candidate references only by
                            # request/final URL within this run; the URLs are never copied into the ledger.
                            for index, attempt in enumerate(attempts):
                                candidate_id = "candidate-" + sha256(f"{query_id}\0{index}".encode()).hexdigest()[:16]
                                candidate = {"candidate_id": candidate_id, "query_id": query_id,
                                    "originating_query": query_id,
                                    "url_resolution": "unavailable", "publisher_extraction": "unavailable",
                                    "evidence": "unavailable", "headline_match_verification": "unavailable",
                                    "claim_verification": "unavailable",
                                    "ranking_eligibility": "not_attempted", "topic_ids": [],
                                    "association_state": "unavailable", "association_attempted": False,
                                    "topic_verification": []}
                                resolution = attempt.get("resolver_diagnostics") or {}
                                resolution_state = resolution.get("result")
                                if resolution.get("failure_stage") == "resolver-budget":
                                    resolution_state = "budget_skipped"
                                elif not attempt.get("google_news_url"):
                                    resolution_state = "not_attempted"
                                elif resolution_state == "resolved":
                                    resolution_state = "success"
                                elif resolution_state not in {"success", "failed", "unavailable", "not_attempted"}:
                                    resolution_state = "failed" if resolution.get("failure") else "unavailable"
                                candidate["url_resolution"] = {"state": resolution_state,
                                    "failure_stage": resolution.get("failure_stage")}
                                extraction = attempt.get("extraction_method")
                                candidate["publisher_extraction"] = ("success" if extraction else
                                    "failed" if attempt.get("page_issue") or attempt.get("rejection_reason") else "unavailable")
                                refs = tuple(normalized for ref in (attempt.get("rss_url"), attempt.get("requested_url"),
                                    attempt.get("final_url"), attempt.get("original_publisher_url"))
                                    if (normalized := _association_reference(ref)) is not None)
                                candidate["association_attempted"] = bool(refs)
                                # Evidence is assigned after URL indexes are complete below. Until then, retain
                                # extraction status independently from evidence attribution.
                                candidate["evidence"] = "not_accepted" if attempt.get("extraction_method") else "unavailable"
                                candidate["headline_match_verification"] = (
                                    "matched" if attempt.get("candidate_verified") else "not_matched"
                                    if attempt.get("google_news_url") else "unavailable")
                                candidate["candidate_ordinal"] = index
                                for ref in refs:
                                    if ref:
                                        ids = candidate_ids_by_url.setdefault(ref, [])
                                        if candidate_id not in ids:
                                            ids.append(candidate_id)
                                ledger["candidates"].append(candidate)
                            for item in report.items:
                                for source in item.sources:
                                    if not (source.accessible and source.evidence):
                                        continue
                                    refs = {normalized for ref in (source.requested_url, source.url, source.final_url,
                                                        source.original_source_url)
                                            if (normalized := _association_reference(ref)) is not None}
                                    matched_ids = {cid for ref in refs for cid in candidate_ids_by_url.get(ref, [])}
                                    if len(matched_ids) == 1:
                                        next(c for c in ledger["candidates"] if c["candidate_id"] in matched_ids)["evidence"] = "accepted"
                                    elif len(matched_ids) > 1:
                                        for candidate in ledger["candidates"]:
                                            if candidate["candidate_id"] in matched_ids:
                                                candidate["evidence"] = "ambiguous"
                                        ledger["relationships"].append({"type": "ambiguous_evidence_attribution",
                                            "candidate_ids": sorted(matched_ids), "status": "ambiguous"})
                            # results_received is the provider's post-deduplication article-candidate count.
                            # Attempts/rejections are stage counts, not additional candidates.
                    except Exception as diagnostic_exc:
                        ledger = None
                        diagnostics_failure = type(diagnostic_exc).__name__
                    rejected = metrics.get("articles_rejected", [])
                    accepted_count = metrics.get("accessible_articles", sum(
                        1 for item in report.items for article in item.sources if article.accessible and article.evidence))
                    statuses[key] = {"status": "ok", "items": before, "topics_before_clustering": before,
                                     "topics_after_clustering": len(discovered_topics),
                                     "category_classification_counts": dict(__import__("collections").Counter(
                                         topic.category for topic in discovered_topics)),
                                     "recommendation_eligible": sum(t.recommendation_eligible for t in discovered_topics),
                                     "stage_counts": {
                                         "discovered_articles": metrics.get("results_received", before),
                                         "rejected_or_inaccessible_articles": len(rejected),
                                         "accepted_articles": accepted_count,
                                         "clustered_topics": len(discovered_topics),
                                         "cross_query_merges": 0,
                                         "category_assignments": dict(__import__("collections").Counter(
                                             t.category for t in discovered_topics)),
                                         "final_selected_category_exclusions": 0,
                                         "final_ranked_shortlist": 0},
                                     "metrics": metrics}
                    topics.extend(discovered_topics)
                    for topic in discovered_topics:
                        topic_queries.setdefault(topic.topic_id, set()).add(key)
                        topic_origins[id(topic)] = key
                        try:
                            if ledger is not None:
                                ledger["topics"].append({"topic_id": topic.topic_id,
                                    "query_id": query_rows[key]["query_id"],
                                    "category": topic.category,
                                    "relevant_categories": list(topic.relevant_categories),
                                    "category_exclusion_reason": None,
                                    "ranking_eligibility": "not_attempted",
                                    "score": topic.score, "verification_status": topic.verification_status,
                                    "duplicate_cluster_relationship": "unavailable"})
                                for article in topic.articles:
                                    ref = _association_reference(article.get("requested_url"))
                                    if ref is None:
                                        ref = _association_reference(article.get("url"))
                                    cids = candidate_ids_by_url.get(ref, [])
                                    for cid in cids:
                                        candidate = next(c for c in ledger["candidates"] if c["candidate_id"] == cid)
                                        if topic.topic_id not in candidate["topic_ids"]:
                                            candidate["topic_ids"].append(topic.topic_id)
                                        candidate["claim_verification"] = topic.verification_status
                                        candidate["topic_verification"].append({"topic_id": topic.topic_id,
                                            "verification_status": topic.verification_status})
                                        candidate["association_state"] = "ambiguous" if len(cids) > 1 else "matched"
                                    if len(cids) > 1:
                                        ledger["relationships"].append({"type": "ambiguous_candidate_association",
                                            "topic_id": topic.topic_id, "candidate_ids": list(cids), "status": "ambiguous"})
                        except Exception as diagnostic_exc:
                            ledger = None
                            diagnostics_failure = type(diagnostic_exc).__name__

                except Exception as exc:
                    from news_research import _safe_provider_failure
                    statuses[key] = {"status": "failed", "error_type": type(exc).__name__,
                                     "error": _safe_provider_failure(exc)}
                    if ledger is not None:
                        query_rows[key]["discovery_status"] = "failed"
                if progress:
                    progress(key, statuses[key])
    # Merge clusters that cross query boundaries, then rank deterministically.
    unique = {}
    for topic in topics:
        if topic.topic_id not in unique:
            unique[topic.topic_id] = topic
        else:
            # Attribute this merge to the query that supplied the duplicate.
            query_key = topic_origins.get(id(topic))
            if query_key:
                query_merge_counts[query_key] = query_merge_counts.get(query_key, 0) + 1
            old = unique[topic.topic_id]
            if ledger is not None:
                try:
                    _record_diagnostic_relationship(ledger, {"type": "cross_query_merge", "survivor_topic_id": old.topic_id,
                        "merged_topic_id": topic.topic_id, "query_id": query_rows.get(
                            topic_origins.get(id(topic)), {}).get("query_id"),
                        "survivor_query_id": next((query_rows.get(k, {}).get("query_id") for obj, k in topic_origins.items()
                                                     if obj == id(old)), None),
                        "merged_query_id": query_rows.get(topic_origins.get(id(topic)), {}).get("query_id"),
                        "survivor_ref": {"query_id": next((query_rows.get(k, {}).get("query_id") for obj, k in topic_origins.items()
                                                              if obj == id(old)), None), "topic_id": old.topic_id},
                        "merged_ref": {"query_id": query_rows.get(topic_origins.get(id(topic)), {}).get("query_id"),
                                       "topic_id": topic.topic_id},
                        "status": "merged"})
                except Exception as diagnostic_exc:
                    ledger = None
                    diagnostics_failure = type(diagnostic_exc).__name__
            merged = {a["url"]: a for a in old.articles + topic.articles}
            old.articles = list(merged.values()); old.claims.extend(topic.claims)
            old.relevant_categories = sorted(set(old.relevant_categories + topic.relevant_categories))
            primary = _category_for(old.headline)
            classified = _categories_for(old.headline)
            if primary:
                classified.add(primary)
            old.category = primary or old.category
            old.relevant_categories = sorted(classified or set(old.relevant_categories))
            old.relevance = ("india_and_global" if {old.relevance, topic.relevance} == {"india", "global"}
                             else old.relevance)
            if "DISPUTED" in {old.verification_status, topic.verification_status} or "CONFLICTING" in {
                    old.verification_status, topic.verification_status}:
                old.verification_status = "CONFLICTING"
            score_topic(old, weights=weights)
    selected_keys = set(selected)
    exclusions = [topic for topic in unique.values()
                  if not (set(topic.relevant_categories) & selected_keys)]
    eligible = {key: topic for key, topic in unique.items()
                if set(topic.relevant_categories) & selected_keys}
    topics = sorted((score_topic(t, weights=weights) for t in eligible.values()),
                    key=lambda t: (-t.score, t.topic_id))
    shortlist_events = [] if ledger is not None else None
    shortlist, diversity_reasons = _build_shortlist(topics, coverage, shortlist_events)
    topics = shortlist + [topic for topic in topics if topic not in shortlist]
    shortlist_ids = {topic.topic_id for topic in shortlist}
    query_exclusions = {key: [topic for topic in exclusions if key in topic_queries.get(topic.topic_id, set())]
                        for key in statuses}
    query_eligible = {key: [topic for topic in eligible.values()
                            if key in topic_queries.get(topic.topic_id, set())]
                      for key in statuses}
    for key, status in statuses.items():
        status.setdefault("topics_after_run_deduplication", 0)
        status["stage_counts"]["cross_query_merges"] = query_merge_counts.get(key, 0)
        status["stage_counts"]["final_selected_category_exclusions"] = len(query_exclusions[key])
        status["stage_counts"]["final_ranked_shortlist"] = sum(
            topic.topic_id in shortlist_ids and key in topic_queries.get(topic.topic_id, set()) for topic in topics)
        status["diversity_adjustments"] = diversity_reasons
        if status.get("status") == "ok":
            status["zero_result_cause"] = _zero_result_cause(status, query_exclusions[key], query_eligible[key])
    for topic in topics:
        for key in topic_queries.get(topic.topic_id, set()):
            if key in statuses:
                statuses[key]["topics_after_run_deduplication"] += 1
    if ledger is not None:
        try:
            final_rows = []
            decision_rows = []
            for topic_id, topic in unique.items():
                included = topic_id in eligible
                origins = [query_rows[k]["query_id"] for k in query_rows
                           if k in topic_queries.get(topic_id, set())]
                final_rows.append({"topic_id": topic_id, "query_ids": origins,
                    "category": topic.category, "relevant_categories": list(topic.relevant_categories),
                    "category_exclusion_reason": None if included else "no relevant category selected",
                    "ranking_eligibility": "eligible" if included else "excluded", "score": topic.score,
                    "verification_status": topic.verification_status,
                    "duplicate_cluster_relationship": "unavailable"})
                if not included:
                    decision_rows.append({"topic_id": topic_id, "candidate_topic_ref": topic_id,
                        "query_ids": origins,
                        "shortlist_eligibility": "ineligible_category", "category": topic.category,
                        "geography_classification": topic.relevance or "unavailable",
                        "publisher_identities": _diagnostic_publisher_identities(topic),
                        "publisher_cap_affected_eligibility": "not_applicable",
                        "geography_balance_affected_selection": "not_applicable",
                        "selected": "no", "decision_reason": "no_relevant_category_selected",
                        "ranking_score": topic.score, "ranking_order": None, "selection_order": None,
                        "constraints": {"selected_categories": sorted(selected_keys),
                                        "shortlist_size": SHORTLIST_SIZE,
                                        "publisher_limit": PUBLISHER_SHORTLIST_LIMIT,
                                        "coverage_mode": coverage}})
                for candidate in ledger["candidates"]:
                    if topic_id in candidate["topic_ids"]:
                        candidate["ranking_eligibility"] = "eligible" if included else "excluded"
                        candidate["topic_verification"] = [x for x in candidate["topic_verification"]
                            if x["topic_id"] != topic_id]
                        candidate["topic_verification"].append({"topic_id": topic_id,
                            "verification_status": topic.verification_status})
                        candidate["claim_verification"] = (candidate["topic_verification"][0]["verification_status"]
                            if len(candidate["topic_verification"]) == 1 else "per_topic")
            ledger["topics"] = final_rows
            for decision in shortlist_events:
                decision["query_ids"] = [query_rows[k]["query_id"] for k in query_rows
                                          if k in topic_queries.get(decision["topic_id"], set())]
            decision_rows.extend(shortlist_events)
            ledger["shortlist_decisions"] = decision_rows
            for candidate in ledger["candidates"]:
                if not candidate["topic_ids"]:
                    candidate["association_state"] = "unmatched" if candidate["association_attempted"] else "unavailable"
            diagnostics.clear()
            diagnostics.update(ledger)
        except Exception as exc:
            # Diagnostics are optional: report an incomplete ledger where possible without
            # routing this failure through the research query's production exception path.
            try:
                diagnostics.clear()
                diagnostics.update({"schema_version": 1, "diagnostics_status": "incomplete",
                                    "diagnostic_failure": diagnostics_failure or type(exc).__name__})
            except Exception:
                pass
    elif diagnostics is not None and diagnostics_failure:
        try:
            diagnostics.clear()
            diagnostics.update({"schema_version": 1, "diagnostics_status": "incomplete",
                                "diagnostic_failure": diagnostics_failure})
        except Exception:
            pass
    return run_id, topics, statuses


def _zero_result_cause(status: dict, exclusions: list[Topic], eligible_topics: list[Topic] | None = None) -> str | None:
    counts = status["stage_counts"]
    metrics = status.get("metrics", {})
    if counts["discovered_articles"] == 0:
        return ("discovery failed or provider unavailable" if
                metrics.get("search_errors") or metrics.get("provider_errors")
                else "discovery returned no articles")
    if counts["accepted_articles"] == 0:
        stages = {row.get("stage") for row in metrics.get("articles_rejected", [])}
        return "article access or quality checks rejected candidates (not evidence of irrelevance): " + (
            ", ".join(sorted(stages)) or "no accessible evidence")
    if counts["clustered_topics"] == 0:
        return "no accessible report items could be clustered"
    if counts["final_ranked_shortlist"]:
        return None
    if exclusions:
        if eligible_topics:
            return "eligible topics did not reach the final shortlist after ranking"
        return "topics were relevant only to unselected categories"
    if eligible_topics:
        return "eligible topics did not reach the final shortlist after ranking"
    return "no topic remained after clustering and ranking"


def persist_research(run_id: str, topics: list[Topic], source_status: dict, *, db_path=None) -> None:
    """Additively migrate and persist a run and its immutable topic snapshots."""
    db = pipeline_store.connect(db_path or pipeline_store.DEFAULT_DATABASE)
    try:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS research_runs(run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, source_status_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS research_topics(topic_row_id TEXT PRIMARY KEY, topic_id TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES research_runs(run_id),
          headline TEXT NOT NULL, summary TEXT NOT NULL, category TEXT NOT NULL, relevance TEXT NOT NULL,
          verification_status TEXT NOT NULL, score REAL NOT NULL, score_json TEXT NOT NULL, snapshot_json TEXT NOT NULL,
          UNIQUE(run_id, topic_id));
        CREATE TABLE IF NOT EXISTS research_source_status(run_id TEXT NOT NULL REFERENCES research_runs(run_id), source_key TEXT NOT NULL,
          status_json TEXT NOT NULL, PRIMARY KEY(run_id, source_key));
        CREATE TABLE IF NOT EXISTS discovered_articles(run_id TEXT NOT NULL REFERENCES research_runs(run_id), topic_id TEXT NOT NULL,
          url TEXT NOT NULL, publisher TEXT NOT NULL, publication_date TEXT, retrieved_at TEXT, article_json TEXT NOT NULL,
          PRIMARY KEY(run_id, topic_id, url));
        CREATE TABLE IF NOT EXISTS research_claims(run_id TEXT NOT NULL REFERENCES research_runs(run_id), topic_id TEXT NOT NULL,
          claim_index INTEGER NOT NULL, claim TEXT NOT NULL, status TEXT NOT NULL, claim_json TEXT NOT NULL,
          PRIMARY KEY(run_id, topic_id, claim_index));
        CREATE TABLE IF NOT EXISTS claim_evidence(run_id TEXT NOT NULL, topic_id TEXT NOT NULL, claim_index INTEGER NOT NULL,
          evidence_url TEXT NOT NULL, PRIMARY KEY(run_id, topic_id, claim_index, evidence_url),
          FOREIGN KEY(run_id, topic_id, claim_index) REFERENCES research_claims(run_id, topic_id, claim_index));
        CREATE TABLE IF NOT EXISTS telegram_briefings(briefing_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES research_runs(run_id),
          created_at TEXT NOT NULL, payload_json TEXT NOT NULL, expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS topic_selections(selection_id TEXT PRIMARY KEY, briefing_id TEXT,
          topic_id TEXT, custom_topic TEXT, user_id TEXT NOT NULL, selected_at TEXT NOT NULL, snapshot_json TEXT NOT NULL);
        """)
        _classify_novelty(db, topics, exclude_run_id=run_id)
        with db:
            db.execute("INSERT OR IGNORE INTO research_runs VALUES(?,?,?)", (run_id, datetime.now(timezone.utc).isoformat(), json.dumps(source_status, sort_keys=True)))
            for source_key, status in source_status.items():
                db.execute("INSERT OR IGNORE INTO research_source_status VALUES(?,?,?)",
                           (run_id, source_key, json.dumps(status, sort_keys=True)))
            for topic in topics:
                snapshot = json.dumps(asdict(topic), ensure_ascii=False, sort_keys=True)
                db.execute("INSERT OR IGNORE INTO research_topics VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (run_id + ":" + topic.topic_id, topic.topic_id, run_id, topic.headline, topic.summary, topic.category,
                            topic.relevance, topic.verification_status, topic.score,
                            json.dumps(topic.score_breakdown, sort_keys=True), snapshot))
                for article in topic.articles:
                    db.execute("INSERT OR IGNORE INTO discovered_articles VALUES(?,?,?,?,?,?,?)",
                               (run_id, topic.topic_id, article["url"], article.get("publisher") or "Unknown",
                                article.get("publication_date"), article.get("retrieved_at"),
                                json.dumps(article, ensure_ascii=False, sort_keys=True)))
                for index, claim in enumerate(topic.claims):
                    db.execute("INSERT OR IGNORE INTO research_claims VALUES(?,?,?,?,?,?)",
                               (run_id, topic.topic_id, index, claim["claim"], claim["status"],
                                json.dumps(claim, ensure_ascii=False, sort_keys=True)))
                    for url in claim.get("evidence_urls", []):
                        db.execute("INSERT OR IGNORE INTO claim_evidence VALUES(?,?,?,?)",
                                   (run_id, topic.topic_id, index, url))
    finally: db.close()


def classify_novelty(topics: list[Topic], *, db_path=None,
                     lookback_days: int = DEFAULT_NOVELTY_LOOKBACK_DAYS) -> None:
    """Apply persisted cross-run novelty to a preview or a saved run."""
    db = pipeline_store.connect(db_path or pipeline_store.DEFAULT_DATABASE)
    try:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_runs'").fetchone()
        if exists:
            _classify_novelty(db, topics, lookback_days=lookback_days)
        else:
            for topic in topics:
                topic.novelty_status = "NEW"
                topic.previous_coverage = []
    finally:
        db.close()


def _parse_timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _classify_novelty(db, topics: list[Topic], exclude_run_id=None, *,
                      lookback_days: int = DEFAULT_NOVELTY_LOOKBACK_DAYS, reference_time=None) -> None:
    """Classify against earlier immutable snapshots, independently of evidence."""
    previous = db.execute("""SELECT r.run_id,r.created_at,t.snapshot_json
      FROM research_runs r JOIN research_topics t USING(run_id)
      ORDER BY r.created_at DESC""").fetchall()
    reference = _parse_timestamp(reference_time) if reference_time else datetime.now(timezone.utc)
    try:
        cutoff = reference.timestamp() - max(0, float(lookback_days)) * 86400
    except (TypeError, ValueError, OverflowError):
        cutoff = reference.timestamp() - DEFAULT_NOVELTY_LOOKBACK_DAYS * 86400
    earlier = []
    for row in previous:
        if row["run_id"] == exclude_run_id:
            continue
        created = _parse_timestamp(row["created_at"])
        if not created or created > reference or created.timestamp() < cutoff:
            continue
        try:
            snapshot = json.loads(row["snapshot_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("headline"), str):
            continue
        articles = snapshot.get("articles", [])
        if not isinstance(articles, list):
            continue
        snapshot["articles"] = [a for a in articles if isinstance(a, dict)]
        earlier.append((row["run_id"], row["created_at"], snapshot))
    for topic in topics:
        topic.previous_coverage = _previous_coverage(topic, earlier)
        if not topic.previous_coverage:
            topic.novelty_status = "NEW"
        else:
            topic.novelty_status = ("MEANINGFUL_UPDATE" if _has_newer_coverage(topic, topic.previous_coverage)
                                    else "PREVIOUSLY_COVERED")


def _previous_coverage(topic: Topic, earlier: list[tuple[str, str, dict]]) -> list[dict]:
    """Find conservative earlier matches and return brief provenance details."""
    matches = []
    current_articles = [a for a in topic.articles if isinstance(a, dict)]
    current_urls = {value for article in current_articles for value in
                    (article.get("url"), article.get("original_source_url")) if value}
    for run_id, created_at, snapshot in earlier:
        headline = snapshot.get("headline", "")
        old_articles = snapshot.get("articles", [])
        old_urls = {value for article in old_articles if isinstance(article, dict) for value in
                    (article.get("url"), article.get("original_source_url")) if value}
        headline_match = _strong_story_match(topic.headline, headline)
        current_tokens, old_tokens = _tokens(topic.headline), _tokens(headline)
        url_match = (bool(current_urls & old_urls) and len(current_tokens) >= 3 and
                     len(old_tokens) >= 3 and len(current_tokens & old_tokens) >= 2)
        if not (headline_match or url_match):
            continue
        matches.append({"run_id": run_id, "covered_at": created_at, "headline": headline,
                        "articles": [{"publisher": a.get("publisher"), "url": a.get("url"),
                                      "original_source_url": a.get("original_source_url"),
                                      "publication_date": a.get("publication_date"),
                                      "retrieved_at": a.get("retrieved_at"),
                                      "headline": a.get("headline"), "evidence": a.get("evidence"),
                                      "primary": a.get("primary") is True}
                                     for a in old_articles[:5] if isinstance(a, dict)]})
    return matches


def _has_newer_coverage(topic: Topic, previous: list[dict]) -> bool:
    def parse(value):
        try:
            return _parse_timestamp(value)
        except (TypeError, ValueError): return None
    latest_current = max((date for article in topic.articles if isinstance(article, dict)
                          if (date := parse(article.get("publication_date") or article.get("retrieved_at")))),
                         default=None)
    latest_old = max((date for match in previous for article in match.get("articles", [])
                      if (date := parse(article.get("publication_date") or article.get("retrieved_at")))),
                     default=None)
    if not (latest_current and latest_old and latest_current > latest_old):
        return False
    old_origins = {_canonical_origin(a) for m in previous for a in m.get("articles", []) if _canonical_origin(a)}
    # Provenance records do not reliably label syndication or independent
    # non-primary reporting. Only a new, explicitly primary source with its
    # own evidence is strong enough; other cases remain Previously Covered.
    for article in topic.articles:
        if not isinstance(article, dict): continue
        origin = _canonical_origin(article)
        if article.get("primary") is True and article.get("evidence") and origin and origin not in old_origins:
            return True
    return False


def _canonical_origin(article):
    if not isinstance(article, dict): return None
    value = article.get("original_source_url") or article.get("url")
    if not isinstance(value, str): return None
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc: return None
    return (parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path.rstrip("/"))


def _strong_story_match(left, right):
    if not isinstance(left, str) or not isinstance(right, str): return False
    a, b = _tokens(left), _tokens(right)
    if len(a) < 3 or len(b) < 3: return False
    # Require at least three shared anchors so an entity plus a generic verb
    # cannot join separate developments.
    return _similar(left, right) and len(a & b) >= 3


def format_briefing(run_id: str, topics: list[Topic]) -> str:
    eligible = [topic for topic in topics if topic.recommendation_eligible]
    lines = ["Today's research shortlist"]
    if not eligible:
        lines.append("No topics currently meet the evidence threshold for a factual publishing recommendation.")
    for rank, topic in enumerate(eligible[:10], 1):
        geo = topic.relevance.replace("_", "/")
        lines += [f"\n{rank}. {topic.headline}", f"{topic.category.replace('_', ' ').title()} · {geo} · {topic.verification_status}",
                  f"Novelty: {topic.novelty_status.replace('_', ' ').title()}"]
        if topic.previous_coverage:
            prior = topic.previous_coverage[0]
            lines.append(f"Earlier coverage: {prior.get('covered_at', 'date unavailable')} — {prior.get('headline', '')}")
            for article in prior.get("articles", [])[:1]:
                if article.get("url"):
                    lines.append(f"Earlier source: {article.get('publisher') or 'Publisher'}: {article['url']}")
        lines += [
                  f"Score {topic.score}/100 — {topic.score_explanation}", topic.summary[:240]]
        for source in topic.articles[:2]:
            lines.append(f"{source['publisher']}: {source['url']}")
    lines.append("\nChoose a topic number with verified evidence or send your own topic for research.")
    return "\n".join(lines)
