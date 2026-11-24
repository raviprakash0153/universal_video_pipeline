"""Persistent Telegram video request intake and safe local production jobs."""
from __future__ import annotations

import re
import json
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import pipeline_store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _url(text: str) -> tuple[str | None, bool]:
    match = re.search(r"https?://[^\s<>]+", text, re.I)
    if not match:
        return None, bool(re.search(r"(?:youtube\.com|youtu\.be|https?://)", text, re.I))
    value = match.group(0).rstrip(".,!?;:)")
    parsed = urlparse(value)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or not host or not (host == "youtu.be" or host.endswith("youtube.com")):
        return None, True
    if host.endswith("youtube.com") and parsed.path.rstrip("/") not in {"", "/watch"} and not parsed.path.startswith(("/shorts/", "/live/", "/embed/")):
        return None, True
    if parsed.path == "/watch" and not parsed.query:
        return None, True
    return value, False


def _fields(text: str) -> dict:
    result: dict = {}
    url, invalid = _url(text)
    if url:
        result["source_url"] = url
    elif invalid:
        result["_invalid_url"] = True
    lower = text.casefold()
    fmt = None
    if re.search(r"\b(both|shorts?\s+and\s+(?:long|long-form)|long-form\s+and\s+shorts?)\b", lower): fmt = "both"
    elif re.search(r"\b(shorts?)\b", lower): fmt = "shorts"
    elif re.search(r"\b(long(?:[- ]form)?(?:\s+video)?)\b", lower): fmt = "long"
    if fmt: result["output_format"] = fmt
    count = re.search(r"\b(\d+)\s+shorts?\b|\b(?:count|number)\s*[:=]\s*(\d+)\b", text, re.I)
    if count:
        number = int(count.group(1) or count.group(2))
        if 1 <= number <= 100: result["shorts_count"] = number
    language = re.search(r"\b(?:caption\s+)?language\s*[:=]\s*([\w][\w -]{0,40})", text, re.I)
    if not language:
        language = re.search(r"\bcaptions?\s+(?:in|to)\s+([\w][\w -]{0,40})", text, re.I)
    if language:
        candidate = language.group(1).strip().rstrip(".!?,;:")
        if candidate.casefold() not in {"original", "same as source"}:
            result["caption_language"] = candidate[:40]
        else: result["caption_language"] = "original"
    # A declaration is accepted only as its own message, with an explicit statement.
    affirmative = re.fullmatch(r"\s*i\s+confirm\s+(?:that\s+)?(?:i\s+own\s+(?:this\s+)?(?:video|source)|i\s+have\s+(?:permission|the\s+rights)\s+to\s+reuse\s+(?:this\s+)?(?:video|source))(?:\s*\.?)\s*", text, re.I)
    negative = re.fullmatch(r"\s*i\s+do\s+not\s+(?:own\s+(?:this\s+)?(?:video|source)|have\s+(?:permission|the\s+rights)\s+to\s+reuse\s+(?:this\s+)?(?:video|source))(?:\s*\.?)\s*", text, re.I)
    if affirmative:
        result["rights_confirmed"] = 1
        result["rights_declaration"] = text
    elif negative: result["rights_confirmed"] = 0
    return result


def _missing(row) -> list[str]:
    missing = []
    if not row["source_url"]: missing.append("YouTube source URL")
    if not row["output_format"]: missing.append("format (Shorts, long-form, or both)")
    if row["output_format"] in {"shorts", "both"} and not row["shorts_count"]: missing.append("number of Shorts")
    if not row["caption_language"]: missing.append("caption language")
    if row["rights_confirmed"] is None: missing.append("separate rights confirmation")
    return missing


def _reply(row, invalid_url=False) -> str:
    miss = _missing(row)
    collected = [f"Source URL: {row['source_url'] or 'missing'}",
                 f"Format: {row['output_format'] or 'missing'}",
                 f"Shorts count: {row['shorts_count'] if row['shorts_count'] is not None else ('not applicable' if row['output_format']=='long' else 'missing')}",
                 f"Caption language: {row['caption_language'] or 'missing'}",
                 f"Rights: {'confirmed explicitly' if row['rights_confirmed']==1 else 'declined' if row['rights_confirmed']==0 else 'not confirmed'}"]
    status = ("Request saved; production queued for local processing." if row["state"] == "ready" else
              "Request saved; still collecting details." if miss else "Request saved.")
    if row["state"] == "rights_denied": status = "Rights were declined. This request cannot proceed; start a new request only for a source you have rights to use."
    if row["state"] == "cancelled": status = "Request cancelled."
    prompt = ""
    if miss: prompt = "\nPlease send: " + "; ".join(miss) + ". Rights must be a separate message: ‘I confirm I own this video’ or ‘I confirm I have permission to reuse this video’."
    if invalid_url: prompt += "\nThat URL is not a valid supported YouTube video URL."
    footer = ("\nNo upload or publishing approval was created." if row["state"] == "ready" else
              "\nNo video processing or upload was started.")
    return status + "\n" + "\n".join(collected) + prompt + footer


