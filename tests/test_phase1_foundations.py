import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import clip_pipeline
import long_video_pipeline
import video_pipeline
from pipeline_store import connect, create_job, create_platform_output
from platform_foundation import PLATFORMS, PlatformOutput, build_platform_packages
from safety_foundations import probe_video, validate_approval, write_approval


class PhaseOneTests(unittest.TestCase):
    def test_actual_probe_accepts_720_1080_and_portrait_rejects_480(self):
        def response(width, height):
            return type("Probe", (), {"stdout": json.dumps({"streams": [{"width": width, "height": height}]})})()
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "source.mp4"
            file.touch()
            with patch("safety_foundations.subprocess.run", side_effect=[
                response(1280, 720), response(1920, 1080), response(720, 1280), response(854, 480)
            ]):
                self.assertEqual(probe_video(file)["height"], 720)
                self.assertEqual(probe_video(file)["height"], 1080)
                self.assertEqual(probe_video(file)["orientation"], "portrait")
                with self.assertRaisesRegex(ValueError, "below the 720p minimum"):
                    probe_video(file)

    def test_approval_is_bound_to_metadata_and_exact_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            media, approval = Path(tmp) / "clip.mp4", Path(tmp) / "approval.json"
            media.write_bytes(b"clip")
            metadata = {"media_file": str(media), "platform": "youtube", "title": "Title", "privacy": "private"}
            write_approval(approval, media, metadata, approver="owner", approved_at="2026-11-23T00:00:00Z", approved=True)
            validate_approval(approval, metadata)
            with self.assertRaises(PermissionError):
                validate_approval(approval, {**metadata, "title": "Changed"})
            media.write_bytes(b"changed")
            with self.assertRaises(PermissionError):
                validate_approval(approval, metadata)

    def test_registry_and_platform_output_records_cover_all_requested_platforms(self):
        self.assertEqual(len(PLATFORMS), 15)
        self.assertNotIn("tiktok", PLATFORMS)
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "clip.mp4"
            file.write_bytes(b"clip")
            record = PlatformOutput.for_media("youtube", file, {"title": "x"})
            self.assertEqual(len(record.sha256), 64)

    def test_sqlite_tracks_jobs_capabilities_and_per_platform_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "pipeline.sqlite3")
            create_job(db, "job-1", "source.mp4", rights_state="owned")
            create_platform_output(db, "out-yt", "job-1", "youtube", metadata={"title": "A"})
            create_platform_output(db, "out-x", "job-1", "x", metadata={"title": "A"})
            self.assertEqual(db.execute("SELECT count(*) FROM platform_outputs").fetchone()[0], 2)
            capability = json.loads(db.execute("SELECT details_json FROM platform_capabilities WHERE platform='x'").fetchone()[0])
            self.assertEqual(capability["official_api"], "unknown")
            db.close()

    def test_platform_package_errors_are_independent(self):
        def builder(platform):
            if platform == "x":
                raise RuntimeError("adapter failed")
            return {"platform": platform}
        records = build_platform_packages(["youtube", "x", "reddit"], builder)
        self.assertEqual([records[p]["status"] for p in ("youtube", "x", "reddit")], ["ready", "failed", "ready"])

    def test_legacy_public_and_scheduled_upload_entrypoints_are_blocked(self):
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            long_video_pipeline.process_long_video("https://example.invalid/video")
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            clip_pipeline.run_scheduled_upload()
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            clip_pipeline.generate_shorts_for_url("https://example.invalid/video", upload_now=True)

    def test_unknown_rights_stop_processing_before_source_resolution(self):
        with patch.object(video_pipeline, "require_tool"), \
                patch.object(video_pipeline, "get_info") as get_info:
            with self.assertRaises(PermissionError):
                video_pipeline.process_video(mode="long", source="source.mp4")
        get_info.assert_not_called()

    def test_public_upload_requires_a_matching_individual_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp4"
            source.write_bytes(b"source")
            with patch.object(video_pipeline, "require_tool"), \
                    patch.object(video_pipeline, "get_info", return_value={"title": "clip", "duration": 8, "local": True}), \
                    patch.object(video_pipeline, "acquire", return_value=source), \
                    patch.object(video_pipeline, "OUTPUT", Path(tmp)), \
                    patch.object(video_pipeline, "render_long", side_effect=lambda _s, p, *_a: p.write_bytes(b"out")), \
                    patch.object(video_pipeline, "upload_youtube") as upload, patch("builtins.print"):
                with self.assertRaisesRegex(SystemExit, "individual approval"):
                    video_pipeline.process_video(mode="long", source=str(source), upload=True,
                                                 rights_state="owned", privacy="public")
            upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
