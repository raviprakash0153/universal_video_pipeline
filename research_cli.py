"""Operational command line for local topic research and Telegram briefings."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import uuid

import pipeline_store
import research_intelligence as intelligence
from news_research import (GoogleNewsRSSProvider, MultiSourceNewsProvider,
                           diagnose_production_discovery)
from research_scheduler import (DEFAULT_CONFIG, LocalScheduler, ScheduleConfig, install_launchd,
    load_config, next_run, remove_launchd, save_config, validate_config, write_plist)


def _print_status(run_id, topics, statuses, *, verbose=False):
    print(f"Research run: {run_id}")
    for key, value in statuses.items():
        print(f"Provider/query {key}: {value['status']} ({value.get('items', 0)} items)"
              + (f" — {value.get('error')}" if value.get('error') else ""))
        for name, diagnostic in value.get("metrics", {}).get("discovery_diagnostics", {}).items():
            print(f"  {name}: {diagnostic.get('status')} ({diagnostic.get('results_parsed', 0)} results; "
                  f"{diagnostic.get('elapsed_seconds', 0):.1f}s)"
                  + (f" — {diagnostic.get('failure_stage')}: {diagnostic.get('safe_exception_detail')}"
                     if diagnostic.get("safe_exception_detail") else ""))
        metrics = value.get("metrics", {})
        resolution = metrics.get("google_url_resolution")
        if resolution:
            print(f"  Google News URL resolution: {resolution.get('attempted', 0)} attempted; "
                  f"{resolution.get('skipped_budget', 0)} skipped by run limit; "
                  f"request timeout {resolution.get('per_request_timeout_seconds')}s; no retries")
            for reason, count in resolution.get("failures", {}).items():
                print(f"    {count} resolver failure(s): {reason}")
        rejected = metrics.get("articles_rejected", [])
        grouped_reason = Counter(item.get("rejection_reason", "unknown reason") for item in rejected)
        grouped_publisher = Counter(item.get("publisher") or "Unknown publisher" for item in rejected)
        discovered = metrics.get("results_received", 0)
        print(f"  Total discovered: {discovered}; accepted: {max(0, discovered - sum(1 for x in rejected if x.get('stage') == 'discovery-quality'))}; "
              f"rejected: {len(rejected)}; "
              f"verification: {json.dumps(metrics.get('verification_outcomes', {}), sort_keys=True)}")
        if rejected:
            print("  Rejections by reason: " + json.dumps(dict(grouped_reason.most_common()), ensure_ascii=False))
            print("  Rejections by publisher: " + json.dumps(dict(grouped_publisher.most_common()), ensure_ascii=False))
            shown = rejected if verbose else [item for reason in grouped_reason
                for item in [x for x in rejected if x.get("rejection_reason", "unknown reason") == reason][:2]]
            for item in shown:
                print(f"  Rejected [{item.get('stage')}]: {item.get('rejection_reason')} — "
                      f"{item.get('publisher') or 'Unknown publisher'}: {item.get('headline') or item.get('url')}")
            if not verbose and len(shown) < len(rejected):
                print(f"  Rejection samples shown: {len(shown)} of {len(rejected)} (use --verbose for all)")
        for name, failure in metrics.get("provider_errors", {}).items():
            if failure.get("safe_exception_detail"):
                print(f"  Provider error {name}: {failure.get('failure_stage')}: {failure['safe_exception_detail']}")
    print(f"Discovered topics: {len(topics)}")
    print("Categories: " + json.dumps(dict(Counter(t.category for t in topics)), sort_keys=True))
    print("Verification: " + json.dumps(dict(Counter(t.verification_status for t in topics)), sort_keys=True))
    print("Final ranking:")
    for i, topic in enumerate(topics[:10], 1):
        print(f" {i}. [{topic.score:.1f}] {topic.headline} — {topic.category}; {topic.verification_status}")
        print(f"    Recommendation eligible: {topic.recommendation_eligible} — {topic.recommendation_reason}")


def _acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    if os.name == "nt":
        import msvcrt
        try: msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError: os.close(fd); return None
    else:
        import fcntl
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: os.close(fd); return None
    os.ftruncate(fd, 0); os.write(fd, str(os.getpid()).encode())
    return fd


def _record_job_start(db, job_id):
    with db:
        # The OS lock guarantees no live worker is running; remaining running rows
        # are therefore safe to classify as interrupted after a crash/reboot.
        db.execute("UPDATE research_job_runs SET status='interrupted', finished_at=? WHERE status='running'",
                   (datetime.now(timezone.utc).isoformat(),))
        db.execute("INSERT INTO research_job_runs(job_id,started_at,status) VALUES(?,?,'running')",
                   (job_id, datetime.now(timezone.utc).isoformat()))


def run_research(*, config: ScheduleConfig | None = None, categories=None, coverage=None,
                 preview=False, diagnostic=False, db_path=None, lock_path=None, send=False, verbose=False):
    config = validate_config(config or ScheduleConfig())
    selected = categories or {key: intelligence.CATEGORIES[key] for key in config.categories}
    coverage = coverage or config.coverage
    db = pipeline_store.connect(db_path or pipeline_store.DEFAULT_DATABASE)
    lock_fd = None
    job_id = str(uuid.uuid4())
    try:
        lock_file = Path(lock_path or (Path(db_path or pipeline_store.DEFAULT_DATABASE).parent / "research.lock"))
        lock_fd = _acquire_lock(lock_file)
        if lock_fd is None:
            print("Research is already running; overlapping job skipped.", file=sys.stderr)
            return False
        _record_job_start(db, job_id)
        timeout = float(os.environ.get("NEWS_REQUEST_TIMEOUT", "8"))
        retries = min(1, max(0, int(os.environ.get("NEWS_RETRY_LIMIT", "0"))))
        # Daily operational runs use free RSS/publisher pages only. Optional
        # account-backed APIs belong to the separate interactive assistant.
        provider = MultiSourceNewsProvider(providers=[], rss=GoogleNewsRSSProvider(timeout=timeout, retries=retries))
        if diagnostic:
            # Independent bounded calls establish whether discovery itself is
            # healthy before category extraction and verification are assessed.
            probe_query = next(iter(next(iter(selected.values()), ("news",))), "news")
            print("Provider discovery diagnostic:")
            print(json.dumps(diagnose_production_discovery(probe_query, provider), indent=2,
                             ensure_ascii=False, default=str))
        def progress(key, value):
            print(f"Progress: {key} — {value['status']}", flush=True)
        run_id, topics, statuses = intelligence.discover_topics(provider=provider, categories=selected,
            coverage=coverage, progress=progress)
        _print_status(run_id, topics, statuses, verbose=verbose)
        if diagnostic:
            print("Diagnostic pipeline detail (discovery, extraction, verification, and clustering):")
            print(json.dumps(statuses, indent=2, ensure_ascii=False, default=str))
            with db:
                db.execute("UPDATE research_job_runs SET status='diagnostic',finished_at=?,run_id=? WHERE job_id=?",
                           (datetime.now(timezone.utc).isoformat(), run_id, job_id))
            return True
        if preview:
            print("Telegram briefing preview:\n" + intelligence.format_briefing(run_id, topics))
            print("Preview only: no research results were saved or sent; preview job status was recorded.")
            with db:
                db.execute("UPDATE research_job_runs SET status='preview',finished_at=?,run_id=? WHERE job_id=?",
                           (datetime.now(timezone.utc).isoformat(), run_id, job_id))
            return True
        intelligence.persist_research(run_id, topics, statuses, db_path=db_path)
        briefing = intelligence.format_briefing(run_id, topics)
        create_briefing(db, run_id, topics)
        with db:
            db.execute("UPDATE research_job_runs SET status='completed',finished_at=?,run_id=? WHERE job_id=?",
                       (datetime.now(timezone.utc).isoformat(), run_id, job_id))
        print(f"Saved research run and briefing to SQLite: {run_id}")
        should_send = send or bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_ALLOWED_USER_ID"))
        if should_send:
            print(deliver_briefing(run_id, briefing, db_path=db_path))
        else:
            print("Telegram preview:\n" + briefing)
            print("No Telegram message sent. Use `python3 research_cli.py deliver --run-id " + run_id + "` to send.")
        return True
    except Exception as exc:
        with db:
            db.execute("UPDATE research_job_runs SET status='failed',finished_at=?,error_type=? WHERE job_id=?",
                       (datetime.now(timezone.utc).isoformat(), type(exc).__name__, job_id))
        raise
    finally:
        db.close()
        if lock_fd is not None:
            os.close(lock_fd)


def create_briefing(db, run_id, topics):
    import json
    from datetime import timedelta
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    payload = {"user_id": os.environ.get("TELEGRAM_ALLOWED_USER_ID", ""),
               "topics": [intelligence.asdict(t) for t in topics if t.recommendation_eligible][:10]}
    briefing_id = "run-" + run_id
    db.execute("INSERT OR IGNORE INTO telegram_briefings VALUES(?,?,?,?,?)", (briefing_id, run_id,
        datetime.now(timezone.utc).isoformat(), json.dumps(payload, ensure_ascii=False), expires))
    db.execute("INSERT OR IGNORE INTO telegram_delivery(run_id,status,updated_at) VALUES(?,'pending',?)",
        (run_id, datetime.now(timezone.utc).isoformat()))
    db.commit()


def deliver_briefing(run_id, text=None, *, db_path=None, retry=False):
    """Send a saved run once; only a definite Telegram rejection is retryable."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    user_id = os.environ.get("TELEGRAM_ALLOWED_USER_ID")
    if not token or not user_id or not user_id.isdecimal():
        return "Delivery not attempted: set TELEGRAM_BOT_TOKEN and numeric TELEGRAM_ALLOWED_USER_ID in the environment."
    from telegram_text_dry_run import _telegram_request
    db = pipeline_store.connect(db_path or pipeline_store.DEFAULT_DATABASE)
    try:
        row = db.execute("SELECT b.payload_json,d.status,d.updated_at FROM telegram_briefings b JOIN telegram_delivery d USING(run_id) WHERE b.run_id=?", (run_id,)).fetchone()
        if not row: return "No saved briefing exists for that run."
        if row["status"] == "sent": return "Briefing already delivered; duplicate send suppressed."
        if row["status"] == "sending":
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["updated_at"])).total_seconds()
            if age > 300:
                with db:
                    db.execute("UPDATE telegram_delivery SET status='uncertain',last_error=?,updated_at=? WHERE run_id=? AND status='sending'",
                               ("previous delivery process stopped before recording outcome", datetime.now(timezone.utc).isoformat(), run_id))
                return "Prior delivery process was interrupted; outcome is uncertain and retry is suppressed to prevent a duplicate."
            return "Delivery is already in progress; duplicate send suppressed."
        if row["status"] == "uncertain": return "Prior delivery outcome is uncertain; automatic retry suppressed to avoid a duplicate."
        if row["status"] == "failed" and not retry: return "Delivery previously failed; use `deliver --retry` to retry."
        payload = json.loads(row["payload_json"])
        text = text or intelligence.format_briefing(run_id, [intelligence.Topic(**t) for t in payload.get("topics", [])])
        now = datetime.now(timezone.utc).isoformat()
        with db:
            updated = db.execute("UPDATE telegram_delivery SET status='sending',attempts=attempts+1,chat_id=?,updated_at=? WHERE run_id=? AND status IN ('pending','failed')",
                                 (user_id, now, run_id)).rowcount
        if not updated: return "Delivery state changed concurrently; duplicate send suppressed."
        try:
            result = _telegram_request(token, "sendMessage", {"chat_id": int(user_id), "text": text})
        except RuntimeError as exc:
            # Telegram's explicit API rejection is definite; transport failure may
            # have accepted the request, so it remains uncertain and is not retried.
            message = str(exc)
            definite = "unsuccessful response" in message
            with db:
                db.execute("UPDATE telegram_delivery SET status=?,last_error=?,updated_at=? WHERE run_id=?",
                           ("failed" if definite else "uncertain", "Telegram rejected request" if definite else "transport outcome uncertain", datetime.now(timezone.utc).isoformat(), run_id))
            return "Telegram delivery failed; briefing remains saved locally." if definite else "Telegram network outcome is uncertain; briefing remains saved and retry is suppressed to prevent duplicate delivery."
        with db:
            db.execute("UPDATE telegram_delivery SET status='sent',message_id=?,last_error=NULL,updated_at=? WHERE run_id=?",
                       (str(result.get("message_id", "")), datetime.now(timezone.utc).isoformat(), run_id))
        return "Telegram briefing delivered successfully."
    finally: db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run, rank, persist and preview/send one research briefing")
    run.add_argument("--preview", "--dry-run", action="store_true")
    run.add_argument("--diagnostic", action="store_true")
    run.add_argument("--verbose", action="store_true", help="Print every rejected record")
    run.add_argument("--category", action="append", choices=sorted(intelligence.CATEGORIES))
    run.add_argument("--coverage", choices=("india", "global", "india_and_global"))
    run.add_argument("--config", default=str(DEFAULT_CONFIG))
    run.add_argument("--db")
    run.add_argument("--send", action="store_true", help="Send the completed briefing to the authorized user")
    retry = sub.add_parser("deliver"); retry.add_argument("--run-id", required=True); retry.add_argument("--retry", action="store_true"); retry.add_argument("--db")
    tick = sub.add_parser("schedule-tick", help="Internal one-minute scheduler tick; safe to invoke manually")
    tick.add_argument("--config", default=str(DEFAULT_CONFIG)); tick.add_argument("--db")
    sched = sub.add_parser("schedule"); sched.add_argument("action", choices=("configure", "status", "pause", "resume", "plist", "install", "remove")); sched.add_argument("--config", default=str(DEFAULT_CONFIG)); sched.add_argument("--time", default="08:00"); sched.add_argument("--timezone", default="Asia/Kolkata"); sched.add_argument("--category", action="append", choices=sorted(intelligence.CATEGORIES)); sched.add_argument("--coverage", choices=("india", "global", "india_and_global"), default="india_and_global"); sched.add_argument("--plist", default="research.launchd.plist")
    args = parser.parse_args(argv)
    if args.command == "run":
        cfg = load_config(args.config)
        cats = args.category or list(cfg.categories)
        unknown = sorted(set(cats) - intelligence.CATEGORIES.keys())
        if unknown:
            parser.error("unknown configured category: " + ", ".join(unknown))
        return 0 if run_research(config=cfg, categories={c: intelligence.CATEGORIES[c] for c in cats}, coverage=args.coverage,
                                 preview=args.preview, diagnostic=args.diagnostic, db_path=args.db, send=args.send,
                                 verbose=args.verbose) else 1
    if args.command == "deliver":
        print(deliver_briefing(args.run_id, db_path=args.db, retry=args.retry)); return 0
    if args.command == "schedule-tick":
        cfg = load_config(args.config)
        scheduler = LocalScheduler(cfg, db_path=args.db)
        return 0 if scheduler.run_due() else 0
    path = Path(args.config).expanduser()
    if args.action == "configure":
        save_config(ScheduleConfig(args.time, args.timezone, tuple(args.category or ScheduleConfig().categories), args.coverage), path); print(f"Saved {path}"); return 0
    cfg = load_config(path)
    if args.action in {"pause", "resume"}:
        from dataclasses import replace
        save_config(replace(cfg, paused=args.action == "pause"), path); print(f"Scheduler {args.action}d."); return 0
    if args.action == "status":
        db = pipeline_store.connect(); last = db.execute("SELECT started_at,status,run_id FROM research_job_runs ORDER BY started_at DESC LIMIT 1").fetchone(); db.close()
        print(json.dumps({"paused": cfg.paused, "daily_time": cfg.daily_time, "timezone": cfg.timezone,
            "categories": cfg.categories, "coverage": cfg.coverage, "last_run": dict(last) if last else None,
            "next_run": None if cfg.paused else next_run(cfg).isoformat(),
            "note": "launchd runs only while this Mac is awake and available"}, indent=2)); return 0
    if args.action == "plist":
        print(write_plist(args.plist, cfg, path)); return 0
    if args.action == "install":
        print(install_launchd(args.plist)); return 0
    print("Removed." if remove_launchd() else "No installed job found."); return 0


if __name__ == "__main__": raise SystemExit(main())
