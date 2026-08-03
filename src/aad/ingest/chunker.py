"""Chunking and spec-type classification.

Chunks are ~1200 tokens with 200 tokens of overlap, split on section boundaries first
so a torque table is not cut in half. Each chunk is tagged with a `spec_type` so
retrieval can be filtered to the kind of data being asked for — a query for a torque
value should not compete against pages of general theory.
"""

from __future__ import annotations

import hashlib
import re

from aad.models import Chunk, SpecType

# Rough token estimate. Service manuals are dense with part numbers and units, which
# tokenize worse than prose, so we bias high rather than overrun the target window.
TOKENS_PER_WORD = 1.35

HEADING_RE = re.compile(r"^\s{0,3}(#{1,6}\s+\S.*|[A-Z][A-Z0-9 ,./()\-]{6,}\s*)$", re.MULTILINE)

_TORQUE_RE = re.compile(
    r"\b(torque|tighten(?:ing)?|lb-?ft|lb-?in|ft-?lbs?|in-?lbs?|n[·.\s]?m|newton[- ]meter)\b",
    re.IGNORECASE,
)
_BOLT_RE = re.compile(r"\bM\d{1,2}\s*[x×]\s*[\d.]+|\b\d+/\d+\s*-\s*\d+\b|\bbolt\b", re.IGNORECASE)
_LABOR_RE = re.compile(
    r"\b(labor (?:time|operation|hours?)|book time|flat rate|warranty time|\d+\.\d\s*(?:hrs?|hours))\b",
    re.IGNORECASE,
)
_WIRING_RE = re.compile(
    r"\b(wiring diagram|schematic|connector|pinout|pin \d+|harness|circuit|ground point|"
    r"wire colou?r)\b",
    re.IGNORECASE,
)
_TSB_RE = re.compile(r"\b(technical service bulletin|TSB|recall|campaign no|bulletin no)\b", re.IGNORECASE)
_DTC_RE = re.compile(r"\b[PBCU][0-3][0-9A-F]{3}\b")
_FLUID_RE = re.compile(
    r"\b(capacit(?:y|ies)|refill|fill (?:volume|capacity)|quarts?|litres?|liters?)\b", re.IGNORECASE
)
_PROCEDURE_RE = re.compile(
    r"\b(removal and installation|remove(?:\s+the)?|install(?:ation)?|disassembly|"
    r"inspection|step \d+|procedure)\b",
    re.IGNORECASE,
)


def classify(text: str) -> SpecType:
    """Assign a spec_type. Order matters: the most specific, most safety-critical
    categories win, because those are the ones retrieval must be able to isolate."""
    if _TORQUE_RE.search(text) and _BOLT_RE.search(text):
        return "torque_spec"
    if _LABOR_RE.search(text):
        return "labor_time"
    if _WIRING_RE.search(text):
        return "wiring_diagram"
    if _TSB_RE.search(text):
        return "tsb"
    if _DTC_RE.search(text):
        return "dtc"
    if _TORQUE_RE.search(text):
        return "torque_spec"
    if _FLUID_RE.search(text):
        return "fluid_capacity"
    if _PROCEDURE_RE.search(text):
        return "procedure"
    return "general"


def estimate_tokens(text: str) -> int:
    return int(len(text.split()) * TOKENS_PER_WORD)


def _split_sections(text: str) -> list[tuple[str | None, str]]:
    """Split on headings, keeping each heading with the body that follows it."""
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        return [(None, text)]

    sections: list[tuple[str | None, str]] = []
    preamble = text[: matches[0].start()].strip()
    if preamble:
        sections.append((None, preamble))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        heading = match.group(0).strip().lstrip("#").strip()
        body = text[match.start() : end].strip()
        if body:
            sections.append((heading or None, body))
    return sections


def _window(words: list[str], size: int, overlap: int) -> list[list[str]]:
    if size <= 0:
        return [words]
    step = max(size - overlap, 1)
    windows = [words[i : i + size] for i in range(0, len(words), step)]
    # Drop a trailing window fully contained in its predecessor.
    if len(windows) > 1 and len(windows[-1]) <= overlap:
        windows.pop()
    return windows


def chunk_document(
    *,
    source: str,
    pages: list[tuple[int, str]],
    chunk_tokens: int = 1200,
    overlap_tokens: int = 200,
    year: int | None = None,
    make: str | None = None,
    model: str | None = None,
    engine: str | None = None,
) -> list[Chunk]:
    """Chunk parsed pages into indexable units carrying full asset metadata."""
    words_per_chunk = max(int(chunk_tokens / TOKENS_PER_WORD), 1)
    words_overlap = min(max(int(overlap_tokens / TOKENS_PER_WORD), 0), words_per_chunk - 1)

    chunks: list[Chunk] = []
    for page_no, page_text in pages:
        if not page_text.strip():
            continue
        for section, body in _split_sections(page_text):
            words = body.split()
            if not words:
                continue
            for window in _window(words, words_per_chunk, words_overlap):
                text = " ".join(window).strip()
                if not text:
                    continue
                digest = hashlib.blake2b(
                    f"{source}|{page_no}|{section}|{text}".encode(), digest_size=12
                ).hexdigest()
                chunks.append(
                    Chunk(
                        chunk_id=digest,
                        text=text,
                        source=source,
                        page=page_no,
                        section=section,
                        spec_type=classify(text),
                        year=year,
                        make=make.lower() if make else None,
                        model=model.lower() if model else None,
                        engine=engine.lower() if engine else None,
                    )
                )
    return chunks
