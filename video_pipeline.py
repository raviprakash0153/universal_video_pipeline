#!/usr/bin/env python3
"""
Universal Video Pipeline
- Cross-platform Python CLI for YouTube/local video sources
- Shorts (9:16) and long-form (16:9)
- Optional Faster-Whisper word-level karaoke subtitles
- Optional YouTube upload through OAuth
- No Antigravity, Docker, Node path assumptions, or external repository required
"""
from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import math
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from source_video_quality import quality_format_selector, validate_video_formats, yt_dlp_runtime_options
from timestamp_validation import valid_word_timestamps
from safety_foundations import resolve_local_input, probe_video, enforce_reuse_rights, validate_approval
from pipeline_store import (connect as connect_lifecycle, create_job as lifecycle_create_job,
                            record_provenance, register_asset, create_platform_output,
                            set_job_status, decide_approval, validate_db_approval,
                            record_validation)

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
WORK = ROOT / "work"
TITLE_BANNER_FONT = Path("/System/Library/Fonts/Geneva.ttf")
OUTPUT.mkdir(exist_ok=True)
WORK.mkdir(exist_ok=True)


@dataclass
class VideoPipelineResult:
    """Files and optional YouTube metadata produced by one pipeline run."""
    output_paths: list[Path] = field(default_factory=list)
    youtube_video_ids: list[str] = field(default_factory=list)
    youtube_urls: list[str] = field(default_factory=list)
    output_ids: list[str] = field(default_factory=list)

    @property
    def outputs(self) -> list[Path]:
        """Compatibility-friendly short name for generated output paths."""
        return self.output_paths

def run(cmd: list[str], *, capture=False):
    print("+", " ".join(str(x) for x in cmd))
    return subprocess.run(cmd, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)

def require_tool(name: str):
    if not shutil.which(name):
        raise SystemExit(f"Required program '{name}' was not found in PATH. Install FFmpeg (ffmpeg + ffprobe) and retry.")

def safe_name(value: str) -> str:
    value = re.sub(r"[^\w.-]+", "_", value.strip(), flags=re.UNICODE)
    return value[:70] or "video"

def get_info(source: str) -> dict:
    if Path(source).exists():
        resolved = resolve_local_input(source)
        p = Path(resolved["path"])
        probe = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "json", str(p)], capture=True)
        duration = float(json.loads(probe.stdout)["format"]["duration"])
        return {"title": p.stem, "uploader": None, "duration": duration, "local": True,
                "width": resolved["width"], "height": resolved["height"],
                "orientation": resolved["orientation"]}
    result = run(["yt-dlp", *yt_dlp_runtime_options(), "--no-warnings", "--format", quality_format_selector(),
                  "--dump-single-json", "--no-download", source], capture=True)
    data = json.loads(result.stdout)
    validate_video_formats(data)
    return {"title": data.get("title") or "video",
            "uploader": data.get("uploader") or data.get("channel") or None,
            "duration": float(data.get("duration") or 0), "local": False}

