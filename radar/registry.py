"""Strict keyword registry; source terms are data, never embedded business logic."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

FERNANDO_SOURCE = "Fernando Oliveira PRO"
FERNANDO_EXPECTED_COUNT = 106
FERNANDO_EXPECTED_CATEGORIES = {
    "Formatos e materiais": 39,
    "Nichos e temas": 26,
    "Oferta, acesso e benefícios": 21,
    "Plataformas e domínios": 20,
}


@dataclass(frozen=True)
class KeywordEntry:
    id: str
    text: str
    source: str
    category: str | None = None


def stable_keyword_id(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")[:48] or "keyword"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def load_fernando_registry(path: str | Path) -> list[KeywordEntry]:
    """Load the canonical PDF transcription and reject any mutation of scope."""
    registry_path = Path(path)
    if registry_path.exists():
        contents = registry_path.read_text(encoding="utf-8")
    elif registry_path.name == "fernando-oliveira-pro-106.json":
        contents = resources.files("radar.data").joinpath(registry_path.name).read_text(encoding="utf-8")
    else:
        raise FileNotFoundError(registry_path)
    raw = json.loads(contents)
    if not isinstance(raw, dict) or raw.get("source") != FERNANDO_SOURCE:
        raise ValueError(f"Fernando registry source must be {FERNANDO_SOURCE!r}")
    categories = raw.get("categories")
    if not isinstance(categories, list):
        raise ValueError("Fernando registry must contain a categories list")
    category_counts: dict[str, int] = {}
    terms: list[tuple[str, str]] = []
    for category in categories:
        if not isinstance(category, dict) or set(category) != {"name", "terms"}:
            raise ValueError("each category must contain only name and terms")
        name, values = category["name"], category["terms"]
        if not isinstance(name, str) or not isinstance(values, list):
            raise ValueError("category name must be a string and terms must be a list")
        category_counts[name] = len(values)
        terms.extend((name, text) for text in values)
    if category_counts != FERNANDO_EXPECTED_CATEGORIES:
        raise ValueError("Fernando registry category counts must be 39/26/21/20")
    texts = [text for _, text in terms]
    if len(texts) != FERNANDO_EXPECTED_COUNT:
        raise ValueError("Fernando registry must contain exactly 106 keyword strings")
    if any(not isinstance(text, str) or not text.strip() or text != text.strip() for text in texts):
        raise ValueError("keywords must be non-empty strings preserving their supplied text")
    if len(set(texts)) != len(texts):
        raise ValueError("Fernando registry contains duplicate keyword strings")
    return [KeywordEntry(stable_keyword_id(text), text, FERNANDO_SOURCE, category) for category, text in terms]
