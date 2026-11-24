"""Isolated, read-only inspection of one persisted research run.

This module intentionally does not import pipeline_store or any live research
code. It opens only existing SQLite files through a mode=ro URI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import Any
from evaluation_bundle import BundleValidationError, _http_url


TABLE_COLUMNS = {
    "research_runs": {"run_id", "created_at", "source_status_json"},
    "research_topics": {"topic_row_id", "topic_id", "run_id", "headline", "summary", "category", "relevance", "verification_status", "score", "score_json", "snapshot_json"},
    "discovered_articles": {"run_id", "topic_id", "url", "publisher", "publication_date", "retrieved_at", "article_json"},
    "research_claims": {"run_id", "topic_id", "claim_index", "claim", "status", "claim_json"},
    "claim_evidence": {"run_id", "topic_id", "claim_index", "evidence_url"},
    "research_source_status": {"run_id", "source_key", "status_json"},
}
METADATA_KEYS = ("configuration", "query", "coverage", "code_version", "source_provenance")


class ResearchRunReadError(ValueError):
    """The database or selected run cannot be read under the expected schema."""


def _open_readonly(path: Path) -> sqlite3.Connection:
    """Connection boundary kept explicit so read-only enforcement is testable."""
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


@dataclass
class ValidationIssue:
    code: str
    message: str
    severity: str = "error"


@dataclass
class ResearchRun:
    run: dict[str, Any]
    topics: list[dict[str, Any]] = field(default_factory=list)
    articles: list[dict[str, Any]] = field(default_factory=list)
    claims: list[dict[str, Any]] = field(default_factory=list)
    evidence_links: list[dict[str, Any]] = field(default_factory=list)
    source_status: list[dict[str, Any]] = field(default_factory=list)
    validation_issues: list[ValidationIssue] = field(default_factory=list)
    # Kept as separate keys; predictions are never human annotations.
    pipeline_predictions: dict[str, Any] = field(default_factory=dict)
    human_annotations: dict[str, Any] = field(default_factory=dict)
    completion_status: str = "unknown"
    saved_content_status: str = "indeterminate"

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.validation_issues)


def _timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    except (ValueError, OverflowError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _url(value: Any) -> bool:
    try:
        _http_url(value, "URL")
    except (BundleValidationError, TypeError, ValueError):
        return False
    return True


def _json(value: Any, where: str, issues: list[ValidationIssue], expected: type = dict):
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        if not isinstance(value, str):
            raise TypeError("JSON value must be text")
        parsed = json.loads(value)
    except (TypeError, UnicodeError, json.JSONDecodeError):
        issues.append(ValidationIssue("malformed_json", f"{where} is not valid JSON"))
        return None
    if not isinstance(parsed, expected):
        issues.append(ValidationIssue("invalid_json_shape", f"{where} must decode to {expected.__name__}"))
        return None
    return parsed


def _table_layout(db: sqlite3.Connection, issues: list[ValidationIssue]) -> None:
    existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, required in TABLE_COLUMNS.items():
        if table not in existing:
            issues.append(ValidationIssue("missing_table", f"required table {table!r} is missing"))
            continue
        columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
        missing = required - columns
        if missing:
            issues.append(ValidationIssue("missing_columns", f"{table} missing column(s): {', '.join(sorted(missing))}"))
    if any(issue.code in {"missing_table", "missing_columns"} for issue in issues):
        raise ResearchRunReadError("Database schema is incomplete: " + "; ".join(i.message for i in issues))


def read_research_run(database: str | Path, run_id: str) -> ResearchRun:
    """Read and validate exactly one saved run; never creates a file or schema."""
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise ResearchRunReadError(f"Database does not exist: {path}")
    try:
        db = _open_readonly(path)
    except sqlite3.Error as exc:
        raise ResearchRunReadError(f"Cannot open database read-only: {exc}") from exc
    db.row_factory = sqlite3.Row
    issues: list[ValidationIssue] = []
    try:
        db.execute("BEGIN")
        _table_layout(db, issues)
        runrow = db.execute("SELECT * FROM research_runs WHERE run_id=?", (run_id,)).fetchone()
        if runrow is None:
            raise ResearchRunReadError(f"Run {run_id!r} does not exist")
        run = dict(runrow)
        if not _timestamp(run["created_at"]):
            issues.append(ValidationIssue("invalid_timestamp", "research_runs.created_at must be timezone-aware ISO 8601"))
        source_meta = _json(run["source_status_json"], "research_runs.source_status_json", issues)
        topics = [dict(row) for row in db.execute("SELECT * FROM research_topics WHERE run_id=?", (run_id,))]
        articles = [dict(row) for row in db.execute("SELECT * FROM discovered_articles WHERE run_id=?", (run_id,))]
        claims = [dict(row) for row in db.execute("SELECT * FROM research_claims WHERE run_id=?", (run_id,))]
        links = [dict(row) for row in db.execute("SELECT * FROM claim_evidence WHERE run_id=?", (run_id,))]
        statuses = [dict(row) for row in db.execute("SELECT * FROM research_source_status WHERE run_id=?", (run_id,))]
        result = ResearchRun(run, topics, articles, claims, links, statuses, issues)
        result.saved_content_status = "empty_saved_run" if not any((topics, articles, claims, links, statuses)) else "content_present_completion_indeterminate"

        def unique(values, label):
            seen = set()
            for value in values:
                if value in seen:
                    issues.append(ValidationIssue("duplicate_identifier", f"duplicate {label}: {value!r}"))
                seen.add(value)
        unique([row["topic_row_id"] for row in topics], "topic_row_id")
        unique([row["topic_id"] for row in topics], "topic_id")
        unique([row["source_key"] for row in statuses], "source_key")
        topic_ids = {row["topic_id"] for row in topics}
        topic_lookup = {row["topic_id"]: row for row in topics}
        claim_lookup = {}
        for topic in topics:
            snapshot = _json(topic["snapshot_json"], f"topic {topic['topic_id']} snapshot_json", issues)
            _json(topic["score_json"], f"topic {topic['topic_id']} score_json", issues)
            if snapshot is not None:
                for field_name in ("topic_id", "headline", "summary", "category", "relevance", "verification_status", "score"):
                    if snapshot.get(field_name) != topic[field_name]:
                        issues.append(ValidationIssue("snapshot_conflict", f"topic {topic['topic_id']} snapshot {field_name} conflicts with normalized column"))
                result.pipeline_predictions[topic["topic_id"]] = {k: snapshot.get(k) for k in ("category", "relevance", "verification_status", "score", "recommendation_eligible")}
        article_associations = set()
        for row in articles:
            article_key = (row["topic_id"], row["url"])
            if article_key in article_associations:
                issues.append(ValidationIssue("duplicate_association", f"duplicate article association {article_key!r}"))
            article_associations.add(article_key)
            if row["topic_id"] not in topic_ids:
                issues.append(ValidationIssue("orphan_topic_association", f"article refers to missing topic {row['topic_id']!r}"))
            article = _json(row["article_json"], f"article {row['url']} article_json", issues)
            if not _url(row["url"]):
                issues.append(ValidationIssue("invalid_url", f"invalid article URL {row['url']!r}"))
            for key in ("publication_date", "retrieved_at"):
                if row[key] is not None and not _timestamp(row[key]):
                    issues.append(ValidationIssue("invalid_timestamp", f"article {row['url']} {key} must be timezone-aware ISO 8601"))
            if article is not None and article.get("url") != row["url"]:
                issues.append(ValidationIssue("snapshot_conflict", f"article JSON URL conflicts with normalized URL {row['url']!r}"))
        claim_associations = set()
        for row in claims:
            key = (row["topic_id"], row["claim_index"])
            if key in claim_associations:
                issues.append(ValidationIssue("duplicate_association", f"duplicate claim association {key!r}"))
                continue
            claim_associations.add(key)
            claim_lookup[key] = row
            if row["topic_id"] not in topic_ids:
                issues.append(ValidationIssue("orphan_topic_association", f"claim refers to missing topic {row['topic_id']!r}"))
            payload = _json(row["claim_json"], f"claim {key} claim_json", issues)
            if payload is not None and (payload.get("claim") != row["claim"] or payload.get("status") != row["status"]):
                issues.append(ValidationIssue("snapshot_conflict", f"claim JSON conflicts with normalized fields for {key!r}"))
        evidence_associations = set()
        for row in links:
            evidence_key = (row["topic_id"], row["claim_index"], row["evidence_url"])
            if evidence_key in evidence_associations:
                issues.append(ValidationIssue("duplicate_association", f"duplicate evidence association {evidence_key!r}"))
            evidence_associations.add(evidence_key)
            if (row["topic_id"], row["claim_index"]) not in claim_lookup:
                issues.append(ValidationIssue("orphan_evidence_link", f"evidence link refers to missing claim {(row['topic_id'], row['claim_index'])!r}"))
            if not _url(row["evidence_url"]):
                issues.append(ValidationIssue("invalid_url", f"invalid evidence URL {row['evidence_url']!r}"))
        for row in statuses:
            _json(row["status_json"], f"source status {row['source_key']} status_json", issues)
        for key in METADATA_KEYS:
            present = isinstance(source_meta, dict) and key in source_meta and source_meta[key] not in (None, "", [], {})
            if not present:
                issues.append(ValidationIssue("missing_metadata", f"missing run metadata: {key}", "warning"))
        return result
    except sqlite3.Error as exc:
        raise ResearchRunReadError(f"Cannot read research run: {exc}") from exc
    finally:
        try:
            if db.in_transaction:
                db.rollback()
        finally:
            db.close()
