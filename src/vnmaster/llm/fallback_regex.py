"""Pure regex-based extractor. Used when LLM is unavailable or budget exhausted.

Lower fidelity than the LLM extractor: no dev summary, brittle to format
variance. Sufficient as a backstop.
"""
from __future__ import annotations

import re
from typing import Any

_VERSION_HEADER = re.compile(r"(?im)^\s*v?(\d+(?:\.\d+)+[a-z]?)\b")
# `\+?` tolerates the common "2050+ renders" / "90+ animations" phrasing.
# Devs call CGs "renders", "images", or "CGs" interchangeably.
_RENDERS = re.compile(
    r"(\d+(?:,\d+)?)\+?\s+(?:new\s+)?(?:renders?|images?|cgs?)", re.IGNORECASE
)
_ANIMATIONS = re.compile(r"(\d+(?:,\d+)?)\+?\s+(?:new\s+)?animations?", re.IGNORECASE)
_WORDS = re.compile(
    r"(\d+(?:,\d+)?)\+?\s+(?:new\s+)?(?:lines?|words?)\s+(?:of\s+)?dialogue",
    re.IGNORECASE,
)
_SCENES = re.compile(r"(\d+)\+?\s+(?:new\s+)?scenes?", re.IGNORECASE)
_LOCATIONS = re.compile(r"(\d+)\+?\s+(?:new\s+)?locations?", re.IGNORECASE)
_CHARACTERS = re.compile(r"(\d+)\+?\s+(?:new\s+)?characters?", re.IGNORECASE)
_BUGFIX_HINT = re.compile(r"\bbug\s*fix(?:es)?\b|\bfixes\b", re.IGNORECASE)


def extract_with_regex(text: str) -> dict[str, Any]:
    if not text.strip():
        return {"versions": []}

    sections = _split_by_version(text)
    out: list[dict[str, Any]] = []
    for version, body in sections:
        renders = _first_int(_RENDERS, body)
        animations = _first_int(_ANIMATIONS, body)
        words = _first_int(_WORDS, body)
        scenes = _first_int(_SCENES, body)
        locations = _first_int(_LOCATIONS, body)
        characters = _first_int(_CHARACTERS, body)
        content_signals = any(
            x is not None for x in (renders, animations, words, scenes, locations, characters)
        )
        bugfix_only = (not content_signals) and bool(_BUGFIX_HINT.search(body))
        out.append({
            "version": version,
            "released_at": None,
            "renders": renders,
            "animations": animations,
            "words": words,
            "scenes": scenes,
            "new_locations": locations,
            "new_characters": characters,
            "bugfix_only": bugfix_only,
            "summary_one_line": "",
        })
    return {"versions": out}


def _split_by_version(text: str) -> list[tuple[str, str]]:
    matches = list(_VERSION_HEADER.finditer(text))
    if not matches:
        return []
    sections = []
    for i, m in enumerate(matches):
        version = m.group(1)
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((version, text[body_start:body_end]))
    return sections


def _first_int(pattern: re.Pattern[str], body: str) -> int | None:
    m = pattern.search(body)
    if not m:
        return None
    return int(m.group(1).replace(",", ""))