def update_was_processed(update_id: int) -> bool:
    """Check the durable ledger so a replay after restart is not routed elsewhere."""
    db = pipeline_store.connect()
    try:
        return db.execute("SELECT 1 FROM telegram_video_updates WHERE update_id=?", (update_id,)).fetchone() is not None
    finally:
        db.close()


def pending_reply(update_id: int):
    db = pipeline_store.connect()
    try:
        return db.execute("SELECT chat_id,response FROM telegram_video_outbox WHERE update_id=? AND status='pending'", (update_id,)).fetchone()
    finally:
        db.close()


def mark_reply_delivered(update_id: int, db=None) -> None:
    own_db = db is None
    db = db or pipeline_store.connect()
    try:
        with db:
            db.execute("UPDATE telegram_video_outbox SET status='delivered',delivered_at=? WHERE update_id=? AND status='pending'", (_now(), update_id))
    finally:
        if own_db: db.close()


def process_update(update: dict, allowed_user: str | None, db=None) -> str | None:
    """Persist and handle one Telegram update. Unauthorized updates are ignored."""
    message = update.get("message") or {}
    chat, sender = message.get("chat") or {}, message.get("from") or {}
    if chat.get("type") != "private" or not allowed_user or str(sender.get("id")) != str(allowed_user): return None
    if not isinstance(message.get("text"), str): return None
    update_id = update.get("update_id")
    if not isinstance(update_id, int): return None
    raw_text = message["text"]
    text = raw_text.strip()
    # These established routes remain available while intake is collecting.
    if text.casefold() in {"brief", "briefing", "daily briefing", "/brief"} or re.match(r"^/(?:research|news)\b|^(?:research|verify)\s+", text, re.I):
        return None
    own_db = db is None
    db = db or pipeline_store.connect()
    try:
        # Serialize the read/modify/write sequence across pollers and threads.
        db.execute("BEGIN IMMEDIATE")
        try:
            prior = db.execute("SELECT response FROM telegram_video_updates WHERE update_id=?", (update_id,)).fetchone()
            if prior:
                pending = db.execute("SELECT response FROM telegram_video_outbox WHERE update_id=? AND status='pending'", (update_id,)).fetchone()
                db.commit()
                return pending["response"] if pending else None
            active = db.execute("SELECT * FROM telegram_video_requests WHERE user_id=? AND chat_id=? AND state='collecting' ORDER BY rowid LIMIT 1", (str(sender["id"]), str(chat["id"]))).fetchone()
            lower = text.casefold()
            is_new = bool(re.match(r"^\s*(?:/video|/request|make\s+(?:a\s+)?video|create\s+(?:a\s+)?video)\b", text, re.I))
            cancel = lower in {"/cancel", "cancel", "cancel request", "stop request"}
            request_id = active["request_id"] if active else None
            if is_new and active:
                response = "A video request is already being collected. Send its details to continue, or send /cancel to cancel it before starting another request."
            elif cancel and active:
                db.execute("UPDATE telegram_video_requests SET state='cancelled',updated_at=? WHERE request_id=?", (_now(), request_id))
                row = db.execute("SELECT * FROM telegram_video_requests WHERE request_id=?", (request_id,)).fetchone()
                response = _reply(row)
            elif is_new or active:
                if is_new:
                    request_id = str(uuid.uuid4()); now = _now()
                    db.execute("INSERT INTO telegram_video_requests(request_id,user_id,chat_id,state,created_at,updated_at) VALUES(?,?,?,'collecting',?,?)", (request_id,str(sender["id"]),str(chat["id"]),now,now))
                row = db.execute("SELECT * FROM telegram_video_requests WHERE request_id=?", (request_id,)).fetchone()
                parsed = _fields(text)
                if parsed.get("rights_confirmed") == 1:
                    parsed["rights_declaration"] = raw_text
                if not is_new and text.casefold() in {"cancel", "/cancel"}:
                    db.execute("UPDATE telegram_video_requests SET state='cancelled',updated_at=? WHERE request_id=?", (_now(), request_id))
                else:
                    values = {k:v for k,v in parsed.items() if not k.startswith("_")}
                    # Conflicting rights answers cannot promote prior denial; denial is terminal.
                    values = {k:v for k,v in values.items() if row[k] is None or k == "rights_confirmed" and row[k] is None}
                    for key, value in values.items(): db.execute(f"UPDATE telegram_video_requests SET {key}=? WHERE request_id=?", (value,request_id))
                    if parsed.get("rights_confirmed") == 0: db.execute("UPDATE telegram_video_requests SET state='rights_denied' WHERE request_id=?", (request_id,))
                    refreshed = db.execute("SELECT * FROM telegram_video_requests WHERE request_id=?", (request_id,)).fetchone()
                    if refreshed["state"] == "collecting" and not _missing(refreshed):
                        db.execute("UPDATE telegram_video_requests SET state='ready' WHERE request_id=?", (request_id,))
                        refreshed = db.execute("SELECT * FROM telegram_video_requests WHERE request_id=?", (request_id,)).fetchone()
                        if refreshed["rights_confirmed"] == 1:
                            db.execute("INSERT OR IGNORE INTO telegram_video_jobs(request_id,state,created_at,rights_declaration) VALUES(?,'queued',?,?)", (request_id,_now(),refreshed["rights_declaration"]))
                row = db.execute("SELECT * FROM telegram_video_requests WHERE request_id=?", (request_id,)).fetchone()
                response = _reply(row, parsed.get("_invalid_url",False))
            else:
                db.rollback()
                return None
            db.execute("INSERT INTO telegram_video_updates(update_id,request_id,response,processed_at) VALUES(?,?,?,?)", (update_id,request_id,response,_now()))
            db.execute("INSERT INTO telegram_video_outbox(update_id,chat_id,response,status) VALUES(?,?,?,'pending')", (update_id,str(chat["id"]),response))
            db.commit()
            return response
        except BaseException:
            db.rollback()
            raise
    finally:
        if own_db: db.close()


