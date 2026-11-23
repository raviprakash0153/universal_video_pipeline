#!/usr/bin/env python3
"""Transparent, local-first ranking of possible gaming moments.

Input is a JSON transcript (word records with start/end/text) and optional audio
window metrics/yt-dlp metadata. This module never acquires media or transcribes.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from source_video_quality import (quality_format_selector, validate_video_formats,
                                  yt_dlp_runtime_options)


@dataclass
class AnalyzerConfig:
    clip_duration: float = 30.0
    min_duration: float = 12.0
    max_duration: float = 45.0
    min_separation: float = 8.0
    context_seconds: float = 12.0
    max_candidates: int = 10


@dataclass
class Candidate:
    id: str
    start: float
    end: float
    duration: float
    transcript_evidence: list[str]
    audio_evidence: list[str]
    engagement_evidence: list[str]
    transcript_score: float
    audio_score: float
    engagement_score: float
    overall_score: float
    confidence: str
    explanation: str
    quality_issues: list[str] = field(default_factory=list)


# Multilingual cues are interpretable signals; contextual sentence scoring below
# means a single cue alone is not enough to label a moment as high confidence.
_CUES = {
    "excitement": ("wow", "whoa", "no way", "let's go", "insane", "crazy", "unbelievable", "gg", "clutch", "वाह", "अरे", "क्या", "भाई", "मजा", "जीत", "कर दिया", "bach gaya", "kya", "bhai", "mazza", "jeet", "kar diya", "bach gaya", "arey", "op", "one tap", "knocked", "squad wipe", "booyah", "chicken dinner", "kya gameplay", "bachke", "save kar liya"),
    "humor": ("haha", "hahaha", "lol", "lmao", "funny", "joke", "😂", "हंस", "मजाक", "हँसी", "haso", "mazaak", "hasna", "troll", "gaand phat", "roasted"),
    "surprise": ("what", "how", "unexpected", "surprise", "suddenly", "seriously", "really", "कैसे", "अचानक", "सच", "अरे यार", "kaise", "achanak", "sach", "where did", "didn't expect", "peeche se", "yeh kaise"),
    "combat": ("fight", "enemy", "kill", "shot", "headshot", "attack", "मार", "दुश्मन", "गोली", "हमला", "maar", "dushman", "goli", "attack", "knock", "revive", "push karo", "cover fire", "reload", "sniper", "spray", "one shot", "finish kar", "maar diya", "down kar diya"),
    "celebration": ("won", "winner", "victory", "we did it", "let's go", "जीत गए", "jeet gaye", "winner winner", "booyah", "chicken dinner", "rank push", "match jeet"),
}
_EMOTION = ("amazing", "awesome", "terrible", "oh my god", "oh god", "oh no", "damn", "shit", "fuck", "मस्त", "बाप रे", "अरे बाप", "khatam", "bachao", "holy shit", "what a shot", "kya baat", "arey bhai", "pagalpan", "too good")


def _contains_cue(text: str, cue: str) -> bool:
    """Match whole words/phrases, avoiding incidental substrings (e.g. 'wow' in 'wowza')."""
    escaped = re.escape(cue.casefold()).replace(r"\ ", r"\s+")
    return re.search(r"(?<!\w)" + escaped + r"(?!\w)", text, flags=re.UNICODE) is not None


def _valid_time(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite non-negative number") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} must be a finite non-negative number")
    return number


def normalize_words(transcript: Iterable[dict] | dict) -> list[dict]:
    if isinstance(transcript, dict):
        transcript = transcript.get("words", [])
    words = []
    for item in transcript:
        if isinstance(item, dict):
            raw_start, raw_end = item.get("start"), item.get("end")
            raw_text = item.get("text", item.get("word", ""))
        else:
            # video_pipeline.transcribe already returns (start, end, token) tuples.
            try:
                raw_start, raw_end, raw_text = item
            except (TypeError, ValueError) as exc:
                raise ValueError("transcript words must be mappings or (start, end, text) tuples") from exc
        start, end = _valid_time(raw_start, "word start"), _valid_time(raw_end, "word end")
        if end <= start:
            raise ValueError("word end timestamp must be after start")
        text = str(raw_text).strip()
        if text:
            words.append({"start": start, "end": end, "text": text})
    return sorted(words, key=lambda w: (w["start"], w["end"]))


def transcription_quality_issues(transcript: Iterable[dict] | dict, *, max_word_duration: float = 2.5) -> list[str]:
    """Describe suspicious word alignment/script output without rewriting recognized text."""
    words = normalize_words(transcript)
    issues = []
    for word in words:
        span = word["end"] - word["start"]
        if span > max_word_duration:
            issues.append(f"token {word['text']!r} spans {span:.2f}s (over {max_word_duration:.2f}s)")
    # Hindi/Hinglish commonly alternates Devanagari and Latin. Cyrillic output is
    # sufficiently unusual for this pipeline to flag, while retaining the token.
    cyrillic = [word for word in words if re.search(r"[\u0400-\u04ff]", word["text"])]
    for word in cyrillic:
        issues.append(f"unexpected Cyrillic script in token {word['text']!r}")
    return issues


def _word_quality_reasons(word: dict, *, max_word_duration: float = 2.5) -> list[str]:
    reasons = []
    span = word["end"] - word["start"]
    if span > max_word_duration:
        reasons.append(f"token spans {span:.2f}s (over {max_word_duration:.2f}s)")
    if re.search(r"[\u0400-\u04ff]", word["text"]):
        reasons.append("unexpected Cyrillic script")
    return reasons


def detect_audio_spikes(windows: Iterable[dict]) -> list[dict]:
    """Find windows unusually loud versus robust median/MAD; input may include speech/laugh flags."""
    rows = []
    for row in windows:
        start, end = _valid_time(row.get("start"), "audio start"), _valid_time(row.get("end"), "audio end")
        if end <= start:
            raise ValueError("audio end timestamp must be after start")
        try:
            energy = float(row.get("rms", row.get("energy")))
        except (TypeError, ValueError):
            continue
        if math.isfinite(energy):
            rows.append({**row, "start": start, "end": end, "rms": max(0.0, energy)})
    if len(rows) < 3:
        return []
    values = sorted(r["rms"] for r in rows)
    median = values[len(values) // 2]
    deviations = sorted(abs(v - median) for v in values)
    mad = deviations[len(deviations) // 2]
    threshold = max(median * 1.8, median + 3 * mad, 1e-6)
    return [r for r in rows if r["rms"] >= threshold and r["rms"] > median]


def _audio_score(start: float, end: float, audio: Iterable[dict]) -> tuple[float, list[str]]:
    score, evidence = 0.0, []
    for row in audio:
        if row["start"] < end and row["end"] > start:
            if row.get("spike"):
                score = max(score, 0.65)
                evidence.append("unusually high audio energy")
            if row.get("laughter"):
                score = max(score, 0.8)
                evidence.append("laughter detected/annotated")
            if row.get("shouting") or row.get("excited_speech"):
                score = max(score, 0.8)
                evidence.append("shouting or excited speech annotated")
    return min(score, 1.0), list(dict.fromkeys(evidence))


def _engagement_score(start: float, end: float, metadata: dict | None) -> tuple[float, list[str]]:
    """Use only supplied public metadata; no inferred/private retention signals."""
    if not metadata:
        return 0.0, []
    evidence, score = [], 0.0
    # Like/view ratio describes the whole upload. It cannot distinguish timestamps.
    chapters = metadata.get("chapters")
    if isinstance(chapters, (list, tuple)):
        for chapter in chapters:
            if not isinstance(chapter, dict):
                continue
            try:
                chapter_start = _valid_time(chapter.get("start_time"), "chapter start")
                chapter_end = _valid_time(chapter.get("end_time"), "chapter end")
            except (TypeError, ValueError):
                continue
            if chapter_end <= chapter_start:
                continue
            if chapter_start < end and chapter_end > start:
                evidence.append("overlaps a publicly listed chapter")
                score = 0.15
                break
    return score, evidence


def _evidence(words: list[dict], start: float, end: float) -> tuple[float, list[str]]:
    text = " ".join(w["text"] for w in words if w["start"] < end and w["end"] > start)
    lowered = text.casefold()
    matched = [name for name, cues in _CUES.items() if any(_contains_cue(lowered, cue) for cue in cues)]
    emotional = any(_contains_cue(lowered, cue) for cue in _EMOTION)
    # Reward different contextual signal families, moderate intensity repetition,
    # and transcript density; cap at one. Generic isolated words score modestly.
    score = min(1.0, 0.12 + 0.22 * len(matched) + 0.13 * emotional + 0.08 * (len(matched) >= 2)) if matched or emotional else 0.0
    snippets = []
    if text:
        snippets.append(text[:360])
    if matched:
        snippets.append("context cues: " + ", ".join(matched))
    return score, snippets


def _merge_overlapping(candidates: list[Candidate], config: AnalyzerConfig) -> list[Candidate]:
    """Combine intersecting candidate windows into evidence-rich moments."""
    ordered = sorted(candidates, key=lambda c: (c.start, c.end))
    groups: list[list[Candidate]] = []
    for candidate in ordered:
        if groups and candidate.start < max(item.end for item in groups[-1]):
            groups[-1].append(candidate)
        else:
            groups.append([candidate])

    confidence_rank = {"low": 0, "review": 1, "high": 2}
    merged = []
    for group in groups:
        start = min(c.start for c in group)
        end = max(c.end for c in group)
        # Preserve the configured maximum duration for long chains of overlaps.
        if end - start > config.max_duration:
            midpoint = (start + end) / 2
            start, end = midpoint - config.max_duration / 2, midpoint + config.max_duration / 2
        start = max(0.0, start)
        end = min(end, max((c.end for c in group), default=end))
        # Shift a capped interval back inside the video-edge candidate bounds.
        if end - start > config.max_duration:
            start = end - config.max_duration
        template = max(group, key=lambda c: (c.overall_score, confidence_rank.get(c.confidence, -1)))
        if len(group) == 1 and start == template.start and end == template.end:
            merged.append(template)
            continue

        def combine(field_name):
            return list(dict.fromkeys(value for c in group for value in getattr(c, field_name)))

        ts = max(c.transcript_score for c in group)
        aus = max(c.audio_score for c in group)
        ens = max(c.engagement_score for c in group)
        overall = max(c.overall_score for c in group)
        confidence = max((c.confidence for c in group), key=lambda value: confidence_rank.get(value, -1))
        quality_issues = combine("quality_issues")
        if quality_issues:
            confidence = "review"
        explanation = (f"Transcript {ts:.2f}; audio {aus:.2f}; engagement {ens:.2f}. "
                       "Evidence combined from overlapping candidate windows.")
        if quality_issues:
            explanation += " Suspicious transcript alignment/script overlaps this candidate; human review required."
        merged.append(Candidate("", round(start, 3), round(end, 3), round(end - start, 3),
                                combine("transcript_evidence"), combine("audio_evidence"),
                                combine("engagement_evidence"), ts, aus, ens, overall,
                                confidence, explanation, quality_issues))
    return merged


def analyze_transcript(transcript: Iterable[dict] | dict, *, duration: float | None = None,
                       audio_windows: Iterable[dict] | None = None, engagement: dict | None = None,
                       config: AnalyzerConfig | None = None) -> list[Candidate]:
    config = config or AnalyzerConfig()
    words = normalize_words(transcript)
    if not words:
        return []
    flagged = [(word, _word_quality_reasons(word)) for word in words]
    suspicious = [(word, reasons) for word, reasons in flagged if reasons]
    usable_words = [word for word, reasons in flagged if not reasons]
    if duration is None:
        duration = max(w["end"] for w in words)
    duration = _valid_time(duration, "video duration")
    audio = list(audio_windows or [])
    spikes = detect_audio_spikes([a for a in audio if a.get("rms") is not None or a.get("energy") is not None])
    for row in audio:
        row.setdefault("spike", row in spikes)
    # Score context-centered windows on transcript word times, never invented.
    centers: set[float] = set()
    for w in usable_words:
        score, _ = _evidence(usable_words, max(0, w["start"] - config.context_seconds / 2), w["end"] + config.context_seconds / 2)
        if score:
            centers.add((w["start"] + w["end"]) / 2)
    candidates = []
    for center in sorted(centers):
        length = min(config.max_duration, max(config.min_duration, config.clip_duration))
        start, end = max(0.0, center - length / 2), min(duration, center + length / 2)
        if end - start < config.min_duration:
            continue
        ts, transcript_ev = _evidence(usable_words, start, end)
        if ts <= 0:
            continue
        aus, audio_ev = _audio_score(start, end, audio)
        ens, eng_ev = _engagement_score(start, end, engagement)
        overall = round(100 * (0.65 * ts + 0.25 * aus + 0.10 * ens), 2)
        confidence = "high" if ts >= .65 and (aus >= .65 or ts >= .87) else "review" if ts >= .35 else "low"
        explanation = f"Transcript {ts:.2f}; audio {aus:.2f}; engagement {ens:.2f}. " + ("Multiple contextual signals support this moment." if ts >= .65 else "Limited contextual evidence; human review recommended.")
        affected = [f"{word['text']!r}: {', '.join(reasons)}" for word, reasons in suspicious
                    if word["start"] < end and word["end"] > start]
        if affected:
            confidence = "review"
            explanation += " Suspicious transcript alignment/script overlaps this candidate; human review required."
        candidates.append(Candidate("", round(start, 3), round(end, 3), round(end-start, 3), transcript_ev,
                                    audio_ev, eng_ev, round(ts, 3), round(aus, 3), round(ens, 3), overall,
                                    confidence, explanation, affected))
    candidates = _merge_overlapping(candidates, config)
    candidates.sort(key=lambda c: (-c.overall_score, c.start))
    selected = candidates[:config.max_candidates]
    selected.sort(key=lambda c: (-c.overall_score, c.start))
    for i, candidate in enumerate(selected, 1):
        candidate.id = f"moment-{i:03d}"
    return selected


def report(transcript: Any, *, duration=None, audio_windows=None, engagement=None, config=None) -> dict:
    candidates = analyze_transcript(transcript, duration=duration, audio_windows=audio_windows,
                                    engagement=engagement, config=config)
    words = normalize_words(transcript)
    quality = [{"start": w["start"], "end": w["end"], "text": w["text"],
                "issues": _word_quality_reasons(w)} for w in words if _word_quality_reasons(w)]
    return {"schema_version": 1, "analysis": "transcript and supplied supporting signals; no private retention data",
            "transcription_quality_issues": quality,
            "candidates": [asdict(c) for c in candidates]}


def inspect_url_metadata(url: str) -> dict:
    """Explicit metadata-only yt-dlp inspection; no media is downloaded."""
    command = [sys.executable, "-m", "yt_dlp", *yt_dlp_runtime_options(),
               "--format", quality_format_selector(), "--dump-single-json", "--no-download", url]
    result = subprocess.run(command, check=True, text=True, stdout=subprocess.PIPE)
    data = json.loads(result.stdout)
    validate_video_formats(data)
    # Keep only fields yt-dlp can expose publicly. No audience retention endpoint exists here.
    return {key: data[key] for key in ("duration", "view_count", "like_count", "chapters", "title") if key in data}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Rank gaming moments from supplied transcript JSON (no media downloads).")
    parser.add_argument("--transcript", help="JSON file with words [{start,end,text}], or '-' for stdin")
    parser.add_argument("--metadata", help="Optional yt-dlp metadata JSON file")
    parser.add_argument("--audio-json", help="Optional JSON file with timestamped audio windows; analyzer does not extract audio")
    parser.add_argument("--url", help="Video URL: metadata-only inspection is not enabled in this first version")
    parser.add_argument("--duration", type=float)
    parser.add_argument("--clip-duration", type=float, default=30)
    parser.add_argument("--min-separation", type=float, default=8)
    parser.add_argument("--output", default="moment_report.json")
    args = parser.parse_args(argv)
    if args.url:
        print("Inspecting public metadata only; video/audio files will not be downloaded.", file=sys.stderr)
    if not args.transcript:
        parser.error("--transcript is required; no media is downloaded or transcribed automatically")
    def load(path):
        return json.load(sys.stdin if path == "-" else open(path, encoding="utf-8"))
    transcript = load(args.transcript)
    metadata = load(args.metadata) if args.metadata else (inspect_url_metadata(args.url) if args.url else None)
    audio = load(args.audio_json) if args.audio_json else None
    result = report(transcript, duration=args.duration or (metadata or {}).get("duration"), audio_windows=audio,
                    engagement=metadata, config=AnalyzerConfig(clip_duration=args.clip_duration, min_separation=args.min_separation))
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Ranked {len(result['candidates'])} candidate moments:")
    for c in result["candidates"]:
        print(f"{c['id']} {c['start']:.1f}-{c['end']:.1f}s score={c['overall_score']:.1f} confidence={c['confidence']}: {c['explanation']}")
    print(f"JSON report: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
