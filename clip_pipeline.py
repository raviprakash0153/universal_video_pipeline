#!/usr/bin/env python3
"""
Native 9:16 Viral Shorts Clipping & Scheduling Pipeline
- Accepts any YouTube URL interactively or from command-line
- Auto-detects video length & generates 3 viral 30s clips
- Renders 1080x1920 vertical format with subtitles & facecam
- Auto-uploads or stages for scheduled release (6 AM, 6 PM, 10 PM daily)
"""

import os
import sys
import glob
import subprocess
import re
import json
import argparse
from datetime import datetime
from faster_whisper import WhisperModel

NODE_PATH = "/Users/raviprakash/.nvm/versions/node/v22.17.1/bin/node"
FONTS_DIR = "/System/Library/Fonts/Supplemental"
WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
QUEUE_DIR = os.path.join(WORKSPACE_DIR, "upload_queue")
SCHEDULE_STATE_FILE = os.path.join(WORKSPACE_DIR, "schedule_state.json")
YTDLP_FORMAT = "299+140/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"

os.makedirs(QUEUE_DIR, exist_ok=True)

# ─────────────────────────── HELPERS ───────────────────────────

def format_ass_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"

def format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def clean_word(w: str) -> str:
    w = re.sub(r'^[^\w]+|[^\w]+$', '', w)
    return w.upper()

def get_video_info(url: str):
    """Fetches video title, duration, and channel metadata using yt-dlp."""
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--js-runtimes", f"node:{NODE_PATH}",
        "--remote-components", "ejs:github",
        "--dump-json", "--no-download",
        url
    ]
    result = subprocess.check_output(cmd, text=True)
    for line in result.splitlines():
        if line.strip().startswith("{") and line.strip().endswith("}"):
            try:
                data = json.loads(line)
                return {
                    "title": data.get("title", "Viral Clip"),
                    "duration": int(data.get("duration", 0)),
                    "uploader": data.get("uploader", "Streamer"),
                    "channel": data.get("channel", "Streamer")
                }
            except Exception:
                continue
    raise ValueError("Could not parse video metadata from YouTube URL.")

def generate_ass_subtitles(words, ass_filename: str, fallback_title: str):
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hormozi,Impact,72,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,2,0,1,6,0,2,20,20,20,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    valid_words = []
    for w in words:
        cw = clean_word(w.word)
        if cw and len(cw) > 0 and not cw.startswith('.'):
            valid_words.append((w.start, w.end, cw))

    events = []
    if not valid_words:
        clean_fallback = clean_word(fallback_title)[:25]
        events.append(f"Dialogue: 0,0:00:01.00,0:00:06.00,Hormozi,,0,0,0,,{{\\pos(540,825)}}{{\\c&H0000FFFF&\\fscx115\\fscy115}}{clean_fallback}{{\\c&H00FFFFFF&\\fscx100\\fscy100}} VIRAL MOMENT")
    else:
        chunk_size = 4
        for i in range(0, len(valid_words), chunk_size):
            chunk = valid_words[i:i + chunk_size]
            for active_idx, (w_start, w_end, _) in enumerate(chunk):
                line_parts = []
                for idx, (_, _, text) in enumerate(chunk):
                    if idx == active_idx:
                        line_parts.append(f"{{\\c&H0000FFFF&\\fscx115\\fscy115}}{text}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}")
                    else:
                        line_parts.append(text)
                line_text = " ".join(line_parts)
                start_str = format_ass_time(w_start)
                end_str   = format_ass_time(max(w_end, w_start + 0.25))
                events.append(f"Dialogue: 0,{start_str},{end_str},Hormozi,,0,0,0,,{{\\pos(540,825)}}{line_text}")

    with open(ass_filename, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(events) + "\n")

# ─────────────────────────── CLIP PROCESSING ───────────────────────────

