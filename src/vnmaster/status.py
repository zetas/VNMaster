"""F95Checker game-status helpers.

F95Checker stores `status` as an integer. The notable values:
2 = Completed, 3 = On Hold, 4 = Abandoned. Everything else (1 = Normal/Ongoing,
the default sentinel, unknown) is treated as "no notable status" — no tag, and
transitions among them don't register.
"""
from __future__ import annotations

_NOTABLE = {2: "Completed", 3: "On Hold", 4: "Abandoned"}
_CALLOUT = {
    "Completed": "✅ Now completed",
    "On Hold": "⏸️ Now on hold",
    "Abandoned": "🚫 Now abandoned",
}


def status_int(value: object) -> int | None:
    """Coerce a status to int, tolerating the str form the DB round-trips."""
    if not isinstance(value, (int, str, bytes, bytearray)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def status_label(value: object) -> str:
    """Standing-tag label for a notable status, else '' (ongoing/unknown)."""
    return _NOTABLE.get(status_int(value) or 0, "")


def status_changed(old: object, new: object) -> bool:
    """True when the *notable* status differs.

    Compares notability labels, so ongoing<->unchecked churn (both "") doesn't
    register, but ongoing->completed, completed->abandoned, and
    completed->ongoing (resumed) all do.
    """
    return status_label(old) != status_label(new)


def status_callout(new: object) -> str:
    """Prominent transition callout. Only meaningful when status changed —
    returns the notable callout, or '▶️ Resumed' when a game left a notable
    status back to ongoing.
    """
    return _CALLOUT.get(status_label(new), "▶️ Resumed")
