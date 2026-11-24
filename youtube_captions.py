"""Read-only YouTube caption probe. This command never downloads media."""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import tempfile
from pathlib import Path

from youtube_inspect import validate_youtube_url
from source_video_quality import yt_dlp_runtime_options


_STAMP = re.compile(r"^(?:(\d{2,}):)?(\d{2}):(\d{2})[.,](\d{3})$")


def _timestamp(value: str) -> float:
    match = _STAMP.fullmatch(value)
    if not match:
        raise ValueError(f"Malformed VTT timestamp: {value!r}")
    hours, minutes, seconds, millis = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def parse_vtt(source: str) -> list[dict]:
    """Parse VTT cues while preserving each cue's timing and text."""
    if not source.lstrip("\ufeff").startswith("WEBVTT"):
        raise ValueError("Caption file is not a WebVTT document")
    cues = []
    for block in re.split(r"\n\s*\n", source.replace("\r\n", "\n").replace("\r", "\n")):
        lines = block.strip().splitlines()
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        sides = lines[timing_index].split("-->")
        if len(sides) != 2:
            raise ValueError("Malformed VTT cue timing")
        start = _timestamp(sides[0].strip())
        end_text = sides[1].strip().split()[0]
        end = _timestamp(end_text)
        text = "\n".join(lines[timing_index + 1:]).strip()
        text = html.unescape(re.sub(r"<[^>]*>", "", text))
        if end <= start or not text:
            raise ValueError("VTT cue must have positive duration and nonempty text")
        cues.append({"start": start, "end": end, "text": text})
    if not cues:
        raise ValueError("VTT contains no caption cues")
    return cues


def _run(command, runner):
    try:
        return runner(command, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, subprocess.CalledProcessError) as exc:
        details = str(getattr(exc, "stderr", "") or exc).lower()
        if any(s in details for s in ("name or service not known", "temporary failure in name resolution", "nodename nor servname", "getaddrinfo")):
            raise RuntimeError("Network/DNS failure; YouTube captions were not inspected or retrieved.") from exc
        if any(s in details for s in ("private video", "sign in", "members-only", "login required", "age-restricted")):
            raise RuntimeError("Video is private or restricted; captions were not retrieved.") from exc
        if any(s in details for s in ("video unavailable", "has been removed", "does not exist")):
            raise RuntimeError("Video is unavailable; captions were not retrieved.") from exc
        raise RuntimeError("yt-dlp caption operation failed; success is not claimed.") from exc


def inspect_captions(url: str, language: str = "en", output_dir: str | Path | None = None,
                     runner=subprocess.run) -> dict:
    validate_youtube_url(url)
    if not re.fullmatch(r"[A-Za-z0-9_-]+(?:-[A-Za-z0-9_-]+)*", language):
        raise ValueError("Language must be a language code such as en or hi")
    base = ["yt-dlp", *yt_dlp_runtime_options(), "--no-warnings", "--no-progress", "--skip-download"]
    info = _run([*base, "--dump-single-json", url], runner)
    try:
        metadata = json.loads(info.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("Track listing failed: yt-dlp returned malformed metadata.") from exc
    manual = metadata.get("subtitles") or {}
    automatic = metadata.get("automatic_captions") or {}
    matches = lambda tracks: [lang for lang, rows in tracks.items()
                              if (lang == language or lang.split("-")[0] == language.split("-")[0]) and rows]
    manual_langs, auto_langs = matches(manual), matches(automatic)
    report = {"track_listing_successful": True, "human_tracks": sorted(manual),
              "automatic_tracks": sorted(automatic), "captions_available": bool(manual or automatic),
              "selected_language": None, "retrieval_successful": False,
              "timestamps_converted": False, "analyzer_suitable": False,
              "caption_segments": [], "words": [], "output_file": None}
    choices = [(lang, False) for lang in manual_langs] + [(lang, True) for lang in auto_langs]
    if not choices:
        report["message"] = f"No human or automatic caption track matches language {language!r}."
        return report
    lang, is_auto = choices[0]
    report["selected_language"] = lang
    parent = Path(output_dir) if output_dir else Path(tempfile.gettempdir())
    parent.mkdir(parents=True, exist_ok=True)
    destination = Path(tempfile.mkdtemp(prefix="youtube-captions-", dir=parent))
    report["output_directory"] = str(destination.resolve())
    pattern = str(destination / "%(id)s.%(language)s.%(ext)s")
    options = ["--write-auto-subs"] if is_auto else ["--write-subs"]
    _run([*base, *options, "--sub-langs", lang, "--sub-format", "vtt", "--output", pattern, url], runner)
    files = sorted(destination.glob("*.vtt"))
    if len(files) != 1:
        raise RuntimeError("Caption retrieval failed: yt-dlp did not produce exactly one VTT file.")
    report["retrieval_successful"] = True
    report["output_file"] = str(files[0].resolve())
    try:
        segments = parse_vtt(files[0].read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        report["message"] = f"Caption retrieval succeeded, but VTT parsing failed: {exc}"
        return report
    report["caption_segments"] = segments
    # A one-token cue provides a genuine segment-level word timestamp. Multiword
    # cues have no per-word alignment and are deliberately not interpolated.
    if all(len(cue["text"].split()) == 1 for cue in segments):
        report["words"] = [{"start": c["start"], "end": c["end"], "text": c["text"]} for c in segments]
        report["timestamps_converted"] = True
        report["analyzer_suitable"] = True
        report["message"] = "Every cue contains one timed token; analyzer input was converted from cue timings."
    else:
        report["message"] = "Caption cues were preserved; multiword cue timing cannot be converted reliably to word-level analyzer input."
    return report


def format_report(report: dict) -> str:
    lines = [f"Metadata/track listing: {'successful' if report['track_listing_successful'] else 'failed'}",
             f"Captions available: {'yes' if report['captions_available'] else 'no'}",
             f"Caption retrieval: {'successful' if report['retrieval_successful'] else 'not performed or failed'}",
             f"Timestamp conversion: {'successful' if report['timestamps_converted'] else 'not successful'}",
             f"Analyzer transcript: {'suitable' if report['analyzer_suitable'] else 'unsuitable'}"]
    for label, key in (("Human tracks", "human_tracks"), ("Automatic tracks", "automatic_tracks")):
        lines.append(f"{label}: {', '.join(report[key]) or 'none'}")
    if report.get("output_file"):
        lines.append(f"Caption file: {report['output_file']}")
    if report.get("output_directory"):
        lines.append(f"Local output directory: {report['output_directory']}")
    lines.append(report.get("message", ""))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Inspect/retrieve YouTube captions only; never downloads video or audio.")
    parser.add_argument("url")
    parser.add_argument("--language", default="en", help="Preferred caption language (default: en)")
    parser.add_argument("--output-dir", help="Parent directory for a new, unique caption output directory")
    args = parser.parse_args(argv)
    try:
        report = inspect_captions(args.url, args.language, args.output_dir)
        print(format_report(report))
        return 0
    except (ValueError, RuntimeError) as exc:
        parser.exit(2, f"Caption inspection failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