def acquire(source: str, title: str, *, metadata_validated: bool = False) -> Path:
    if Path(source).exists():
        return Path(source).resolve()
    target = WORK / f"{safe_name(title)}.%(ext)s"
    if not metadata_validated:
        metadata = run(["yt-dlp", *yt_dlp_runtime_options(), "--no-warnings", "--format", quality_format_selector(),
                        "--dump-single-json", "--no-download", source], capture=True)
        validate_video_formats(json.loads(metadata.stdout))
    run(["yt-dlp", *yt_dlp_runtime_options(), "--no-warnings", "--no-playlist",
         "-f", quality_format_selector(),
         "--merge-output-format", "mp4", "-o", str(target), source])
    matches = sorted(WORK.glob(f"{safe_name(title)}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        raise RuntimeError("yt-dlp finished but the downloaded file was not found.")
    resolved = matches[0]
    # Metadata preflight can be stale or differ from the merged download. Probe the
    # actual bytes before they enter any edit/render stage.
    probe_video(resolved)
    return resolved

def parse_time(value: str) -> float:
    bits = value.split(":")
    if not 1 <= len(bits) <= 3:
        raise argparse.ArgumentTypeError("Use seconds, MM:SS, or HH:MM:SS.")
    try:
        nums = [float(x) for x in bits]
    except ValueError:
        raise argparse.ArgumentTypeError("Invalid timestamp.")
    total = 0.0
    for n in nums:
        total = total * 60 + n
    if not math.isfinite(total) or total < 0:
        raise argparse.ArgumentTypeError("Timestamp must be finite and cannot be negative.")
    return total

def validate_range(start: float, end: float, duration: float | None = None):
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        raise ValueError("Clip range must use finite, non-negative times with end after start.")
    if duration is not None and end > duration:
        raise ValueError("Clip end is after source duration.")

def automatic_clip_ranges(duration: float, count: int, clip_length: float) -> list[tuple[float, float]]:
    """Plan distinct, evenly distributed clip windows for Shorts."""
    if count <= 0 or clip_length <= 0:
        raise ValueError("--auto-clips and --clip-length must be positive.")
    # A one second minimum gives a practical lower bound for a valid generated clip.
    if not math.isfinite(duration) or duration < count:
        raise ValueError(
            f"Source duration ({duration:g}s) is too short for {count} clips; "
            "at least one second per clip is required."
        )
    width = min(float(clip_length), duration / count)
    if count == 1:
        starts = [(duration - width) / 2]
    else:
        # Place window centers evenly from the beginning to the end of the source.
        centers = [duration * i / (count - 1) for i in range(count)]
        starts = [min(max(center - width / 2, 0), duration - width) for center in centers]
    ranges = [(start, start + width) for start in starts]
    for start, end in ranges:
        validate_range(start, end, duration)
    if len(set(ranges)) != count:
        raise ValueError(f"Source duration is too short to create {count} distinct clips.")
    return ranges

def ensure_outputs_available(paths):
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise FileExistsError("Refusing to overwrite existing output: " + ", ".join(existing))

def ff_time(seconds: float) -> str:
    return f"{seconds:.3f}"

def transcribe(video: Path, model_name: str, language: str | None = None):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("Faster-Whisper is not installed; continuing without subtitles.")
        return []
    # Intel Mac / CPU-safe defaults. Change model to small/medium if the machine has enough RAM.
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    options = {"word_timestamps": True, "vad_filter": True, "beam_size": 5}
    if language is not None:
        if language not in {"hi", "en"}:
            raise ValueError("Transcription language must be 'hi', 'en', or None for automatic detection.")
        options["language"] = language
    segments, _ = model.transcribe(str(video), **options)
    words = []
    for segment in segments:
        if segment.words:
            for word in segment.words:
                token = str(getattr(word, "word", "")).strip()
                if token:
                    timestamps = valid_word_timestamps(getattr(word, "start", None), getattr(word, "end", None), token)
                    if timestamps is None:
                        continue
                    start, end = timestamps
                    words.append((start, end, token))
    # Surface alignment and script anomalies for review while preserving the model output.
    try:
        from viral_moment_analyzer import transcription_quality_issues
        for issue in transcription_quality_issues(words):
            print(f"Transcription quality warning: {issue}")
    except ImportError:
        pass
    return words

def ass_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"

def write_ass(words, path: Path, width: int, height: int, font_size: int, y: int,
              caption_language: str = "original"):
    from hinglish import caption_words
    words = caption_words(words, caption_language)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,Arial,{font_size},&H00FFFFFF,&H0000E5FF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,5,1,2,40,40,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    # Keep each caption to a short phrase while highlighting the currently spoken word.
    for i in range(0, len(words), 4):
        group = words[i:i+4]
        for active, (start, end, _) in enumerate(group):
            parts = []
            for j, (_, _, token) in enumerate(group):
                if j == active:
                    parts.append(r"{\c&H0000E5FF&\fscx112\fscy112}" + token + r"{\c&H00FFFFFF&\fscx100\fscy100}")
                else:
                    parts.append(token)
            line = " ".join(parts).replace("{", r"\{").replace("}", r"\}")
            # Restore intentional ASS override tags after escaping plain text.
            line = line.replace(r"\{", "{").replace(r"\}", "}")
            events.append(f"Dialogue: 0,{ass_timestamp(start)},{ass_timestamp(end)},Karaoke,,0,0,0,,{{\\pos({width//2},{y})}}{line}")
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")

def words_for_range(words, start: float, end: float | None):
    """Clip word timings to a rendered range and shift them to output time zero."""
    selected = []
    for word_start, word_end, token in words:
        if word_end <= start or (end is not None and word_start >= end):
            continue
        selected.append((max(word_start, start) - start,
                         (min(word_end, end) if end is not None else word_end) - start,
                         token))
    return selected

def escape_filter_path(path: Path) -> str:
    # FFmpeg filter parser needs escaping for drive colon, apostrophe, comma, brackets.
    s = str(path.resolve()).replace("\\", "/")
    return s.replace(":", r"\:").replace("'", r"\'").replace(",", r"\,")

def _filter_value_path(path: Path) -> str:
    """Escape a path embedded in a quoted FFmpeg filter option."""
    value = str(path.resolve()).replace("\\", "/")
    for char in ("\\", "'", ":", ",", "[", "]", ";"):
        value = value.replace(char, "\\" + char)
    return value

def banner_filter(width: int, height: int, title: str, channel: str, directory: Path) -> str:
    """Write banner copy to files and return drawbox/drawtext filters using file paths."""
    title_file = directory / "banner_title.txt"
    channel_file = directory / "banner_channel.txt"
    title_file.write_text(title, encoding="utf-8")
    channel_file.write_text(channel, encoding="utf-8")
    if height == 1920:
        box_y, box_h, title_y, channel_y, title_size, channel_size = 1580, 230, 1615, 1690, 48, 34
    else:
        box_y, box_h, title_y, channel_y, title_size, channel_size = 850, 190, 875, 940, 40, 30
    common = f":fontfile='{_filter_value_path(TITLE_BANNER_FONT)}':fontcolor=white:expansion=none"
    return (
        f"drawbox=x=0:y={box_y}:w={width}:h={box_h}:color=black@0.62:t=fill,"
        f"drawtext=textfile='{_filter_value_path(title_file)}'{common}:fontsize={title_size}:x=48:y={title_y},"
        f"drawtext=textfile='{_filter_value_path(channel_file)}'{common}:fontsize={channel_size}:x=48:y={channel_y}"
    )

def render_short(source: Path, output: Path, start: float, end: float, subtitle_model: str | None,
                 banner: bool = False, title: str | None = None, channel: str | None = None,
                 whisper_language: str | None = None, caption_language: str = "original"):
    validate_range(start, end)
    duration = end - start
    temp = WORK / f"{output.stem}_trim.mp4"
    ass = WORK / f"{output.stem}.ass"
    # Re-encode trim to normalize timestamps and avoid independent DASH audio/video cuts.
    try:
        run(["ffmpeg", "-y", "-ss", ff_time(start), "-i", str(source), "-t", ff_time(duration),
             "-map", "0:v:0", "-map", "0:a?", "-vf", "setpts=PTS-STARTPTS",
             "-af", "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-c:a", "aac", "-b:a", "160k", "-avoid_negative_ts", "make_zero", str(temp)])
        words = transcribe(temp, subtitle_model, language=whisper_language) if subtitle_model else []
        # 9:16 background plus centered horizontal foreground.
        filters = (
            "[0:v]setpts=PTS-STARTPTS,scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=24:2,eq=brightness=-0.16[bg];"
            "[0:v]setpts=PTS-STARTPTS,scale=1080:608:force_original_aspect_ratio=decrease,"
            "pad=1080:608:(ow-iw)/2:(oh-ih)/2:color=black[fg];"
            "[bg][fg]overlay=0:560[v0]"
        )
        if banner:
            with tempfile.TemporaryDirectory(prefix="banner_", dir=WORK) as banner_dir:
                banner_filters = banner_filter(1080, 1920, title or "", channel or "", Path(banner_dir))
                filters += f";[v0]{banner_filters}[v0b]"
                base_video = "[v0b]"
                if words:
                    write_ass(words, ass, 1080, 1920, 64, 1370, caption_language)
                    filters += f";{base_video}ass='{escape_filter_path(ass)}'[v]"
                    vmap = "[v]"
                else:
                    vmap = base_video
                _render_short_output(temp, output, filters, vmap)
        else:
            if words:
                write_ass(words, ass, 1080, 1920, 64, 1370, caption_language)
                filters += f";[v0]ass='{escape_filter_path(ass)}'[v]"
                vmap = "[v]"
            else:
                vmap = "[v0]"
            _render_short_output(temp, output, filters, vmap)
        verify(output)
    finally:
        temp.unlink(missing_ok=True)
        ass.unlink(missing_ok=True)

def _render_short_output(temp: Path, output: Path, filters: str, vmap: str):
    run(["ffmpeg", "-n", "-i", str(temp), "-filter_complex", filters,
         "-map", vmap, "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
         "-movflags", "+faststart", str(output)])

def bounded_number(minimum: float, maximum: float, label: str):
    def parse(value: str) -> float:
        try:
            number = float(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{label} must be a number.") from exc
        if not math.isfinite(number) or not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(
                f"{label} must be finite and between {minimum:g} and {maximum:g}.")
        return number
    return parse


def render_long(source: Path, output: Path, start: float, end: float | None,
                brightness: float = 0, contrast: float = 1.03, saturation: float = 1.05,
                volume: float = 1.0, bass_gain: float = 0, banner: bool = False,
                title: str | None = None, channel: str | None = None,
                subtitle_model: str | None = None, whisper_language: str | None = None,
                caption_language: str = "original"):
    if not math.isfinite(start) or start < 0 or (end is not None and (not math.isfinite(end) or end <= start)):
        raise ValueError("Start and end must be finite, non-negative times with end after start.")
    temp_handle = tempfile.NamedTemporaryFile(prefix=f".{output.stem}_", suffix=".mp4",
                                              dir=output.parent, delete=False)
    temp = Path(temp_handle.name)
    temp_handle.close()
    banner_temp = None
    subtitle_file = None
    try:
        cmd = ["ffmpeg", "-y"]
        if start > 0:
            cmd += ["-ss", ff_time(start)]
        cmd += ["-i", str(source)]
        if end is not None:
            cmd += ["-t", ff_time(end-start)]
        video_filter = "setpts=PTS-STARTPTS,scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2"
        if (brightness, contrast, saturation) == (0, 1.03, 1.05):
            video_filter += ",eq=contrast=1.03:saturation=1.05"
        else:
            video_filter += f",eq=brightness={brightness:g}:contrast={contrast:g}:saturation={saturation:g}"
        if banner:
            banner_temp = tempfile.TemporaryDirectory(prefix="banner_", dir=WORK)
            video_filter += "," + banner_filter(1920, 1080, title or "", channel or "", Path(banner_temp.name))
        if subtitle_model:
            subtitle_file = WORK / f"{output.stem}_{uuid.uuid4().hex}.ass"
            words = transcribe(source, subtitle_model, language=whisper_language)
            words = words_for_range(words, start, end)
            if words:
                write_ass(words, subtitle_file, 1920, 1080, 48, 940, caption_language)
                video_filter += f",ass='{escape_filter_path(subtitle_file)}'"
        audio_filter = "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0"
        if volume != 1.0:
            audio_filter += f",volume={volume:g}"
        if bass_gain != 0:
            audio_filter += f",bass=g={bass_gain:g}"
        cmd += ["-map", "0:v:0", "-map", "0:a?",
                "-vf", video_filter,
                "-af", audio_filter,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(temp)]
        run(cmd)
        verify(temp)
        os.link(temp, output)
    finally:
        temp.unlink(missing_ok=True)
        if banner_temp is not None:
            banner_temp.cleanup()
        if subtitle_file is not None:
            subtitle_file.unlink(missing_ok=True)

def verify(path: Path):
    result = run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,start_time,width,height",
                  "-of", "json", str(path)], capture=True)
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    if not any(stream.get("codec_type") == "video" for stream in streams):
        raise ValueError("Output verification failed: no valid video stream was found.")
    print("OUTPUT CHECK:", streams)

def upload_youtube(path: Path, title: str, description: str, privacy: str, client_file: Path,
                   tags: list[str] | None = None, category_id: str = "22"):
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as e:
        raise SystemExit("Install YouTube upload dependencies: pip install -r requirements.txt") from e
    token_file = ROOT / "youtube_token.json"
    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(client_file), scopes)
            creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json(), encoding="utf-8")
    youtube = build("youtube", "v3", credentials=creds)
    snippet = {"title": title[:100], "description": description,
               "categoryId": category_id}
    if tags:
        snippet["tags"] = tags
    body = {"snippet": snippet,
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    media = MediaFileUpload(str(path), chunksize=8*1024*1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"Upload progress: {int(status.progress()*100)}%")
    video_id = response["id"]
    url = "https://youtu.be/" + video_id
    print("Uploaded:", url)
    return {"video_id": video_id, "url": url}


def process_video(*, mode: str, source: str, start: float | None = None,
                  end: float | None = None, clip: list[str] | None = None,
                  short_clip: list[str] | None = None, long_start: float | None = None,
                  long_end: float | None = None, auto_clips: int = 0,
                  clip_length: int = 35, whisper_model: str = "small",
                  no_subtitles: bool = False, upload: bool = False,
                  client_secrets: str | Path = ROOT / "client_secret.json",
                  title: str | None = None, banner: bool = False,
                  channel_name: str | None = None, description: str = "",
                  privacy: str = "private", tag: list[str] | None = None,
                  category_id: str = "22", brightness: float = 0,
                  contrast: float = 1.03, saturation: float = 1.05,
                  volume: float = 1.0, bass_gain: float = 0,
                  whisper_language: str | None = None,
                  caption_language: str = "original",
                  rights_state: str = "unknown", approval_files: list[str | Path] | None = None,
                  progress_callback=None) -> VideoPipelineResult:
    """Process a source into the requested Shorts and/or long-form outputs."""
    if mode not in {"shorts", "long", "both"}:
        raise ValueError("mode must be one of: shorts, long, both")
    clip = list(clip or [])
    short_clip = list(short_clip or [])
    tag = list(tag or [])
    result = VideoPipelineResult()

    def progress(event: str, **details):
        if progress_callback is not None:
            progress_callback({"event": event, **details})

    require_tool("ffmpeg")
    require_tool("ffprobe")
    # Creating clips/derivatives reuses source material even when the result is
    # kept local. Unknown rights therefore stop the job before source intake.
    enforce_reuse_rights(rights_state)
    info = get_info(source)
    if not math.isfinite(info["duration"]) or info["duration"] <= 0:
        raise SystemExit("Source duration must be a positive finite number.")
    print(f"Source: {info['title']} | duration: {info['duration']:.1f}s")
    job_id = str(uuid.uuid4())
    lifecycle_db = connect_lifecycle()
    lifecycle_create_job(lifecycle_db, job_id, source,
                         rights_state=getattr(rights_state, "value", rights_state),
                         metadata={"source_title": info["title"], "source_duration": info["duration"]})
    record_provenance(lifecycle_db, job_id, source,
                      "url" if source.startswith(("http://", "https://")) else "local")
    set_job_status(lifecycle_db, job_id, "processing")
    stem = safe_name(title or info["title"])
    subtitle_model = None if no_subtitles else whisper_model

    resolved_long_start = long_start if mode == "both" and long_start is not None else start
    resolved_long_end = long_end if mode == "both" and long_end is not None else end
    if mode in ("long", "both"):
        resolved_long_start = resolved_long_start or 0.0
        if resolved_long_start >= info["duration"] or (resolved_long_end is not None and resolved_long_end > info["duration"]):
            raise SystemExit("Requested range is outside the source duration.")
        if resolved_long_end is not None:
            try:
                validate_range(resolved_long_start, resolved_long_end, info["duration"])
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc

    if mode in ("shorts", "both"):
        ranges = []
        short_items = short_clip if mode == "both" and short_clip else clip
        for item in short_items:
            if "-" not in item:
                raise SystemExit("--clip must be START-END, for example 00:01:00-00:01:35")
            a, b = item.split("-", 1)
            ranges.append((parse_time(a), parse_time(b)))
        if not ranges and auto_clips:
            if auto_clips < 0 or clip_length <= 0:
                raise SystemExit("--auto-clips and --clip-length must be positive.")
            try:
                ranges = automatic_clip_ranges(info["duration"], auto_clips, clip_length)
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
        if not ranges and mode == "shorts" and start is not None and end is not None:
            ranges = [(start, end)]
        if not ranges and mode == "both":
            raise SystemExit("For both mode, provide --short-clip START-END or --auto-clips N.")
        if not ranges:
            raise SystemExit("For Shorts, provide --clip START-END (repeatable), or --auto-clips N.")
        try:
            for a, b in ranges:
                validate_range(a, b, info["duration"])
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        planned = [OUTPUT / f"{stem}_short_{i:02d}.mp4" for i in range(1, len(ranges) + 1)]
        long_output = OUTPUT / f"{stem}_16x9.mp4"
        ensure_outputs_available(planned + ([long_output] if mode == "both" else []))
        acquired = acquire(source, info["title"], metadata_validated=True)
        for i, (a, b) in enumerate(ranges, 1):
            out = planned[i - 1]
            progress("rendering", kind="short", index=i, total=len(ranges), output_path=out)
            try:
                render_args = (acquired, out, a, b, subtitle_model, banner,
                               title or info.get("title"), info.get("uploader") or channel_name)
                render_options = {}
                if whisper_language is not None:
                    render_options["whisper_language"] = whisper_language
                if caption_language != "original":
                    render_options["caption_language"] = caption_language
                render_short(*render_args, **render_options)
            except Exception as exc:
                out.unlink(missing_ok=True)
                raise RuntimeError(f"Shorts output failed ({out}): {exc}") from exc
            result.output_paths.append(out)
            progress("completed", kind="short", index=i, total=len(ranges), output_path=out)

    if mode in ("long", "both"):
        out = OUTPUT / f"{stem}_16x9.mp4"
        if mode == "long":
            ensure_outputs_available([out])
            acquired = acquire(source, info["title"], metadata_validated=True)
        progress("rendering", kind="long", index=1, total=1, output_path=out)
        try:
            render_long(acquired, out, resolved_long_start, resolved_long_end, brightness, contrast,
                        saturation, volume, bass_gain, banner, title or info.get("title"),
                        info.get("uploader") or channel_name,
                        subtitle_model, whisper_language, caption_language)
        except Exception as exc:
            out.unlink(missing_ok=True)
            raise RuntimeError(f"Long-form output failed ({out}): {exc}") from exc
        result.output_paths.append(out)
        progress("completed", kind="long", index=1, total=1, output_path=out)

    print("\nFinished files:")
    for path in result.output_paths:
        print(f"  {path} ({path.stat().st_size/1024/1024:.1f} MB)")
        metadata = {"media_file": str(path.resolve()), "title": title or path.stem,
                    "description": description, "privacy": privacy, "tags": tag,
                    "category_id": category_id, "platform": "youtube"}
        asset_id = register_asset(lifecycle_db, job_id, path)
        output_id = f"{job_id}:{path.stem}:youtube"
        result.output_ids.append(output_id)
        create_platform_output(lifecycle_db, output_id, job_id, "youtube", asset_id=asset_id,
                               metadata=metadata)
        record_validation(lifecycle_db, output_id, "passed", {"container": "mp4", "rendered_output": True})
    set_job_status(lifecycle_db, job_id, "awaiting_approval")
    if upload:
        approvals = list(approval_files or [])
        if len(approvals) != len(result.output_paths):
            raise SystemExit("Publishing requires one individual approval file per output.")
        client_file = Path(client_secrets).expanduser()
        if not client_file.exists():
            raise SystemExit("OAuth client JSON not found. Create a new Desktop App OAuth client and pass --client-secrets.")
        for i, (path, approval_file) in enumerate(zip(result.output_paths, approvals), 1):
            approval_metadata = {"media_file": str(path.resolve()), "title": title or path.stem,
                                 "description": description, "privacy": privacy,
                                 "tags": tag, "category_id": category_id,
                                 "platform": "youtube"}
            try:
                validate_approval(approval_file, approval_metadata)
                output_id = f"{job_id}:{path.stem}:youtube"
                if Path(approval_file).is_file():
                    approval_record = json.loads(Path(approval_file).read_text(encoding="utf-8"))
                    decide_approval(lifecycle_db, output_id, True, approval_record["approver"])
                    validate_db_approval(lifecycle_db, output_id)
            except (ValueError, PermissionError, OSError, TypeError) as exc:
                raise SystemExit(f"Publishing approval rejected for {path}: {exc}") from exc
            progress("uploading", index=i, total=len(result.output_paths), output_path=path)
            try:
                uploaded = upload_youtube(path, title or path.stem, description, privacy, client_file,
                                          tags=tag, category_id=category_id)
            except Exception as exc:
                with lifecycle_db:
                    lifecycle_db.execute("INSERT INTO publishing_attempts VALUES(?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), output_id, "failed", None, None,
                         json.dumps({"error_type": type(exc).__name__}),
                         datetime.now(timezone.utc).isoformat(timespec="seconds")))
                    lifecycle_db.execute("UPDATE platform_outputs SET publishing_status='failed' WHERE output_id=?",
                                         (output_id,))
                raise
            uploaded_record = uploaded if isinstance(uploaded, dict) else {}
            with lifecycle_db:
                lifecycle_db.execute("INSERT INTO publishing_attempts VALUES(?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), output_id, "succeeded" if uploaded else "failed",
                     uploaded_record.get("video_id"), uploaded_record.get("url"), "{}",
                     datetime.now(timezone.utc).isoformat(timespec="seconds")))
                lifecycle_db.execute("UPDATE platform_outputs SET publishing_status=?,platform_post_id=?,platform_post_url=? WHERE output_id=?",
                    ("succeeded" if uploaded else "failed", uploaded_record.get("video_id"),
                     uploaded_record.get("url"), output_id))
            if uploaded_record.get("video_id"):
                result.youtube_video_ids.append(uploaded_record["video_id"])
                result.youtube_urls.append(uploaded_record["url"])
            progress("uploaded", index=i, total=len(result.output_paths), output_path=path,
                     video_id=uploaded_record.get("video_id"), url=uploaded_record.get("url"))
    set_job_status(lifecycle_db, job_id, "completed")
    lifecycle_db.close()
    return result

