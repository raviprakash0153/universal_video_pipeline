import argparse
import sys
import tempfile
import types
import unittest
import unittest.mock
import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import video_pipeline as pipeline
import source_video_quality as quality


class InputValidationTests(unittest.TestCase):
    def test_transcribe_discards_invalid_timestamps_and_preserves_valid_boundaries(self):
        cases = [
            (None, 1), ("bad", 2), ("1:2", 2), (float("nan"), 2),
            (1, float("inf")), (-1, 2), (3, 2), (2, 2),
        ]

        class FakeModel:
            def transcribe(self, path, **options):
                entries = [types.SimpleNamespace(word=" original ", start=1.23456789, end=1.23456799)]
                entries.extend(types.SimpleNamespace(word=" unchanged ", start=start, end=end) for start, end in cases)
                entries.append(types.SimpleNamespace(word=" long ", start=40, end=80))
                entries.append(types.SimpleNamespace(word=" missing "))
                return ([types.SimpleNamespace(words=entries)], None)

        module = types.SimpleNamespace(WhisperModel=lambda *args, **kwargs: FakeModel())
        output = io.StringIO()
        with patch.dict(sys.modules, {"faster_whisper": module}), redirect_stdout(output):
            words = pipeline.transcribe(Path("synthetic.mp4"), "small")
        self.assertEqual(words, [(1.23456789, 1.23456799, "original"), (40.0, 80.0, "long")])
        self.assertIn("review alignment", output.getvalue())

    def test_transcribe_uses_reviewable_multilingual_defaults_and_preserves_tokens(self):
        class FakeModel:
            def transcribe(self, path, **options):
                self.options = options
                word = types.SimpleNamespace(word=" bhai!", start=1.0, end=1.1)
                return ([types.SimpleNamespace(words=[word])], None)

        fake = FakeModel()
        module = types.SimpleNamespace(WhisperModel=lambda *args, **kwargs: fake)
        with patch.dict(sys.modules, {"faster_whisper": module}), patch("builtins.print"):
            words = pipeline.transcribe(Path("synthetic.mp4"), "small")
        self.assertEqual(words, [(1.0, 1.1, "bhai!")])
        self.assertEqual(fake.options, {"word_timestamps": True, "vad_filter": True, "beam_size": 5})

    def test_transcribe_can_explicitly_select_hindi(self):
        class FakeModel:
            def transcribe(self, path, **options):
                self.options = options
                return ([], None)

        fake = FakeModel()
        module = types.SimpleNamespace(WhisperModel=lambda *args, **kwargs: fake)
        with patch.dict(sys.modules, {"faster_whisper": module}):
            pipeline.transcribe(Path("synthetic.mp4"), "base", language="hi")
        self.assertEqual(fake.options["language"], "hi")

    def test_transcribe_can_explicitly_select_english(self):
        class FakeModel:
            def transcribe(self, path, **options):
                self.options = options
                return ([], None)

        fake = FakeModel()
        module = types.SimpleNamespace(WhisperModel=lambda *args, **kwargs: fake)
        with patch.dict(sys.modules, {"faster_whisper": module}):
            pipeline.transcribe(Path("synthetic.mp4"), "small", language="en")
        self.assertEqual(fake.options["language"], "en")

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


