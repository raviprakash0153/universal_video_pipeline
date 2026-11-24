"""Read-only metadata inspection for a YouTube URL."""
import argparse
import json
import re
import subprocess
from urllib.parse import urlparse, parse_qs

from source_video_quality import quality_format_selector, validate_video_formats, yt_dlp_runtime_options


def validate_youtube_url(url: str) -> None:
    parsed = urlparse(url)
    valid_host = parsed.hostname in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtube-nocookie.com"}
    video_id = (parse_qs(parsed.query).get("v") or [""])[0] if parsed.hostname != "youtu.be" else parsed.path.strip("/")
    if parsed.scheme != "https" or not valid_host or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Malformed YouTube URL. Use a full https://www.youtube.com/watch?v=VIDEO_ID URL.")


def inspect_url(url: str, runner=subprocess.run) -> dict:
    """Fetch metadata only; raises a clear error if inspection cannot complete."""
    validate_youtube_url(url)
    command = ["yt-dlp", *yt_dlp_runtime_options(), "--no-warnings", "--format",
               quality_format_selector(), "--dump-single-json", "--no-download", url]
    try:
        result = runner(command, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        metadata = json.loads(result.stdout)
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or "").lower()
        if any(term in details for term in ("name or service not known", "temporary failure in name resolution", "nodename nor servname", "getaddrinfo")):
            raise RuntimeError("Network/DNS error: could not reach YouTube, so metadata was not inspected.") from exc
        if any(term in details for term in ("private video", "sign in", "members-only", "login required", "age-restricted")):
            raise RuntimeError("Video is private or restricted; metadata could not be inspected.") from exc
        if any(term in details for term in ("video unavailable", "has been removed", "does not exist")):
            raise RuntimeError("Video is unavailable; metadata could not be inspected.") from exc
        raise RuntimeError("yt-dlp could not inspect this URL; metadata was not retrieved.") from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Metadata inspection failed: {exc}") from exc

    formats = metadata.get("formats") or []
    video_formats = [f for f in formats if f.get("vcodec") not in (None, "none") and isinstance(f.get("height"), (int, float))]
    heights = sorted({int(f["height"]) for f in video_formats}, reverse=True)
    audio_formats = sorted({str(f.get("acodec")) + (f" ({f.get('ext')})" if f.get("ext") else "")
                            for f in formats if f.get("acodec") not in (None, "none")})
    has_720 = any(f["height"] >= 720 for f in video_formats)
    try:
        best_eligible = validate_video_formats(metadata)
        eligible, reason = True, "meets the existing quality eligibility rules"
    except ValueError:
        best_eligible, eligible, reason = None, False, "does not meet the existing quality eligibility rules"
    return {"title": metadata.get("title"), "duration": metadata.get("duration"), "resolutions": heights,
            "has_720p_video": has_720, "best_eligible": best_eligible,
            "audio_formats": audio_formats, "eligible": eligible, "eligibility_reason": reason}


def format_report(info: dict) -> str:
    duration = info["duration"]
    duration_text = f"{int(duration // 60)}m {int(duration % 60)}s" if isinstance(duration, (int, float)) else "not available"
    return "\n".join([
        "Metadata inspection: successful",
        f"Title: {info['title'] or 'not available'}",
        f"Duration: {duration_text}",
        f"Video resolutions: {', '.join(f'{h}p' for h in info['resolutions']) or 'none listed'}",
        f"Video stream at least 720p: {'yes' if info['has_720p_video'] else 'no'}",
        f"Best eligible resolution: {str(info['best_eligible']) + 'p' if info['best_eligible'] else 'none'}",
        f"Audio formats: {', '.join(info['audio_formats']) or 'none listed'}",
        f"Source eligibility: {'eligible' if info['eligible'] else 'not eligible'} ({info['eligibility_reason']})",
    ])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Inspect YouTube metadata without downloading media.")
    parser.add_argument("url", help="YouTube video URL")
    args = parser.parse_args(argv)
    try:
        print(format_report(inspect_url(args.url)))
    except (ValueError, RuntimeError) as exc:
        parser.exit(2, f"Inspection failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
