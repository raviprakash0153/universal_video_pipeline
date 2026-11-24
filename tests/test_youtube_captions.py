import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from youtube_captions import inspect_captions, parse_vtt

URL = "https://www.youtube.com/watch?v=OIeLbmyraRQ"
VTT = "WEBVTT\n\n00:00:01.000 --> 00:00:01.400\nWow\n\n00:00:02.000 --> 00:00:02.500\nClutch\n"


class YoutubeCaptionTests(unittest.TestCase):
    def test_track_listing_retrieval_and_analyzer_compatibility(self):
        with tempfile.TemporaryDirectory() as temp:
            def runner(command, **kwargs):
                if "--dump-single-json" in command:
                    return Mock(stdout=json.dumps({"subtitles": {"en": [{"ext": "vtt"}]}, "automatic_captions": {"hi": [{}]}}), stderr="")
                self.assertIn("--skip-download", command)
                self.assertIn("--write-subs", command)
                self.assertNotIn("--write-auto-subs", command)
                self.assertNotIn("--format", command)
                Path(command[command.index("--output") + 1].replace("%(id)s", "abc").replace("%(language)s", "en").replace("%(ext)s", "vtt")).write_text(VTT)
                return Mock(stdout="", stderr="")
            result = inspect_captions(URL, output_dir=temp, runner=runner)
            self.assertTrue(result["track_listing_successful"])
            self.assertTrue(result["retrieval_successful"])
            self.assertTrue(result["timestamps_converted"])
            self.assertTrue(result["analyzer_suitable"])
            self.assertEqual(result["words"][0], {"start": 1.0, "end": 1.4, "text": "Wow"})
            self.assertTrue(Path(result["output_file"]).exists())

    def test_missing_caption_language_does_not_retrieve(self):
        runner = Mock(return_value=Mock(stdout=json.dumps({"subtitles": {"fr": [{}]}, "automatic_captions": {}}), stderr=""))
        result = inspect_captions(URL, runner=runner)
        self.assertFalse(result["captions_available"] if not result["human_tracks"] else False)
        self.assertFalse(result["retrieval_successful"])
        self.assertIn("No human", result["message"])
        runner.assert_called_once()

    def test_network_failure_is_not_success(self):
        error = subprocess.CalledProcessError(1, "yt-dlp", stderr="Temporary failure in name resolution")
        with self.assertRaisesRegex(RuntimeError, "Network/DNS"):
            inspect_captions(URL, runner=Mock(side_effect=error))

    def test_malformed_vtt_rejected(self):
        with self.assertRaisesRegex(ValueError, "not a WebVTT"):
            parse_vtt("not vtt")
        with self.assertRaisesRegex(ValueError, "timestamp"):
            parse_vtt("WEBVTT\n\n00:xx --> 00:00:01.000\nhello")

    def test_vtt_timestamp_formats_and_text(self):
        cues = parse_vtt("WEBVTT\n\n01:02.500 --> 00:01:04.000 align:start\n<b>Hello &amp; bye</b>")
        self.assertEqual(cues, [{"start": 62.5, "end": 64.0, "text": "Hello & bye"}])

    def test_multiword_cues_are_not_falsely_word_aligned(self):
        with tempfile.TemporaryDirectory() as temp:
            def runner(command, **kwargs):
                if "--dump-single-json" in command:
                    return Mock(stdout=json.dumps({"subtitles": {"en": [{}]}}), stderr="")
                path = command[command.index("--output") + 1].replace("%(id)s", "abc").replace("%(language)s", "en").replace("%(ext)s", "vtt")
                Path(path).write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello there friend\n")
                return Mock(stdout="", stderr="")
            result = inspect_captions(URL, output_dir=temp, runner=runner)
            self.assertTrue(result["retrieval_successful"])
            self.assertFalse(result["timestamps_converted"])
            self.assertFalse(result["analyzer_suitable"])
            self.assertEqual(result["caption_segments"][0]["text"], "Hello there friend")


if __name__ == "__main__":
    unittest.main()