class AutomaticClipRangeTests(unittest.TestCase):
    def test_requested_clip_counts_are_exact_and_distinct(self):
        for count in (1, 3, 5):
            with self.subTest(count=count):
                ranges = pipeline.automatic_clip_ranges(120, count, 35)
                self.assertEqual(len(ranges), count)
                self.assertEqual(len(set(ranges)), count)

    def test_source_shorter_than_clip_length_shortens_clips(self):
        ranges = pipeline.automatic_clip_ranges(8, 3, 35)
        self.assertEqual(len(ranges), 3)
        self.assertTrue(all(end - start < 35 for start, end in ranges))

    def test_eight_second_source_produces_three_distinct_valid_ranges(self):
        ranges = pipeline.automatic_clip_ranges(8, 3, 35)
        self.assertEqual(len(ranges), 3)
        self.assertEqual(len(set(ranges)), 3)
        for start, end in ranges:
            pipeline.validate_range(start, end, 8)

    def test_exact_count_for_short_sources_when_duration_permits(self):
        for duration, count in ((1, 1), (3.5, 3), (8, 8), (9, 5)):
            with self.subTest(duration=duration, count=count):
                ranges = pipeline.automatic_clip_ranges(duration, count, 35)
                self.assertEqual(len(ranges), count)
                self.assertEqual(len(set(ranges)), count)

    def test_source_too_short_for_requested_count_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "too short"):
            pipeline.automatic_clip_ranges(4.99, 5, 35)

    def test_ranges_are_nonzero_and_within_source(self):
        duration = 24.5
        ranges = pipeline.automatic_clip_ranges(duration, 5, 35)
        for start, end in ranges:
            self.assertGreater(end, start)
            self.assertGreaterEqual(start, 0)
            self.assertLessEqual(end, duration)

    def test_cli_rejects_short_source_before_rendering(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        source = Path(tmp.name) / "source.mp4"
        source.write_bytes(b"source")
        argv = ["video_pipeline.py", "shorts", "--source", str(source),
                "--no-subtitles", "--auto-clips", "5", "--rights-state", "owned"]
        with patch.object(sys, "argv", argv), patch.object(pipeline, "require_tool"), \
                patch.object(pipeline, "get_info", return_value={"title": "source", "duration": 4.5, "local": True}), \
                patch.object(pipeline, "acquire", return_value=source), \
                patch.object(pipeline, "render_short") as render, patch("builtins.print"):
            with self.assertRaisesRegex(SystemExit, "too short"):
                pipeline.main()
        render.assert_not_called()


class SourceVideoQualityTests(unittest.TestCase):
    def metadata(self, *heights):
        return {"formats": [
            *[{"format_id": str(h), "height": h, "vcodec": "avc1",
               "acodec": "none"} for h in heights],
            {"format_id": "audio", "height": None, "vcodec": "none", "acodec": "opus"},
        ]}

    def test_separate_video_and_audio_are_eligible(self):
        self.assertEqual(quality.validate_video_formats(self.metadata(720, 2160)), 2160)

    def test_combined_format_is_valid_fallback(self):
        data = {"formats": [{"format_id": "combined", "height": 1080,
                              "vcodec": "avc1", "acodec": "aac"}]}
        self.assertEqual(quality.validate_video_formats(data), 1080)

    def test_no_eligible_video_audio_pair_or_combined_format_is_rejected(self):
        data = {"formats": [
            {"height": 1080, "vcodec": "avc1", "acodec": "none"},
            {"height": 480, "vcodec": "avc1", "acodec": "aac"},
        ]}
        with self.assertRaisesRegex(ValueError, "no video format"):
            quality.validate_video_formats(data)

    def test_highest_eligible_resolution_is_preferred(self):
        self.assertEqual(quality.validate_video_formats(
            self.metadata(720, 1080, 1440, 2160, 4320, 8640)), 8640)

    def test_each_supported_resolution_is_accepted(self):
        for height in (720, 1080, 1440, 2160, 4320, 8640):
            with self.subTest(height=height):
                self.assertEqual(quality.validate_video_formats(self.metadata(height)), height)

    def test_720p_is_accepted_without_1080p(self):
        self.assertEqual(quality.validate_video_formats(self.metadata(720)), 720)

    def test_below_720p_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no video format"):
            quality.validate_video_formats(self.metadata(480, 360))

    def test_unknown_video_resolution_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no video format"):
            quality.validate_video_formats({"formats": [{"vcodec": "avc1", "height": None}]})

    def test_best_audio_and_minimum_height_are_in_selector(self):
        selector = quality.quality_format_selector()
        self.assertIn("+ba/", selector)
        self.assertIn("height>=720", selector)
        self.assertNotIn("height<=1080", selector)
        branches = selector.split("/")
        self.assertEqual(len(branches), 2)
        self.assertTrue(all("height>=720" in branch for branch in branches))
        self.assertEqual(branches, ["bv[height>=720]+ba", "b[height>=720]"])

    def test_shared_runtime_options_use_portable_node_discovery(self):
        self.assertEqual(quality.yt_dlp_runtime_options(), ["--js-runtimes", "node"])
        self.assertNotIn("/Users/", " ".join(quality.yt_dlp_runtime_options()))

    def test_video_pipeline_metadata_and_download_share_runtime_options(self):
        import json
        data = {"title": "source", "duration": 5,
                "formats": [{"height": 720, "vcodec": "avc1", "acodec": "aac"}]}
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            def fake_run(command, **kwargs):
                if "--dump-single-json" in command:
                    return types.SimpleNamespace(stdout=json.dumps(data))
                Path(str(command[command.index("-o") + 1]).replace("%(ext)s", "mp4")).write_bytes(b"video")
            with patch.object(pipeline.Path, "exists", return_value=False), \
                    patch.object(pipeline, "WORK", work), patch.object(pipeline, "run", side_effect=fake_run) as run, \
                    patch.object(pipeline, "probe_video", return_value={"width": 1280, "height": 720}):
                pipeline.get_info("https://example.invalid/video")
                pipeline.acquire("https://example.invalid/video", "source")
            yt_commands = [call.args[0] for call in run.call_args_list]
            runtime_args = quality.yt_dlp_runtime_options()
            self.assertEqual(len(yt_commands), 3)
            self.assertTrue(all(command[1:3] == runtime_args for command in yt_commands))

    def test_all_url_pipelines_share_strict_policy(self):
        import clip_pipeline
        import long_video_pipeline
        import process_raj_podcast
        self.assertEqual(clip_pipeline.YTDLP_FORMAT, quality.quality_format_selector())
        self.assertEqual(long_video_pipeline.YTDLP_FORMAT, quality.quality_format_selector())
        self.assertEqual(process_raj_podcast.FORMAT_SPEC, quality.quality_format_selector())
        self.assertEqual(pipeline.quality_format_selector(), quality.quality_format_selector())

    def test_shorts_preflight_and_download_use_identical_selector(self):
        import clip_pipeline
        import process_raj_podcast
        selector = quality.quality_format_selector()
        self.assertIn("--format", clip_pipeline.get_video_info.__code__.co_consts)
        self.assertIn("YTDLP_FORMAT", clip_pipeline.get_video_info.__code__.co_names)
        self.assertIn("--format", process_raj_podcast.process_podcast_clip.__code__.co_consts)
        self.assertIn("FORMAT_SPEC", process_raj_podcast.process_podcast_clip.__code__.co_names)
        self.assertEqual(clip_pipeline.YTDLP_FORMAT, selector)
        self.assertEqual(process_raj_podcast.FORMAT_SPEC, selector)

    def test_clip_pipeline_metadata_and_download_share_runtime_options(self):
        import json
        import clip_pipeline
        data = {"title": "source", "duration": 10, "uploader": "uploader", "channel": "channel",
                "formats": [{"height": 720, "vcodec": "avc1", "acodec": "aac"}]}
        payload = json.dumps(data)
        with patch.object(clip_pipeline.subprocess, "check_output", return_value=payload) as check_output:
            clip_pipeline.get_video_info("https://example.invalid/video")
        metadata_command = check_output.call_args.args[0]
        clip = {"id": 1, "title": "clip", "start": "00:00:01", "end": "00:00:02"}
        model = types.SimpleNamespace(transcribe=lambda *args, **kwargs: ([], None))
        with patch.object(clip_pipeline.subprocess, "check_output", return_value=payload) as check_output, \
                patch.object(clip_pipeline.subprocess, "run") as run, \
                patch.object(clip_pipeline, "generate_ass_subtitles"), \
                patch.object(clip_pipeline, "WORKSPACE_DIR", tempfile.gettempdir()), \
                patch("builtins.print"):
            clip_pipeline.process_clip("https://example.invalid/video", clip, model, "output.mp4")
        clip_metadata_command = check_output.call_args_list[0].args[0]
        runtime_args = quality.yt_dlp_runtime_options()
        yt_commands = [call.args[0] for call in run.call_args_list
                       if "yt_dlp" in call.args[0]]
        self.assertEqual(len(yt_commands), 1)
        download_command = yt_commands[0]
        for command in (metadata_command, clip_metadata_command, download_command):
            self.assertIn("yt_dlp", command)
            start = command.index(runtime_args[0])
            self.assertEqual(command[start:start + len(runtime_args)], runtime_args)


class OutputProtectionTests(unittest.TestCase):
    def test_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "final.mp4"
            path.write_bytes(b"keep me")
            with self.assertRaises(FileExistsError):
                pipeline.ensure_outputs_available([path])
            self.assertEqual(path.read_bytes(), b"keep me")

    def test_long_render_uses_no_overwrite_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.mp4"
            def fake_run(command, **kwargs):
                Path(command[-1]).write_bytes(b"encoded")
            with patch.object(pipeline, "run", side_effect=fake_run) as run, \
                    patch.object(pipeline, "verify"), patch.object(pipeline.os, "link"):
                pipeline.render_long(Path("source.mp4"), output, 0, None)
            self.assertEqual(run.call_args.args[0][:2], ["ffmpeg", "-y"])


class LongRenderTests(unittest.TestCase):
    def render_command(self, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "final.mp4"
            commands = []
            def fake_run(command, **run_kwargs):
                commands.append(command)
                Path(command[-1]).write_bytes(b"encoded")
            with patch.object(pipeline, "run", side_effect=fake_run), \
                    patch.object(pipeline, "verify"), patch.object(pipeline.os, "link"):
                pipeline.render_long(Path("source.mp4"), output, 0, None, **kwargs)
            command = commands[0]
            return command[command.index("-vf") + 1], command[command.index("-af") + 1]

    def test_default_filter_values_preserve_legacy_filters(self):
        vf, af = self.render_command()
        self.assertTrue(vf.endswith("eq=contrast=1.03:saturation=1.05"))
        self.assertEqual(af, "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0")

    def test_custom_video_and_audio_filter_values(self):
        vf, af = self.render_command(brightness=0.2, contrast=1.2, saturation=0.8,
                                     volume=1.4, bass_gain=3)
        self.assertTrue(vf.endswith("eq=brightness=0.2:contrast=1.2:saturation=0.8"))
        self.assertEqual(af, "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0,volume=1.4,bass=g=3")

    def test_each_custom_filter_control(self):
        expected = (("brightness", 0.1, "brightness=0.1"),
                    ("contrast", 1.2, "contrast=1.2"),
                    ("saturation", 1.2, "saturation=1.2"),
                    ("volume", 1.2, "volume=1.2"),
                    ("bass_gain", 2.0, "bass=g=2"))
        for name, value, fragment in expected:
            with self.subTest(name=name):
                vf, af = self.render_command(**{name: value})
                self.assertIn(fragment, vf if name in {"brightness", "contrast", "saturation"} else af)

    def test_numeric_arguments_reject_invalid_nonfinite_and_unreasonable_values(self):
        cases = (("brightness", "bad"), ("contrast", "nan"), ("saturation", "inf"),
                 ("volume", "-inf"), ("bass_gain", "100"), ("brightness", "2"))
        types = {"brightness": pipeline.bounded_number(-1, 1, "Brightness"),
                 "contrast": pipeline.bounded_number(0, 3, "Contrast"),
                 "saturation": pipeline.bounded_number(0, 3, "Saturation"),
                 "volume": pipeline.bounded_number(0, 10, "Volume"),
                 "bass_gain": pipeline.bounded_number(-20, 20, "Bass gain")}
        for name, value in cases:
            with self.subTest(name=name, value=value), self.assertRaises(argparse.ArgumentTypeError):
                types[name](value)

    def test_audio_less_mapping_remains_optional(self):
        vf, af = self.render_command()
        self.assertIsInstance(vf, str)
        # Audio map remains optional, so FFmpeg accepts a source with no audio stream.
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "final.mp4"
            def fake_run(command, **kwargs):
                self.assertIn("0:a?", command)
                Path(command[-1]).write_bytes(b"encoded")
            with patch.object(pipeline, "run", side_effect=fake_run), patch.object(pipeline, "verify"), \
                    patch.object(pipeline.os, "link"):
                pipeline.render_long(Path("silent.mp4"), output, 0, None)

    def test_ffmpeg_and_probe_failures_clean_temporary_file(self):
        for failure in ("ffmpeg", "probe"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "final.mp4"
                captured = []
                def fail(command, **kwargs):
                    temp = Path(command[-1])
                    captured.append(temp)
                    temp.write_bytes(b"partial")
                    raise RuntimeError("failure")
                with patch.object(pipeline, "run", side_effect=fail if failure == "ffmpeg" else lambda *a, **k: None), \
                        patch.object(pipeline, "verify", side_effect=RuntimeError("failure") if failure == "probe" else None):
                    if failure == "probe":
                        def write_then_verify(command, **kwargs):
                            Path(command[-1]).write_bytes(b"encoded")
                            captured.append(Path(command[-1]))
                        pipeline.run = unittest.mock.Mock(side_effect=write_then_verify)
                    with self.assertRaises(RuntimeError):
                        pipeline.render_long(Path("source.mp4"), output, 0, None)
                self.assertFalse(output.exists())
                self.assertFalse(captured[0].exists())

    def test_verify_rejects_missing_video_stream(self):
        with patch.object(pipeline, "run", return_value=types.SimpleNamespace(stdout='{"streams":[{"codec_type":"audio"}]}')):
            with self.assertRaisesRegex(ValueError, "video stream"):
                pipeline.verify(Path("bad.mp4"))

    def test_existing_output_preserved_and_success_published_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "final.mp4"
            output.write_bytes(b"keep")
            def encode(command, **kwargs):
                Path(command[-1]).write_bytes(b"new")
            with patch.object(pipeline, "run", side_effect=encode), patch.object(pipeline, "verify"), \
                    patch.object(pipeline.os, "link", side_effect=FileExistsError):
                with self.assertRaises(FileExistsError):
                    pipeline.render_long(Path("source.mp4"), output, 0, None)
            self.assertEqual(output.read_bytes(), b"keep")
            self.assertEqual(list(root.glob(".final_*.mp4")), [])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "final.mp4"
            def encode(command, **kwargs):
                Path(command[-1]).write_bytes(b"new")
            def link(src, dst):
                Path(dst).hardlink_to(src)
            with patch.object(pipeline, "run", side_effect=encode), patch.object(pipeline, "verify"), \
                    patch.object(pipeline.os, "link", side_effect=link):
                pipeline.render_long(Path("source.mp4"), output, 0, None)
            self.assertEqual(output.read_bytes(), b"new")
            self.assertEqual(list(root.glob(".final_*.mp4")), [])

    def test_banner_is_opt_in_and_added_after_long_form_scaling(self):
        plain, _ = self.render_command()
        self.assertNotIn("drawbox", plain)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "final.mp4"
            commands = []
            def encode(command, **kwargs):
                commands.append(command)
                Path(command[-1]).write_bytes(b"encoded")
            with patch.object(pipeline, "WORK", Path(directory)), \
                    patch.object(pipeline, "run", side_effect=encode), patch.object(pipeline, "verify"), \
                    patch.object(pipeline.os, "link"):
                pipeline.render_long(Path("source.mp4"), output, 0, None, banner=True,
                                     title="A title", channel="A channel")
            vf = commands[0][commands[0].index("-vf") + 1]
            self.assertIn("drawbox", vf)
            self.assertLess(vf.index("scale="), vf.index("drawbox"))
            self.assertEqual(list(Path(directory).glob("banner_*")), [])


class TemporaryCleanupTests(unittest.TestCase):
    def test_short_banner_renders_and_is_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            output = work / "clip.mp4"
            commands = []
            def encode(command, **kwargs):
                commands.append(command)
                Path(command[-1]).write_bytes(b"encoded")
            with patch.object(pipeline, "WORK", work), patch.object(pipeline, "run", side_effect=encode), \
                    patch.object(pipeline, "verify"):
                pipeline.render_short(Path("source.mp4"), output, 0, 2, None)
            short_filter = commands[-1][commands[-1].index("-filter_complex") + 1]
            self.assertNotIn("drawbox", short_filter)
            commands.clear()
            with patch.object(pipeline, "WORK", work), patch.object(pipeline, "run", side_effect=encode), \
                    patch.object(pipeline, "verify"):
                pipeline.render_short(Path("source.mp4"), output, 0, 2, None, banner=True,
                                      title="title", channel="channel")
            short_filter = commands[-1][commands[-1].index("-filter_complex") + 1]
            self.assertIn("drawbox", short_filter)
            self.assertEqual(list(work.glob("banner_*")), [])

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

    def test_short_banner_text_files_are_cleaned_after_ffmpeg_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            def fail(command, **kwargs):
                if "-filter_complex" in command:
                    self.assertTrue(list(work.glob("banner_*/banner_title.txt")))
                    self.assertIn("drawbox", command[command.index("-filter_complex") + 1])
                    raise RuntimeError("simulated FFmpeg failure")
                Path(command[-1]).write_bytes(b"trim")
            with patch.object(pipeline, "WORK", work), patch.object(pipeline, "run", side_effect=fail):
                with self.assertRaisesRegex(RuntimeError, "simulated"):
                    pipeline.render_short(Path("source.mp4"), Path("clip.mp4"), 0, 2, None,
                                          banner=True, title="x", channel="y")
            self.assertEqual(list(work.glob("banner_*")), [])

    def test_long_banner_text_files_are_cleaned_after_ffmpeg_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def fail(command, **kwargs):
                self.assertTrue(list(root.glob("banner_*/banner_title.txt")))
                raise RuntimeError("simulated FFmpeg failure")
            with patch.object(pipeline, "WORK", root), patch.object(pipeline, "run", side_effect=fail):
                with self.assertRaisesRegex(RuntimeError, "simulated"):
                    pipeline.render_long(Path("source.mp4"), root / "final.mp4", 0, None,
                                         banner=True, title="x", channel="y")
            self.assertEqual(list(root.glob("banner_*")), [])


class TitleBannerTests(unittest.TestCase):
    def test_special_text_stays_in_text_files_not_filter_string(self):
        with tempfile.TemporaryDirectory(prefix="filter,quote'") as directory:
            root = Path(directory)
            filters = pipeline.banner_filter(1920, 1080, "A:'quoted', [title]; %", "Canal\\name", root)
            self.assertNotIn("A:'quoted'", filters)
            self.assertNotIn("Canal\\name", filters)
            self.assertEqual((root / "banner_title.txt").read_text(), "A:'quoted', [title]; %")
            self.assertEqual((root / "banner_channel.txt").read_text(), "Canal\\name")
            self.assertIn("drawbox", filters)
            self.assertIn("expansion=none", filters)

    def test_source_metadata_retains_uploader_and_falls_back_safely(self):
        data = {"title": "Title", "uploader": "Uploader", "channel": "Channel", "duration": 3,
                "formats": [{"height": 720, "vcodec": "avc1", "acodec": "aac"}]}
        with patch.object(pipeline.Path, "exists", return_value=False), \
                patch.object(pipeline, "run", return_value=types.SimpleNamespace(stdout=__import__("json").dumps(data))):
            self.assertEqual(pipeline.get_info("https://example.invalid/video"), {
                "title": "Title", "uploader": "Uploader", "duration": 3.0, "local": False
            })
        data = {"title": None, "uploader": None, "channel": "Channel fallback", "duration": 3,
                "formats": [{"height": 720, "vcodec": "avc1", "acodec": "aac"}]}
        with patch.object(pipeline.Path, "exists", return_value=False), \
                patch.object(pipeline, "run", return_value=types.SimpleNamespace(stdout=__import__("json").dumps(data))):
            info = pipeline.get_info("https://example.invalid/video")
        self.assertEqual(info["title"], "video")
        self.assertEqual(info["uploader"], "Channel fallback")


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
                    "--tag", "podcast", "--tag", "education", "--rights-state", "owned",
                    "--approval-file", "approval.json"]
            with patch.object(sys, "argv", args), patch.object(pipeline, "OUTPUT", output), \
                    patch.object(pipeline, "require_tool"), \
                    patch.object(pipeline, "get_info", return_value={"title": "source", "duration": 30, "local": True}), \
                    patch.object(pipeline, "acquire", return_value=Path("source.mp4")), \
                    patch.object(pipeline, "render_long", side_effect=lambda source, path, start, end, *filters: path.write_bytes(b"video")), \
                    patch.object(pipeline, "upload_youtube") as upload, \
                    patch.object(pipeline, "validate_approval"):
                with patch("builtins.print"):
                    pipeline.main()
            self.assertEqual(upload.call_args.kwargs["category_id"], "27")
            self.assertEqual(upload.call_args.kwargs["tags"], ["podcast", "education"])


class BothModeTests(unittest.TestCase):
    def run_cli(self, mode, extra=(), short_error=False, long_error=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        outputs = root / "output"
        outputs.mkdir()
        source = root / "source.mp4"
        source.write_bytes(b"source")
        calls = {"short": [], "long": []}
        def short(source_path, output, *args):
            calls["short"].append(source_path)
            if short_error:
                raise RuntimeError("short render broke")
            output.write_bytes(b"short")
        def long(source_path, output, *args):
            calls["long"].append(source_path)
            if long_error:
                raise RuntimeError("long render broke")
            output.write_bytes(b"long")
        if "--upload" in extra:
            extra = [*extra, "--rights-state", "owned", "--approval-file", "approval-a.json",
                     "--approval-file", "approval-b.json"]
        argv = ["video_pipeline.py", mode, "--source", str(source), "--no-subtitles",
                "--rights-state", "owned", *extra]
        with patch.object(sys, "argv", argv), patch.object(pipeline, "OUTPUT", outputs), \
                patch.object(pipeline, "require_tool"), \
                patch.object(pipeline, "get_info", return_value={"title": "source", "duration": 60, "local": True}), \
                patch.object(pipeline, "acquire", return_value=source) as acquire, \
                patch.object(pipeline, "render_short", side_effect=short) as rs, \
                patch.object(pipeline, "render_long", side_effect=long) as rl, \
                patch.object(pipeline, "upload_youtube") as upload, \
                patch.object(pipeline, "validate_approval"), patch("builtins.print"):
            try:
                pipeline.main()
            except BaseException as exc:
                return root, calls, acquire, rs, rl, upload, exc
        return root, calls, acquire, rs, rl, upload, None

    def test_both_acquires_once_reuses_source_and_generates_both_outputs(self):
        root, calls, acquire, _, _, _, error = self.run_cli(
            "both", ["--short-clip", "1-5", "--long-start", "10", "--long-end", "30"])
        self.assertIsNone(error)
        acquire.assert_called_once()
        self.assertEqual(calls["short"], [root / "source.mp4"])
        self.assertEqual(calls["long"], [root / "source.mp4"])
        self.assertTrue((root / "output/source_short_01.mp4").exists())
        self.assertTrue((root / "output/source_16x9.mp4").exists())

    def test_both_uploads_both_outputs_when_enabled_and_none_when_disabled(self):
        *_, upload_off, error_off = self.run_cli("both", ["--short-clip", "1-5"])
        self.assertIsNone(error_off)
        upload_off.assert_not_called()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        client = Path(tmp.name) / "client.json"
        client.write_text("placeholder")
        *_, upload_on, error_on = self.run_cli(
            "both", ["--short-clip", "1-5", "--upload", "--client-secrets", str(client)])
        self.assertIsNone(error_on)
        self.assertEqual(upload_on.call_count, 2)

    def test_invalid_ranges_rejected_before_rendering(self):
        _, _, acquire, short, long, _, error = self.run_cli(
            "both", ["--short-clip", "1-5", "--long-start", "40", "--long-end", "70"])
        self.assertIsInstance(error, SystemExit)
        acquire.assert_not_called()
        short.assert_not_called()
        long.assert_not_called()

    def test_invalid_shorts_range_does_not_acquire_source(self):
        _, _, acquire, short, _, _, error = self.run_cli("shorts", ["--clip", "55-70"])
        self.assertIsInstance(error, SystemExit)
        acquire.assert_not_called()
        short.assert_not_called()

    def test_valid_url_job_acquires_source_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outputs = root / "output"
            outputs.mkdir()
            downloaded = root / "downloaded.mp4"
            argv = ["video_pipeline.py", "shorts", "--source", "https://example.invalid/video",
                    "--clip", "1-5", "--no-subtitles", "--rights-state", "owned"]
            with patch.object(sys, "argv", argv), patch.object(pipeline, "OUTPUT", outputs), \
                    patch.object(pipeline, "require_tool"), \
                    patch.object(pipeline, "get_info", return_value={"title": "source", "duration": 60, "local": False}), \
                    patch.object(pipeline, "acquire", return_value=downloaded) as acquire, \
                    patch.object(pipeline, "render_short", side_effect=lambda source, path, *args: path.write_bytes(b"short")), \
                    patch("builtins.print"):
                pipeline.main()
            acquire.assert_called_once_with("https://example.invalid/video", "source", metadata_validated=True)

    def test_failure_names_output_cleans_partial_and_skips_upload(self):
        root, _, _, _, _, upload, error = self.run_cli(
            "both", ["--short-clip", "1-5", "--upload"], long_error=True)
        self.assertRegex(str(error), "Long-form output failed")
        upload.assert_not_called()
        self.assertFalse((root / "output/source_16x9.mp4").exists())

    def test_shorts_failure_names_output_cleans_partial_and_skips_upload(self):
        root, _, _, _, _, upload, error = self.run_cli(
            "both", ["--short-clip", "1-5", "--upload"], short_error=True)
        self.assertRegex(str(error), "Shorts output failed")
        upload.assert_not_called()
        self.assertFalse((root / "output/source_short_01.mp4").exists())

    def test_existing_modes_still_accept_their_original_range_options(self):
        *_, shorts_error = self.run_cli("shorts", ["--clip", "1-5"])
        self.assertIsNone(shorts_error)
        *_, long_error = self.run_cli("long", ["--start", "1", "--end", "5"])
        self.assertIsNone(long_error)


class ReusablePipelineTests(unittest.TestCase):
    def run_process(self, mode, **options):
        options.setdefault("rights_state", "owned")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        output = root / "output"
        output.mkdir()
        source = root / "source.mp4"
        source.write_bytes(b"source")

        def render(_source, path, *args):
            path.write_bytes(b"video")

        with patch.object(pipeline, "OUTPUT", output), patch.object(pipeline, "require_tool"), \
                patch.object(pipeline, "get_info", return_value={"title": "source", "duration": 60, "local": True}), \
                patch.object(pipeline, "acquire", return_value=source), \
                patch.object(pipeline, "render_short", side_effect=render) as short, \
                patch.object(pipeline, "render_long", side_effect=render) as long, \
                patch.object(pipeline, "upload_youtube", return_value={
                    "video_id": "mock-id", "url": "https://youtu.be/mock-id"}) as upload, \
                patch.object(pipeline, "validate_approval"), \
                patch("builtins.print"):
            result = pipeline.process_video(mode=mode, source=str(source), no_subtitles=True, **options)
        return output, result, short, long, upload

    def test_modes_generate_exact_requested_outputs_and_structured_paths(self):
        output, shorts, short_render, long_render, _ = self.run_process(
            "shorts", auto_clips=3, clip_length=10)
        self.assertEqual(len(shorts.output_paths), 3)
        self.assertEqual(short_render.call_count, 3)
        long_render.assert_not_called()
        self.assertTrue(all(path.parent == output for path in shorts.output_paths))
        self.assertEqual(shorts.outputs, shorts.output_paths)

        _, long_result, short_render, long_render, _ = self.run_process("long")
        self.assertEqual(len(long_result.output_paths), 1)
        short_render.assert_not_called()
        long_render.assert_called_once()

        _, both, short_render, long_render, _ = self.run_process(
            "both", auto_clips=2, clip_length=10, short_clip=[])
        self.assertEqual(len(both.output_paths), 3)
        self.assertEqual(short_render.call_count, 2)
        long_render.assert_called_once()
        self.assertEqual(both.youtube_video_ids, [])
        self.assertEqual(both.youtube_urls, [])

    def test_progress_callback_and_upload_metadata_are_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            client = Path(directory) / "client.json"
            client.write_text("mock client")
            events = []
            output, result, _, _, upload = self.run_process(
                "shorts", auto_clips=1, upload=True, client_secrets=client,
                rights_state="owned", approval_files=["approval.json"],
                progress_callback=events.append)
        self.assertEqual(result.youtube_video_ids, ["mock-id"])
        self.assertEqual(result.youtube_urls, ["https://youtu.be/mock-id"])
        upload.assert_called_once()
        self.assertEqual([event["event"] for event in events],
                         ["rendering", "completed", "uploading", "uploaded"])
        self.assertEqual(result.output_paths, [output / "source_short_01.mp4"])

    def test_main_delegates_parsed_cli_arguments_to_reusable_function(self):
        argv = ["video_pipeline.py", "shorts", "--source", "source.mp4", "--clip", "1-5",
                "--no-subtitles", "--banner", "--volume", "1.5"]
        expected = pipeline.VideoPipelineResult()
        with patch.object(sys, "argv", argv), patch.object(pipeline, "process_video", return_value=expected) as process:
            self.assertIs(pipeline.main(), expected)
        self.assertEqual(process.call_args.kwargs["mode"], "shorts")
        self.assertEqual(process.call_args.kwargs["clip"], ["1-5"])
        self.assertTrue(process.call_args.kwargs["banner"])
        self.assertEqual(process.call_args.kwargs["volume"], 1.5)


if __name__ == "__main__":
    unittest.main()
