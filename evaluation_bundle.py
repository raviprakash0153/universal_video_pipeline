"""Versioned, local-only containers for future independent evaluation.

Validation is structural only. It never accesses evidence references or assesses
source credibility, sourcing independence, annotation independence, or quality.
"""
from __future__ import annotations

from datetime import datetime
import ipaddress
import json
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any
from urllib.parse import urlsplit


SCHEMA_VERSION = "1.0.0"
ANNOTATION_STATES = {
    "categories": {"relevant", "not_relevant", "unknown", "not_assessable"},
    "geographic_relevance": {"india", "global", "both", "neither", "unknown", "not_assessable"},
    "primary_source": {"yes", "no", "unknown", "not_assessable"},
    "evidence_sufficiency": {"sufficient", "insufficient", "unknown", "not_assessable"},
    "corroboration": {"independently_corroborated", "not_corroborated", "unknown", "not_assessable"},
    "assessability": {"assessable", "partially_assessable", "not_assessable", "uncertain"},
    "source_identity": {"known", "unknown", "not_assessable"},
    "syndication": {"original", "syndicated", "not_syndicated", "unknown", "not_assessable"},
}
ANNOTATION_FIELDS = {
    "annotator_id", "story_group_id", "categories", "geographic_relevance",
    "primary_source", "evidence_sufficiency", "corroboration", "source_identity",
    "syndication", "assessability", "uncertainty",
}
PUBLISHER_IDENTITY_STATES = {"unknown", "not_assessable"}


class BundleValidationError(ValueError):
    """Raised when a bundle does not match the supported structural contract."""


class BundleExistsError(FileExistsError):
    """Raised when write_bundle would overwrite an existing destination."""


def _object(value: Any, where: str) -> dict:
    if type(value) is not dict or any(type(k) is not str for k in value):
        raise BundleValidationError(f"{where} must be an object with string keys")
    return value


def _required(obj: dict, fields: tuple[str, ...], where: str) -> None:
    missing = [key for key in fields if key not in obj]
    if missing:
        raise BundleValidationError(f"{where} missing required field(s): {', '.join(missing)}")


def _text(value: Any, where: str, *, allow_empty: bool = False) -> None:
    if type(value) is not str or (not allow_empty and not value.strip()):
        raise BundleValidationError(f"{where} must be {'a string' if allow_empty else 'a non-empty string'}")


def _json_value(value: Any, where: str, active: set[int] | None = None) -> None:
    """Reject Python-only objects recursively; JSON serialization must not leak errors."""
    if active is None:
        active = set()
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise BundleValidationError(f"{where} must be a finite JSON number")
        return
    if type(value) is list:
        marker = id(value)
        if marker in active:
            raise BundleValidationError(f"{where} contains a circular reference")
        active.add(marker)
        try:
            for i, member in enumerate(value):
                _json_value(member, f"{where}[{i}]", active)
        finally:
            active.remove(marker)
        return
    if type(value) is dict:
        _object(value, where)
        marker = id(value)
        if marker in active:
            raise BundleValidationError(f"{where} contains a circular reference")
        active.add(marker)
        try:
            for key, member in value.items():
                _json_value(member, f"{where}.{key}", active)
        finally:
            active.remove(marker)
        return
    raise BundleValidationError(f"{where} contains a non-JSON value")


def _timestamp(value: Any, where: str) -> None:
    _text(value, where)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError) as exc:
        raise BundleValidationError(f"{where} must be ISO 8601 with timezone information") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BundleValidationError(f"{where} must include timezone information")


def _http_url(value: Any, where: str) -> None:
    _text(value, where)
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise BundleValidationError(f"{where} must be a valid HTTP or HTTPS URL")
    try:
        parsed = urlsplit(value)
        # Accessing .port also detects malformed/out-of-range ports.
        _ = parsed.port
    except ValueError as exc:
        raise BundleValidationError(f"{where} must be a valid HTTP or HTTPS URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise BundleValidationError(f"{where} must be a valid HTTP or HTTPS URL with a host")
    hostname = _normalized_hostname(parsed.hostname, where)
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        labels = hostname.split(".")
        if any(not label or len(label) > 63 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label) for label in labels):
            raise BundleValidationError(f"{where} must have a valid host name")


def _normalized_hostname(hostname: str, where: str) -> str:
    """Normalize one DNS root dot and reject malformed dot placement."""
    hostname = hostname.lower()
    if hostname.endswith(".."):
        raise BundleValidationError(f"{where} must have a valid host name")
    if hostname.endswith("."):
        hostname = hostname[:-1]
    if not hostname or any(not label for label in hostname.split(".")):
        raise BundleValidationError(f"{where} must have a valid host name")
    return hostname


def _google_news_rss(value: str) -> bool:
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").lower()
    if hostname.endswith(".") and not hostname.endswith(".."):
        hostname = hostname[:-1]
    return hostname in {"news.google.com", "www.news.google.com"} and parsed.path.lower().startswith("/rss")


