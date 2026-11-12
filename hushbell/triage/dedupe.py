"""Merge the same news arriving from different apps (Remind + the WhatsApp parent group)."""

from __future__ import annotations

import re

from hushbell.models import Item

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {
    "a", "an", "the", "and", "or", "to", "of", "for", "in", "on", "at", "is", "are", "be",
    "please", "reminder", "remember", "hi", "hello", "hey", "just", "all", "our", "your",
    "you", "we", "this", "that", "it", "by", "with", "from", "re", "fwd", "fw",
}
WINDOW_SECONDS = 12 * 3600
MIN_WORDS = 4
THRESHOLD = 0.7


def words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1}


def overlap(a: str, b: str) -> float:
    """Share of the shorter message's words that also appear in the other one."""
    wa, wb = words(a), words(b)
    if min(len(wa), len(wb)) < MIN_WORDS:
        return 1.0 if wa and wa == wb else 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def find_duplicate(text: str, candidates: list[Item]) -> Item | None:
    """Return the existing item this message repeats, if any."""
    best: tuple[float, Item] | None = None
    for item in candidates:
        score = overlap(text, item.text)
        if score >= THRESHOLD and (best is None or score > best[0]):
            best = (score, item)
    return best[1] if best else None