def process_clip(url: str, clip_info: dict, model: WhisperModel, output_path: str):
    raw_file = os.path.join(WORKSPACE_DIR, f"temp_raw_{clip_info['id']}.mp4")
    ass_file = os.path.join(WORKSPACE_DIR, f"temp_sub_{clip_info['id']}.ass")

    print(f"\n=======================================================")
    print(f"🎬 Processing Short {clip_info['id']}: {clip_info['title']}")
    print(f"   Time Range: {clip_info['start']} -> {clip_info['end']}")
    print(f"   Target: {output_path}")
    print(f"=======================================================")

    # 1. Download segment
    dl_cmd = [
        sys.executable, "-m", "yt_dlp",
        "--extractor-args", "youtube:player_client=tv_embedded",
        "--format", YTDLP_FORMAT,
        "--download-sections", f"*{clip_info['start']}-{clip_info['end']}",
        "--force-keyframes-at-cuts",
        "--merge-output-format", "mp4",
        "-o", raw_file,
        url
    ]
    print("▶ Step 1: Ingesting 1080p DASH segment...")
    subprocess.run(dl_cmd, check=True)

    # 2. Transcribe
    print("▶ Step 2: Transcribing speech with faster-whisper...")
    segments, _ = model.transcribe(raw_file, word_timestamps=True)
    all_words = []
    for s in segments:
        if s.words:
            all_words.extend(s.words)
    print(f"   Detected {len(all_words)} words in audio.")
    generate_ass_subtitles(all_words, ass_file, clip_info['title'])

    # 3. Framing & Rendering (9:16 layout)
    print("▶ Step 3: Rendering 1080x1920 vertical short...")
    filter_complex = (
        "[0:v]setpts=PTS-STARTPTS,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:2,eq=brightness=-0.15[bg];"
        "[0:v]setpts=PTS-STARTPTS,crop=ih*3/4:ih:(iw-ih*3/4)/2:0,scale=1080:1440[gameplay];"
        "[0:v]setpts=PTS-STARTPTS,crop=iw*0.20:ih*0.22:iw*0.005:ih*0.425,scale=620:400[facecam];"
        "[bg][gameplay]overlay=0:440[v1];"
        "[v1][facecam]overlay=230:20[v2];"
        f"[v2]ass={ass_file}:fontsdir={FONTS_DIR}[outv]"
    )
    af_filter = "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0"

    render_cmd = [
        "ffmpeg", "-y",
        "-i", raw_file,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "0:a?",
        "-af", af_filter,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-c:a", "aac", "-b:a", "128k",
        output_path
    ]
    subprocess.run(render_cmd, check=True)

    # 4. Verify output PTS
    pts_cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=start_time",
        "-of", "default=noprint_wrappers=1:nokey=1",
        output_path
    ]
    pts_out = subprocess.check_output(pts_cmd, text=True).strip().splitlines()
    print(f"   Verification: Start PTS values = {pts_out}")

    # Cleanup temp raw files
    for tmp in [raw_file, ass_file]:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass

# ─────────────────────────── SCHEDULE QUEUE RUNNER ───────────────────────────

def run_scheduled_upload():
    """Triggered by cron/scheduler at 6 AM, 6 PM, or 10 PM. Picks the next short in queue and uploads it."""
    import youtube_uploader

    manifests = sorted(glob.glob(os.path.join(QUEUE_DIR, "*.json")))
    if not manifests:
        print("ℹ️ No pending shorts in the upload queue.")
        return

    next_manifest = manifests[0]
    with open(next_manifest, "r", encoding="utf-8") as f:
        meta = json.load(f)

    video_path = meta.get("file_path")
    if not os.path.exists(video_path):
        print(f"⚠️ Video file missing: {video_path}")
        os.remove(next_manifest)
        return

    now_str = datetime.now().strftime("%Y-%m-%d %I:%M %p")
    print(f"\n⏰ [{now_str}] Triggering Scheduled Upload: {meta['title']}")

    try:
        response = youtube_uploader.upload_video(
            file_path=video_path,
            title=meta["title"],
            description=meta["description"],
            tags=meta["tags"],
            category_id="20",
            privacy_status=meta.get("privacy", "public")
        )
        print(f"🎉 Scheduled Upload Complete: https://youtu.be/{response.get('id')}")
        # Remove manifest from queue
        os.remove(next_manifest)
    except Exception as e:
        print(f"❌ Failed to upload scheduled short: {e}")

# ─────────────────────────── WORKSPACE CLEANUP ───────────────────────────

def cleanup_workspace():
    print("\n🧹 Rule D: Workspace Cleanup...")
    patterns = [
        os.path.join(WORKSPACE_DIR, "temp_raw_*"),
        os.path.join(WORKSPACE_DIR, "temp_sub_*"),
        os.path.join(WORKSPACE_DIR, "*.part"),
        os.path.join(WORKSPACE_DIR, "*.ass")
    ]
    for p in patterns:
        for f in glob.glob(p):
            try:
                os.remove(f)
            except Exception:
                pass

# ─────────────────────────── MAIN PIPELINE ───────────────────────────

