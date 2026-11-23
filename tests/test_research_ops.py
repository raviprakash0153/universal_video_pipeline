import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pipeline_store
from research_scheduler import (LocalScheduler, ScheduleConfig, load_config, next_run,
                               save_config, validate_config, plist)
import research_cli


class SchedulerTests(unittest.TestCase):
    def test_config_roundtrip_validation_and_timezone_next_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schedule.json"
            config = ScheduleConfig("07:30", "Asia/Kolkata", ("science",), "india")
            save_config(config, path)
            self.assertEqual(load_config(path), config)
            local_now = datetime(2026, 11, 23, 7, 29, tzinfo=ZoneInfo("Asia/Kolkata"))
            self.assertEqual(next_run(config, local_now).hour, 7)
            self.assertEqual(next_run(config, local_now).minute, 30)
            with self.assertRaises(ValueError): validate_config(ScheduleConfig("25:90"))
            self.assertEqual(plist(path)["StartInterval"], 60)

    def test_duplicate_daily_schedule_suppressed_and_interrupted_run_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "db.sqlite3"
            db = pipeline_store.connect(db_path)
            db.execute("INSERT INTO research_job_runs(job_id,started_at,status) VALUES(?,?,?)",
                       ("dead", "2026-11-23T01:00:00+00:00", "running"))
            db.execute("INSERT INTO research_job_runs(job_id,started_at,finished_at,status) VALUES(?,?,?,?)",
                       ("done", "2026-11-23T02:00:00+00:00", "2026-11-23T02:01:00+00:00", "completed"))
            db.commit(); db.close()
            config = ScheduleConfig("00:00", "UTC", ("science",), "global")
            with patch("research_cli.run_research") as execute:
                scheduler = LocalScheduler(config, db_path=db_path, lock_path=Path(tmp)/"lock")
                self.assertFalse(scheduler.run_due(datetime(2026, 11, 23, 3, tzinfo=ZoneInfo("UTC"))))
                execute.assert_not_called()
            research_cli._record_job_start(pipeline_store.connect(db_path), "fresh")
            db = pipeline_store.connect(db_path)
            self.assertEqual(db.execute("SELECT status FROM research_job_runs WHERE job_id='dead'").fetchone()[0], "interrupted")
            db.close()

    def test_overlapping_process_lock_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"locked"
            first = research_cli._acquire_lock(path)
            try: self.assertIsNone(research_cli._acquire_lock(path))
            finally:
                if first is not None: __import__("os").close(first)


class DeliveryTests(unittest.TestCase):
    def _database(self, path):
        import research_intelligence as ri
        ri.persist_research("run1", [], {}, db_path=path)
        db = pipeline_store.connect(path)
        research_cli.create_briefing(db, "run1", [])
        db.close()

    def test_success_and_duplicate_delivery_suppression(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp)/"db.sqlite3"; self._database(db_path)
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN":"secret", "TELEGRAM_ALLOWED_USER_ID":"123"}), \
                 patch("telegram_text_dry_run._telegram_request", return_value={"message_id": 7}) as request:
                self.assertIn("successfully", research_cli.deliver_briefing("run1", db_path=db_path))
                self.assertIn("already delivered", research_cli.deliver_briefing("run1", db_path=db_path))
                request.assert_called_once()
            db = pipeline_store.connect(db_path)
            self.assertEqual(db.execute("SELECT status FROM telegram_delivery").fetchone()[0], "sent")
            db.close()

    def test_definite_failure_is_saved_and_explicit_retry_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp)/"db.sqlite3"; self._database(db_path)
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN":"secret", "TELEGRAM_ALLOWED_USER_ID":"123"}), \
                 patch("telegram_text_dry_run._telegram_request", side_effect=[RuntimeError("Telegram returned an unsuccessful response."), {"message_id": 8}]) as request:
                self.assertIn("failed", research_cli.deliver_briefing("run1", db_path=db_path))
                self.assertIn("use `deliver --retry`", research_cli.deliver_briefing("run1", db_path=db_path))
                self.assertIn("successfully", research_cli.deliver_briefing("run1", db_path=db_path, retry=True))
                self.assertEqual(request.call_count, 2)

    def test_network_ambiguous_failure_cannot_be_retried_automatically(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp)/"db.sqlite3"; self._database(db_path)
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN":"secret", "TELEGRAM_ALLOWED_USER_ID":"123"}), \
                 patch("telegram_text_dry_run._telegram_request", side_effect=RuntimeError("Telegram request failed (details suppressed).")) as request:
                self.assertIn("uncertain", research_cli.deliver_briefing("run1", db_path=db_path))
                self.assertIn("uncertain", research_cli.deliver_briefing("run1", db_path=db_path, retry=True))
                request.assert_called_once()


class ResearchCommandTests(unittest.TestCase):
    def test_preview_runs_research_without_saving_or_sending(self):
        import research_intelligence as ri
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp)/"db.sqlite3"
            topic = ri.Topic("t", "Headline", "Summary", "science", "global")
            output = io.StringIO()
            with patch("research_intelligence.discover_topics", return_value=("preview-run", [topic], {"science": {"status":"ok","items":1}})), \
                 patch("research_cli.deliver_briefing") as send, redirect_stdout(output):
                self.assertTrue(research_cli.run_research(config=ScheduleConfig(categories=("science",)),
                    preview=True, db_path=db_path, lock_path=Path(tmp)/"lock"))
            self.assertIn("Preview only", output.getvalue())
            send.assert_not_called()
            db = pipeline_store.connect(db_path)
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='research_runs'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT status FROM research_job_runs").fetchone()[0], "preview")
            db.close()

    def test_diagnostic_mode_reports_provider_failures_without_discovery_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with patch("research_cli.diagnose_production_discovery", return_value={"rss":{"status":"failed"}}), \
                 redirect_stdout(output):
                self.assertTrue(research_cli.run_research(config=ScheduleConfig(categories=("science",)),
                    diagnostic=True, db_path=Path(tmp)/"db.sqlite3", lock_path=Path(tmp)/"lock"))
            self.assertIn('"failed"', output.getvalue())
            db = pipeline_store.connect(Path(tmp)/"db.sqlite3")
            self.assertEqual(db.execute("SELECT status FROM research_job_runs").fetchone()[0], "diagnostic")
            db.close()


if __name__ == "__main__": unittest.main()
