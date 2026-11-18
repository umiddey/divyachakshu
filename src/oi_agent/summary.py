"""Render the stored rolling conversation summary for prompts and the CLI.

P4 defines the stored document shape (``{"v": 1, "items": [...]}`` with the
fixed sections below) and renders it. Rendering is pure and applies decay and
the render cap at read time, so a stale item vanishes even when no update has
run for months. P5 adds the model-driven update controller to this same
module; these rules must stay unit-testable without Discord.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

# Fixed section order; every header renders even when its section is empty.
SECTIONS = ("now", "decisions", "open", "people", "long_term")
_HEADERS = {
    "now": "NOW",
    "decisions": "DECISIONS",
    "open": "OPEN",
    "people": "PEOPLE",
    "long_term": "LONG-TERM",
}


def _parse_date(value: object) -> date | None:
    """Return a validated date or None for anything unparsable."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _item_line(item: dict) -> str:
    """Render one summary item as a single bounded line."""
    topic = item.get("topic") or ""
    text = item.get("text") or ""
    return f"- {topic}: {text}".rstrip()


def render_summary(
    summary_json: str,
    *,
    today: date,
    decay_days: int,
    max_chars: int,
) -> str:
    """Render a stored summary document; return "" when absent or unusable.

    Non-``long_term`` items whose ``touched`` date is older than ``decay_days``
    are hidden (render-time decay). The rendered text never exceeds
    ``max_chars``: items are ranked newest-``touched`` first and older items
    are dropped until the cap holds. Every section header renders, with
    ``- none`` for an empty section.

    Args:
        summary_json: Stored summary document as JSON text.
        today: Reference date for decay and ranking.
        decay_days: Non-durable items older than this many days are hidden.
        max_chars: Hard cap on the rendered text length.

    Returns:
        str: Rendered summary, or "" when the document is absent, not version
        1, or not parsable.
    """
    try:
        document = json.loads(summary_json)
    except ValueError:
        return ""
    if not isinstance(document, dict) or document.get("v") != 1:
        return ""
    items = document.get("items")
    if not isinstance(items, list):
        return ""
    horizon = today - timedelta(days=decay_days)
    visible = [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("section") in _HEADERS
        and (
            item.get("section") == "long_term"
            or (_parse_date(item.get("touched")) is not None and _parse_date(item["touched"]) >= horizon)
        )
    ]
    ranked = sorted(
        visible,
        key=lambda item: _parse_date(item.get("touched")) or date.min,
        reverse=True,
    )
    kept: list[dict] = []
    size = sum(len(_HEADERS[section]) + len("\n- none") for section in SECTIONS)
    for item in ranked:
        line = _item_line(item)
        if size + len(line) + 1 > max_chars:
            break
        kept.append(item)
        size += len(line) + 1
    lines: list[str] = []
    for section in SECTIONS:
        lines.append(_HEADERS[section])
        section_items = [item for item in kept if item["section"] == section]
        if section_items:
            lines.extend(_item_line(item) for item in section_items)
        else:
            lines.append("- none")
    return "\n".join(lines)
