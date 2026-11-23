"""Local SQLite persistence for content jobs and publishing evidence."""
from __future__ import annotations

import json
import sqlite3
import hashlib
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dataclasses import asdict
from platform_foundation import CAPABILITY_REGISTRY, PLATFORMS


DEFAULT_DATABASE = Path(os.environ.get("PIPELINE_DATABASE", Path(__file__).resolve().parent / "output" / "pipeline.sqlite3"))
SCHEMA_VERSION = 3


def connect(database: str | Path = DEFAULT_DATABASE) -> sqlite3.Connection:
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    initialize(db)
    return db


def initialize(db: sqlite3.Connection) -> None:
    """Apply additive, versioned migrations. A transaction protects each migration."""
    db.execute("PRAGMA journal_mode=WAL")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
      db.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS content_jobs (
            job_id TEXT PRIMARY KEY, source_path TEXT NOT NULL,
            rights_state TEXT NOT NULL DEFAULT 'unknown', status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS media_assets (
            asset_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES content_jobs(job_id),
            path TEXT NOT NULL, sha256 TEXT NOT NULL, width INTEGER, height INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS evidence (
            evidence_id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES content_jobs(job_id),
            kind TEXT NOT NULL, uri TEXT, details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS platform_capabilities (
            platform TEXT PRIMARY KEY, details_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS platform_outputs (
            output_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES content_jobs(job_id),
            platform TEXT NOT NULL, asset_id TEXT REFERENCES media_assets(asset_id),
            metadata_json TEXT NOT NULL DEFAULT '{}', approval_state TEXT NOT NULL DEFAULT 'not_requested',
            publishing_status TEXT NOT NULL DEFAULT 'not_started', publishing_method TEXT NOT NULL DEFAULT 'manual',
            analytics_status TEXT NOT NULL DEFAULT 'unknown', platform_post_id TEXT, platform_post_url TEXT,
            UNIQUE(job_id, platform, output_id)
        );
        CREATE TABLE IF NOT EXISTS approval_history (
            approval_id INTEGER PRIMARY KEY, output_id TEXT NOT NULL REFERENCES platform_outputs(output_id),
            file_sha256 TEXT NOT NULL, metadata_sha256 TEXT NOT NULL,
            approved INTEGER NOT NULL CHECK(approved IN (0,1)), approver TEXT NOT NULL,
            approved_at TEXT NOT NULL, invalidated_at TEXT, invalidation_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        PRAGMA user_version = 1;
        INSERT OR IGNORE INTO schema_migrations VALUES (1, CURRENT_TIMESTAMP);
        COMMIT;
      """)
    if version < 2:
      db.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS source_provenance (
          provenance_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES content_jobs(job_id),
          source_uri TEXT NOT NULL, source_type TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS format_validations (
          validation_id TEXT PRIMARY KEY, output_id TEXT NOT NULL REFERENCES platform_outputs(output_id),
          status TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS publishing_attempts (
          attempt_id TEXT PRIMARY KEY, output_id TEXT NOT NULL REFERENCES platform_outputs(output_id),
          status TEXT NOT NULL, platform_post_id TEXT, platform_post_url TEXT,
          details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS analytics_records (
          analytics_id TEXT PRIMARY KEY, output_id TEXT NOT NULL REFERENCES platform_outputs(output_id),
          status TEXT NOT NULL, metrics_json TEXT NOT NULL DEFAULT '{}', retrieved_at TEXT NOT NULL
        );
        PRAGMA user_version = 2;
        INSERT OR IGNORE INTO schema_migrations VALUES (2, CURRENT_TIMESTAMP);
        COMMIT;
        """)
    if version < 3:
      db.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS research_job_runs(
          job_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
          status TEXT NOT NULL, run_id TEXT, error_type TEXT
        );
        CREATE TABLE IF NOT EXISTS telegram_delivery(
          run_id TEXT PRIMARY KEY REFERENCES research_runs(run_id),
          status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','failed','uncertain')),
          attempts INTEGER NOT NULL DEFAULT 0, chat_id TEXT, message_id TEXT,
          last_error TEXT, updated_at TEXT NOT NULL
        );
        PRAGMA user_version = 3;
        INSERT OR IGNORE INTO schema_migrations VALUES (3, CURRENT_TIMESTAMP);
        COMMIT;
      """)
    db.executemany("INSERT OR IGNORE INTO platform_capabilities(platform, details_json) VALUES (?, ?)",
                   ((platform, json.dumps(asdict(CAPABILITY_REGISTRY[platform]), sort_keys=True))
                    for platform in PLATFORMS))
    db.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def transaction(db: sqlite3.Connection):
    """Expose an explicit transaction context for multi-record lifecycle updates."""
    return db


def create_job(db: sqlite3.Connection, job_id: str, source_path: str, *,
               rights_state: str = "unknown", metadata: dict[str, Any] | None = None) -> None:
    db.execute("INSERT INTO content_jobs(job_id, source_path, rights_state, metadata_json) VALUES (?, ?, ?, ?)",
               (job_id, source_path, rights_state, json.dumps(metadata or {}, sort_keys=True)))
    db.commit()


def set_job_status(db: sqlite3.Connection, job_id: str, status: str) -> None:
    with db:
        changed = db.execute("UPDATE content_jobs SET status=? WHERE job_id=?", (status, job_id)).rowcount
        if not changed:
            raise KeyError(f"Unknown job: {job_id}")


def record_provenance(db: sqlite3.Connection, job_id: str, source_uri: str, source_type: str = "local",
                      details: dict[str, Any] | None = None) -> None:
    db.execute("INSERT INTO source_provenance VALUES (?, ?, ?, ?, ?, ?)",
               (str(uuid.uuid4()), job_id, source_uri, source_type,
                json.dumps(details or {}, sort_keys=True), _now()))
    db.commit()


def register_asset(db: sqlite3.Connection, job_id: str, path: str | Path, *,
                   width: int | None = None, height: int | None = None) -> str:
    file_path = Path(path).resolve(strict=True)
    digest = hashlib.sha256()
    with file_path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    asset_id = str(uuid.uuid4())
    try:
        stored_path = file_path.relative_to(Path(__file__).resolve().parent).as_posix()
    except ValueError:
        stored_path = str(file_path)
    with db:
        db.execute("INSERT INTO media_assets(asset_id,job_id,path,sha256,width,height) VALUES(?,?,?,?,?,?)",
                   (asset_id, job_id, stored_path, digest.hexdigest(), width, height))
    return asset_id


def set_rights(db: sqlite3.Connection, job_id: str, state: str, evidence: dict[str, Any] | None = None) -> None:
    with db:
        changed = db.execute("UPDATE content_jobs SET rights_state=? WHERE job_id=?", (state, job_id)).rowcount
        if not changed:
            raise KeyError(f"Unknown job: {job_id}")
        if evidence is not None:
            db.execute("INSERT INTO evidence(job_id,kind,details_json) VALUES(?,?,?)",
                       (job_id, "rights", json.dumps(evidence, sort_keys=True)))


def record_validation(db: sqlite3.Connection, output_id: str, status: str, details: dict[str, Any] | None = None) -> None:
    with db:
        db.execute("INSERT INTO format_validations VALUES(?,?,?,?,?)",
                   (str(uuid.uuid4()), output_id, status, json.dumps(details or {}, sort_keys=True), _now()))


def _metadata_hash(metadata_json: str) -> str:
    obj = json.loads(metadata_json)
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def decide_approval(db: sqlite3.Connection, output_id: str, approved: bool, approver: str) -> None:
    with db:
        row = db.execute("""SELECT o.metadata_json, a.path, a.sha256, j.rights_state
          FROM platform_outputs o JOIN content_jobs j ON j.job_id=o.job_id
          LEFT JOIN media_assets a ON a.asset_id=o.asset_id WHERE o.output_id=?""", (output_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown output: {output_id}")
        if approved and row["rights_state"] not in {"owned", "licensed", "permission_granted", "public_domain"}:
            raise PermissionError(f"Source rights '{row['rights_state']}' are not eligible for approval.")
        if approved and (not row["path"] or not row["sha256"]):
            raise PermissionError("An output with registered media and SHA-256 is required for approval.")
        db.execute("INSERT INTO approval_history(output_id,file_sha256,metadata_sha256,approved,approver,approved_at) VALUES(?,?,?,?,?,?)",
                   (output_id, row["sha256"] or "", _metadata_hash(row["metadata_json"]), int(approved), approver, _now()))
        db.execute("UPDATE platform_outputs SET approval_state=? WHERE output_id=?",
                   ("approved" if approved else "rejected", output_id))


def list_outputs(db: sqlite3.Connection, state: str | None = None):
    query = """SELECT o.output_id,o.platform,o.approval_state,o.metadata_json,o.publishing_status,
      a.path,a.sha256,j.rights_state,j.source_path,j.job_id
      FROM platform_outputs o JOIN content_jobs j ON j.job_id=o.job_id
      LEFT JOIN media_assets a ON a.asset_id=o.asset_id"""
    args = ()
    if state:
        query += " WHERE o.approval_state=?"; args = (state,)
    return db.execute(query + " ORDER BY o.rowid", args).fetchall()


def validate_db_approval(db: sqlite3.Connection, output_id: str) -> sqlite3.Row:
    row = db.execute("""SELECT o.*,a.path,a.sha256 AS current_sha,j.rights_state,h.approved,h.file_sha256,
            h.metadata_sha256,h.approver,h.approved_at,h.invalidated_at
          FROM platform_outputs o JOIN content_jobs j ON j.job_id=o.job_id
          LEFT JOIN media_assets a ON a.asset_id=o.asset_id
          LEFT JOIN approval_history h ON h.approval_id=(SELECT MAX(approval_id) FROM approval_history WHERE output_id=o.output_id)
          WHERE o.output_id=?""", (output_id,)).fetchone()
    if row is None or row["approved"] != 1 or row["invalidated_at"] or row["approval_state"] != "approved":
        raise PermissionError("No current approval exists for this platform output.")
    reason = None
    if row["rights_state"] not in {"owned", "licensed", "permission_granted", "public_domain"}:
        reason = "source rights are no longer eligible"
    else:
        file_path = Path(row["path"])
        if not file_path.is_absolute(): file_path = Path(__file__).resolve().parent / file_path
        try:
            digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        except OSError:
            digest = ""
        if digest != row["file_sha256"] or digest != row["current_sha"]:
            reason = "media SHA-256 changed or media is unavailable"
        elif _metadata_hash(row["metadata_json"]) != row["metadata_sha256"]:
            reason = "output metadata changed"
    if reason:
        with db:
            db.execute("UPDATE approval_history SET invalidated_at=?, invalidation_reason=? WHERE approval_id=(SELECT MAX(approval_id) FROM approval_history WHERE output_id=?)",
                       (_now(), reason, output_id))
            db.execute("UPDATE platform_outputs SET approval_state='invalidated' WHERE output_id=?", (output_id,))
        raise PermissionError(f"Approval invalidated: {reason}.")
    return row


def create_platform_output(db: sqlite3.Connection, output_id: str, job_id: str,
                           platform: str, *, asset_id: str | None = None,
                           metadata: dict[str, Any] | None = None) -> None:
    if platform not in PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform}")
    db.execute("INSERT INTO platform_outputs(output_id, job_id, platform, asset_id, metadata_json) VALUES (?, ?, ?, ?, ?)",
               (output_id, job_id, platform, asset_id, json.dumps(metadata or {}, sort_keys=True)))
    db.commit()