def _evidence_reference(value: Any, where: str) -> None:
    ref = _object(value, where)
    _required(ref, ("kind", "location"), where)
    _text(ref["kind"], f"{where}.kind")
    location = ref["location"]
    _text(location, f"{where}.location")
    # POSIX relative paths only; no empty, dot, traversal, drive, URL, or backslash forms.
    if (any(ord(character) < 32 for character in location) or "\\" in location or location.startswith("/")
            or re.match(r"^[A-Za-z]:", location) or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", location)):
        raise BundleValidationError(f"{where}.location must be a safe relative POSIX path")
    parts = location.split("/")
    if any(part in {"", ".", ".."} for part in parts) or PurePosixPath(location).is_absolute():
        raise BundleValidationError(f"{where}.location must be a safe relative POSIX path")


def validate_bundle(bundle: Any) -> dict:
    """Validate structure without accessing evidence or making external requests."""
    try:
        _json_value(bundle, "bundle")
    except RecursionError as exc:
        raise BundleValidationError("bundle nesting exceeds the supported depth") from exc
    b = _object(bundle, "bundle")
    _required(b, ("schema_version", "capture", "project", "scope", "discovery", "candidates", "evaluation"), "bundle")
    _text(b["schema_version"], "schema_version")
    if b["schema_version"] != SCHEMA_VERSION:
        raise BundleValidationError(f"unsupported schema_version: {b['schema_version']!r}; supported: {SCHEMA_VERSION}")
    capture = _object(b["capture"], "capture")
    _required(capture, ("timestamp", "timezone"), "capture")
    _timestamp(capture["timestamp"], "capture.timestamp")
    _text(capture["timezone"], "capture.timezone")
    project = _object(b["project"], "project")
    _required(project, ("code_version",), "project")
    _text(project["code_version"], "project.code_version")
    scope = _object(b["scope"], "scope")
    _required(scope, ("selected_categories", "coverage_mode"), "scope")
    if not isinstance(scope["selected_categories"], list) or any(not isinstance(x, str) or not x.strip() for x in scope["selected_categories"]):
        raise BundleValidationError("scope.selected_categories must be a list of non-empty strings")
    _text(scope["coverage_mode"], "scope.coverage_mode")
    if scope["coverage_mode"] not in {"india", "global", "india_and_global"}:
        raise BundleValidationError("scope.coverage_mode must be india, global, or india_and_global")
    discovery = _object(b["discovery"], "discovery")
    _required(discovery, ("queries", "metadata"), "discovery")
    if not isinstance(discovery["queries"], list):
        raise BundleValidationError("discovery.queries must be a list")
    for i, query in enumerate(discovery["queries"]):
        _object(query, f"discovery.queries[{i}]")
    _object(discovery["metadata"], "discovery.metadata")
    if not isinstance(b["candidates"], list):
        raise BundleValidationError("candidates must be a list")
    candidate_ids = set()
    for i, raw in enumerate(b["candidates"]):
        where = f"candidates[{i}]"
        c = _object(raw, where)
        _required(c, ("candidate_id", "discovery_provenance", "pipeline_predictions", "sources", "outcomes", "annotations"), where)
        _text(c["candidate_id"], f"{where}.candidate_id")
        if c["candidate_id"] in candidate_ids:
            raise BundleValidationError(f"duplicate candidate_id: {c['candidate_id']!r}")
        candidate_ids.add(c["candidate_id"])
        _object(c["discovery_provenance"], f"{where}.discovery_provenance")
        _object(c["pipeline_predictions"], f"{where}.pipeline_predictions")
        if not isinstance(c["sources"], list):
            raise BundleValidationError(f"{where}.sources must be a list (use [] when unavailable)")
        for j, raw_source in enumerate(c["sources"]):
            sw = f"{where}.sources[{j}]"
            s = _object(raw_source, sw)
            _required(s, ("discovery_source", "source_url", "resolved_url", "publisher", "retrieved_at", "http_outcome", "extraction_outcome", "evidence_reference"), sw)
            _text(s["discovery_source"], f"{sw}.discovery_source")
            for key in ("source_url", "resolved_url"):
                if s[key] is not None:
                    _http_url(s[key], f"{sw}.{key}")
            for key in ("retrieved_at",):
                if s[key] is not None:
                    _timestamp(s[key], f"{sw}.{key}")
            for key in ("http_outcome", "extraction_outcome"):
                if s[key] is not None:
                    _text(s[key], f"{sw}.{key}")
            if s["publisher"] is not None:
                pub = _object(s["publisher"], f"{sw}.publisher")
                _required(pub, ("name", "identity", "original_publisher"), f"{sw}.publisher")
                if set(pub) != {"name", "identity", "original_publisher"}:
                    raise BundleValidationError(f"{sw}.publisher has unexpected field(s)")
                _text(pub["name"], f"{sw}.publisher.name")
                identity = pub["identity"]
                _text(identity, f"{sw}.publisher.identity")
                if identity not in PUBLISHER_IDENTITY_STATES:
                    _http_url("https://" + identity, f"{sw}.publisher.identity")
                    identity_url = urlsplit("https://" + identity)
                    if identity_url.path not in {"", "/"} or identity_url.query or identity_url.fragment or identity_url.username or identity_url.password or identity_url.port:
                        raise BundleValidationError(f"{sw}.publisher.identity must be a host name or an allowed state")
                    identity_hostname = _normalized_hostname(identity_url.hostname or "", f"{sw}.publisher.identity")
                    if identity_hostname in {"news.google.com", "www.news.google.com"}:
                        raise BundleValidationError(f"{sw}.publisher.identity cannot identify Google News as publisher evidence")
                original = pub["original_publisher"]
                if original is not None:
                    _text(original, f"{sw}.publisher.original_publisher")
                urls = [s[k] for k in ("source_url", "resolved_url") if s[k] is not None]
                if any(_google_news_rss(url) for url in urls) and identity not in PUBLISHER_IDENTITY_STATES:
                    raise BundleValidationError(f"{sw}.publisher cannot identify Google News RSS as publisher evidence")
            if s["evidence_reference"] is not None:
                _evidence_reference(s["evidence_reference"], f"{sw}.evidence_reference")
        _object(c["outcomes"], f"{where}.outcomes")
        annotations = _object(c["annotations"], f"{where}.annotations")
        extra = set(annotations) - ANNOTATION_FIELDS
        if extra:
            raise BundleValidationError(f"{where}.annotations has unexpected field(s): {', '.join(sorted(extra))}")
        _required(annotations, tuple(sorted(ANNOTATION_FIELDS)), f"{where}.annotations")
        _text(annotations["annotator_id"], f"{where}.annotations.annotator_id")
        _text(annotations["story_group_id"], f"{where}.annotations.story_group_id")
        categories = _object(annotations["categories"], f"{where}.annotations.categories")
        for category, state in categories.items():
            _text(category, "annotation category")
            _text(state, f"annotation category {category}")
            if state not in ANNOTATION_STATES["categories"]:
                raise BundleValidationError(f"invalid category annotation state: {state!r}")
        for field, allowed in ANNOTATION_STATES.items():
            if field == "categories":
                continue
            state = annotations[field]
            _text(state, f"{where}.annotations.{field}")
            if state not in allowed:
                raise BundleValidationError(f"invalid annotation state: {field}")
        if not isinstance(annotations["uncertainty"], list) or any(not isinstance(x, str) for x in annotations["uncertainty"]):
            raise BundleValidationError(f"{where}.annotations.uncertainty must be a list of strings")
    evaluation = _object(b["evaluation"], "evaluation")
    _required(evaluation, ("provenance", "limitations"), "evaluation")
    _object(evaluation["provenance"], "evaluation.provenance")
    if not isinstance(evaluation["limitations"], list) or any(not isinstance(x, str) for x in evaluation["limitations"]):
        raise BundleValidationError("evaluation.limitations must be a list of strings")
    return b


def dumps_bundle(bundle: dict) -> str:
    """Validate and serialize deterministically for review and version control."""
    validate_bundle(bundle)
    return json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def loads_bundle(serialized: str) -> dict:
    """Parse and validate serialized JSON."""
    if not isinstance(serialized, str):
        raise BundleValidationError("bundle must be valid JSON text")
    try:
        value = json.loads(serialized)
    except (TypeError, json.JSONDecodeError) as exc:
        raise BundleValidationError("bundle must be valid JSON") from exc
    return validate_bundle(value)


def write_bundle(path: str | Path, bundle: dict, *, overwrite: bool = False) -> None:
    """Write a validated local JSON bundle; refuse existing paths unless overwrite=True.

    Default creation uses exclusive mode to avoid a check-then-write race. Explicit
    overwrite uses the normal truncating serialization behavior.
    """
    if not isinstance(overwrite, bool):
        raise BundleValidationError("overwrite must be a boolean")
    serialized = dumps_bundle(bundle)
    try:
        destination = Path(path)
    except (TypeError, ValueError) as exc:
        raise BundleValidationError("bundle destination must be a valid local path") from exc
    if overwrite:
        try:
            destination.write_text(serialized, encoding="utf-8")
        except OSError as exc:
            raise BundleValidationError(f"bundle destination cannot be written: {destination}") from exc
        return
    try:
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(serialized)
    except FileExistsError as exc:
        if destination.is_dir():
            raise BundleValidationError(f"bundle destination must be a file path: {destination}") from exc
        raise BundleExistsError(f"bundle destination already exists: {destination}") from exc
    except OSError as exc:
        raise BundleValidationError(f"bundle destination cannot be written: {destination}") from exc


def read_bundle(path: str | Path) -> dict:
    """Read and validate a local JSON bundle."""
    return loads_bundle(Path(path).read_text(encoding="utf-8"))