def recover_interrupted_jobs(db=None) -> int:
    """Fail stale running jobs after restart; never relaunch an uncertain process."""
    own_db = db is None
    db = db or pipeline_store.connect()
    try:
        with db:
            rows = db.execute("SELECT j.request_id,r.chat_id FROM telegram_video_jobs j JOIN telegram_video_requests r USING(request_id) WHERE j.state='running'").fetchall()
            for row in rows:
                message = "Production did not finish before the service restarted. Its outcome is uncertain, so it was not started again; inspect local outputs before creating a new request."
                db.execute("UPDATE telegram_video_jobs SET state='failed',completed_at=?,error_message=? WHERE request_id=? AND state='running'", (_now(),message,row["request_id"]))
                db.execute("INSERT INTO telegram_video_job_outbox(request_id,chat_id,message,created_at) VALUES(?,?,?,?)", (row["request_id"],row["chat_id"],message,_now()))
            return len(rows)
    finally:
        if own_db: db.close()


def pending_job_notifications(db=None):
    own_db = db is None
    db = db or pipeline_store.connect()
    try:
        return db.execute("SELECT event_id,chat_id,message FROM telegram_video_job_outbox WHERE status='pending' ORDER BY event_id").fetchall()
    finally:
        if own_db: db.close()


def mark_job_notification_delivered(event_id: int, db=None) -> None:
    own_db = db is None
    db = db or pipeline_store.connect()
    try:
        with db:
            db.execute("UPDATE telegram_video_job_outbox SET status='delivered',delivered_at=? WHERE event_id=? AND status='pending'", (_now(),event_id))
    finally:
        if own_db: db.close()


def _safe_failure(exc: BaseException) -> str:
    # Avoid leaking exception reprs, credential-bearing URLs, or local config paths.
    message = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    message = re.sub(r"https?://[^\s]+", "[source URL]", message)
    message = re.sub(r"(?:/Users/|/home/|[A-Za-z]:\\)[^\s:]+", "[local path]", message)
    message = re.sub(r"(?i)(token|secret|password|credential)[=: ]+[^\s,;]+", r"\1=[redacted]", message)
    return (message[:240] or "Unexpected production error.")


