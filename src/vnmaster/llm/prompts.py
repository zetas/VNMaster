"""System prompt + JSON tool schema for the Anthropic extractor.

The system prompt is constant per process and stable across runs — perfect
candidate for prompt caching with cache_control.
"""
from __future__ import annotations

SYSTEM_PROMPT = """You are a precise data-extraction assistant for adult-VN
changelogs. Given a raw changelog from F95Zone (which may cover one or more
versions), call the `record_changelog` tool exactly once with a structured
list of versions.

Rules:
- For each version block, emit one object.
- Integers must come from the text. If a metric is absent, use null.
- `renders` counts CG/render/image additions. Developers may call these
  "renders", "images", "CGs", or "pictures" — they all map to `renders`.
- `words` is the count of DIALOGUE words or lines only. Do NOT count "lines of
  code" — that is source code, not dialogue. If a block only gives "lines of
  code", leave `words` null.
- `bugfix_only` is true only when the version contains NO content additions
  (no renders, animations, words, scenes, locations, characters) AND the
  text mentions bugfixes/maintenance.
- `summary_one_line` is your concise (≤80 chars) summary of the *content*
  changes (not bugs). If no content changes, return "".
- Preserve the developer's version label as-is in `version` (e.g., "0.7.0",
  "0.7a", "v0.9.2-5").
- Also set `version_normalized`: a clean dotted version (e.g., "0.9.5") for the
  HIGHEST version this block covers. For a range label like "0.9.2-5" use the
  upper bound "0.9.5". Strip prefixes/suffix words ("v", "Public", "Hotfix",
  "Beta", "Week 3") and trailing letters ("0.7a" -> "0.7"). If you cannot
  determine a dotted version, use null.
- If `released_at` isn't clearly stated, return null.
- Do not invent numbers. When in doubt, prefer null over guessing.
"""

TOOL_SCHEMA = {
    "name": "record_changelog",
    "description": "Record the structured per-version delta data.",
    "input_schema": {
        "type": "object",
        "properties": {
            "versions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "version": {"type": "string"},
                        "version_normalized": {"type": ["string", "null"]},
                        "released_at": {"type": ["string", "null"]},
                        "renders": {"type": ["integer", "null"]},
                        "animations": {"type": ["integer", "null"]},
                        "words": {"type": ["integer", "null"]},
                        "scenes": {"type": ["integer", "null"]},
                        "new_locations": {"type": ["integer", "null"]},
                        "new_characters": {"type": ["integer", "null"]},
                        "bugfix_only": {"type": "boolean"},
                        "summary_one_line": {"type": "string"},
                    },
                    "required": [
                        "version", "version_normalized", "bugfix_only",
                        "summary_one_line",
                    ],
                },
            }
        },
        "required": ["versions"],
    },
}
