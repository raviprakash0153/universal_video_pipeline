import tempfile
import unittest
import contextlib
import io
from pathlib import Path
from unittest.mock import patch

import approval_cli
from pipeline_store import connect, create_job, create_platform_output, register_asset, set_rights


class ApprovalPreviewCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "pipeline.sqlite"
        self.client_file = self.root / "client.json"
        self.client_file.write_text("{}")
        self.db = connect(self.db_path)
        self.media = self.root / "render.mp4"
        self.media.write_bytes(b"valid fixture video bytes")
        create_job(self.db, "job", "source.mp4", rights_state="licensed")
        asset = register_asset(self.db, "job", self.media, width=1280, height=720)
        create_platform_output(self.db, "out", "job", "youtube", asset_id=asset,
                               metadata={"title": "Local title", "privacy": "private"})

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def run_cli(self, *args):
        approval_cli.main(["--database", str(self.db_path), *args])

    def assert_cli_error(self, message):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            self.run_cli("preview", "out")
        self.assertEqual(raised.exception.code, 2)
        self.assertIn(message, stderr.getvalue())

    def test_preview_opens_actual_registered_file_and_displays_review_metadata(self):
        with patch.object(approval_cli, "_media_details", return_value={
                "format": "mov,mp4", "dimensions": "1280x720", "duration_seconds": 4.5,
                "subtitles": 1}) as probe, patch.object(approval_cli, "_open_video") as open_video, \
                patch("builtins.print") as output:
            self.run_cli("preview", "out")
        probe.assert_called_once_with(self.media.resolve())
        open_video.assert_called_once_with(self.media.resolve())
        printed = "\n".join(call.args[0] for call in output.call_args_list)
        for value in ("1280x720", "4.5", "licensed", "private", "media_sha256"):
            self.assertIn(value, printed)
        self.assertEqual(self.db.execute("select count(*) from output_previews").fetchone()[0], 1)

    def test_invalid_and_missing_outputs_do_not_record_preview(self):
        with patch.object(approval_cli, "_media_details", side_effect=ValueError("invalid video")), \
                patch.object(approval_cli, "_open_video") as open_video:
            self.assert_cli_error("invalid video")
        open_video.assert_not_called()
        self.assertEqual(self.db.execute("select count(*) from output_previews").fetchone()[0], 0)
        self.media.unlink()
        self.assert_cli_error("missing")

    def test_launch_success_records_only_accurate_status_and_never_uploads(self):
        with patch.object(approval_cli, "_media_details", return_value={"format": "mp4"}), \
                patch.object(approval_cli, "_open_video") as player, patch("builtins.print") as output:
            self.run_cli("preview", "out")
        player.assert_called_once_with(self.media.resolve())
        text = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn("player launched successfully", text)
        self.assertIn("playback or viewing was verified", text)
        self.assertNotIn("watched", text)
        self.assertIn("No media was uploaded", text)
        self.assertEqual(self.db.execute("select count(*) from output_previews").fetchone()[0], 1)

    def test_player_failure_and_file_change_do_not_record_preview(self):
        with patch.object(approval_cli, "_media_details", return_value={"format": "mp4"}), \
                patch.object(approval_cli, "_open_video", side_effect=OSError("player unavailable")):
            self.assert_cli_error("player unavailable")
        self.assertEqual(self.db.execute("select count(*) from output_previews").fetchone()[0], 0)
        with patch.object(approval_cli, "_media_details", return_value={"format": "mp4"}), \
                patch.object(approval_cli, "_open_video", side_effect=lambda _path: self.media.write_bytes(b"changed")):
            self.assert_cli_error("changed while launching")
        self.assertEqual(self.db.execute("select count(*) from output_previews").fetchone()[0], 0)

    def test_missing_null_empty_and_invalid_registered_paths_are_clear(self):
        self.db.execute("UPDATE platform_outputs SET asset_id=NULL WHERE output_id='out'")
        self.db.commit()
        self.assert_cli_error("missing or invalid")
        self.db.execute("UPDATE platform_outputs SET asset_id=? WHERE output_id='out'", (self.db.execute("SELECT asset_id FROM media_assets LIMIT 1").fetchone()[0],))
        self.db.commit()
        cases = [("", "missing or invalid"),
                 (str(self.root / "absent.mp4"), "missing"), (str(self.root), "not a regular file")]
        for path, message in cases:
            with self.subTest(path=path):
                self.db.execute("UPDATE media_assets SET path=?", (path,))
                self.db.commit()
                with patch.object(approval_cli, "_media_details") as probe, patch.object(approval_cli, "_open_video") as player:
                    self.assert_cli_error(message)
                probe.assert_not_called()
                player.assert_not_called()
                self.assertEqual(self.db.execute("select count(*) from output_previews").fetchone()[0], 0)
        self.db.execute("UPDATE media_assets SET path=?", (str(self.media),))
        self.db.commit()

    def test_media_or_metadata_changes_after_approval_invalidate_it(self):
        from pipeline_store import record_output_preview, decide_approval, validate_db_approval
        record_output_preview(self.db, "out")
        decide_approval(self.db, "out", True, "reviewer")
        self.db.execute("UPDATE platform_outputs SET metadata_json='{}' WHERE output_id='out'")
        self.db.commit()
        with self.assertRaisesRegex(PermissionError, "metadata changed"):
            validate_db_approval(self.db, "out")

    def test_explicit_approval_cli_requires_preview_and_rejection_is_recorded(self):
        with patch("builtins.print"):
            with self.assertRaisesRegex(PermissionError, "Preview this exact"):
                self.run_cli("approve", "out", "--by", "reviewer")
            with patch.object(approval_cli, "_media_details", return_value={"format": "mp4"}), \
                    patch.object(approval_cli, "_open_video"):
                self.run_cli("preview", "out")
            self.run_cli("reject", "out", "--by", "reviewer")
        self.assertEqual(self.db.execute("select approved from approval_history").fetchone()[0], 0)

    def approve_output(self, output_id="out"):
        from pipeline_store import record_output_preview, decide_approval
        record_output_preview(self.db, output_id)
        decide_approval(self.db, output_id, True, "reviewer")

    def test_publish_exact_output_without_production_and_records_success(self):
        self.approve_output()
        with patch("video_pipeline.process_video", side_effect=AssertionError("production invoked")) as production, \
                patch("video_pipeline.upload_youtube", return_value={"video_id": "mock-id", "url": "https://youtu.be/mock-id"}) as upload, \
                patch("builtins.print"):
            self.run_cli("publish", "out", "--client-secrets", str(self.client_file))
        production.assert_not_called()
        self.assertEqual(upload.call_args.args[0], self.media.resolve())
        self.assertEqual(self.db.execute("select publishing_status from platform_outputs where output_id='out'").fetchone()[0], "succeeded")
        attempt = self.db.execute("select output_id,status,platform_post_id from publishing_attempts").fetchone()
        self.assertEqual(tuple(attempt), ("out", "succeeded", "mock-id"))

    def test_publish_rejects_missing_rejected_missing_preview_changed_media_and_metadata(self):
        from pipeline_store import record_output_preview, decide_approval
        with patch("video_pipeline.upload_youtube") as upload:
            with self.assertRaises(SystemExit): self.run_cli("publish", "out", "--client-secrets", str(self.client_file))
            upload.assert_not_called()
        self.approve_output()
        with patch("video_pipeline.upload_youtube") as upload:
            self.run_cli("reject", "out", "--by", "reviewer")
            with self.assertRaises(SystemExit): self.run_cli("publish", "out", "--client-secrets", str(self.client_file))
            upload.assert_not_called()
        # Freshly approve, then independently mutate each bound input.
        record_output_preview(self.db, "out"); decide_approval(self.db, "out", True, "reviewer")
        self.media.write_bytes(b"changed")
        with self.assertRaises(SystemExit): self.run_cli("publish", "out", "--client-secrets", str(self.client_file))
        self.media.write_bytes(b"valid fixture video bytes")
        self.db.execute("UPDATE media_assets SET sha256=?", (__import__('hashlib').sha256(self.media.read_bytes()).hexdigest(),))
        self.db.execute("UPDATE platform_outputs SET approval_state='approved' WHERE output_id='out'")
        self.db.commit()
        self.db.execute("UPDATE platform_outputs SET metadata_json='{}' WHERE output_id='out'"); self.db.commit()
        with self.assertRaises(SystemExit): self.run_cli("publish", "out", "--client-secrets", str(self.client_file))

    def test_publish_requires_current_rights_preview_and_not_already_published(self):
        from pipeline_store import record_output_preview, decide_approval
        # An approval without a preview is impossible through the public API;
        # remove preview evidence after approval to exercise publish-time validation.
        self.approve_output()
        self.db.execute("DELETE FROM output_previews WHERE output_id='out'"); self.db.commit()
        with patch("video_pipeline.upload_youtube") as upload:
            with self.assertRaises(SystemExit): self.run_cli("publish", "out", "--client-secrets", str(self.client_file))
            upload.assert_not_called()
        self.approve_output()
        self.db.execute("UPDATE content_jobs SET rights_state='unknown' WHERE job_id='job'"); self.db.commit()
        with self.assertRaises(SystemExit): self.run_cli("publish", "out", "--client-secrets", str(self.client_file))
        self.db.execute("UPDATE content_jobs SET rights_state='licensed' WHERE job_id='job'"); self.db.commit()
        self.approve_output()
        self.db.execute("UPDATE platform_outputs SET publishing_status='succeeded' WHERE output_id='out'"); self.db.commit()
        with patch("video_pipeline.upload_youtube") as upload:
            with self.assertRaises(SystemExit): self.run_cli("publish", "out", "--client-secrets", str(self.client_file))
            upload.assert_not_called()

    def test_publish_rejects_changed_metadata_hash(self):
        from pipeline_store import create_platform_output, record_output_preview, decide_approval
        asset = self.db.execute("select asset_id from media_assets limit 1").fetchone()[0]
        create_platform_output(self.db, "metadata-output", "job", "youtube", asset_id=asset,
                               metadata={"title": "Approved title", "privacy": "private"})
        record_output_preview(self.db, "metadata-output")
        decide_approval(self.db, "metadata-output", True, "reviewer")
        self.db.execute("UPDATE platform_outputs SET metadata_json=? WHERE output_id='metadata-output'",
                        ('{"title":"Changed title","privacy":"private"}',)); self.db.commit()
        with patch("video_pipeline.upload_youtube") as upload:
            with self.assertRaises(SystemExit): self.run_cli("publish", "metadata-output", "--client-secrets", str(self.client_file))
            upload.assert_not_called()
        self.assertEqual(self.db.execute("select approval_state from platform_outputs where output_id='metadata-output'").fetchone()[0], "invalidated")

    def test_upload_failure_is_uncertain_and_cannot_be_blindly_retried(self):
        self.approve_output()
        with patch("video_pipeline.upload_youtube", side_effect=ConnectionError("lost response")) as upload:
            with self.assertRaises(SystemExit): self.run_cli("publish", "out", "--client-secrets", str(self.client_file))
            self.assertEqual(self.db.execute("select publishing_status from platform_outputs where output_id='out'").fetchone()[0], "uncertain")
            self.assertEqual(self.db.execute("select status from publishing_attempts").fetchone()[0], "uncertain")
            with self.assertRaises(SystemExit): self.run_cli("publish", "out", "--client-secrets", str(self.client_file))
            self.assertEqual(upload.call_count, 1)

    def test_outputs_have_independent_approval_and_publish_state(self):
        from pipeline_store import create_platform_output, register_asset
        second_media = self.root / "second.mp4"; second_media.write_bytes(b"second output")
        second_asset = register_asset(self.db, "job", second_media)
        create_platform_output(self.db, "out-2", "job", "youtube", asset_id=second_asset,
                               metadata={"title": "Second", "privacy": "private"})
        self.approve_output("out")
        from pipeline_store import record_output_preview, decide_approval
        record_output_preview(self.db, "out-2"); decide_approval(self.db, "out-2", True, "reviewer")
        with patch("video_pipeline.upload_youtube", return_value={"video_id": "one", "url": "https://youtu.be/one"}) as upload, patch("builtins.print"):
            self.run_cli("publish", "out", "--client-secrets", str(self.client_file))
        upload.assert_called_once()
        states = dict(self.db.execute("select output_id,publishing_status from platform_outputs"))
        self.assertEqual(states, {"out": "succeeded", "out-2": "not_started"})
        self.assertEqual(self.db.execute("select approval_state from platform_outputs where output_id='out-2'").fetchone()[0], "approved")


if __name__ == "__main__":
    unittest.main()