def run_queued_production(db=None, notification_callback=None) -> str | None:
    """Claim and synchronously execute at most one ready Telegram production job."""
    own_db = db is None
    db = db or pipeline_store.connect()
    request_id = None
    production_succeeded = False
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT j.request_id,r.* FROM telegram_video_jobs j JOIN telegram_video_requests r USING(request_id) WHERE j.state='queued' ORDER BY j.created_at LIMIT 1").fetchone()
        if row is None:
            db.commit(); return None
        if row["state"] != "ready" or row["rights_confirmed"] != 1 or _missing(row):
            db.execute("UPDATE telegram_video_jobs SET state='failed',completed_at=?,error_message=? WHERE request_id=? AND state='queued'", (_now(),"Request is not ready with explicit positive rights confirmation.",row["request_id"]))
            db.commit(); return None
        request_id = row["request_id"]
        claimed = db.execute("UPDATE telegram_video_jobs SET state='running',started_at=? WHERE request_id=? AND state='queued'", (_now(),request_id)).rowcount
        db.commit()
        if not claimed: return None

        def notify(message):
            with db:
                db.execute("INSERT INTO telegram_video_job_outbox(request_id,chat_id,message,created_at) VALUES(?,?,?,?)", (request_id,row["chat_id"],message,_now()))
            if notification_callback:
                try: notification_callback()
                except Exception: pass

        notify("Production started. Upload is disabled; this creates local outputs for a later preview and approval step.")
        import video_pipeline
        def progress(event):
            stage = str(event.get("event", "processing"))
            kind = event.get("kind")
            index, total = event.get("index"), event.get("total")
            detail = f" {kind}" if kind else ""
            if index is not None and total is not None: detail += f" {index}/{total}"
            notify(f"Production stage: {stage}{detail}.")
        mode = row["output_format"]
        # This value represents only the user's declaration; it is not ownership verification.
        result = video_pipeline.process_video(
            mode=mode, source=row["source_url"],
            auto_clips=row["shorts_count"] if mode in {"shorts", "both"} else 0,
            caption_language=row["caption_language"], rights_state="permission_granted",
            upload=False, progress_callback=progress,
        )
        output_ids = list(getattr(result, "output_ids", []))
        production_succeeded = True
        ids = ", ".join(output_ids) if output_ids else "none returned"
        with db:
            db.execute("UPDATE telegram_video_jobs SET state='completed',completed_at=?,output_ids_json=? WHERE request_id=? AND state='running'", (_now(),json.dumps(output_ids),request_id))
            instructions = (f"Production completed. Each output has its own ID: {ids}. "
                "From the production machine and project directory, with the same `PIPELINE_DATABASE` "
                "setting (default: output/pipeline.sqlite3), run `python3 approval_cli.py inspect <ID>`, then "
                "`python3 approval_cli.py preview <ID>`, then approve with "
                "`python3 approval_cli.py approve <ID> --by YOUR_NAME` or reject with "
                "`python3 approval_cli.py reject <ID> --by YOUR_NAME`. Publish the approved ID with "
                "`python3 approval_cli.py publish <ID>`. "
                "Production did not upload or approve anything.")
            db.execute("INSERT INTO telegram_video_job_outbox(request_id,chat_id,message,created_at) VALUES(?,?,?,?)", (request_id,row["chat_id"],instructions,_now()))
        if notification_callback:
            try: notification_callback()
            except Exception: pass
        return request_id
    except (Exception, SystemExit) as exc:
        if request_id is not None and not production_succeeded:
            explanation = "Production failed: " + _safe_failure(exc)
            try:
                with db:
                    db.execute("UPDATE telegram_video_jobs SET state='failed',completed_at=?,error_message=? WHERE request_id=? AND state='running'", (_now(),explanation,request_id))
                    req = db.execute("SELECT chat_id FROM telegram_video_requests WHERE request_id=?",(request_id,)).fetchone()
                    db.execute("INSERT INTO telegram_video_job_outbox(request_id,chat_id,message,created_at) VALUES(?,?,?,?)", (request_id,req["chat_id"],explanation,_now()))
                if notification_callback:
                    try: notification_callback()
                    except Exception: pass
            except Exception: pass
        return request_id
    finally:
        if own_db: db.close()

