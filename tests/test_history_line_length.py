"""History evidence lines must render admitted messages whole up to Discord's own maximum.

A history message the watcher already admitted may be as long as Discord
itself allows (~4,000 chars). Rendering clipped such a line silently made the
model guess (Amir's pricing proposal was cut mid-sentence at 600 chars). A
line longer than the render cap must say so itself with an explicit marker
naming both lengths; it must never be truncated without one.
"""

from datetime import datetime, timedelta, timezone

import pytest

from oi_agent.watch.discord_client import (
    _fetch_conversation_context,
    _message_evidence_line,
)

_EPOCH = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


class _FakeAuthor:
    id = 7
    bot = False
    display_name = "amir"
    name = "amir"


class _FakeMessage:
    def __init__(self, message_id: int, content: str) -> None:
        self.id = message_id
        self.content = content
        self.author = _FakeAuthor()
        self.mentions = []
        self.attachments = []
        self.embeds = []
        self.created_at = _EPOCH + timedelta(minutes=message_id)


class _FakeChannel:
    """Fake channel exposing only the `_history` list (no history() method)."""

    def __init__(self, messages: list[_FakeMessage]) -> None:
        self.id = 111
        self._history = list(messages)


def _long_content(total_chars: int, tail_word: str) -> str:
    """Build filler text of exactly total_chars ending in tail_word."""
    filler = "pricing proposal paragraph. "
    pad = total_chars - len(tail_word)
    content = (filler * (pad // len(filler) + 1))[:pad] + tail_word
    assert len(content) == total_chars
    assert content.endswith(tail_word)
    return content


def test_history_line_renders_long_message_whole() -> None:
    """A ~1,300-char message (Discord-legal) renders with its tail present."""
    content = _long_content(1300, "PROPOSALTAILWORD")
    line = _message_evidence_line(_FakeMessage(1, content))
    assert "PROPOSALTAILWORD" in line


def test_history_line_beyond_discord_max_clips_with_marker() -> None:
    """A 5,000-char body is clipped at the 4,000 cap WITH an explicit marker."""
    content = _long_content(5000, "TAILWORD")
    line = _message_evidence_line(_FakeMessage(2, content))
    assert "[clipped: 4000 of 5000 chars]" in line


@pytest.mark.asyncio
async def test_fetched_context_renders_long_history_message_whole() -> None:
    """The rendered conversation context carries the whole long message."""
    content = _long_content(1300, "PROPOSALTAILWORD")
    channel = _FakeChannel([_FakeMessage(1, content)])
    context = await _fetch_conversation_context(channel, limit=5)
    assert "PROPOSALTAILWORD" in context.excerpt
