import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from command_engine import InputType, Intent, OutputFormat, process_command
import telegram_text_dry_run as telegram_adapter
from telegram_text_dry_run import _reply_to_update, build_reply, format_job
from news_research import Article


class FakeNewsProvider:
    def search(self, query):
        return [Article("https://ign.com/story", "IGN", "Studio announced an update")]
    def open(self, article):
        return Article(article.url, article.publisher, article.headline, None,
                       "2026-11-23T00:00:00+00:00", "Article page states an update was announced.", True)


class CommandEngineTests(unittest.TestCase):
    def test_news_discovery_extracts_topic(self):
        job = process_command("Fetch the latest news about GTA 6.")
        self.assertEqual(job.intent, Intent.NEWS_DISCOVERY)
        self.assertEqual(job.topic_or_claim, "GTA 6")
        self.assertIsNone(job.output_format)
        self.assertNotIn("Which output format do you want: Shorts, long, or both?", job.clarification_needed)
        self.assertEqual(job.status, "parsed")

    def test_news_discovery_understands_english_hinglish_and_hindi_variants(self):
        cases = (
            ("Fetch the latest news about GTA 6", "GTA 6"),
            ("GTA 6 ki latest news fetch karo aur mujhe batao kya naya update aaya hai", "GTA 6"),
            ("GTA 6 ke baare mein news batao", "GTA 6"),
            ("ताज़ा खबर बताओ GTA 6 के बारे में", "GTA 6"),
            ("लेटेस्ट न्यूज़ बताओ जीटिए सिक्स", "GTA 6"),
            ("जीटिए सिक्स की लेटिस नूज स्फेच करो और मुझे बताओ क्या नया अब्टेट आया है", "GTA 6"),
            ("Tell me the latest news about GTA 6", "GTA 6"),
            ("ताज़ा खबर बताओ", None),
        )
        for phrase, topic in cases:
            with self.subTest(phrase=phrase):
                job = process_command(phrase, input_type=InputType.TRANSCRIBED_VOICE)
                self.assertEqual(job.intent, Intent.NEWS_DISCOVERY)
                self.assertEqual(job.topic_or_claim, topic)
                self.assertIsNone(job.output_format)
                self.assertEqual(job.status, "parsed" if topic else "pending_clarification")
                self.assertNotIn("Which output format do you want: Shorts, long, or both?", job.clarification_needed)
                self.assertEqual(job.raw_input, phrase)

    def test_telegram_keeps_phonetic_transcript_visible_while_parsing(self):
        transcript = "जीटिए सिक्स की लेटिस नूज स्फेच करो और मुझे बताओ क्या नया अब्टेट आया है"
        reply = build_reply(transcript, input_type=InputType.TRANSCRIBED_VOICE, transcript=transcript,
                            news_provider=FakeNewsProvider())
        self.assertIn("Recognized transcript: " + transcript, reply)
        self.assertIn("News research: GTA 6", reply)

    def test_telegram_replies_once_per_update_id(self):
        update = {
            "update_id": 42,
            "message": {
                "text": "Fetch the latest news about GTA 6",
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
            },
        }
        replied_ids = set()
        with patch("telegram_text_dry_run._telegram_request") as request:
            _reply_to_update("token", update, "7", replied_ids, FakeNewsProvider())
            _reply_to_update("token", update, "7", replied_ids, FakeNewsProvider())
        self.assertEqual(request.call_count, 1)
        payload = request.call_args.args[2]
        self.assertIn("News research: GTA 6", payload["text"])
        self.assertNotIn("Which output format", payload["text"])

    def test_telegram_send_failure_is_not_suppressed_or_marked_replied(self):
        update = {
            "update_id": 43,
            "message": {
                "text": "Fetch news about GTA 6",
                "chat": {"id": 7, "type": "private"},
                "from": {"id": 7},
            },
        }
        replied_ids = set()
        with patch("telegram_text_dry_run._telegram_request", side_effect=RuntimeError("send failed")):
            with self.assertRaisesRegex(RuntimeError, "send failed"):
                _reply_to_update("token", update, "7", replied_ids)
        self.assertNotIn(43, replied_ids)

    def test_verification_is_not_news_discovery(self):
        job = process_command("Verify this news about GTA 6.")
        self.assertEqual(job.intent, Intent.NEWS_VERIFICATION)
        self.assertEqual(job.topic_or_claim, "GTA 6")

    def test_shorts_preserves_exact_requested_count_and_missing_url(self):
        job = process_command("Make 3 Shorts")
        self.assertEqual(job.output_format, OutputFormat.SHORTS)
        self.assertEqual(job.requested_shorts_count, 3)
        self.assertIsNone(job.source_url)
        self.assertTrue(any("URL" in item for item in job.clarification_needed))

    def test_shorts_missing_count_requires_clarification(self):
        job = process_command("Make Shorts from https://youtube.com/watch?v=abc")
        self.assertIsNone(job.requested_shorts_count)
        self.assertTrue(any("How many" in item for item in job.clarification_needed))

    def test_long_and_both_format_selection(self):
        long = process_command("Make a long video from https://youtu.be/abc")
        both = process_command("Make both formats")
        self.assertEqual(long.output_format, OutputFormat.LONG)
        self.assertEqual(both.output_format, OutputFormat.BOTH)
        self.assertFalse(any("format" in item for item in both.clarification_needed))
        self.assertTrue(any("URL" in item for item in both.clarification_needed))

    def test_direct_youtube_url_is_url_input_and_video_processing(self):
        job = process_command("https://youtu.be/abc")
        self.assertEqual(job.input_type, InputType.URL)
        self.assertEqual(job.intent, Intent.VIDEO_PROCESSING)
        self.assertEqual(job.source_url, "https://youtu.be/abc")
        self.assertIsNone(job.output_format)

    def test_claim_and_video_url_are_distinguished(self):
        claim = process_command("Verify this news about GTA 6")
        video = process_command("https://www.youtube.com/watch?v=abc")
        self.assertEqual(claim.intent, Intent.NEWS_VERIFICATION)
        self.assertEqual(video.intent, Intent.VIDEO_PROCESSING)

    def test_defaults_do_not_auto_select_both_or_hinglish(self):
        job = process_command("Make a video from https://youtu.be/abc")
        self.assertIsNone(job.output_format)
        self.assertEqual(job.caption_language, "original")
        self.assertIsNone(job.rights_confirmed)
        self.assertEqual(job.approval_state, "not_requested")

    def test_hinglish_must_be_explicit(self):
        job = process_command("Make a long video in Hinglish from https://youtu.be/abc")
        self.assertEqual(job.caption_language, "hinglish")

    def test_transcribed_voice_is_a_supported_adapter_input(self):
        job = process_command("Fetch news about GTA 6", input_type=InputType.TRANSCRIBED_VOICE)
        self.assertEqual(job.input_type, InputType.TRANSCRIBED_VOICE)

    def test_dry_run_formatter_only_describes_work(self):
        job = process_command("Make both formats")
        result = format_job(job)
        self.assertIn("Dry run only", result)
        self.assertIn("no search, download, render, or upload", result)
        self.assertEqual(job.status, "pending_clarification")

    def test_reply_building_makes_no_telegram_or_pipeline_calls(self):
        with patch("telegram_text_dry_run._telegram_request") as request:
            reply = build_reply("Make both formats")
        request.assert_not_called()
        self.assertIn("Output: both", reply)

    def test_voice_reply_transcribes_and_uses_shared_parser(self):
        update = {"update_id": 70, "message": {"voice": {"file_id": "file"},
            "chat": {"id": 7, "type": "private"}, "from": {"id": 7}}}
        with patch.object(telegram_adapter, "_download_voice") as download, \
             patch.object(telegram_adapter, "transcribe_voice", return_value="Fetch news about GTA 6") as transcribe, \
             patch.object(telegram_adapter, "_telegram_request") as request:
            download.return_value = Path("/controlled/temp/voice.oga")
            _reply_to_update("token", update, "7", set(), FakeNewsProvider())
        transcribe.assert_called_once()
        payload = request.call_args.args[2]
        self.assertIn("Recognized transcript: Fetch news about GTA 6", payload["text"])
        self.assertIn("News research: GTA 6", payload["text"])
        self.assertNotIn("Which output format", payload["text"])

    def test_polling_path_constructs_and_passes_multi_source_provider(self):
        provider = object()
        update = {"update_id": 80, "message": {"text": "Fetch news about GTA 6",
            "chat": {"id": 7, "type": "private"}, "from": {"id": 7}}}
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_ALLOWED_USER_ID": "7"}), \
             patch.object(telegram_adapter.argparse.ArgumentParser, "parse_args", return_value=SimpleNamespace(dry_run=True)), \
             patch.object(telegram_adapter, "MultiSourceNewsProvider", return_value=provider) as build_provider, \
             patch.object(telegram_adapter, "_telegram_request", side_effect=[[update], KeyboardInterrupt()]), \
             patch.object(telegram_adapter, "_reply_to_update", side_effect=KeyboardInterrupt()) as reply:
            self.assertEqual(telegram_adapter.run(), 0)
        build_provider.assert_called_once()
        self.assertIs(reply.call_args.args[4], provider)

    def test_text_and_voice_share_injected_research_provider(self):
        provider = FakeNewsProvider()
        text_reply = build_reply("Fetch news about GTA 6", news_provider=provider)
        with patch.object(telegram_adapter, "_download_voice", return_value=Path("voice.oga")), \
             patch.object(telegram_adapter, "transcribe_voice", return_value="Fetch news about GTA 6"):
            voice_reply = telegram_adapter._voice_reply("token", {"voice": {"file_id": "f"}}, provider)
        self.assertIn("News research: GTA 6", text_reply)
        self.assertIn("News research: GTA 6", voice_reply)

    def test_voice_transcription_handles_english_hindi_and_hinglish_via_parser(self):
        for phrase in ("Make 3 Shorts from https://youtu.be/abc",
                       "एक वीडियो बनाओ https://youtu.be/abc",
                       "Bhai make 3 Shorts from https://youtu.be/abc"):
            reply = build_reply(phrase, input_type=InputType.TRANSCRIBED_VOICE, transcript=phrase)
            self.assertIn("Input: transcribed_voice", reply)
            self.assertIn("Recognized transcript: " + phrase, reply)

    def test_empty_or_failed_transcription_prompts_repeat_without_guessing(self):
        message = {"voice": {"file_id": "file"}}
        with patch.object(telegram_adapter, "_download_voice", return_value=Path("voice.oga")), \
             patch.object(telegram_adapter, "transcribe_voice", return_value=""):
            self.assertIn("send text instead", telegram_adapter._voice_reply("token", message))
        with patch.object(telegram_adapter, "_download_voice", side_effect=RuntimeError("private")):
            self.assertIn("send text instead", telegram_adapter._voice_reply("token", message))

    def test_authorization_requires_private_chat_and_matching_allowlist(self):
        update = {"update_id": 71, "message": {"text": "hello", "chat": {"id": 8, "type": "private"},
                                                "from": {"id": 8}}}
        with patch("telegram_text_dry_run._telegram_request") as request:
            _reply_to_update("token", update, None, set())
            _reply_to_update("token", update, "7", set())
        request.assert_not_called()

    def test_voice_duplicate_update_is_replied_to_once(self):
        update = {"update_id": 72, "message": {"voice": {"file_id": "file"},
            "chat": {"id": 7, "type": "private"}, "from": {"id": 7}}}
        with patch.object(telegram_adapter, "_voice_reply", return_value="done") as voice_reply, \
             patch.object(telegram_adapter, "_telegram_request") as request:
            replied = set()
            _reply_to_update("token", update, "7", replied)
            _reply_to_update("token", update, "7", replied)
        self.assertEqual(voice_reply.call_count, 1)
        self.assertEqual(request.call_count, 1)

    def test_temporary_voice_directory_is_removed_after_transcription_success_or_failure(self):
        message = {"voice": {"file_id": "file"}}
        for failure in (False, True):
            seen = []
            def fake_download(token, file_id, directory):
                seen.append(Path(directory))
                path = Path(directory) / "audio.oga"
                path.write_bytes(b"voice")
                return path
            with patch.object(telegram_adapter, "_download_voice", side_effect=fake_download), \
                 patch.object(telegram_adapter, "transcribe_voice",
                              side_effect=RuntimeError("failed") if failure else "make shorts"):
                telegram_adapter._voice_reply("token", message)
            self.assertEqual(len(seen), 1)
            self.assertFalse(seen[0].exists())


if __name__ == "__main__":
    unittest.main()
