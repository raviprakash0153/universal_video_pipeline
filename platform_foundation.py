"""Shared platform job records, cautious capability registry, and packaging."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from safety_foundations import file_sha256


PLATFORMS = (
    "youtube", "instagram", "facebook", "x", "threads", "linkedin",
    "pinterest", "snapchat", "reddit", "bluesky", "telegram", "whatsapp",
    "moj", "josh", "sharechat",
)


class Capability(str, Enum):
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class PlatformCapability:
    platform: str
    official_api: str = Capability.UNKNOWN.value
    access_cost: str = Capability.UNKNOWN.value
    credentials: list[str] = field(default_factory=list)
    account_type: str = Capability.UNKNOWN.value
    app_approval: str = Capability.UNKNOWN.value
    supported_formats: list[str] = field(default_factory=list)
    video_limits: dict[str, Any] = field(default_factory=dict)
    publishing: str = Capability.UNKNOWN.value
    analytics: str = Capability.UNKNOWN.value
    public_hosting_required: str = Capability.UNKNOWN.value
    manual_fallback: str = Capability.UNKNOWN.value


CAPABILITY_REGISTRY = {name: PlatformCapability(platform=name) for name in PLATFORMS}


@dataclass
class PlatformOutput:
    platform: str
    media_file_path: str
    sha256: str
    metadata: dict[str, Any]
    format_validation: dict[str, Any] = field(default_factory=dict)
    approval_state: str = "not_requested"
    publishing_method: str = "manual"
    publishing_status: str = "not_started"
    platform_post_id: str | None = None
    platform_post_url: str | None = None
    analytics_availability: str = "unknown"
    analytics_retrieval_status: str = "not_requested"

    @classmethod
    def for_media(cls, platform: str, media_file_path: str | Path,
                  metadata: dict[str, Any]) -> "PlatformOutput":
        if platform not in CAPABILITY_REGISTRY:
            raise ValueError(f"Unsupported platform: {platform}")
        path = Path(media_file_path)
        return cls(platform, str(path), file_sha256(path), dict(metadata))


@dataclass
class ContentJob:
    job_id: str
    source_path: str
    rights_state: str = "unknown"
    outputs: dict[str, PlatformOutput] = field(default_factory=dict)
    status: str = "draft"

    def add_output(self, output: PlatformOutput) -> None:
        if output.platform not in CAPABILITY_REGISTRY:
            raise ValueError(f"Unsupported platform: {output.platform}")
        self.outputs[output.platform] = output


def reject_paid_api(capability: PlatformCapability, *, allow_paid: bool = False) -> None:
    if capability.access_cost == "paid" and not allow_paid:
        raise PermissionError(f"Paid API access is disabled for {capability.platform}.")


def build_platform_packages(platforms: list[str], builder: Callable[[str], Any]) -> dict[str, Any]:
    """Build requested platform packages independently; retain per-platform errors."""
    results: dict[str, Any] = {}
    for platform in platforms:
        if platform not in CAPABILITY_REGISTRY:
            results[platform] = {"status": "failed", "error": "unsupported platform"}
            continue
        try:
            results[platform] = {"status": "ready", "package": builder(platform)}
        except Exception as exc:
            results[platform] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    return results


def create_content_package(*, platform: str, vertical_video: str | None = None,
                           landscape_video: str | None = None,
                           caption: str = "", description: str = "", hook: str = "",
                           text_post: str = "", hashtags: list[str] | None = None,
                           cover_path: str | None = None, thumbnail_path: str | None = None,
                           manual_instructions: str = "") -> dict[str, Any]:
    if platform not in CAPABILITY_REGISTRY:
        raise ValueError(f"Unsupported platform: {platform}")
    if not (vertical_video or landscape_video or text_post):
        raise ValueError("A package needs at least one video variant or a text post.")
    return {"platform": platform, "media_variants": {
        "vertical": vertical_video, "landscape": landscape_video},
        "caption": caption, "description": description, "hook": hook,
        "text_post": text_post, "hashtags": list(hashtags or []),
        "cover_path": cover_path, "thumbnail_path": thumbnail_path,
        "manual_instructions": manual_instructions}
