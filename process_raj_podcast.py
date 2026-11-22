#!/usr/bin/env python3
"""
Custom Pipeline for Raj Shamani x Dr. Sahar Yousef Podcast (4Vz6L8B73i4)
Clips the top 3 most-watched heatmap peaks in full 1080p, renders with clean vertical framing
and Hormozi subtitles, and queues them for scheduled release (6 AM, 6 PM, 10 PM) or immediate upload.
"""

import os
import sys
import glob
import subprocess
import re
import json
import argparse
from faster_whisper import WhisperModel

NODE_PATH = "/Users/raviprakash/.nvm/versions/node/v22.17.1/bin/node"
FONTS_DIR = "/System/Library/Fonts/Supplemental"
WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
QUEUE_DIR = os.path.join(WORKSPACE_DIR, "upload_queue")
os.makedirs(QUEUE_DIR, exist_ok=True)

VIDEO_URL = "https://www.youtube.com/watch?v=4Vz6L8B73i4"
# Format 137 = 1920x1080 H.264 mp4, Format 140 = 128k AAC m4a
FORMAT_SPEC = "137+140/bestvideo[height<=1080]+bestaudio/best"

CLIPS = [
    {
        "id": "01",
        "start": "00:19:40",
        "end": "00:20:30",
        "title": "Your Brain Only Looks For Data That Confirms Your Beliefs! 🧠🤯 #Shorts #RajShamani",
        "slot": "06:00 AM (Morning)",
        "description": """Your brain is biologically wired to find data that confirms your beliefs! 🧠🤯 Dr. Sahar Yousef breaks down the Default Mode Network (DMN) on FO559 with Raj Shamani.

🔥 Speaker: Dr. Sahar Yousef (Neuroscientist at UC Berkeley)
🎙️ Podcast: Figuring Out with @RajShamani
🎬 Full Episode: https://www.youtube.com/watch?v=4Vz6L8B73i4

Subscribe for more daily neuroscience & productivity insights!

#Shorts #RajShamani #Neuroscience #Productivity #Mindset #BrainHacks #Dopamine #Focus
""",
        "tags": ["Shorts", "Raj Shamani", "Dr Sahar Yousef", "Neuroscience", "Mindset", "Brain", "Psychology", "Focus", "Self Improvement"]
    },
    {
        "id": "02",
        "start": "00:14:06",
        "end": "00:14:56",
        "title": "How To Build Main Character Energy When You Have Zero Self Belief 📝✨ #Shorts",
        "slot": "06:00 PM (Evening)",
        "description": """How can someone who never believed in themselves build main character energy? 📝✨ Neuroscientist Dr. Sahar Yousef gives the exact scientific exercise to rewire your identity.

🔥 Speaker: Dr. Sahar Yousef
🎙️ Host: @RajShamani
🎬 Full Episode: https://www.youtube.com/watch?v=4Vz6L8B73i4

Subscribe for daily wisdom and life-changing podcasts!

#Shorts #RajShamani #DrSaharYousef #MainCharacterEnergy #SelfBelief #Confidence #Motivation #Success
""",
        "tags": ["Shorts", "Raj Shamani", "Dr Sahar Yousef", "Main Character Energy", "Confidence", "Motivation", "Identity", "Habits"]
    },
    {
        "id": "03",
        "start": "00:07:32",
        "end": "00:08:24",
        "title": "Why Olympic Athletes Never Rely On Just Motivation 🏅🧠 #Shorts #RajShamani",
        "slot": "10:00 PM (Night)",
        "description": """Why having big dreams will never work if you don't hack the biology of focus and energy! 🏅🧠 Dr. Sahar Yousef explains biological laws to Raj Shamani.

🔥 Speaker: Dr. Sahar Yousef
🎙️ Host: @RajShamani
🎬 Full Episode: https://www.youtube.com/watch?v=4Vz6L8B73i4

Subscribe for daily productivity & neuroscience shorts!

#Shorts #RajShamani #Motivation #Discipline #Productivity #SuccessMindset #Focus
""",
        "tags": ["Shorts", "Raj Shamani", "Dr Sahar Yousef", "Motivation", "Discipline", "Focus", "Productivity", "Goals"]
    }
]

def format_ass_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"

def clean_word(w: str) -> str:
    w = re.sub(r'^[^\w]+|[^\w]+$', '', w)
    return w.upper()

def generate_ass_subtitles(words, ass_filename: str):
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hormozi,Impact,70,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,2,0,1,6,0,2,20,20,20,1

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
        events.append("Dialogue: 0,0:00:01.00,0:00:05.00,Hormozi,,0,0,0,,{\\pos(540,1100)}{\\c&H0000FFFF&\\fscx115\\fscy115}NEUROSCIENCE{\\c&H00FFFFFF&\\fscx100\\fscy100} SECRET")
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
                end_str = format_ass_time(max(w_end, w_start + 0.25))
                events.append(f"Dialogue: 0,{start_str},{end_str},Hormozi,,0,0,0,,{{\\pos(540,1150)}}{line_text}")

    with open(ass_filename, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(events) + "\n")

