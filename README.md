# Universal Video Pipeline

A local, cross-platform Python CLI for producing:
- YouTube Shorts in 9:16 (1080x1920), with optional word-highlight karaoke captions.
- Long-form videos in 16:9 (1920x1080).
- Optional YouTube uploads using OAuth.

No Antigravity, Docker, Node.js path, or external repository is required.

## Important security note
Do not copy the uploaded `client_secret.json` or `token.json` into this project. Those files contained OAuth credentials and were exposed in this chat. Revoke the existing Google OAuth grant, rotate/delete the exposed OAuth client secret in Google Cloud, create a new OAuth Client ID of type **Desktop app**, and save its JSON here as `client_secret.json`. The pipeline creates its own `youtube_token.json` after a new browser authorization. Keep all three files private and out of Git/cloud sharing.

## 1. Install prerequisites

Install Python 3.10+ and FFmpeg (both `ffmpeg` and `ffprobe` must work in Terminal).

macOS with Homebrew:
```bash
brew install ffmpeg
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Windows:
```powershell
py -3 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```
Install FFmpeg separately and ensure `ffmpeg` and `ffprobe` are on PATH.

Linux: install FFmpeg with your distribution package manager, then create the venv and install requirements.

## 2. Make Shorts

One clip:
```bash
python video_pipeline.py shorts --source "YOUTUBE_URL" --clip 00:02:10-00:02:45
```

Three manually selected clips:
```bash
python video_pipeline.py shorts --source "YOUTUBE_URL" \
  --clip 00:02:10-00:02:45 \
  --clip 00:08:20-00:08:55 \
  --clip 00:14:00-00:14:35
```

Create evenly spaced candidate clips (not a true viral-moment detector):
```bash
python video_pipeline.py shorts --source "YOUTUBE_URL" --auto-clips 3 --clip-length 35
```

Disable subtitles:
```bash
python video_pipeline.py shorts --source "input.mp4" --clip 00:00:20-00:00:55 --no-subtitles
```

## 3. Make a long-form 16:9 video

Whole source:
```bash
python video_pipeline.py long --source "YOUTUBE_URL"
```

Trim a section:
```bash
python video_pipeline.py long --source "YOUTUBE_URL" --start 00:12:00 --end 00:23:30
```

## 4. Upload to YouTube

Enable YouTube Data API v3 in your Google Cloud project. Configure a Desktop app OAuth client, save its JSON as `client_secret.json`, and run:
```bash
python video_pipeline.py shorts --source "YOUTUBE_URL" \
  --clip 00:02:10-00:02:45 --upload --privacy private
```
The first upload opens a browser authorization window. Start with `--privacy private`; review the video in YouTube Studio before making it public.

## 5. Output folders

- `output/`: final MP4 deliverables. They are not auto-deleted.
- `work/`: downloaded source, intermediate trim and temporary subtitle files.
- `youtube_token.json`: new OAuth token created after authorization.

Do not run aggressive cleanup while a render is active. Delete `work/` manually after confirming the final outputs.

## Important limitations / design choices

- The automatic clip option spaces clips across the source; it does **not** claim to detect viral moments. Real viral-moment selection requires transcript/semantic scoring and human review.
- The Shorts layout uses a blurred full-screen background with a centered horizontal foreground. It does not automatically detect or isolate a facecam. A facecam-specific layout should be configured for the exact source layout or added using face detection.
- The pipeline re-encodes audio and video to normalize timestamps and avoid independently copying DASH audio/video segments.
- The output is H.264/AAC MP4. The Intel Mac can render on CPU; expect slower processing than a modern GPU.
- Only download and republish content you own or have permission to use. Editing alone does not establish copyright permission.
