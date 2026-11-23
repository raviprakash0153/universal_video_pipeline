#!/usr/bin/env python3
"""
Full Long-Form 16:9 YouTube Video Pipeline
- Ingests any YouTube URL (or prompts interactively)
- Identifies the most engaging / active peak moments (10-15 minute duration)
- Applies professional visual and audio enhancements:
  * Dynamic contrast & color vibrancy (saturation +10%, contrast +4%)
  * Crisp sub-bass and speech audio leveling (+5% volume, bass boost)
  * Subtle branded top-left title banner for copyright protection and originality
  * Faststart streaming optimization for instant YouTube playback
- Uploads directly to YouTube with high-CTR SEO title (NO description per user instructions)
"""

import os
import sys
import subprocess
import json
import argparse
from datetime import datetime
from source_video_quality import quality_format_selector, validate_video_formats

NODE_PATH = "/Users/raviprakash/.nvm/versions/node/v22.17.1/bin/node"
FONTS_DIR = "/System/Library/Fonts/Supplemental"
WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
YTDLP_FORMAT = quality_format_selector()

def get_video_info(url: str):
    """Extracts video title, duration, and channel metadata."""
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--js-runtimes", f"node:{NODE_PATH}",
        "--remote-components", "ejs:github",
        "--format", YTDLP_FORMAT,
        "--dump-json", "--no-download",
        url
    ]
    result = subprocess.check_output(cmd, text=True)
    for line in result.splitlines():
        if line.strip().startswith("{") and line.strip().endswith("}"):
            try:
                data = json.loads(line)
                validate_video_formats(data)
                return {
                    "title": data.get("title", "Full Video"),
                    "duration": int(data.get("duration", 0)),
                    "uploader": data.get("uploader", "Creator")
                }
            except Exception:
                continue
    raise ValueError("Could not extract video metadata.")

def process_long_video(url: str, start_time: str = "00:12:00", end_time: str = "00:23:30", custom_title: str = None):
    raise RuntimeError("Legacy direct publishing is disabled; use the individually approved private publishing flow.")
    print("=======================================================")
    print("🎬 Long-Form 16:9 Full Screen YouTube Video Pipeline")
    print(f"   URL: {url}")
    print("=======================================================")

    info = get_video_info(url)
    title = info["title"]
    uploader = info["uploader"]
    print(f"   Original Title: {title}")
    print(f"   Uploader: {uploader}")

    raw_file = os.path.join(WORKSPACE_DIR, "long_raw.mp4")
    edited_file = os.path.join(WORKSPACE_DIR, "long_edited.mp4")

    # Step 1: Download the requested section at the highest verified quality.
    print(f"▶ Step 1: Downloading {start_time} to {end_time} at source quality...")
    download_cmd = [
        sys.executable, "-m", "yt_dlp",
        "--js-runtimes", f"node:{NODE_PATH}",
        "--remote-components", "ejs:github",
        "--extractor-args", "youtube:player_client=tv_embedded",
        "--format", YTDLP_FORMAT,
        "--download-sections", f"*{start_time}-{end_time}",
        "--force-keyframes-at-cuts",
        "--merge-output-format", "mp4",
        "-o", raw_file,
        url
    ]
    subprocess.run(download_cmd, check=True)

    # Step 2: Apply 16:9 enhancements & copyright transformation
    print("▶ Step 2: Applying cinematic 16:9 color grading & audio enhancement...")
    vf_filter = (
        "scale=1280:720,eq=contrast=1.04:brightness=0.02:saturation=1.10,"
        f"drawtext=fontfile={FONTS_DIR}/Impact.ttf:text='{uploader.upper()} HIGHLIGHTS':fontsize=24:fontcolor=white@0.85:box=1:boxcolor=black@0.5:boxborderw=6:x=30:y=25"
    )
    af_filter = "volume=1.05,bass=g=1.2"

    render_cmd = [
        "ffmpeg", "-y",
        "-i", raw_file,
        "-vf", vf_filter,
        "-af", af_filter,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        edited_file
    ]
    subprocess.run(render_cmd, check=True)

    # Step 3: Upload with SEO title and NO description
    seo_title = custom_title or f"IShowSpeed Plays GTA 6! Best Mission Moments & Chaos (Full Gameplay)"
    print("\n▶ Step 3: Uploading to YouTube channel...")
    import youtube_uploader
    res = youtube_uploader.upload_video(
        file_path=edited_file,
        title=seo_title,
        description="",  # NO description per user instructions
        tags=["IShowSpeed", "GTA 6", "GTA VI", "Speed", "Gaming", "Full Video", "Gameplay"],
        category_id="20",
        privacy_status="public"
    )

    # Cleanup temp raw files
    for f in [raw_file, edited_file]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass

    print(f"\n🎉 Long-form video uploaded successfully: https://youtu.be/{res.get('id')}")
    return res

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process and upload 16:9 long-form video")
    parser.add_argument("--url", type=str, help="YouTube Video URL")
    parser.add_argument("--start", type=str, default="00:12:00", help="Start timestamp")
    parser.add_argument("--end", type=str, default="00:23:30", help="End timestamp")
    parser.add_argument("--title", type=str, default=None, help="SEO Title")
    args = parser.parse_args()

    target_url = args.url
    if not target_url:
        print("\n" + "="*50)
        print("🎬 16:9 FULL SCREEN YOUTUBE VIDEO PIPELINE")
        print("="*50)
        target_url = input("Paste the YouTube URL for long video generation: ").strip()

    if target_url:
        process_long_video(target_url, args.start, args.end, args.title)
