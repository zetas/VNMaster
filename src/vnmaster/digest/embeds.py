"""Discord embed JSON builders for update items."""
from __future__ import annotations

from typing import Any

from vnmaster.digest.select import SelectedUpdate
from vnmaster.magnitude import runtime_label, star_band
from vnmaster.status import status_callout, status_label

UPDATE_COLOR = 0xF0B232

# Confidence (a reliability axis) is shown with precision words, NOT a high/low
# badge — a "High" next to the ★ rating reads as "lots of content". Words like
# "approx" can't be conflated with the magnitude quantity.
_ACCURACY_TIER = {"high": "exact", "medium": "≈ approx", "low": "rough"}


def _accuracy_value(confidence: str, basis: str) -> str:
    """The "Accuracy" column: a precision tier plus a short basis line."""
    tier = _ACCURACY_TIER.get(confidence, "rough")
    return f"{tier}\n{basis}" if basis else tier


def _version_label(version: str | None) -> str:
    """Render a version for display, adding "v" only when it's missing.

    Version strings reach us verbatim from two sources that disagree about
    prefixes. F95Zone thread titles usually carry their own ("v23", "v0.7.05")
    or a chapter/season label instead ("Ch.4", "S2 Ch.18", "Ep.11"), while
    Ren'Py save files hold a bare `config.version` ("0.5.2", "4.0"). Gluing a
    "v" on unconditionally produced "vv23" and "vCh.4", so only bare numeric
    versions get the prefix.
    """
    if not version:
        return "?"
    return f"v{version}" if version[0].isdigit() else version


def build_update_embed(
    sel: SelectedUpdate,
    *,
    deltas: dict[str, int],
    magnitude_score: float,
    summary_one_line: str,
    confidence: str = "low",
    accuracy_basis: str = "",
    no_delta_note: str = "",
) -> dict[str, Any]:
    upstream = _version_label(sel.latest_upstream_version)
    title = (
        f"{sel.game_title} — {upstream} "
        f"(yours: {_version_label(sel.installed_version)})"
        if sel.installed_version
        else f"{sel.game_title} — {upstream} (never played)"
    )
    # Developer, plus a standing status tag (Completed/Abandoned/On Hold).
    tag = status_label(sel.status)
    description = " · ".join(b for b in [sel.developer or "?", tag] if b)
    # Prominent callout the run a game's notable status changed.
    if sel.status_changed:
        description = f"**{status_callout(sel.status)}**\n{description}"

    delta_lines = []
    if deltas.get("renders"):
        delta_lines.append(f"+{deltas['renders']} renders")
    if deltas.get("animations"):
        delta_lines.append(f"+{deltas['animations']} animations")
    if deltas.get("words"):
        delta_lines.append(f"+{deltas['words'] // 1000}k words")
    if deltas.get("scenes"):
        delta_lines.append(f"{deltas['scenes']} new scenes")
    if deltas.get("new_locations"):
        delta_lines.append(f"{deltas['new_locations']} new locations")

    rating = f"{star_band(magnitude_score)} · {runtime_label(magnitude_score)}"
    summary_text = f"{rating}\n{summary_one_line}" if summary_one_line else rating

    embed: dict[str, Any] = {
        "title": title,
        "description": description,
        "color": UPDATE_COLOR,
        "fields": [
            {
                "name": "Since you last played",
                "value": (
                    "\n".join(delta_lines)
                    or no_delta_note
                    or "Changes listed, but no counts given"
                ),
                "inline": True,
            },
            {
                "name": "Est. added playtime",
                "value": summary_text,
                "inline": True,
            },
            {
                "name": "Accuracy",
                "value": _accuracy_value(confidence, accuracy_basis),
                "inline": True,
            },
        ],
        "footer": {"text": f"thread #{sel.f95_thread_id} · ⬇️ DM link · 📦 got it"},
    }
    if sel.image_url:
        embed["thumbnail"] = {"url": sel.image_url}
    if sel.upstream_thread_url:
        embed["url"] = sel.upstream_thread_url
    return embed