def generate_shorts_for_url(url: str, upload_now: bool = False):
    print("=======================================================")
    print("🚀 Viral Shorts Clipping & Scheduling Pipeline")
    print(f"   URL: {url}")
    print("=======================================================")

    print("▶ Step 0: Analyzing YouTube Video...")
    info = get_video_info(url)
    title = info["title"]
    duration = info["duration"]
    uploader = info["uploader"]
    print(f"   Title: {title}")
    print(f"   Duration: {duration}s ({format_timestamp(duration)})")
    print(f"   Channel: {uploader}")

    if duration < 90:
        print("⚠️ Video is shorter than 90 seconds. Adjusting clip boundaries.")
        clips = [
            {"id": "01", "start": "00:00:00", "end": "00:00:30", "title": f"{title} Part 1"},
            {"id": "02", "start": "00:00:15", "end": "00:00:45", "title": f"{title} Part 2"},
            {"id": "03", "start": "00:00:30", "end": "00:01:00", "title": f"{title} Part 3"}
        ]
    else:
        # Pick 3 engaging spaced segments
        p1 = max(60, int(duration * 0.10))
        p2 = max(p1 + 90, int(duration * 0.35))
        p3 = max(p2 + 90, int(duration * 0.65))

        clips = [
            {
                "id": "01",
                "start": format_timestamp(p1),
                "end": format_timestamp(p1 + 30),
                "title": f"INSANE MOMENT! 😂🔥 {title[:40]} #Shorts #{uploader.replace(' ', '')}"
            },
            {
                "id": "02",
                "start": format_timestamp(p2),
                "end": format_timestamp(p2 + 30),
                "title": f"NO WAY HE DID THIS! 💀 {title[:40]} #Shorts #{uploader.replace(' ', '')}"
            },
            {
                "id": "03",
                "start": format_timestamp(p3),
                "end": format_timestamp(p3 + 30),
                "title": f"BEST CLIP OF THE STREAM! 🚀 {title[:40]} #Shorts #{uploader.replace(' ', '')}"
            }
        ]

    print("Loading faster-whisper model (tiny, int8 CPU)...")
    model = WhisperModel("tiny", device="cpu", compute_type="int8")

    generated_files = []
    slot_names = ["06:00 AM (Morning)", "06:00 PM (Evening)", "10:00 PM (Night)"]

    for idx, clip in enumerate(clips):
        filename = f"viral_short_{clip['id']}.mp4"
        output_file = os.path.join(WORKSPACE_DIR, filename)
        process_clip(url, clip, model, output_file)
        generated_files.append(output_file)

        # Build viral metadata
        meta = {
            "file_path": output_file,
            "title": clip["title"],
            "description": f"""{clip['title']}

🔥 Channel: @{uploader}
🎬 Full Video: {url}
⏰ Scheduled Slot: {slot_names[idx]}

Subscribe for daily viral shorts & gaming clips!

#Shorts #{uploader.replace(' ', '')} #Gaming #Viral #FunnyMoments
""",
            "tags": ["Shorts", uploader, "Gaming", "Viral", "Funny Moments", "Speed Clips", "Clips"],
            "privacy": "public",
            "scheduled_slot": slot_names[idx]
        }

        # Save to queue for scheduled publishing
        manifest_path = os.path.join(QUEUE_DIR, f"queue_slot_{clip['id']}.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

    cleanup_workspace()

    print("\n=======================================================")
    print("✅ All 3 Viral Shorts have been generated in 1080p60!")
    for idx, g in enumerate(generated_files):
        size_mb = os.path.getsize(g) / (1024 * 1024)
        print(f"   📦 {os.path.basename(g)} ({size_mb:.2f} MB) -> Scheduled for {slot_names[idx]}")
    print("=======================================================")

    if upload_now:
        print("\n🚀 Direct Upload option selected — uploading all 3 immediately...")
        import youtube_uploader
        for manifest_file in sorted(glob.glob(os.path.join(QUEUE_DIR, "*.json"))):
            with open(manifest_file, "r") as f:
                meta = json.load(f)
            youtube_uploader.upload_video(
                file_path=meta["file_path"],
                title=meta["title"],
                description=meta["description"],
                tags=meta["tags"],
                privacy_status="public"
            )
            os.remove(manifest_file)
    else:
        print("\n📅 Scheduled Mode: The shorts are queued in 'upload_queue/' to upload at:")
        print("   🌅 Slot 1: 6:00 AM (Morning)")
        print("   🌆 Slot 2: 6:00 PM (Evening)")
        print("   🌙 Slot 3: 10:00 PM (Night)")

def main():
    parser = argparse.ArgumentParser(description="Generate & Schedule 3 Viral YouTube Shorts")
    parser.add_argument("--url", type=str, help="YouTube Video URL")
    parser.add_argument("--upload-now", action="store_true", help="Upload all 3 immediately instead of queueing for schedule")
    parser.add_argument("--cron-upload", action="store_true", help="Invoked by cron scheduler to upload next queued video")

    args = parser.parse_args()

    if args.cron_upload:
        run_scheduled_upload()
        return

    url = args.url
    if not url:
        # Prompt interactively
        print("\n" + "="*50)
        print("🎥 YOUTUBE SHORTS AUTO-GENERATOR & SCHEDULER")
        print("="*50)
        try:
            url = input("Paste the YouTube URL from which you want to make 3 shorts: ").strip()
        except EOFError:
            url = ""

    if not url:
        print("❌ Error: No URL provided.")
        sys.exit(1)

    generate_shorts_for_url(url, upload_now=args.upload_now)

if __name__ == "__main__":
    main()
