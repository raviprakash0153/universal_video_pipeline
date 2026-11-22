import argparse
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch

import video_pipeline as pipeline


class InputValidationTests(unittest.TestCase):
    def test_parse_time_rejects_non_finite_values(self):
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                pipeline.parse_time(value)

    def test_range_requires_positive_order_and_source_bounds(self):
        for start, end, duration in ((2, 2, 10), (-1, 3, 10), (1, 11, 10)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                pipeline.validate_range(start, end, duration)

    def test_valid_range(self):
        pipeline.validate_range(0, 3.5, 10)


class OutputProtectionTests(unittest.TestCase):
    def test_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "final.mp4"
            path.write_bytes(b"keep me")
            with self.assertRaises(FileExistsError):
                pipeline.ensure_outputs_available([path])
            self.assertEqual(path.read_bytes(), b"keep me")

    def test_long_render_uses_no_overwrite_flag(self):
        with patch.object(pipeline, "run") as run:
            with patch.object(pipeline, "verify"):
                pipeline.render_long(Path("source.mp4"), Path("out.mp4"), 0, None)
        self.assertEqual(run.call_args.args[0][:2], ["ffmpeg", "-n"])


class TemporaryCleanupTests(unittest.TestCase):
    def test_short_render_cleans_intermediates_after_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            temp = work / "clip_trim.mp4"
            ass = work / "clip.ass"

            def fail_after_creating_intermediate(command, **kwargs):
                if str(temp) in command:
                    temp.write_text("temporary")
                    ass.write_text("temporary")
                    raise RuntimeError("simulated FFmpeg failure")

            with patch.object(pipeline, "WORK", work), patch.object(pipeline, "run", side_effect=fail_after_creating_intermediate):
                with self.assertRaisesRegex(RuntimeError, "simulated"):
                    pipeline.render_short(Path("source.mp4"), Path("clip.mp4"), 0, 2, None)
            self.assertFalse(temp.exists())
            self.assertFalse(ass.exists())


class YouTubeMetadataTests(unittest.TestCase):
    def _upload_with_modules(self, *upload_args, **upload_kwargs):
        class Request:
            def next_chunk(self):
                return None, {"id": "video-id"}

        insert = unittest.mock.Mock(return_value=Request())
        videos = types.SimpleNamespace(insert=insert)
        youtube = types.SimpleNamespace(videos=lambda: videos)
        build = unittest.mock.Mock(return_value=youtube)
        credentials = types.SimpleNamespace(valid=True)
        credentials_type = types.SimpleNamespace(from_authorized_user_file=unittest.mock.Mock(return_value=credentials))
        modules = {
            "googleapiclient": types.ModuleType("googleapiclient"),
            "googleapiclient.discovery": types.SimpleNamespace(build=build),
            "googleapiclient.http": types.SimpleNamespace(MediaFileUpload=lambda *a, **k: object()),
            "google_auth_oauthlib": types.ModuleType("google_auth_oauthlib"),
            "google_auth_oauthlib.flow": types.SimpleNamespace(InstalledAppFlow=object),
            "google": types.ModuleType("google"),
            "google.auth": types.ModuleType("google.auth"),
            "google.auth.transport": types.ModuleType("google.auth.transport"),
            "google.auth.transport.requests": types.SimpleNamespace(Request=object),
            "google.oauth2": types.ModuleType("google.oauth2"),
            "google.oauth2.credentials": types.SimpleNamespace(Credentials=credentials_type),
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(pipeline, "ROOT", Path(directory)), patch.dict(sys.modules, modules):
                (Path(directory) / "youtube_token.json").write_text("unused")
                with patch("builtins.print"):
                    pipeline.upload_youtube(Path("video.mp4"), "title", "description", "private",
                                            Path("client.json"), *upload_args, **upload_kwargs)
            body = insert.call_args.kwargs["body"]
        return body

    def test_default_category_id_and_existing_metadata(self):
        body = self._upload_with_modules()
        self.assertEqual(body["snippet"], {
            "title": "title", "description": "description", "categoryId": "22"
        })
        self.assertEqual(body["status"], {
            "privacyStatus": "private", "selfDeclaredMadeForKids": False
        })

    def test_custom_category_and_multiple_tags(self):
        body = self._upload_with_modules(tags=["podcast", "motivation", "education"], category_id="27")
        self.assertEqual(body["snippet"]["categoryId"], "27")
        self.assertEqual(body["snippet"]["tags"], ["podcast", "motivation", "education"])

    def test_empty_tags_are_omitted(self):
        body = self._upload_with_modules(tags=[])
        self.assertNotIn("tags", body["snippet"])

    def test_cli_passes_category_and_repeated_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = root / "client.json"
            client.write_text("placeholder")
            output = root / "output"
            output.mkdir()
            args = ["video_pipeline.py", "long", "--source", "source.mp4", "--upload",
                    "--client-secrets", str(client), "--category-id", "27",
                    "--tag", "podcast", "--tag", "education"]
            with patch.object(sys, "argv", args), patch.object(pipeline, "OUTPUT", output), \
                    patch.object(pipeline, "require_tool"), \
                    patch.object(pipeline, "get_info", return_value={"title": "source", "duration": 30, "local": True}), \
                    patch.object(pipeline, "acquire", return_value=Path("source.mp4")), \
                    patch.object(pipeline, "render_long", side_effect=lambda source, path, start, end: path.write_bytes(b"video")), \
                    patch.object(pipeline, "upload_youtube") as upload:
                with patch("builtins.print"):
                    pipeline.main()
            self.assertEqual(upload.call_args.kwargs["category_id"], "27")
            self.assertEqual(upload.call_args.kwargs["tags"], ["podcast", "education"])


if __name__ == "__main__":
    unittest.main()
