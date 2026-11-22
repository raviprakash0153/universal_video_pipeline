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
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
WORK = ROOT / "work"
OUTPUT.mkdir(exist_ok=True)
WORK.mkdir(exist_ok=True)

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
        p = Path(source).resolve()
        probe = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "json", str(p)], capture=True)
        duration = float(json.loads(probe.stdout)["format"]["duration"])
        return {"title": p.stem, "duration": duration, "local": True}
    result = run(["yt-dlp", "--no-warnings", "--dump-single-json", "--no-download", source], capture=True)
    data = json.loads(result.stdout)
    return {"title": data.get("title") or "video",
            "duration": float(data.get("duration") or 0), "local": False}

def acquire(source: str, title: str) -> Path:
    if Path(source).exists():
        return Path(source).resolve()
    target = WORK / f"{safe_name(title)}.%(ext)s"
    run(["yt-dlp", "--no-warnings", "--no-playlist",
         "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
         "--merge-output-format", "mp4", "-o", str(target), source])
    matches = sorted(WORK.glob(f"{safe_name(title)}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        raise RuntimeError("yt-dlp finished but the downloaded file was not found.")
    return matches[0]

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

def ensure_outputs_available(paths):
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise FileExistsError("Refusing to overwrite existing output: " + ", ".join(existing))

def ff_time(seconds: float) -> str:
    return f"{seconds:.3f}"

def transcribe(video: Path, model_name: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("Faster-Whisper is not installed; continuing without subtitles.")
        return []
    # Intel Mac / CPU-safe defaults. Change model to small/medium if the machine has enough RAM.
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(video), word_timestamps=True, vad_filter=True)
    words = []
    for segment in segments:
        if segment.words:
            for word in segment.words:
                token = re.sub(r"^[^\w]+|[^\w]+$", "", word.word, flags=re.UNICODE).upper()
                if token:
                    words.append((float(word.start), max(float(word.end), float(word.start) + 0.12), token))
    return words

def ass_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"

def write_ass(words, path: Path, width: int, height: int, font_size: int, y: int):
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

def escape_filter_path(path: Path) -> str:
    # FFmpeg filter parser needs escaping for drive colon, apostrophe, comma, brackets.
    s = str(path.resolve()).replace("\\", "/")
    return s.replace(":", r"\:").replace("'", r"\'").replace(",", r"\,")

def render_short(source: Path, output: Path, start: float, end: float, subtitle_model: str | None):
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
        words = transcribe(temp, subtitle_model) if subtitle_model else []
        # 9:16 background plus centered horizontal foreground.
        filters = (
            "[0:v]setpts=PTS-STARTPTS,scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=24:2,eq=brightness=-0.16[bg];"
            "[0:v]setpts=PTS-STARTPTS,scale=1080:608:force_original_aspect_ratio=decrease,"
            "pad=1080:608:(ow-iw)/2:(oh-ih)/2:color=black[fg];"
            "[bg][fg]overlay=0:560[v0]"
        )
        if words:
            write_ass(words, ass, 1080, 1920, 64, 1370)
            filters += f";[v0]ass='{escape_filter_path(ass)}'[v]"
            vmap = "[v]"
        else:
            vmap = "[v0]"
        run(["ffmpeg", "-n", "-i", str(temp), "-filter_complex", filters,
             "-map", vmap, "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
             "-movflags", "+faststart", str(output)])
        verify(output)
    finally:
        temp.unlink(missing_ok=True)
        ass.unlink(missing_ok=True)

def render_long(source: Path, output: Path, start: float, end: float | None):
    if not math.isfinite(start) or start < 0 or (end is not None and (not math.isfinite(end) or end <= start)):
        raise ValueError("Start and end must be finite, non-negative times with end after start.")
    cmd = ["ffmpeg", "-n"]
    if start > 0:
        cmd += ["-ss", ff_time(start)]
    cmd += ["-i", str(source)]
    if end is not None:
        if end <= start:
            raise ValueError("End time must be after start time.")
        cmd += ["-t", ff_time(end-start)]
    cmd += ["-map", "0:v:0", "-map", "0:a?",
            "-vf", "setpts=PTS-STARTPTS,scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,eq=contrast=1.03:saturation=1.05",
            "-af", "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(output)]
    run(cmd)
    verify(output)

def verify(path: Path):
    result = run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,start_time,width,height",
                  "-of", "json", str(path)], capture=True)
    data = json.loads(result.stdout)
    print("OUTPUT CHECK:", data.get("streams", []))

def upload_youtube(path: Path, title: str, description: str, privacy: str, client_file: Path):
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
    body = {"snippet": {"title": title[:100], "description": description,
                        "categoryId": "22"},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    media = MediaFileUpload(str(path), chunksize=8*1024*1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"Upload progress: {int(status.progress()*100)}%")
    print("Uploaded:", "https://youtu.be/" + response["id"])

def main():
    parser = argparse.ArgumentParser(description="Portable Shorts + Long-form video production")
    parser.add_argument("mode", choices=["shorts", "long"])
    parser.add_argument("--source", required=True, help="YouTube URL or local video path")
    parser.add_argument("--start", type=parse_time, default=None, help="Start timestamp, e.g. 00:02:10")
    parser.add_argument("--end", type=parse_time, default=None, help="End timestamp, e.g. 00:02:45")
    parser.add_argument("--clip", action="append", default=[], help="Short clip range START-END; repeat for multiple clips")
    parser.add_argument("--auto-clips", type=int, default=0, help="Create N evenly spaced candidate clips (not AI viral detection)")
    parser.add_argument("--clip-length", type=int, default=35)
    parser.add_argument("--whisper-model", default="tiny", choices=["tiny", "base", "small", "medium"])
    parser.add_argument("--no-subtitles", action="store_true")
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--client-secrets", default=str(ROOT / "client_secret.json"))
    parser.add_argument("--title", default=None)
    parser.add_argument("--description", default="")
    parser.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private")
    args = parser.parse_args()

    require_tool("ffmpeg")
    require_tool("ffprobe")
    info = get_info(args.source)
    if not math.isfinite(info["duration"]) or info["duration"] <= 0:
        raise SystemExit("Source duration must be a positive finite number.")
    print(f"Source: {info['title']} | duration: {info['duration']:.1f}s")
    source = acquire(args.source, info["title"])
    stem = safe_name(args.title or info["title"])
    subtitle_model = None if args.no_subtitles else args.whisper_model

    outputs = []
    if args.mode == "shorts":
        ranges = []
        for item in args.clip:
            if "-" not in item:
                raise SystemExit("--clip must be START-END, for example 00:01:00-00:01:35")
            a, b = item.split("-", 1)
            ranges.append((parse_time(a), parse_time(b)))
        if not ranges and args.auto_clips:
            length = args.clip_length
            if args.auto_clips < 0 or length <= 0:
                raise SystemExit("--auto-clips and --clip-length must be positive.")
            if info["duration"] <= length:
                ranges = [(0, info["duration"])]
            else:
                # Evenly spaced candidates; inspect them before publishing.
                starts = [int((i+1) * (info["duration"]-length) / (args.auto_clips+1))
                          for i in range(args.auto_clips)]
                ranges = [(s, min(s+length, info["duration"])) for s in starts]
        if not ranges and args.start is not None and args.end is not None:
            ranges = [(args.start, args.end)]
        if not ranges:
            raise SystemExit("For Shorts, provide --clip START-END (repeatable), or --auto-clips N.")
        try:
            for a, b in ranges:
                validate_range(a, b, info["duration"])
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        planned = [OUTPUT / f"{stem}_short_{i:02d}.mp4" for i in range(1, len(ranges) + 1)]
        ensure_outputs_available(planned)
        for i, (a, b) in enumerate(ranges, 1):
            out = planned[i - 1]
            render_short(source, out, a, b, subtitle_model)
            outputs.append(out)
    else:
        out = OUTPUT / f"{stem}_16x9.mp4"
        ensure_outputs_available([out])
        start = args.start or 0.0
        if start >= info["duration"] or (args.end is not None and args.end > info["duration"]):
            raise SystemExit("Requested range is outside the source duration.")
        if args.end is not None:
            try:
                validate_range(start, args.end, info["duration"])
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
        render_long(source, out, start, args.end)
        outputs.append(out)

    print("\nFinished files:")
    for p in outputs:
        print(f"  {p} ({p.stat().st_size/1024/1024:.1f} MB)")
    if args.upload:
        client_file = Path(args.client_secrets).expanduser()
        if not client_file.exists():
            raise SystemExit("OAuth client JSON not found. Create a new Desktop App OAuth client and pass --client-secrets.")
        for p in outputs:
            upload_youtube(p, args.title or p.stem, args.description, args.privacy, client_file)

if __name__ == "__main__":
    main()
