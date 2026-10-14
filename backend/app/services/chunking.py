"""Deterministic text chunking.

A pure function of its input and its configuration. That is the whole contract: same text
plus same config must give the same chunk count, the same order, and the same content,
on any machine and any Python version. Nothing here is random, model-generated, or
time-dependent - a chunker that produced slightly different boundaries on a retry would
silently re-index a document and make "did my update land?" unanswerable.

The approach is fixed-size windows with overlap, snapped to the nearest whitespace so a
word is never cut in half. Paragraph-aware splitting is deliberately not done: it looks
smarter, but it makes the output depend on irregular blank-line patterns, which is the
kind of subtlety that turns an update into an unrelated re-index.
"""

import re
from dataclasses import dataclass

# Collapses every run of whitespace (including newlines) to one space. Applied before
# measuring, so a document's chunk boundaries do not depend on how it happened to be
# indented.
_WHITESPACE = re.compile(r"\s+")


class EmptyContent(ValueError):
    """The input was blank. Not an error the caller can retry, so it is its own type."""


@dataclass(frozen=True)
class TextChunk:
    """One chunk and its position. `index` is 0-based and contiguous."""

    index: int
    text: str

    @property
    def character_count(self) -> int:
        return len(self.text)


def normalize(text: str) -> str:
    """Collapse whitespace and trim. Idempotent: normalize(normalize(x)) == normalize(x)."""
    return _WHITESPACE.sub(" ", text or "").strip()


def chunk(
    text: str,
    *,
    chunk_size: int = 1200,
    overlap: int = 200,
) -> list[TextChunk]:
    """Split `text` into overlapping chunks.

    `chunk_size` and `overlap` are character counts. `overlap` is how much consecutive
    chunks share, which keeps a sentence or a table row from being severed at a boundary -
    the reason overlap exists at all.

    Raises `EmptyContent` for blank input rather than returning an empty list, because a
    caller that persists zero chunks has almost certainly made a mistake and should hear
    about it instead of storing a document that can never be retrieved.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be at least 1")
    if overlap < 0:
        raise ValueError("overlap must not be negative")
    if overlap >= chunk_size:
        # A window that does not advance would loop forever, or produce unbounded chunks.
        raise ValueError("overlap must be smaller than chunk_size")

    normalized = normalize(text)
    if not normalized:
        raise EmptyContent("cannot chunk empty or whitespace-only content")

    if len(normalized) <= chunk_size:
        return [TextChunk(index=0, text=normalized)]

    chunks: list[TextChunk] = []
    start = 0
    while start < len(normalized):
        end = min(start + chunk_size, len(normalized))
        if end < len(normalized):
            end = _snap_to_whitespace(normalized, start, end, chunk_size)
        piece = normalized[start:end].strip()
        if piece:
            chunks.append(TextChunk(index=len(chunks), text=piece))
        if end >= len(normalized):
            break
        # Always advance by at least one character, so a pathological config terminates.
        start = max(end - overlap, start + 1)
    return chunks


def _snap_to_whitespace(text: str, start: int, end: int, chunk_size: int) -> int:
    """Pull `end` back to the nearest space, so a word is not cut in half.

    Bounded by `chunk_size`: if there is no whitespace in the window (a very long
    unbroken token such as a base64 blob), the boundary is left where it is rather than
    walking backwards through the whole document.
    """
    floor = start + max(1, chunk_size // 2)
    for index in range(end, floor, -1):
        if text[index - 1] == " ":
            return index
    return end


__all__ = ["EmptyContent", "TextChunk", "chunk", "normalize"]
