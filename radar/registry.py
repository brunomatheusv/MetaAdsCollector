"""Strict keyword registry; source terms are data, never embedded business logic."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

FERNANDO_SOURCE = "Fernando Oliveira PRO"
FERNANDO_EXPECTED_COUNT = 106


@dataclass(frozen=True)
class KeywordEntry:
    id: str
    text: str
    source: str


def stable_keyword_id(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")[:48] or "keyword"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def load_fernando_registry(path: str | Path) -> list[KeywordEntry]:
    """Load the user-supplied canonical JSON list and reject any mutation of scope."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or len(raw) != FERNANDO_EXPECTED_COUNT:
        raise ValueError("Fernando registry must contain exactly 106 keyword strings")
    if any(not isinstance(text, str) or not text.strip() or text != text.strip() for text in raw):
        raise ValueError("keywords must be non-empty strings preserving their supplied text")
    if len(set(raw)) != len(raw):
        raise ValueError("Fernando registry contains duplicate keyword strings")
    return [KeywordEntry(stable_keyword_id(text), text, FERNANDO_SOURCE) for text in raw]
