"""Optional Telegram adapter for persistent intake and local production."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from command_engine import CommandJob, InputType, process_command
from news_research import (GoogleNewsRSSProvider, MultiSourceNewsProvider,
                           diagnose_discovery_providers, diagnose_production_discovery,
                           diagnose_explicit_google_urls, format_report, research_news)
from research_intelligence import (discover_topics, format_briefing, persist_research)
import pipeline_store
import telegram_video_intake


API_ROOT = "https://api.telegram.org/bot{token}/{method}"
FILE_ROOT = "https://api.telegram.org/file/bot{token}/{path}"
MAX_VOICE_BYTES = 20 * 1024 * 1024
_voice_model = None


def format_job(job: CommandJob) -> str:
    """Render a parsed job for a human; this function has no side effects."""
    fields = [
        f"Job: {job.job_id}", f"Input: {job.input_type.value}",
        f"Intent: {job.intent.value}", f"Source URL: {job.source_url or 'not provided'}",
        f"Topic/claim: {job.topic_or_claim or 'not provided'}",
        f"Output: {job.output_format.value if job.output_format else 'not specified'}",
        f"Requested Shorts: {job.requested_shorts_count if job.requested_shorts_count is not None else 'not specified'}",
        f"Caption language: {job.caption_language}",
        f"Rights confirmed: {job.rights_confirmed}",
        f"Status: {job.status}", f"Approval: {job.approval_state}",
    ]
    if job.clarification_needed:
        fields.append("Needs clarification: " + " ".join(job.clarification_needed))
    fields.append("Dry run only: no search, download, render, or upload was performed.")
    return "\n".join(fields)


def build_reply(text: str, *, input_type: InputType = InputType.TEXT,
                transcript: str | None = None, news_provider=None,
                include_diagnostics: bool = False) -> str:
    """Parse and format a user message; intentionally does not contact Telegram."""
    job = process_command(text, input_type=input_type)
    if job.intent.value in {"news_discovery", "news_verification"}:
        report = research_news(job.topic_or_claim or "", provider=news_provider or MultiSourceNewsProvider(),
                               verify_claim=job.intent.value == "news_verification",
                               latest=bool(re.search(r"\b(latest|today|recent|current|newest)\b", text, re.I)))
        reply = format_report(report, include_diagnostics=include_diagnostics)
    else:
        reply = format_job(job)
    if transcript is not None:
        reply = "Recognized transcript: " + transcript + "\n" + reply
    return reply


def _download_voice(token: str, file_id: str, directory: str) -> Path:
    """Download a bounded Telegram audio file into the caller-owned temp directory."""
    metadata = _telegram_request(token, "getFile", {"file_id": file_id})
    relative = metadata.get("file_path")
    if not isinstance(relative, str) or not relative or relative.startswith("/") or ".." in Path(relative).parts:
        raise RuntimeError("Telegram returned an invalid voice file path.")
    suffix = Path(relative).suffix.lower()
    if suffix not in {".oga", ".ogg", ".mp3", ".m4a", ".wav", ".webm", ".opus"}:
        suffix = ".audio"
    request = Request(FILE_ROOT.format(token=token, path=relative))
    destination = Path(directory) / ("telegram-voice" + suffix)
    try:
        with urlopen(request, timeout=40) as response, destination.open("wb") as output:
            total = 0
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_VOICE_BYTES:
                    raise RuntimeError("Telegram voice file exceeds the supported size limit.")
                output.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise RuntimeError("Telegram voice download failed (details suppressed).") from None
    return destination


def transcribe_voice(path: Path) -> str:
    """Transcribe locally with the same CPU/int8/automatic language defaults as the pipeline."""
    global _voice_model
    from faster_whisper import WhisperModel
    if _voice_model is None:
        _voice_model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, _ = _voice_model.transcribe(str(path), word_timestamps=False, vad_filter=True, beam_size=5)
    text_parts = []
    reliable = False
    for segment in segments:
        text = str(getattr(segment, "text", "")).strip()
        if not text:
            continue
        text_parts.append(text)
        no_speech = getattr(segment, "no_speech_prob", 0.0)
        logprob = getattr(segment, "avg_logprob", 0.0)
        if no_speech is None:
            no_speech = 0.0
        if logprob is None:
            logprob = 0.0
        if float(no_speech) < 0.6 and float(logprob) > -1.0:
            reliable = True
    transcript = " ".join(text_parts).strip()
    if not transcript or not reliable:
        return ""
    return transcript


def _voice_reply(token: str, message: dict, news_provider=None) -> str:
    voice = message.get("voice") or message.get("audio")
    if not isinstance(voice, dict) or not isinstance(voice.get("file_id"), str):
        return "I couldn't read that voice message. Please repeat it or send text instead."
    try:
        # TemporaryDirectory owns all downloaded voice bytes and removes them on every exit path.
        with tempfile.TemporaryDirectory(prefix="universal-video-telegram-") as directory:
            path = _download_voice(token, voice["file_id"], directory)
            transcript = transcribe_voice(path)
    except Exception:
        return "I couldn't transcribe that voice message. Please repeat it or send text instead."
    if not transcript:
        return "I couldn't reliably recognize that voice message. Please repeat it or send text instead."
    return build_reply(transcript, input_type=InputType.TRANSCRIBED_VOICE,
                       transcript=transcript, news_provider=news_provider)


def _telegram_request(token: str, method: str, payload: dict) -> dict:
    # Never include request URLs, headers, or exception text: the token is in the API URL.
    request = Request(
        API_ROOT.format(token=token, method=method),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=40 if method == "getUpdates" else 15) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception:
        raise RuntimeError("Telegram request failed (details suppressed).") from None
    if not body.get("ok"):
        raise RuntimeError("Telegram returned an unsuccessful response.")
    return body["result"]


def _reply_to_update(token: str, update: dict, allowed_user: str | None,
                     replied_update_ids: set[int], news_provider=None) -> None:
    """Send at most one reply for each Telegram update seen by this poller."""
    update_id = update["update_id"]
    if update_id in replied_update_ids:
        return
    message = update.get("message", {})
    chat = message.get("chat", {})
    sender = message.get("from", {})
    is_text = isinstance(message.get("text"), str)
    is_voice = isinstance(message.get("voice"), dict) or isinstance(message.get("audio"), dict)
    if chat.get("type") != "private" or not (is_text or is_voice):
        return
    if not allowed_user or str(sender.get("id")) != allowed_user:
        return
    if is_text:
        # Persistent video intake runs before the established briefing/research
        # routes, which remain available when no video request is active.
        intake_response = telegram_video_intake.process_update(update, allowed_user)
        if intake_response is not None:
            telegram_video_intake.run_queued_production(notification_callback=lambda: _deliver_video_job_notifications(token))
            _deliver_video_job_notifications(token)
            _telegram_request(token, "sendMessage", {"chat_id": chat["id"], "text": intake_response})
            telegram_video_intake.mark_reply_delivered(update_id)
            replied_update_ids.add(update_id)
            return
        if message["text"].strip().casefold() in {"brief", "briefing", "daily briefing", "/brief"}:
            response = _topic_selection_or_brief(message["text"], str(sender["id"]), news_provider)
            db = _research_db()
            try:
                latest = db.execute("SELECT run_id FROM telegram_briefings ORDER BY created_at DESC LIMIT 1").fetchone()
            finally: db.close()
            if latest is not None:
                from research_cli import deliver_briefing
                result = deliver_briefing(latest["run_id"], response)
                if "failed" in result or "uncertain" in result:
                    print("Telegram briefing delivery did not complete; local briefing retained.", file=sys.stderr)
                replied_update_ids.add(update_id)
                return
        response = _topic_selection_or_brief(message["text"], str(sender["id"]), news_provider)
        if response is not None:
            _telegram_request(token, "sendMessage", {"chat_id": chat["id"], "text": response})
            replied_update_ids.add(update_id)
            return
    # Record only after a successful send so Telegram/API errors remain visible
    # and do not masquerade as a successful reply.
    _telegram_request(token, "sendMessage", {
        "chat_id": chat["id"],
        "text": build_reply(message["text"], news_provider=news_provider) if is_text
        else _voice_reply(token, message, news_provider=news_provider),
    })
    replied_update_ids.add(update_id)


def _research_db():
    return pipeline_store.connect()


def _deliver_video_job_notifications(token: str) -> None:
    """Send durable production events; leave failed sends pending for retry."""
    for event in telegram_video_intake.pending_job_notifications():
        _telegram_request(token, "sendMessage", {"chat_id": event["chat_id"], "text": event["message"]})
        telegram_video_intake.mark_job_notification_delivered(event["event_id"])


def _topic_selection_or_brief(text: str, user_id: str, provider=None) -> str | None:
    """Handle explicit briefing requests and selections from its active snapshot."""
    normalized = text.strip().casefold()
    if normalized not in {"brief", "briefing", "daily briefing", "/brief"}:
        # Only interpret a selection when this user has an unexpired briefing.
        db = _research_db()
        try:
            row = db.execute("SELECT * FROM telegram_briefings WHERE expires_at>? ORDER BY created_at DESC LIMIT 1",
                             (datetime.now(timezone.utc).isoformat(),)).fetchone()
            if row is None:
                if re.fullmatch(r"(?:topic\s*)?#?[0-9]+", normalized):
                    return "That briefing has expired. Send 'brief' to get a fresh shortlist."
                return None
            # Selections are bound to the briefing's intended Telegram user.
            payload = json.loads(row["payload_json"])
            if payload.get("user_id") != user_id:
                return "This topic briefing belongs to another authorized account."
            if normalized in {"no", "none", "skip", "decline", "no thanks"}:
                return "Okay, no topic selected today."
            snapshot_topics = payload.get("topics", [])
            detail = re.fullmatch(r"(?:details?|more)\s+(?:topic\s*)?#?([0-9]+)", normalized)
            if detail:
                index = int(detail.group(1)) - 1
                if index < 0 or index >= len(snapshot_topics):
                    return "That number is not in the current shortlist."
                topic = snapshot_topics[index]
                links = "\n".join(f"{a.get('publisher')}: {a.get('url')}" for a in topic.get("articles", [])[:6])
                claim_text = "\n".join(f"{c.get('status')}: {c.get('claim')} — {c.get('uncertain') or c.get('confirmed')}"
                                         for c in topic.get("claims", [])[:6])
                return f"{topic['headline']}\n{topic['verification_status']} · score {topic['score']}/100\n{claim_text}\n{links}"
            match = re.fullmatch(r"(?:topic\s*)?#?([0-9]+)", normalized)
            if match:
                index = int(match.group(1)) - 1
                if index < 0 or index >= len(snapshot_topics):
                    return "That number is not in the current shortlist. Send a listed number, or 'no'."
                selected = snapshot_topics[index]
                db.execute("INSERT INTO topic_selections VALUES(?,?,?,?,?,?,?)",
                           (str(uuid.uuid4()), row["briefing_id"], selected["topic_id"], None, user_id,
                            datetime.now(timezone.utc).isoformat(), json.dumps(selected, ensure_ascii=False, sort_keys=True)))
                db.commit()
                return f"Saved topic {index+1}: {selected['headline']}\nYour research snapshot and evidence are saved for production."
            if len(text.strip()) < 3:
                return "Send a topic number, a custom topic, or 'no'."
            custom = {"headline": text.strip(), "category": "custom", "verification_status": "UNRESEARCHED", "articles": [], "claims": []}
            db.execute("INSERT INTO topic_selections VALUES(?,?,?,?,?,?,?)",
                       (str(uuid.uuid4()), row["briefing_id"], None, text.strip(), user_id,
                        datetime.now(timezone.utc).isoformat(), json.dumps(custom, ensure_ascii=False, sort_keys=True)))
            db.commit()
            return f"Saved your custom topic: {text.strip()}\nIt is marked unresearched until a research run inspects sources."
        except sqlite3.OperationalError:
            return None
        finally:
            db.close()

    run_id, topics, source_status = discover_topics(provider=provider or MultiSourceNewsProvider())
    persist_research(run_id, topics, source_status)
    db = _research_db()
    try:
        briefing_id = str(uuid.uuid4())
        expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        payload = {"user_id": user_id, "topics": [asdict_topic(t) for t in topics[:10]]}
        db.execute("INSERT INTO telegram_briefings VALUES(?,?,?,?,?)", (briefing_id, run_id,
                   datetime.now(timezone.utc).isoformat(), json.dumps(payload, ensure_ascii=False), expires))
        db.execute("INSERT OR IGNORE INTO telegram_delivery(run_id,status,updated_at) VALUES(?,'pending',?)",
                   (run_id, datetime.now(timezone.utc).isoformat()))
        db.commit()
        return format_briefing(run_id, topics)
    finally:
        db.close()


def asdict_topic(topic):
    from dataclasses import asdict
    return asdict(topic)


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Required; never execute production work.")
    parser.add_argument("--diagnose-news", metavar="QUERY",
                        help="Probe the three discovery providers independently without starting Telegram polling.")
    parser.add_argument("--diagnose-production-news", metavar="QUERY",
                        help="Run the actual production discovery methods sequentially and concurrently, without Telegram polling.")
    parser.add_argument("--diagnose-selected-news", metavar="QUERY",
                        help="Run selected-article Google News research and print safe resolver diagnostics; no Telegram polling.")
    parser.add_argument("--diagnose-news-urls", metavar="URL", nargs="+",
                        help="Diagnose each supplied supported Google News article URL independently; no discovery or ranking.")
    parser.add_argument("--connect-timeout", type=float, default=5.0, help="Diagnostic TCP connect timeout in seconds.")
    parser.add_argument("--read-timeout", type=float, default=8.0, help="Diagnostic response read timeout in seconds.")
    args = parser.parse_args()
    if not args.dry_run:
        parser.error("--dry-run is required")
    diagnostic_query = getattr(args, "diagnose_news", None)
    production_query = getattr(args, "diagnose_production_news", None)
    selected_query = getattr(args, "diagnose_selected_news", None)
    explicit_urls = getattr(args, "diagnose_news_urls", None)
    connect_timeout = getattr(args, "connect_timeout", 5.0)
    read_timeout = getattr(args, "read_timeout", 8.0)
    if diagnostic_query is not None:
        if connect_timeout <= 0 or read_timeout <= 0:
            parser.error("diagnostic timeouts must be positive")
        print(json.dumps(diagnose_discovery_providers(diagnostic_query,
                          connect_timeout=connect_timeout, read_timeout=read_timeout), indent=2))
        return 0
    if production_query is not None:
        timeout = float(os.environ.get("NEWS_REQUEST_TIMEOUT", "8"))
        retries = int(os.environ.get("NEWS_RETRY_LIMIT", "0"))
        production_provider = MultiSourceNewsProvider(
            rss=GoogleNewsRSSProvider(timeout=timeout, retries=retries))
        print(json.dumps(diagnose_production_discovery(production_query, production_provider), indent=2))
        return 0
    if selected_query is not None:
        timeout = float(os.environ.get("NEWS_REQUEST_TIMEOUT", "8"))
        retries = int(os.environ.get("NEWS_RETRY_LIMIT", "0"))
        rss_provider = GoogleNewsRSSProvider(timeout=timeout, retries=retries)
        provider = MultiSourceNewsProvider(rss=rss_provider)
        print(build_reply("Fetch news about " + selected_query, news_provider=provider,
                          include_diagnostics=True))
        return 0
    if explicit_urls is not None:
        timeout = float(os.environ.get("NEWS_REQUEST_TIMEOUT", "8"))
        retries = int(os.environ.get("NEWS_RETRY_LIMIT", "0"))
        provider = GoogleNewsRSSProvider(timeout=timeout, retries=retries)
        print(json.dumps(diagnose_explicit_google_urls(explicit_urls, provider), indent=2))
        return 0
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Set TELEGRAM_BOT_TOKEN in the environment.", file=sys.stderr)
        return 2
    allowed_user = os.environ.get("TELEGRAM_ALLOWED_USER_ID")
    if not allowed_user or not allowed_user.isdecimal():
        print("Set TELEGRAM_ALLOWED_USER_ID to a numeric Telegram user ID.", file=sys.stderr)
        return 2
    offset = None
    replied_update_ids: set[int] = set()
    timeout = float(os.environ.get("NEWS_REQUEST_TIMEOUT", "8"))
    retries = int(os.environ.get("NEWS_RETRY_LIMIT", "0"))
    news_provider = MultiSourceNewsProvider(rss=GoogleNewsRSSProvider(timeout=timeout, retries=retries))
    print("Telegram text/voice assistant is polling. News search uses configured social APIs and RSS discovery.")
    telegram_video_intake.recover_interrupted_jobs()
    while True:
        try:
            _deliver_video_job_notifications(token)
            telegram_video_intake.run_queued_production(notification_callback=lambda: _deliver_video_job_notifications(token))
            query = {"timeout": 30, "allowed_updates": ["message"]}
            if offset is not None:
                query["offset"] = offset
            updates = _telegram_request(token, "getUpdates", query)
            for update in updates:
                _reply_to_update(token, update, allowed_user, replied_update_ids, news_provider)
                # Telegram acknowledges updates only when the next getUpdates
                # call uses this offset. A processing/send exception leaves it
                # unchanged, so Telegram redelivers and the durable ledger can
                # retry a pending intake response.
                offset = update["update_id"] + 1
        except KeyboardInterrupt:
            return 0
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            time.sleep(2)
        except (KeyError, TypeError, ValueError):
            # Ignore malformed updates without logging user input or credentials.
            continue


if __name__ == "__main__":
    raise SystemExit(run())
