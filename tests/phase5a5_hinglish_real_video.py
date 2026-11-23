#!/usr/bin/env python3
"""Phase 5A.5 real-video Hinglish integration check (manual, networked)."""
from __future__ import annotations

import json
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

# This script is run as a file, so Python puts tests/ rather than the repository
# root on sys.path. Add the root before importing project modules.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hinglish import caption_words, to_hinglish
from source_video_quality import quality_format_selector, validate_video_formats, yt_dlp_runtime_options


SOURCE = "https://www.youtube.com/watch?v=4Vz6L8B73i4"
START_SECONDS = 300
END_SECONDS = 390
EXPECTED_DURATION = END_SECONDS - START_SECONDS


def command(args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    print("+", " ".join(str(arg) for arg in args), flush=True)
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def probe(path: Path) -> dict:
    result = command(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                     capture=True)
    return json.loads(result.stdout)


def import_pipeline_from_scratch(scratch: Path):
    """Import the unchanged pipeline source with ROOT anchored in scratch."""
    repository = PROJECT_ROOT
    source = repository / "video_pipeline.py"
    scratch_source = scratch / "video_pipeline.py"
    shutil.copyfile(source, scratch_source)
    spec = importlib.util.spec_from_file_location("phase5a5_video_pipeline", scratch_source)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load pipeline source from {scratch_source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    if not module.OUTPUT.is_relative_to(scratch) or not module.WORK.is_relative_to(scratch):
        raise RuntimeError(f"Pipeline paths escaped scratch: OUTPUT={module.OUTPUT}, WORK={module.WORK}")
    return module


def main() -> None:
    for tool in ("yt-dlp", "ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"Required program '{tool}' was not found in PATH.")

    # Keep review artifacts outside the repository and production output tree.
    retained = Path(tempfile.mkdtemp(prefix="phase5a5-hinglish-", dir="/tmp")).resolve()
    scratch = retained
    report_path = retained / "phase5a5_report.json"
    preview_path = retained / "hinglish_preview.mp4"
    section_path = scratch / "source_section.%(ext)s"
    downloaded_path = scratch / "source_section.mp4"

    validation: dict[str, object] = {
        "source_quality_minimum_height": 720,
        "source_quality_validated": False,
        "requested_section_seconds": [START_SECONDS, END_SECONDS],
        "download_section_only": False,
        "transcription_model": "small",
        "transcription_language": "automatic",
        "same_transcript_for_original_and_hinglish": False,
        "timestamps_unchanged": False,
        "word_order_unchanged": False,
        "original_transcript_text_unchanged": False,
        "preview_duration_seconds": None,
        "preview_resolution": None,
        "checks_passed": False,
    }
    report: dict[str, object] = {
        "source": SOURCE,
        "retained_artifacts_directory": str(retained),
        "validation": validation,
        "original_transcript_words": [],
        "hinglish_caption_words": [],
    }

    try:
        pipeline = import_pipeline_from_scratch(scratch)
        metadata_result = command(["yt-dlp", *yt_dlp_runtime_options(), "--no-warnings", "--no-playlist",
                                   "--format", quality_format_selector(), "--dump-single-json", "--no-download", SOURCE],
                                  capture=True)
        metadata = json.loads(metadata_result.stdout)
        max_height = validate_video_formats(metadata)
        validation["source_quality_validated"] = max_height >= 720
        validation["highest_eligible_source_height"] = max_height
        if max_height < 720:
            raise AssertionError("Source did not meet the existing 720p minimum quality policy.")

        # --download-sections limits acquisition to this time range; no full source download path exists here.
        command(["yt-dlp", *yt_dlp_runtime_options(), "--no-warnings", "--no-playlist",
                 "--format", quality_format_selector(), "--download-sections",
                 f"*{START_SECONDS}-{END_SECONDS}", "--force-keyframes-at-cuts", "--merge-output-format", "mp4",
                 "--output", str(section_path), SOURCE])
        validation["download_section_only"] = True
        matches = list(scratch.glob("source_section.*"))
        if len(matches) != 1:
            raise RuntimeError(f"Expected one section download, found {len(matches)}: {matches}")
        downloaded_path = matches[0]

        source_probe = probe(downloaded_path)
        source_video = next((s for s in source_probe.get("streams", []) if s.get("codec_type") == "video"), None)
        if source_video is None or int(source_video.get("height", 0)) < 720:
            raise AssertionError("Downloaded section video is below 720p.")
        validation["downloaded_section_height"] = int(source_video["height"])

        # language=None deliberately leaves Faster-Whisper language detection automatic.
        original = pipeline.transcribe(downloaded_path, "small", language=None)
        hinglish = caption_words(original, "hinglish")
        original_caption = caption_words(original, "original")
        original_pairs = [(start, end) for start, end, _ in original]
        validation["same_transcript_for_original_and_hinglish"] = original_caption == original
        validation["timestamps_unchanged"] = original_pairs == [(start, end) for start, end, _ in hinglish]
        validation["word_order_unchanged"] = (
            len(original) == len(hinglish)
            and [to_hinglish(text) for _, _, text in original] == [text for _, _, text in hinglish]
        )
        validation["original_transcript_text_unchanged"] = [word for _, _, word in original] == [
            word for _, _, word in original_caption]
        assert validation["same_transcript_for_original_and_hinglish"]
        assert validation["timestamps_unchanged"]
        assert validation["word_order_unchanged"]
        assert validation["original_transcript_text_unchanged"]

        width, height = int(source_video["width"]), int(source_video["height"])
        ass_path = scratch / "hinglish_karaoke.ass"
        pipeline.write_ass(original, ass_path, width, height, max(28, height // 18), height - max(90, height // 12),
                           "hinglish")
        pipeline.run(["ffmpeg", "-y", "-i", str(downloaded_path), "-vf",
                      f"ass='{pipeline.escape_filter_path(ass_path)}'", "-c:v", "libx264", "-preset", "veryfast",
                      "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                      "-movflags", "+faststart", str(preview_path)])

        output_probe = probe(preview_path)
        output_video = next((s for s in output_probe.get("streams", []) if s.get("codec_type") == "video"), None)
        if output_video is None:
            raise AssertionError("Preview has no video stream.")
        duration = float(output_probe["format"]["duration"])
        output_resolution = [int(output_video["width"]), int(output_video["height"])]
        validation["preview_duration_seconds"] = duration
        validation["preview_resolution"] = output_resolution
        validation["preview_duration_valid"] = abs(duration - EXPECTED_DURATION) <= 1.0
        validation["preview_resolution_valid"] = output_resolution == [width, height]
        assert validation["preview_duration_valid"], f"Expected ~{EXPECTED_DURATION}s, got {duration}s"
        assert validation["preview_resolution_valid"], f"Expected {[width, height]}, got {output_resolution}"

        report["original_transcript_words"] = [
            {"start": start, "end": end, "text": text} for start, end, text in original]
        report["hinglish_caption_words"] = [
            {"start": start, "end": end, "text": text} for start, end, text in hinglish]
        validation["checks_passed"] = True
    except BaseException as exc:
        report["failure"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
        if preview_path.exists():
            preview_path.unlink()
        raise
    finally:
        # Preserve only the report and successful preview; remove media and import-created paths.
        for path in scratch.iterdir():
            if path not in (report_path, preview_path):
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Report: {report_path}")
    print(f"Preview: {preview_path}")


if __name__ == "__main__":
    main()
