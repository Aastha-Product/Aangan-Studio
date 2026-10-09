"""Verify that a quote really appears in the transcript.

CLAUDE.md: "Every point must cite a quote from the transcript. No quote = 0 for that line."
The model is told to copy quotes verbatim; this check enforces it, so a hallucinated
quote can never earn points.
"""
import re

_REPLACEMENTS = {
    "’": "'", "‘": "'", "“": '"', "”": '"',
    "—": "-", "–": "-", " ": " ",
}
_STRIP = " .,!?;:\"'-()"


def normalise(s: str) -> str:
    for a, b in _REPLACEMENTS.items():
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_in_transcript(quote: str | None, transcript: str) -> bool:
    if not quote or not quote.strip():
        return False
    haystack = normalise(transcript)
    parts = [normalise(p).strip(_STRIP) for p in re.split(r"\.\.\.|…", quote)]
    parts = [p for p in parts if p]
    return bool(parts) and all(p in haystack for p in parts)
