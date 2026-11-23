"""Shared, side-effect-free command normalization for future assistant adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import re
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4


class InputType(str, Enum):
    TEXT = "text"
    URL = "url"
    TRANSCRIBED_VOICE = "transcribed_voice"


class Intent(str, Enum):
    NEWS_DISCOVERY = "news_discovery"
    NEWS_VERIFICATION = "news_verification"
    VIDEO_PROCESSING = "video_processing"
    UNKNOWN = "unknown"


class OutputFormat(str, Enum):
    SHORTS = "shorts"
    LONG = "long"
    BOTH = "both"


@dataclass
class CommandJob:
    job_id: str
    input_type: InputType
    raw_input: str
    intent: Intent
    source_url: str | None = None
    topic_or_claim: str | None = None
    output_format: OutputFormat | None = None
    requested_shorts_count: int | None = None
    caption_language: str = "original"
    rights_confirmed: bool | None = None
    status: str = "pending_clarification"
    approval_state: str = "not_requested"
    clarification_needed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in ("input_type", "intent", "output_format"):
            value = getattr(self, key)
            result[key] = value.value if value is not None else None
        return result


_URL_RE = re.compile(r"https?://[^\s<>\]\[\)\}\,\;]+", re.IGNORECASE)
_SHORTS_RE = re.compile(r"\b(\d+)\s+shorts?\b", re.IGNORECASE)
_TOPIC_RE = re.compile(r"\b(?:about|on|regarding)\s+(.+?)[.!?]*$", re.IGNORECASE)


def _normalize_for_analysis(text: str) -> str:
    """Map common Hindi/Hinglish command words and ASR spellings for matching only."""
    value = text.casefold()
    # Whisper often emits English words as approximate Devanagari phonetics.
    replacements = {
        "जीटिए": "gta", "जी टी ए": "gta", "जीटीए": "gta",
        "सिक्स": "6", "सिक्स्थ": "6",
        "लेटिस": "latest", "लेटेस्ट": "latest", "लेटेस्ट": "latest",
        "नूज": "news", "न्यूज़": "news", "न्यूज": "news", "न्यूज़": "news",
        "खबर": "news", "ख़बर": "news", "खबरें": "news", "समाचार": "news",
        "स्फेच": "fetch", "फेच": "fetch", "फेच": "fetch",
        "बताओ": "tell", "बताइए": "tell", "बता": "tell",
        "ताज़ा": "latest", "ताजा": "latest", "नयी": "new", "नई": "new",
        "अपडेट": "update", "अब्टेट": "update", "अपडेट्स": "update",
        "क्या": "what", "है": "is", "आया": "came", "आई": "came",
        "करो": "do", "की": "of", "का": "of", "के": "of",
        "बारे में": "about", "के बारे में": "about", "मुझे": "me",
    }
    # Longer phrases first to prevent a component word from consuming them.
    for source in sorted(replacements, key=len, reverse=True):
        value = value.replace(source, replacements[source])
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    # Frequent Romanized command words are normalized to stable English equivalents.
    value = re.sub(r"\b(batao|btao|batado|bata|bta)\b", "tell", value)
    value = re.sub(r"\b(karo|kro|kar do)\b", "do", value)
    value = re.sub(r"\b(taaza|taza|nayi|nai|naya|naye)\b", "latest", value)
    value = re.sub(r"\b(khabar|khabrein|khabre|samachar)\b", "news", value)
    value = re.sub(r"\b(ke baare mein|ke bare mein|ke baare me|ke bare me)\b", "about", value)
    value = re.sub(r"\b(kya|kaisi|kaisa)\b", "what", value)
    value = re.sub(r"\b(ki|ka|ke)\b", "of", value)
    value = re.sub(r"\b(aaya|aya|aayi|ayi)\b", "came", value)
    # GTA 6 is a well-known entity; require both phonetic name components.
    value = re.sub(r"\bgta\s+6\b", "gta 6", value)
    return value


def _discovery_topic(raw: str, normalized: str, source_url: str | None) -> str | None:
    if source_url:
        normalized = _URL_RE.sub("", normalized)
    # Clear topic anchors used in English and Hinglish ("GTA 6 ki latest news").
    patterns = (
        r"\b(?:about|on|regarding)\s+(.+?)(?=\s+(?:latest|new|news|update|tell|fetch|find|what|is|came|and|then)\b|$)",
        r"\b(.+?)\s+of\s+(?=(?:latest|new|news|update)\b)",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized, re.I)
        if match:
            candidate = match.group(1).strip()
            candidate = re.sub(r"^(?:tell|me|fetch|find|latest|news|what|new)\s+", "", candidate, flags=re.I)
            if candidate in {"news", "update", "latest", "new", "what"}:
                continue
            # Only accept an unanchored entity form we can resolve confidently.
            if re.fullmatch(r"gta\s+6", candidate, re.I):
                return "GTA 6"
            if candidate and len(candidate.split()) <= 8:
                return _display_topic(candidate, raw)
    # Recognize the phonetic GTA 6 alias only when both parts survived normalization.
    if re.search(r"\bgta\s+6\b", normalized, re.I):
        return "GTA 6"
    # Retain original English extraction behavior for the established anchor form.
    match = _TOPIC_RE.search(raw)
    return match.group(1).strip(" \t\n.,!?;:") if match else None


def _display_topic(value: str, raw: str | None = None) -> str:
    value = value.strip(" \t\n.,!?;:")
    if value.casefold() == "gta 6":
        return "GTA 6"
    if raw:
        match = re.search(re.escape(value), raw, re.I)
        if match:
            return match.group(0).strip(" \t\n.,!?;:")
    return value


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def process_command(text: str, *, input_type: InputType | str = InputType.TEXT) -> CommandJob:
    """Turn text or a transcript into a job description; never performs work."""
    input_type = InputType(input_type)
    raw = (text or "").strip()
    url_match = _URL_RE.search(raw)
    source_url = url_match.group(0).rstrip(".!?") if url_match else None
    if source_url and not _valid_http_url(source_url):
        source_url = None
    if source_url and raw.strip().rstrip(".!?") == source_url and input_type == InputType.TEXT:
        input_type = InputType.URL

    normalized = raw.lower()
    analysis_text = _normalize_for_analysis(raw)
    clarification: list[str] = []
    fmt: OutputFormat | None = None
    count: int | None = None
    topic: str | None = None

    officiality_question = (re.search(r"\b(official|legit|real)\b", analysis_text)
                            and re.search(r"\b(announcement|announced|news|claim)\b", analysis_text))
    if re.search(r"\b(verify|check|fact.?check)\b", analysis_text) or officiality_question:
        intent = Intent.NEWS_VERIFICATION
        topic = _topic_from(raw, source_url)
        if not topic and not source_url:
            clarification.append("What news or claim should be verified?")
    elif (
        re.search(r"\b(fetch|find|latest|news|headlines|discover|update)\b", analysis_text)
        or (re.search(r"\btell\b", analysis_text) and re.search(r"\b(news|latest|update|headlines)\b", analysis_text))
    ) and not source_url:
        intent = Intent.NEWS_DISCOVERY
        topic = _discovery_topic(raw, analysis_text, source_url)
        if re.search(r"\bboth\b", analysis_text):
            fmt = OutputFormat.BOTH
        elif re.search(r"\blong(?:\s+video)?\b", analysis_text):
            fmt = OutputFormat.LONG
        elif re.search(r"\bshorts?\b", analysis_text):
            fmt = OutputFormat.SHORTS
        if not topic:
            clarification.append("What topic should I find news about?")
    elif source_url or re.search(r"\b(make|create|process|video|shorts?|long video|both formats)\b", analysis_text):
        intent = Intent.VIDEO_PROCESSING
        topic = _topic_from(raw, source_url)
        if re.search(r"\bboth\b", analysis_text):
            fmt = OutputFormat.BOTH
        elif re.search(r"\blong(?:\s+video)?\b", analysis_text):
            fmt = OutputFormat.LONG
        elif re.search(r"\bshorts?\b", analysis_text):
            fmt = OutputFormat.SHORTS
        count_match = _SHORTS_RE.search(raw)
        if count_match:
            count = int(count_match.group(1))
        if fmt is None:
            clarification.append("Which output format do you want: Shorts, long, or both?")
        if fmt == OutputFormat.SHORTS and count is None:
            clarification.append("How many Shorts do you want?")
        if source_url is None:
            clarification.append("Please provide the source video URL.")
    else:
        intent = Intent.UNKNOWN
        clarification.append("What would you like me to do?")

    caption_language = "hinglish" if re.search(r"\bhinglish\b", normalized) else "original"
    return CommandJob(
        job_id=str(uuid4()), input_type=input_type, raw_input=raw,
        intent=intent, source_url=source_url, topic_or_claim=topic,
        output_format=fmt, requested_shorts_count=count,
        caption_language=caption_language, rights_confirmed=None,
        status="pending_clarification" if clarification else "parsed",
        approval_state="not_requested", clarification_needed=clarification,
    )


def _topic_from(raw: str, source_url: str | None) -> str | None:
    without_url = _URL_RE.sub("", raw).strip(" \t\n.,!?;:") if source_url else raw
    match = _TOPIC_RE.search(without_url)
    if match:
        return match.group(1).strip(" \t\n.,!?;:") or None
    # A bare verification request is a claim in its own right, excluding command verbs.
    if re.search(r"\b(verify|check|fact.?check)\b", raw, re.I):
        claim = re.sub(r"^\s*(please\s+)?(verify|check|fact.?check)\s+(this\s+)?(news|claim)?\s*(i\s+heard\s*:?\s*)?(about|on|regarding)?\s*", "", without_url, flags=re.I)
        return claim.strip(" \t\n.,!?;:") or None
    if re.search(r"\b(official|legit|real)\b", raw, re.I):
        claim = re.sub(r"^\s*(is\s+this|is\s+that|kya)\s+", "", without_url, flags=re.I)
        claim = re.sub(r"\b(official|legit|real)\b", "", claim, flags=re.I)
        return claim.strip(" \t\n.,!?;:") or None
    return None
