"""Validation helpers for word-level transcription timestamps."""
import math


# Speech words are normally much shorter than this. Keep long spans for review
# while leaving their reported boundaries untouched.
LONG_WORD_SPAN_SECONDS = 30.0


def valid_word_timestamps(start, end, token="", *, warn=print):
    """Return numeric timestamps when valid, otherwise warn and return None.

    Numeric conversion is used for downstream subtitle formatting. No boundary
    is clamped, extended, or otherwise adjusted.
    """
    label = f" for {token!r}" if token else ""
    try:
        numeric_start = float(start)
        numeric_end = float(end)
    except (TypeError, ValueError, OverflowError):
        warn(f"Warning: discarding nonnumeric or malformed word timestamp {start!r}-{end!r}{label}.")
        return None
    if not math.isfinite(numeric_start) or not math.isfinite(numeric_end):
        warn(f"Warning: discarding nonfinite word timestamp {start!r}-{end!r}{label}.")
        return None
    if numeric_start < 0 or numeric_end < 0:
        warn(f"Warning: discarding negative word timestamp {start!r}-{end!r}{label}.")
        return None
    if numeric_end <= numeric_start:
        warn(f"Warning: discarding reversed or zero-duration word timestamp {start!r}-{end!r}{label}.")
        return None
    if numeric_end - numeric_start > LONG_WORD_SPAN_SECONDS:
        warn(f"Warning: unusually long word timestamp span ({numeric_end - numeric_start:g}s) {start!r}-{end!r}{label}; review alignment.")
    return numeric_start, numeric_end
