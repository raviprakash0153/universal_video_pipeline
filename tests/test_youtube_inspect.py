import json
import subprocess
import unittest
from unittest.mock import Mock

from youtube_inspect import format_report, inspect_url


URL = "https://www.youtube.com/watch?v=OIeLbmyraRQ"


def metadata(formats=None):
    return {"title": "Sample", "duration": 125, "formats": formats or [
        {"height": 1080, "vcodec": "avc1", "acodec": "none", "ext": "mp4"},
        {"height": None, "vcodec": "none", "acodec": "opus", "ext": "webm"},
    ]}


class YoutubeInspectTests(unittest.TestCase):
    def test_success_is_metadata_only_and_reports_quality(self):
        runner = Mock(return_value=Mock(stdout=json.dumps(metadata()), stderr=""))
        result = inspect_url(URL, runner=runner)
        command = runner.call_args.args[0]
        self.assertIn("--dump-single-json", command)
        self.assertIn("--no-download", command)
        self.assertNotIn("--download-sections", command)
        self.assertTrue(result["has_720p_video"])
        self.assertEqual(result["best_eligible"], 1080)
        self.assertTrue(result["eligible"])
        self.assertIn("Metadata inspection: successful", format_report(result))
        self.assertIn("Source eligibility: eligible", format_report(result))

    def test_inspection_can_succeed_when_source_is_ineligible(self):
        runner = Mock(return_value=Mock(stdout=json.dumps(metadata([
            {"height": 480, "vcodec": "avc1", "acodec": "aac", "ext": "mp4"}
        ])), stderr=""))
        result = inspect_url(URL, runner=runner)
        self.assertFalse(result["eligible"])
        self.assertFalse(result["has_720p_video"])
        self.assertIn("Metadata inspection: successful", format_report(result))
        self.assertIn("Source eligibility: not eligible", format_report(result))

    def test_malformed_url_is_rejected_before_network(self):
        runner = Mock()
        with self.assertRaisesRegex(ValueError, "Malformed YouTube URL"):
            inspect_url("https://example.com/watch?v=OIeLbmyraRQ", runner=runner)
        runner.assert_not_called()

    def test_dns_and_restricted_errors_are_clear(self):
        for stderr, message in [("Temporary failure in name resolution", "Network/DNS error"),
                                ("This is a private video", "private or restricted")]:
            error = subprocess.CalledProcessError(1, "yt-dlp", stderr=stderr)
            with self.subTest(stderr=stderr), self.assertRaisesRegex(RuntimeError, message):
                inspect_url(URL, runner=Mock(side_effect=error))


if __name__ == "__main__":
    unittest.main()
