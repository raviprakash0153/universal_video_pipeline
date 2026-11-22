import argparse
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
