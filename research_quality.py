"""Deterministic, local quality evaluation for saved research snapshots.

Synthetic labels exercise category behavior; they are never evidence of real
world coverage quality. Historical snapshots have no independent labels.
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import sqlite3

import research_intelligence as ri


def synthetic_fixtures():
    """Small labelled examples. All titles and evidence are invented."""
    rows = [
        ("technology_ai", "AI software system improves computing", "india", "A"),
        ("business_economy", "Market economy revenue grows", "global", "B"),
        ("science", "Scientists discover new ocean species", "india", "C"),
        ("entertainment", "Actor stars in new film", "global", "D"),
        ("gaming", "Video game arrives on Xbox", "india", "E"),
        ("sports", "Cricket league wins tournament", "global", "F"),
        ("politics", "Parliament debates government policy", "india", "G"),
        ("international_affairs", "Ceasefire follows international conflict", "global", "H"),
        ("explainers", "Explainer: how unusual custom works", "india", "I"),
        ("viral_trends", "Viral internet trend spreads online", "global", "J"),
        ("consumer_technology", "Smartphones introduce new camera", "india", "K"),
        ("public_interest", "Public health climate study published", "global", "L"),
    ]
    topics = []
    for i, (category, headline, geo, publisher) in enumerate(rows):
        status = ("CONFIRMED", "CORROBORATED", "DEVELOPING", "UNVERIFIED",
                  "CONFLICTING", "UNABLE_TO_VERIFY")[i % 6]
        articles = [{"url": f"https://{publisher.lower()}.example/{i}", "publisher": publisher,
                     "original_source_url": f"https://{publisher.lower()}.example/{i}",
                     "accessible": status != "UNABLE_TO_VERIFY",
                     "evidence": "Synthetic supporting text" if status not in {"UNVERIFIED", "UNABLE_TO_VERIFY"} else None,
                     "primary": i % 2 == 0}]
        if i == 0:  # syndicated copy resolves to the same original source
            articles.append({**articles[0], "url": "https://republisher.example/copy", "publisher": "Republisher"})
        predicted = ri._categories_for(headline)
        primary = ri._category_for(headline)
        if primary:
            predicted.add(primary)
        topics.append({"topic_id": str(i), "headline": headline, "category": primary or "uncategorized",
                       "relevant_categories": sorted(predicted),
                       "expected_categories": [category], "relevance": geo, "articles": articles,
                       "claims": [{"status": status, "disagreement": "synthetic conflict" if status == "CONFLICTING" else None}],
                       "verification_status": status, "score": float(100-i),
                       "score_breakdown": {"freshness": 80, "evidence": None if i == 9 else 50}})
    # Explicit duplicate, mixed-category, political/legal precedence, technology edge cases.
    topics += [
        {**topics[0], "topic_id": "dup", "headline": topics[0]["headline"]},
        {**topics[6], "topic_id": "mixed", "headline": "Government AI policy debate", "category": ri._category_for("Government AI policy debate"), "relevant_categories": sorted(ri._categories_for("Government AI policy debate") | {ri._category_for("Government AI policy debate")}), "expected_categories": ["politics", "technology_ai"]},
        {**topics[1], "topic_id": "tech-false-positive", "headline": "Processor elected to parliament", "category": ri._category_for("Processor elected to parliament"), "relevant_categories": sorted(ri._categories_for("Processor elected to parliament")), "expected_categories": ["politics"]},
        {**topics[0], "topic_id": "tech-true-positive", "headline": "Semiconductor chip computing system", "category": ri._category_for("Semiconductor chip computing system"), "relevant_categories": sorted(ri._categories_for("Semiconductor chip computing system")), "expected_categories": ["technology_ai"]},
        {**topics[2], "topic_id": "unknown-pub", "articles": [{"url": "https://example.invalid/x", "publisher": None, "original_source_url": None, "accessible": False, "evidence": None}]},
    ]
    return sorted(topics, key=lambda topic: (-topic["score"], topic["topic_id"]))


def _class_metrics(topics):
    labeled = [t for t in topics if t.get("expected_categories") is not None]
    if not labeled:
        return {"status": "unavailable", "reason": "independent labelled ground truth unavailable"}
    tp = fp = fn = 0
    for t in labeled:
        expected, actual = set(t.get("expected_categories") or []), set(t.get("relevant_categories") or [])
        tp += len(expected & actual); fp += len(actual - expected); fn += len(expected - actual)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else None
    per_category = {}
    for category in ri.CATEGORIES:
        ctp = cfp = cfn = 0
        for t in labeled:
            expected, actual = set(t.get("expected_categories") or []), set(t.get("relevant_categories") or [])
            ctp += category in expected and category in actual
            cfp += category not in expected and category in actual
            cfn += category in expected and category not in actual
        per_category[category] = {"true_positives": ctp, "false_positives": cfp, "false_negatives": cfn}
    return {"status": "measured on labelled fixtures", "labeled_topics": len(labeled),
            "averaging": "micro: pool category assignments across fixtures before ratios",
            "true_positives": tp, "false_positives": fp, "false_negatives": fn,
            "precision": precision, "recall": recall, "f1_score": f1, "per_category": per_category}


def _ranking_is_deterministic(rank_and_select, topics):
    """Compare production results across repeated and reversed identical inputs."""
    expected = rank_and_select(topics)
    return expected == rank_and_select(list(reversed(topics))) == rank_and_select(topics)


def _publisher_identity(article):
    origin = article.get("original_source_url")
    host = (ri.urlparse(origin).hostname or "").casefold() if origin else ""
    if host:
        return host.removeprefix("www.")
    publisher = " ".join(str(article.get("publisher") or "").casefold().split())
    return publisher or None


def evaluate(topics, *, requested_categories=None, coverage="india_and_global", source="synthetic"):
    topics = [dict(t) for t in topics]
    requested = set(requested_categories if requested_categories is not None else ri.CATEGORIES)
    shortlist_ids = set()
    shortlist_reasons = "unavailable from historical snapshots"
    if source != "historical" and coverage:
        models = [ri.Topic(str(t.get("topic_id", i)), t.get("headline", ""), "", t.get("category", ""),
                           t.get("relevance", "unknown"), articles=t.get("articles", []),
                           verification_status=t.get("verification_status", "UNVERIFIED"), score=float(t.get("score", 0)))
                  for i, t in enumerate(topics)]
        selected_topics, shortlist_reasons = ri._build_shortlist(models, coverage)
        shortlist_ids = {t.topic_id for t in selected_topics}
    by_category = {}
    for category in ri.CATEGORIES:
        selected = [t for t in topics if category in (t.get("relevant_categories") or [])]
        by_category[category] = {"requested": category in requested, "synthetic_fixture_rows": len(selected) if source != "historical" else "unavailable",
                                 "shortlisted_fixture_rows": (sum(
                                     str(t.get("topic_id", i)) in shortlist_ids for i, t in enumerate(topics)
                                     if category in (t.get("relevant_categories") or []))
                                     if source != "historical" else "unavailable"),
                                 "no_results": not selected if category in requested else None,
                                 "filtered_merged_excluded": "unavailable",
                                 "metric_status": "unavailable in historical snapshots" if source == "historical" else "synthetic fixture row count"}
    flat_articles = [a for t in topics for a in t.get("articles", [])]
    identity_by_article = [_publisher_identity(a) for a in flat_articles]
    known = [identity for identity in identity_by_article if identity]
    identities = set(known)
    publisher_counts = Counter(known)
    statuses = Counter((t.get("verification_status") or "UNKNOWN").upper() for t in topics)
    evidence_counts = Counter()
    for t in topics:
        arts = t.get("articles", [])
        evidence_counts["with_evidence"] += any(a.get("evidence") for a in arts)
        evidence_counts["missing_evidence"] += not any(a.get("evidence") for a in arts)
        evidence_counts["inaccessible"] += any(a.get("accessible") is False for a in arts)
        evidence_counts["primary_evidence"] += any(a.get("primary") is True and a.get("evidence") for a in arts)
        evidence_counts["conflicting_claims"] += any(c.get("disagreement") or str(c.get("status", "")).upper() in {"CONFLICTING", "DISPUTED"} for c in t.get("claims", []))
    ranks = [t.get("score") for t in topics]
    can_rank = all(isinstance(x, (int,float)) for x in ranks)
    def production_result(rows):
        models = [ri.Topic(str(t.get("topic_id", i)), t.get("headline", ""), "", t.get("category", ""),
                           t.get("relevance", "unknown"), articles=t.get("articles", []), score=float(t["score"]))
                  for i, t in enumerate(rows)]
        models.sort(key=lambda t: (-t.score, t.topic_id))
        selected, _ = ri._build_shortlist(models, coverage or "india_and_global")
        return [t.topic_id for t in selected]
    deterministic = _ranking_is_deterministic(production_result, topics) if can_rank and source != "historical" else None
    ranking = {"score_ordered": all(ranks[i] >= ranks[i+1] for i in range(len(ranks)-1)) if can_rank else None,
               "production_ranking_determinism": (deterministic if deterministic is not None else "unavailable: historical snapshots do not validate production ranking" if source == "historical" else "unavailable: scores missing"),
               "score_components_available": sorted({k for t in topics for k,v in (t.get("score_breakdown") or {}).items() if v is not None}),
               "score_components_missing": sorted({k for t in topics for k,v in (t.get("score_breakdown") or {}).items() if v is None})}
    geo = Counter(t.get("relevance", "unknown") for t in topics)
    return {"source": source, "topics": len(topics), "requested_categories": sorted(requested),
            "category_coverage": by_category,
            "geography": {"requested_mode": coverage or "unavailable", "india_provenance": geo["india"], "global_provenance": geo["global"],
                          "shortlist_distribution": ({k: sum(t.get("relevance") == k and str(t.get("topic_id", i)) in shortlist_ids for i, t in enumerate(topics)) for k in ("india", "global")} if source != "historical" else "unavailable"),
                          "both_streams_produced_eligible": bool(geo["india"] and geo["global"]),
                          "balancing_adjustments": shortlist_reasons,
                          "geographic_impact_verified": False},
            "publishers": {"distinct_known_identities": len(identities), "unknown_identity_articles": len(flat_articles)-len(known),
                           "topics_per_publisher": dict(sorted(publisher_counts.items())),
                           "approximate_synthetic_syndicated_article_rows": sum(bool(a.get("original_source_url") and a.get("url") != a.get("original_source_url")) for a in flat_articles) if source != "historical" else "unavailable",
                           "approximate_synthetic_duplicate_headline_groups": len(topics)-len({ri._cluster_key(t.get("headline", "")) for t in topics}) if source != "historical" else "unavailable",
                           "publisher_cap_impact": "unavailable from snapshots"},
            "classification": _class_metrics(topics), "verification": {"statuses": dict(sorted(statuses.items())),
                 "evidence": dict(evidence_counts), "independent_sources": "not inferred from URL count",
                 "uncertainty_preserved": True}, "ranking": ranking,
            "warnings": ["Discovery provenance does not prove geographic impact.",
                         "Synthetic fixture rows are not production discovery-funnel counts; duplicate and syndication indicators are approximate fixture checks, not production clustering or source independence.",
                         "Historical category accuracy is unavailable without independent labels." if source == "historical" else "Classification metrics describe the synthetic labelled fixtures only.",
                         "Unavailable snapshot fields are not inferred."],
            "recommended_next_actions": ["Review categories with missed labelled fixture classifications.",
                "Inspect missing evidence and inaccessible-source rates before changing verification rules.",
                "Capture candidate-stage and shortlist decision metadata in future snapshots to measure historical funnel and constraint impact."]}


def load_history(db_path, run_id=None):
    """Read immutable snapshots only; never migrates or writes the database."""
    # SQLite URI mode=ro prevents database creation and mutation.
    db_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    db = None
    try:
        db = sqlite3.connect(db_uri, uri=True)
        query = "SELECT run_id,snapshot_json FROM research_topics"
        params = ()
        if run_id:
            query += " WHERE run_id=?"; params = (run_id,)
        query += " ORDER BY run_id,topic_id"
        rows = db.execute(query, params).fetchall()
        return [json.loads(row[1]) for row in rows], sorted({row[0] for row in rows})
    except sqlite3.OperationalError:
        return [], []
    finally:
        if db is not None:
            db.close()


def report(data, *, detailed=False):
    lines = [f"Research quality evaluation ({data['source']}): {data['topics']} topics",
             f"Categories requested: {len(data['requested_categories'])}/12",
             "Category | Requested | Synthetic fixture rows | Shortlisted fixture rows"]
    for name, row in data["category_coverage"].items():
        lines.append(f"{name} | {'yes' if row['requested'] else 'no'} | {row['synthetic_fixture_rows']} | {row['shortlisted_fixture_rows']}")
    g = data["geography"]
    lines += [f"Geography ({g['requested_mode']}): India={g['india_provenance']}, global={g['global_provenance']}; impact unverified",
              f"Publishers: {data['publishers']['distinct_known_identities']} known identities; {data['publishers']['unknown_identity_articles']} unknown articles",
             f"Classification: {data['classification']['status']} (micro precision={data['classification'].get('precision')}, recall={data['classification'].get('recall')}, F1={data['classification'].get('f1_score')})",
              f"Verification statuses: {data['verification']['statuses']}",
              f"Ranking score-ordered: {data['ranking']['score_ordered']}"]
    if detailed:
        lines.extend("Warning: " + w for w in data["warnings"])
        lines.extend("Recommended next action: " + action for action in data["recommended_next_actions"])
        lines.append("Approximate synthetic publisher/duplicate metrics (unknown identity is separate; URL differences do not prove independent sources): " + json.dumps(data["publishers"], sort_keys=True))
        lines.append("Evidence metrics: " + json.dumps(data["verification"], sort_keys=True))
        lines.append("Ranking metrics: " + json.dumps(data["ranking"], sort_keys=True))
    return "\n".join(lines)