def process_podcast_clip(clip_info: dict, model: WhisperModel):
    raw_file = os.path.join(WORKSPACE_DIR, f"raj_raw_{clip_info['id']}.mp4")
    ass_file = os.path.join(WORKSPACE_DIR, f"raj_sub_{clip_info['id']}.ass")
    output_file = os.path.join(WORKSPACE_DIR, f"viral_short_{clip_info['id']}.mp4")

    print(f"\n=======================================================")
    print(f"🎬 Processing Clip {clip_info['id']}: {clip_info['title']}")
    print(f"   Time Range: {clip_info['start']} -> {clip_info['end']}")
    print(f"   Release Slot: {clip_info['slot']}")
    print(f"=======================================================")

    # 1. Ingest 1080p DASH
    dl_cmd = [
        sys.executable, "-m", "yt_dlp",
        "--js-runtimes", f"node:{NODE_PATH}",
        "--remote-components", "ejs:github",
        "--format", FORMAT_SPEC,
        "--download-sections", f"*{clip_info['start']}-{clip_info['end']}",
        "--force-keyframes-at-cuts",
        "--merge-output-format", "mp4",
        "-o", raw_file,
        VIDEO_URL
    ]
    print("▶ Step 1: Ingesting 1080p DASH clip section...")
    subprocess.run(dl_cmd, check=True)

    # 2. Transcribe with Whisper
    print("▶ Step 2: Transcribing speech with faster-whisper...")
    segments, _ = model.transcribe(raw_file, word_timestamps=True)
    all_words = []
    for s in segments:
        if s.words:
            all_words.extend(s.words)
    print(f"   Detected {len(all_words)} words in audio.")
    generate_ass_subtitles(all_words, ass_file)

    # 3. Clean Podcast Framing Formula (1080x1920)
    # Background: blurred fill
    # Center: 16:9 full HD feed centered at y=480 with subtle dark drop border
    # Subtitles: Impact 70pt at y=1150 (perfect eye-level)
    print("▶ Step 3: Rendering 1080x1920 vertical format with Hormozi captions...")
    filter_complex = (
        "[0:v]setpts=PTS-STARTPTS,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:2,eq=brightness=-0.15[bg];"
        "[0:v]setpts=PTS-STARTPTS,scale=1080:608[fg];"
        "[bg][fg]overlay=0:656[v1];"
        f"[v1]ass={ass_file}:fontsdir={FONTS_DIR}[outv]"
    )
    af_filter = "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0"

    render_cmd = [
        "ffmpeg", "-y",
        "-i", raw_file,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "0:a?",
        "-af", af_filter,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        output_file
    ]
    subprocess.run(render_cmd, check=True)

    # 4. Verify output PTS
    pts_cmd = ["ffprobe", "-v", "error", "-show_entries", "stream=start_time", "-of", "default=noprint_wrappers=1:nokey=1", output_file]
    pts_out = subprocess.check_output(pts_cmd, text=True).strip().splitlines()
    print(f"   Verified Start PTS: {pts_out}")

    # Enqueue for scheduled release
    meta = {
        "file_path": output_file,
        "title": clip_info["title"],
        "description": clip_info["description"],
        "tags": clip_info["tags"],
        "privacy": "public",
        "scheduled_slot": clip_info["slot"]
    }
    manifest_path = os.path.join(QUEUE_DIR, f"queue_slot_{clip_info['id']}.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    # Cleanup temp raw files
    for tmp in [raw_file, ass_file]:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass

def main():
    print("=======================================================")
    print("🎙️ Processing Raj Shamani Podcast Viral Clips (1080p)")
    print(f"   URL: {VIDEO_URL}")
    print("=======================================================")

    print("Loading faster-whisper model (tiny, int8 CPU)...")
    model = WhisperModel("tiny", device="cpu", compute_type="int8")

    for clip in CLIPS:
        process_podcast_clip(clip, model)

    print("\n=======================================================")
    print("✅ All 3 Raj Shamani Viral Shorts Rendered & Queued!")
    for clip in CLIPS:
        f = os.path.join(WORKSPACE_DIR, f"viral_short_{clip['id']}.mp4")
        if os.path.exists(f):
            mb = os.path.getsize(f) / (1024 * 1024)
            print(f"   📦 viral_short_{clip['id']}.mp4 ({mb:.2f} MB) -> {clip['slot']}")

if __name__ == "__main__":
    main()
