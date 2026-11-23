"""Local, deterministic topic discovery, clustering, ranking and persistence.

Google News is used only to discover candidate publisher pages. The existing
``research_news`` workflow inspects publisher pages and supplies conservative
claim assessments; ranking never upgrades verification status.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from urllib.parse import urlparse

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
    return overlap >= .50 and len(left & right) >= 2


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
        relevant = sorted(({category, primary_category} | _categories_for(headline)) - {"uncategorized"})
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
        if matches:
            scores[category] = len(matches)
    for category, pattern in {
        "technology_ai": r"\b(ai|artificial intelligence|software|chip|semiconductor|technology|tech)\b",
        "science": r"\b(science|scientists?|study|research(?:er)?s?|discover(?:y|ies|s|ed)?|climate|space|planet|species)\b",
        "gaming": r"\b(game|gaming|xbox|playstation|nintendo|steam|gta|grand theft auto|video game)\b",
        "business_economy": r"\b(stocks?|market|economy|earnings|revenue|trade|jobs|inflation)\b",
        "sports": r"\b(match|tournament|league|championship|football|cricket|tennis)\b",
        "politics": r"\b(election|parliament|government|minister|president|policy|vote)\b",
        "entertainment": r"\b(film|movie|actor|actress|music|singer|celebrity|box office)\b",
    }.items():
        if re.search(pattern, text): scores[category] = scores.get(category, 0) + 2
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
        elif category == "technology_ai" and re.search(r"\b(technology|tech|artificial intelligence|AI|software|chip|semiconductor)\b", topic, re.I):
            result.add(category)
        elif category == "business_economy" and re.search(r"\b(business|economy|market|earnings|revenue|jobs|inflation|creator economy|monetization)\b", text):
            result.add(category)
        elif category == "entertainment" and re.search(r"\b(film|movie|actor|actress|music|singer|celebrity|box office)\b", text):
            result.add(category)
        elif category == "gaming" and re.search(r"\b(game|gaming|xbox|playstation|nintendo|steam|gta|grand theft auto|video game)\b", text):
            result.add(category)
    return result


def discover_topics(*, provider=None, categories: dict | None = None, queries_per_category=1,
                    weights: dict | None = None, coverage="india_and_global", progress=None) -> tuple[str, list[Topic], dict]:
    """Run configurable free-source queries then inspect and rank candidate stories."""
    provider = provider or MultiSourceNewsProvider()
    selected = categories or CATEGORIES
    run_id = sha256(datetime.now(timezone.utc).isoformat().encode()).hexdigest()[:24]
    topics, statuses = [], {}
    for category, queries in selected.items():
        for query in list(queries)[:queries_per_category]:
            # Separate India and worldwide discovery so a strong global result
            # cannot hide locally relevant developments (or the reverse).
            geos = ((query, "global"), (query + " India", "india")) if coverage == "india_and_global" else (
                ((query + " India", "india"),) if coverage == "india" else ((query, "global"),))
            for geo_query, relevance in geos:
                key = category + ":" + geo_query
                try:
                    report = research_news(geo_query, provider=provider, latest=True)
                    before = len(report.items)
                    discovered_topics = cluster_report_items(report.items, category=category, relevance=relevance)
                    discovered_topics = [score_topic(topic, weights=weights) for topic in discovered_topics]
                    statuses[key] = {"status": "ok", "items": before, "topics_before_clustering": before,
                                     "topics_after_clustering": len(discovered_topics),
                                     "category_classification_counts": dict(__import__("collections").Counter(
                                         topic.category for topic in discovered_topics)),
                                     "recommendation_eligible": sum(t.recommendation_eligible for t in discovered_topics),
                                     "metrics": report.metrics}
                    topics.extend(discovered_topics)
                except Exception as exc:
                    from news_research import _safe_provider_failure
                    statuses[key] = {"status": "failed", "error_type": type(exc).__name__,
                                     "error": _safe_provider_failure(exc)}
                if progress:
                    progress(key, statuses[key])
    # Merge clusters that cross query boundaries, then rank deterministically.
    unique = {}
    for topic in topics:
        if topic.topic_id not in unique: unique[topic.topic_id] = topic
        else:
            old = unique[topic.topic_id]
            merged = {a["url"]: a for a in old.articles + topic.articles}
            old.articles = list(merged.values()); old.claims.extend(topic.claims)
            old.relevant_categories = sorted(set(old.relevant_categories + topic.relevant_categories + [topic.category]))
            old.category = _category_for(old.headline) or old.category
            old.relevance = ("india_and_global" if {old.relevance, topic.relevance} == {"india", "global"}
                             else old.relevance)
            if "DISPUTED" in {old.verification_status, topic.verification_status} or "CONFLICTING" in {
                    old.verification_status, topic.verification_status}:
                old.verification_status = "CONFLICTING"
            score_topic(old, weights=weights)
    # Query fallback categories are useful when a headline is genuinely
    # ambiguous, but must not cause unrelated topics to leak into the result.
    selected_keys = set(selected)
    unique = {key: topic for key, topic in unique.items() if topic.category in selected_keys}
    topics = sorted((score_topic(t, weights=weights) for t in unique.values()),
                    key=lambda t: (-t.score, t.topic_id))
    for status in statuses.values():
        status.setdefault("topics_after_run_deduplication", 0)
    for topic in topics:
        matching = [s for key, s in statuses.items() if key.split(":", 1)[0] in topic.relevant_categories]
        if matching:
            matching[0]["topics_after_run_deduplication"] += 1
    return run_id, topics, statuses


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


def format_briefing(run_id: str, topics: list[Topic]) -> str:
    eligible = [topic for topic in topics if topic.recommendation_eligible]
    lines = ["Today's research shortlist"]
    if not eligible:
        lines.append("No topics currently meet the evidence threshold for a factual publishing recommendation.")
    for rank, topic in enumerate(eligible[:10], 1):
        geo = topic.relevance.replace("_", "/")
        lines += [f"\n{rank}. {topic.headline}", f"{topic.category.replace('_', ' ').title()} · {geo} · {topic.verification_status}",
                  f"Score {topic.score}/100 — {topic.score_explanation}", topic.summary[:240]]
        for source in topic.articles[:2]:
            lines.append(f"{source['publisher']}: {source['url']}")
    lines.append("\nChoose a topic number with verified evidence or send your own topic for research.")
    return "\n".join(lines)
