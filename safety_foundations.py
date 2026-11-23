"""Safety primitives shared by source intake and platform publishing."""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class RightsState(str, Enum):
    UNKNOWN = "unknown"
    OWNED = "owned"
    LICENSED = "licensed"
    PERMISSION_GRANTED = "permission_granted"
    PUBLIC_DOMAIN = "public_domain"
    PROHIBITED = "prohibited"


REUSE_RIGHTS = {RightsState.OWNED, RightsState.LICENSED,
                RightsState.PERMISSION_GRANTED, RightsState.PUBLIC_DOMAIN}


def enforce_reuse_rights(state: str | RightsState) -> None:
    try:
        normalized = RightsState(state)
    except ValueError as exc:
        raise ValueError(f"Unknown source rights state: {state}") from exc
    if normalized not in REUSE_RIGHTS:
        raise PermissionError(f"Source rights state '{normalized.value}' does not authorize reuse or publishing.")


def resolve_local_input(path: str | Path, *, min_height: int = 720) -> dict[str, Any]:
    """Probe a local video and reject missing, malformed, or sub-720p inputs."""
    return probe_video(path, min_height=min_height)


def probe_video(path: str | Path, *, min_height: int = 720) -> dict[str, Any]:
    """Probe a resolved local media file and enforce source dimensions after download."""
    source = Path(path).expanduser().resolve(strict=True)
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "json", str(source)],
        check=True, text=True, capture_output=True,
    )
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise ValueError("Resolved input has no video stream.")
    width, height = int(streams[0].get("width", 0)), int(streams[0].get("height", 0))
    if width < 1 or height < 1:
        raise ValueError("Resolved input has invalid video dimensions.")
    if min(width, height) < min_height:
        raise ValueError(f"Resolved input resolution {width}x{height} is below the {min_height}p minimum.")
    return {"path": str(source), "width": width, "height": height,
            "orientation": "portrait" if height > width else "landscape" if width > height else "square"}


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def metadata_sha256(metadata: dict[str, Any]) -> str:
    payload = json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PublishingApproval:
    file_sha256: str
    metadata_sha256: str
    approved: bool
    approver: str
    approved_at: str


def validate_approval(path: str | Path, metadata: dict[str, Any]) -> PublishingApproval:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    approval = PublishingApproval(**data)
    if not approval.approved:
        raise PermissionError("Publishing approval is not granted.")
    media_path = Path(metadata["media_file"])
    if not media_path.is_absolute():
        media_path = Path(path).parent / media_path
    if approval.file_sha256 != file_sha256(media_path):
        raise PermissionError("Publishing approval does not match the current media file SHA-256.")
    if approval.metadata_sha256 != metadata_sha256(metadata):
        raise PermissionError("Publishing approval does not match current metadata.")
    return approval


def write_approval(path: str | Path, media_path: str | Path, metadata: dict[str, Any],
                   *, approver: str, approved_at: str, approved: bool = True) -> None:
    """Persist an explicit human decision bound to the exact file and metadata."""
    record = PublishingApproval(file_sha256(media_path), metadata_sha256(metadata),
                                approved, approver, approved_at)
    Path(path).write_text(json.dumps(asdict(record), indent=2) + "\n", encoding="utf-8")