def main():
    parser = argparse.ArgumentParser(description="Portable Shorts + Long-form video production")
    parser.add_argument("mode", choices=["shorts", "long", "both"])
    parser.add_argument("--source", required=True, help="YouTube URL or local video path")
    parser.add_argument("--start", type=parse_time, default=None, help="Start timestamp, e.g. 00:02:10")
    parser.add_argument("--end", type=parse_time, default=None, help="End timestamp, e.g. 00:02:45")
    parser.add_argument("--clip", action="append", default=[], help="Short clip range START-END; repeat for multiple clips")
    parser.add_argument("--short-clip", action="append", default=[], help="Shorts range for both mode; repeatable START-END")
    parser.add_argument("--long-start", type=parse_time, default=None, help="Long-form start time in both mode")
    parser.add_argument("--long-end", type=parse_time, default=None, help="Long-form end time in both mode")
    parser.add_argument("--auto-clips", type=int, default=0, help="Create N evenly spaced candidate clips (not AI viral detection)")
    parser.add_argument("--clip-length", type=int, default=35)
    parser.add_argument("--whisper-model", default="small", choices=["tiny", "base", "small", "medium"])
    parser.add_argument("--whisper-language", choices=["hi", "en"], default=None,
                        help="Force Hindi (hi) or English (en); default is automatic detection for code-switching.")
    parser.add_argument("--caption-language", choices=["original", "hinglish"], default="original")
    parser.add_argument("--no-subtitles", action="store_true")
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--rights-state", choices=["unknown", "owned", "licensed", "permission_granted", "public_domain", "prohibited"], default="unknown")
    parser.add_argument("--approval-file", action="append", default=[], help="Individual JSON approval, one per output; binds SHA-256 and metadata")
    parser.add_argument("--client-secrets", default=str(ROOT / "client_secret.json"))
    parser.add_argument("--title", default=None)
    parser.add_argument("--banner", action="store_true", help="Render an optional title and channel lower-third")
    parser.add_argument("--channel-name", default=None, help="Channel name for the banner when source metadata is unavailable")
    parser.add_argument("--description", default="")
    parser.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private")
    parser.add_argument("--tag", action="append", default=[], help="Video tag; repeat to add multiple tags")
    parser.add_argument("--category-id", default="22", help="YouTube category ID (default: 22)")
    parser.add_argument("--brightness", type=bounded_number(-1, 1, "Brightness"), default=0)
    parser.add_argument("--contrast", type=bounded_number(0, 3, "Contrast"), default=1.03)
    parser.add_argument("--saturation", type=bounded_number(0, 3, "Saturation"), default=1.05)
    parser.add_argument("--volume", type=bounded_number(0, 10, "Volume"), default=1.0)
    parser.add_argument("--bass-gain", type=bounded_number(-20, 20, "Bass gain"), default=0)
    args = parser.parse_args()
    return process_video(
        mode=args.mode, source=args.source, start=args.start, end=args.end,
        clip=args.clip, short_clip=args.short_clip, long_start=args.long_start,
        long_end=args.long_end, auto_clips=args.auto_clips, clip_length=args.clip_length,
        whisper_model=args.whisper_model, no_subtitles=args.no_subtitles,
        whisper_language=args.whisper_language,
        caption_language=args.caption_language,
        upload=args.upload, client_secrets=args.client_secrets, title=args.title,
        banner=args.banner, channel_name=args.channel_name, description=args.description,
        privacy=args.privacy, tag=args.tag, category_id=args.category_id,
        rights_state=args.rights_state, approval_files=args.approval_file,
        brightness=args.brightness, contrast=args.contrast, saturation=args.saturation,
        volume=args.volume, bass_gain=args.bass_gain)

if __name__ == "__main__":
    main()
