"""Shared strict yt-dlp source resolution policy."""
import json
import subprocess


MIN_HEIGHT = 720


def quality_format_selector() -> str:
    """Select the best eligible video with the best audio, or a combined format."""
    return (
        "bv[height>=720]+ba/"
        "b[height>=720]"
    )


def validate_video_formats(metadata: dict) -> int:
    """Return best height usable by the selector, requiring video and audio."""
    formats = metadata.get("formats") or []
    audio_only = any(
        fmt.get("vcodec") == "none" and fmt.get("acodec") not in (None, "none")
        for fmt in formats
    )
    eligible = []
    for fmt in formats:
        height = fmt.get("height")
        if not isinstance(height, (int, float)) or height < MIN_HEIGHT:
            continue
        has_video = fmt.get("vcodec") not in (None, "none")
        has_audio = fmt.get("acodec") not in (None, "none")
        if has_video and (has_audio or (fmt.get("acodec") == "none" and audio_only)):
            eligible.append(height)
    if not eligible:
        raise ValueError(
            "Source has no video format with a known resolution of at least "
            f"{MIN_HEIGHT}p; refusing to download."
        )
    return max(eligible)


def inspect_source(url: str, command: list[str], selector: str = None) -> dict:
    """Run yt-dlp metadata inspection using the caller's runtime options."""
    format_args = ["--format", selector] if selector else []
    result = subprocess.run(
        [*command, *format_args, "--dump-single-json", "--no-download", url],
        check=True, text=True, stdout=subprocess.PIPE,
    )
    metadata = json.loads(result.stdout)
    validate_video_formats(metadata)
    return metadata
