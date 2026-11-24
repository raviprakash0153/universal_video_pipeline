import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import pipeline_store
import telegram_video_intake as intake
import telegram_text_dry_run as telegram_adapter


class TelegramVideoIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = pipeline_store.connect(Path(self.tmp.name) / "pipeline.sqlite3")
        self.seq = 1

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def update(self, text, *, user=7, chat=7, private=True):
        value = {"update_id": self.seq, "message": {"text": text,
            "chat": {"id": chat, "type": "private" if private else "group"}, "from": {"id": user}}}
        self.seq += 1
        return value

    def send(self, text, **kwargs):
        return intake.process_update(self.update(text, **kwargs), "7", self.db)

    def test_multimessage_creation_and_collection(self):
        reply = self.send("/video")
        self.assertIn("YouTube source URL", reply)
        self.send("https://www.youtube.com/watch?v=abc123")
        self.send("both")
        self.send("4 Shorts")
        self.send("caption language: Spanish")
        reply = self.send("I confirm I have permission to reuse this video")
        self.assertIn("production queued", reply)
        row = self.db.execute("SELECT * FROM telegram_video_requests").fetchone()
        self.assertEqual((row["state"], row["source_url"], row["output_format"], row["shorts_count"], row["caption_language"], row["rights_confirmed"]),
                         ("ready", "https://www.youtube.com/watch?v=abc123", "both", 4, "Spanish", 1))
        self.assertEqual(row["rights_declaration"], "I confirm I have permission to reuse this video")
        job = self.db.execute("SELECT rights_declaration FROM telegram_video_jobs").fetchone()
        self.assertEqual(job["rights_declaration"], row["rights_declaration"])

    def test_all_formats_and_count_applicability(self):
        for fmt, state, count in (("long-form", "long", None), ("3 shorts", "shorts", 3), ("both", "both", 2)):
            self.send("/video")
            self.send("https://youtu.be/abc")
            self.send(fmt)
            if count == 2: self.send("2 shorts")
            self.send("language: English")
            response = self.send("I confirm I own this video")
            row = self.db.execute("SELECT * FROM telegram_video_requests ORDER BY rowid DESC LIMIT 1").fetchone()
            self.assertEqual(row["output_format"], state)
            self.assertEqual(row["shorts_count"], count)
            self.assertEqual(row["state"], "ready", response)

    def test_invalid_url_is_not_saved_and_can_be_corrected(self):
        self.send("/video")
        self.assertIn("not a valid", self.send("https://example.com/video"))
        self.assertIsNone(self.db.execute("SELECT source_url FROM telegram_video_requests").fetchone()[0])
        self.send("https://youtu.be/xyz")
        self.assertEqual(self.db.execute("SELECT source_url FROM telegram_video_requests").fetchone()[0], "https://youtu.be/xyz")

    def test_rights_must_be_separate_explicit_and_positive(self):
        self.send("/video")
        self.send("https://youtu.be/abc shorts 2 language: English I confirm I own this video")
        row = self.db.execute("SELECT * FROM telegram_video_requests").fetchone()
        self.assertIsNone(row["rights_confirmed"])
        self.assertEqual(row["state"], "collecting")
        self.send("I do not own this video")
        row = self.db.execute("SELECT * FROM telegram_video_requests").fetchone()
        self.assertEqual((row["rights_confirmed"], row["state"]), (0, "rights_denied"))
        self.assertIsNone(row["rights_declaration"])
        self.assertIsNone(self.send("I confirm I own this video"))

    def test_cancel_authorization_duplicate_and_restart_recovery(self):
        update = self.update("/video")
        first = intake.process_update(update, "7", self.db)
        intake.mark_reply_delivered(update["update_id"], self.db)
        self.assertIsNone(intake.process_update(update, "7", self.db))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM telegram_video_requests").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM telegram_video_updates").fetchone()[0], 1)
        self.assertIsNone(intake.process_update(self.update("/video", user=99), "7", self.db))
        self.assertIsNone(intake.process_update(self.update("/video", private=False), "7", self.db))
        path = self.db.execute("PRAGMA database_list").fetchone()[2]
        self.db.close()
        self.db = pipeline_store.connect(path)
        self.assertIn("YouTube source URL", self.send("both"))
        self.assertIn("cancelled", self.send("/cancel"))
        self.assertEqual(self.db.execute("SELECT state FROM telegram_video_requests").fetchone()[0], "cancelled")

    def test_incomplete_or_unauthorized_cannot_trigger_production_or_upload(self):
        with patch("video_pipeline.process_video") as production, patch("telegram_text_dry_run._telegram_request") as api:
            response = intake.process_update(self.update("/video", user=9), "7", self.db)
            self.assertIsNone(response)
            self.send("/video")
            self.assertEqual(self.db.execute("SELECT state FROM telegram_video_requests").fetchone()[0], "collecting")
            intake.run_queued_production(db=self.db)
            production.assert_not_called()
            api.assert_not_called()

    def test_overlapping_request_is_prompted_and_keeps_existing_request(self):
        self.send("/video")
        original = self.db.execute("SELECT request_id FROM telegram_video_requests").fetchone()[0]
        reply = self.send("/video")
        self.assertIn("already being collected", reply)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM telegram_video_requests").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT request_id FROM telegram_video_requests").fetchone()[0], original)

    def test_concurrent_request_creation_preserves_single_collecting_request(self):
        barrier = threading.Barrier(2)
        errors = []
        def create(update_id):
            db = pipeline_store.connect(Path(self.tmp.name) / "pipeline.sqlite3")
            try:
                barrier.wait()
                intake.process_update({"update_id": update_id, "message": {"text": "/video", "chat": {"id": 7, "type": "private"}, "from": {"id": 7}}}, "7", db)
            except Exception as exc:
                errors.append(exc)
            finally:
                db.close()
        threads = [threading.Thread(target=create, args=(n,)) for n in (101, 102)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM telegram_video_requests WHERE state='collecting'").fetchone()[0], 1)

    def test_reply_failure_can_retry_after_restart_and_duplicate(self):
        update = self.update("/video")
        response = intake.process_update(update, "7", self.db)
        self.assertTrue(response)
        update_id = update["update_id"]
        path = self.db.execute("PRAGMA database_list").fetchone()[2]
        self.db.close()
        self.db = pipeline_store.connect(path)
        self.assertEqual(intake.process_update(update, "7", self.db), response)
        intake.mark_reply_delivered(update_id, self.db)
        self.assertIsNone(intake.process_update(update, "7", self.db))
        self.assertEqual(self.db.execute("SELECT status FROM telegram_video_outbox WHERE update_id=?", (update_id,)).fetchone()[0], "delivered")

    def test_telegram_send_failure_retries_pending_outbox(self):
        update = self.update("/video")
        database_path = self.db.execute("PRAGMA database_list").fetchone()[2]
        connect = pipeline_store.connect
        calls = []
        def send(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1: raise RuntimeError("injected Telegram send failure")
            return {"message_id": 123}
        with patch("telegram_video_intake.pipeline_store.connect", side_effect=lambda: connect(database_path)), \
             patch("telegram_text_dry_run._telegram_request", side_effect=send):
            with self.assertRaises(RuntimeError):
                telegram_adapter._reply_to_update("token", update, "7", set())
            self.assertEqual(self.db.execute("SELECT status FROM telegram_video_outbox WHERE update_id=?", (update["update_id"],)).fetchone()[0], "pending")
            replied = set()
            telegram_adapter._reply_to_update("token", update, "7", replied)
            telegram_adapter._reply_to_update("token", update, "7", replied)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.db.execute("SELECT status FROM telegram_video_outbox WHERE update_id=?", (update["update_id"],)).fetchone()[0], "delivered")

    def test_ledger_insert_failure_rolls_back_request_and_outbox(self):
        self.db.execute("CREATE TRIGGER fail_telegram_ledger BEFORE INSERT ON telegram_video_updates BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(Exception):
            self.send("/video")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM telegram_video_requests").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM telegram_video_outbox").fetchone()[0], 0)

    def test_database_write_and_commit_failures_roll_back(self):
        self.db.execute("CREATE TRIGGER fail_video_insert BEFORE INSERT ON telegram_video_requests BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(Exception): self.send("/video")
        self.db.execute("DROP TRIGGER fail_video_insert")
        class CommitFailure:
            def __init__(self, wrapped): self.wrapped = wrapped
            def execute(self, *args): return self.wrapped.execute(*args)
            def commit(self): raise RuntimeError("injected commit failure")
            def rollback(self): return self.wrapped.rollback()
        failing = CommitFailure(self.db)
        update = self.update("/video")
        with self.assertRaises(RuntimeError): intake.process_update(update, "7", failing)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM telegram_video_requests").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM telegram_video_updates").fetchone()[0], 0)

    def test_fully_populated_rights_denied_request_is_terminal(self):
        with patch("video_pipeline.process_video") as production, patch("telegram_text_dry_run._telegram_request") as api:
            for text in ("/video", "https://youtu.be/abc", "long-form", "language: English", "I do not own this video"):
                self.send(text)
            production.assert_not_called()
            api.assert_not_called()
        row = self.db.execute("SELECT * FROM telegram_video_requests").fetchone()
        self.assertEqual(row["state"], "rights_denied")
        self.assertEqual((row["source_url"], row["output_format"], row["caption_language"], row["rights_confirmed"]),
                         ("https://youtu.be/abc", "long", "English", 0))
        self.assertIsNone(row["rights_declaration"])
        self.assertIsNone(self.send("I confirm I own this video"))
        self.assertEqual(self.db.execute("SELECT state FROM telegram_video_requests").fetchone()[0], "rights_denied")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM telegram_video_jobs").fetchone()[0], 0)

    def complete_request(self, fmt="shorts", rights="I confirm I own this video", count="3 shorts"):
        self.send("/video")
        self.send("https://youtu.be/abc")
        self.send(fmt)
        if fmt != "long-form": self.send(count)
        self.send("caption language: Spanish")
        return self.send(rights)

    def test_ready_request_runs_once_with_explicit_disabled_upload_and_output_ids(self):
        result = type("Result", (), {"output_ids": ["lifecycle:output:youtube"]})()
        with patch("video_pipeline.process_video", return_value=result) as production, \
             patch("video_pipeline.upload_youtube") as upload, \
             patch("telegram_text_dry_run._telegram_request") as api:
            self.complete_request()
            final_update_id = self.seq - 1
            duplicate = self.update("I confirm I own this video")
            duplicate["update_id"] = final_update_id
            intake.process_update(duplicate, "7", self.db)
            request_id = intake.run_queued_production(db=self.db)
            self.assertEqual(intake.run_queued_production(db=self.db), None)
        self.assertTrue(request_id)
        kwargs = production.call_args.kwargs
        self.assertEqual((kwargs["mode"], kwargs["source"], kwargs["auto_clips"], kwargs["caption_language"]),
                         ("shorts", "https://youtu.be/abc", 3, "Spanish"))
        self.assertEqual((kwargs["rights_state"], kwargs["upload"]), ("permission_granted", False))
        upload.assert_not_called()
        api.assert_not_called()
        job = self.db.execute("SELECT state,output_ids_json FROM telegram_video_jobs WHERE request_id=?", (request_id,)).fetchone()
        self.assertEqual((job["state"], job["output_ids_json"]), ("completed", '["lifecycle:output:youtube"]'))

    def test_format_mapping_and_explicit_positive_rights_gate(self):
        with patch("video_pipeline.process_video", return_value=type("R", (), {"output_ids": []})()) as production:
            self.complete_request("long-form", count="")
            long_id = self.db.execute("SELECT request_id FROM telegram_video_jobs").fetchone()[0]
            intake.run_queued_production(db=self.db)
            self.assertEqual(production.call_args.kwargs["mode"], "long")
            self.assertEqual(production.call_args.kwargs["auto_clips"], 0)
            # Each request is isolated by creating a new collection after the previous one is ready.
            self.complete_request("both", count="5 shorts")
            intake.run_queued_production(db=self.db)
            self.assertEqual(production.call_args.kwargs["mode"], "both")
            self.assertEqual(production.call_args.kwargs["auto_clips"], 5)
        self.assertEqual(self.db.execute("SELECT state FROM telegram_video_jobs WHERE request_id=?", (long_id,)).fetchone()[0], "completed")

    def test_missing_or_invalid_rights_never_queue_production(self):
        self.send("/video")
        for text in ("https://youtu.be/abc", "shorts", "2 shorts", "language: English", "I have rights"):
            self.send(text)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM telegram_video_jobs").fetchone()[0], 0)
        self.assertEqual(tuple(self.db.execute("SELECT rights_confirmed,state FROM telegram_video_requests").fetchone()), (None, "collecting"))

    def test_progress_completion_failure_notifications_and_restart_recovery(self):
        self.complete_request()
        notifications = []
        result = type("Result", (), {"output_ids": ["out-1"]})()
        with patch("video_pipeline.process_video") as production:
            def invoke(**kwargs):
                kwargs["progress_callback"]({"event": "rendering", "kind": "short", "index": 1, "total": 3})
                return result
            production.side_effect = invoke
            intake.run_queued_production(db=self.db, notification_callback=lambda: notifications.extend([e["message"] for e in intake.pending_job_notifications(db=self.db)]))
        self.assertFalse(production.call_args.kwargs["upload"])
        messages = [e["message"] for e in intake.pending_job_notifications(db=self.db)]
        self.assertTrue(any("Production started" in m for m in messages))
        self.assertTrue(any("rendering short 1/3" in m for m in messages))
        self.assertTrue(any("out-1" in m for m in messages))
        completion = next(m for m in messages if "Production completed" in m)
        for text in ("PIPELINE_DATABASE", "output/pipeline.sqlite3", "inspect <ID>", "preview <ID>", "approve <ID>",
                     "reject <ID>", "publish <ID>", "out-1"):
            self.assertIn(text, completion)
        request_id = self.db.execute("SELECT request_id FROM telegram_video_jobs").fetchone()[0]
        with patch("video_pipeline.process_video", side_effect=RuntimeError("render failed")):
            # A separate ready request with an injected pipeline failure.
            self.complete_request()
            intake.run_queued_production(db=self.db)
        failed = self.db.execute("SELECT state,error_message FROM telegram_video_jobs WHERE request_id<>?", (request_id,)).fetchone()
        self.assertEqual(failed["state"], "failed")
        self.assertIn("render failed", failed["error_message"])
        with self.db:
            self.db.execute("UPDATE telegram_video_jobs SET state='running' WHERE request_id=?", (request_id,))
        self.assertEqual(intake.recover_interrupted_jobs(self.db), 1)
        self.assertEqual(self.db.execute("SELECT state FROM telegram_video_jobs WHERE request_id=?", (request_id,)).fetchone()[0], "failed")
        self.assertIsNone(intake.run_queued_production(db=self.db))

    def test_claimed_job_interrupted_before_production_requires_manual_inspection(self):
        self.complete_request()
        request_id = self.db.execute("SELECT request_id FROM telegram_video_jobs").fetchone()[0]
        with self.db:
            self.db.execute("UPDATE telegram_video_jobs SET state='running' WHERE request_id=?", (request_id,))
        with patch("video_pipeline.process_video") as production:
            self.assertEqual(intake.recover_interrupted_jobs(self.db), 1)
            self.assertIsNone(intake.run_queued_production(db=self.db))
        production.assert_not_called()
        self.assertEqual(self.db.execute("SELECT state FROM telegram_video_jobs").fetchone()[0], "failed")

    def test_successful_production_completion_persistence_failure_is_uncertain(self):
        self.complete_request()
        self.db.execute("CREATE TRIGGER fail_completion BEFORE UPDATE OF state ON telegram_video_jobs WHEN NEW.state='completed' BEGIN SELECT RAISE(ABORT,'injected completion write failure'); END")
        result = type("Result", (), {"output_ids": ["out-uncertain"]})()
        with patch("video_pipeline.process_video", return_value=result) as production:
            request_id = intake.run_queued_production(db=self.db)
        production.assert_called_once()
        self.assertEqual(self.db.execute("SELECT state FROM telegram_video_jobs WHERE request_id=?", (request_id,)).fetchone()[0], "running")
        self.assertEqual(intake.recover_interrupted_jobs(self.db), 1)
        with patch("video_pipeline.process_video") as production:
            self.assertIsNone(intake.run_queued_production(db=self.db))
        production.assert_not_called()

    def test_job_notification_failure_is_retryable_without_rerunning_production(self):
        self.complete_request()
        result = type("Result", (), {"output_ids": ["out-1"]})()
        with patch("video_pipeline.process_video", return_value=result) as production:
            intake.run_queued_production(db=self.db, notification_callback=lambda: (_ for _ in ()).throw(RuntimeError("send failed")))
            job = self.db.execute("SELECT state FROM telegram_video_jobs").fetchone()
            self.assertEqual(job["state"], "completed")
            notifications = intake.pending_job_notifications(self.db)
            self.assertTrue(notifications)
            # Delivering the durable outbox event is independent of production.
            intake.mark_job_notification_delivered(notifications[-1]["event_id"], self.db)
            self.assertEqual(self.db.execute("SELECT status FROM telegram_video_job_outbox WHERE event_id=?", (notifications[-1]["event_id"],)).fetchone()[0], "delivered")
            self.assertIsNone(intake.run_queued_production(db=self.db))
        production.assert_called_once()

    def test_ambiguous_rights_do_not_enter_production(self):
        self.send("/video")
        for text in ("https://youtu.be/abc", "long-form", "language: English", "I have rights to use this video"):
            self.send(text)
        with patch("video_pipeline.process_video") as production:
            self.assertIsNone(intake.run_queued_production(db=self.db))
        production.assert_not_called()
        row = self.db.execute("SELECT rights_confirmed,rights_declaration,state FROM telegram_video_requests").fetchone()
        self.assertEqual(tuple(row), (None, None, "collecting"))

    def test_briefing_route_bypasses_active_video_collection(self):
        self.send("/video")
        update = self.update("brief")
        self.assertIsNone(intake.process_update(update, "7", self.db))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM telegram_video_updates").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
