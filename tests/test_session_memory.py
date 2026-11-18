"""P4 rolling-summary contract: prompt wiring, reset, render decay/caps, store guards."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import (
    FakeAuthor,
    FakeChannel,
    FakeMention,
    FakeMessage,
    make_watcher,
    process_messages,
)

from oi_agent.agent.reply import Reply
from oi_agent.poster import DeliveryResult
from oi_agent.store import Store
from oi_agent.summary import render_summary


def _summary_doc(items):
    """Build a stored v1 summary document from plain item dicts."""
    return json.dumps({"v": 1, "items": items})


def _evidence_lines(excerpt):
    """Return the `[id] ...` history evidence lines of an excerpt in order."""
    return [line for line in excerpt.splitlines() if line.startswith("[")]


@pytest.mark.asyncio
async def test_prompt_places_summary_before_newest_context_messages(tmp_path, monkeypatch):
    """Prompt excerpt = rendered summary block, then exactly context_messages newest messages."""
    watcher = make_watcher(tmp_path)
    watcher._cfg.context_messages = 5
    today = datetime.now(UTC).date()
    watcher._store.set_summary(
        111,
        _summary_doc([
            {"section": "now", "topic": "release", "text": "v2 rollout in progress",
             "touched": today.isoformat()},
            {"section": "decisions", "topic": "database", "text": "keeping sqlite",
             "touched": (today - timedelta(days=2)).isoformat()},
        ]),
        last_summarized_message_id=8,
        max_chars=watcher._cfg.summary_max_chars,
    )
    channel = FakeChannel(111)
    # Discord history arrives newest-first; the window keeps the 5 newest.
    # Explicit timestamps pin the chronological order to the ids.
    moment = datetime.now(UTC)
    trigger = FakeMessage(8, channel, FakeAuthor(7), "<@99> inspect", [FakeMention(99)])
    newest_first = [trigger]
    newest_first.extend(
        FakeMessage(message_id, channel, FakeAuthor(7), f"message {message_id} text", [])
        for message_id in range(7, 0, -1)
    )
    for message in newest_first:
        message.created_at = moment + timedelta(minutes=message.id)
    channel._history = newest_first
    captured = {}

    async def fake_run(
        target, author, question, excerpt, member_id=None, bot_name="", bot_id=None, open_channel_prompt=None
    ):
        """Capture the assembled prompt context."""
        captured["excerpt"] = excerpt
        return Reply("answer", "sha")

    async def fake_deliver(self, **kwargs):
        """Accept the post."""
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr("oi_agent.watch.discord_client.Poster.deliver_chunk", fake_deliver)
    await process_messages(watcher, channel, trigger)

    excerpt = captured["excerpt"]
    assert excerpt.startswith("ROLLING SUMMARY")
    assert excerpt.index("ROLLING SUMMARY") < excerpt.index("CONVERSATION HISTORY")
    assert "release: v2 rollout in progress" in excerpt
    assert "database: keeping sqlite" in excerpt
    lines = _evidence_lines(excerpt)
    assert len(lines) == 5
    assert [line.split("]")[0] for line in lines] == ["[4", "[5", "[6", "[7", "[8"]


@pytest.mark.asyncio
async def test_fresh_resets_stored_summary(tmp_path, monkeypatch):
    """`!fresh` deletes the stored summary before the run builds its prompt."""
    watcher = make_watcher(tmp_path)
    today = datetime.now(UTC).date()
    watcher._store.set_summary(
        111,
        _summary_doc([{"section": "now", "topic": "old", "text": "stale state",
                       "touched": today.isoformat()}]),
        last_summarized_message_id=1,
        max_chars=watcher._cfg.summary_max_chars,
    )
    channel = FakeChannel(111)
    message = FakeMessage(1, channel, FakeAuthor(7), "<@99> !fresh inspect", [FakeMention(99)])
    channel._history = [message]
    captured = {}

    async def fake_run(
        target, author, question, excerpt, member_id=None, bot_name="", bot_id=None, open_channel_prompt=None
    ):
        """Capture the post-reset prompt context."""
        captured["excerpt"] = excerpt
        return Reply("answer", "sha")

    async def fake_deliver(self, **kwargs):
        """Accept the post."""
        assert kwargs["chunk"] == "answer"
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr("oi_agent.watch.discord_client.Poster.deliver_chunk", fake_deliver)
    await process_messages(watcher, channel, message)

    assert watcher._store.get_summary(111) is None
    assert "ROLLING SUMMARY" not in captured["excerpt"]


def test_render_decay_hides_stale_nondurable_items():
    """Render-time decay hides non-durable items past summary_decay_days; long-term survives."""
    rendered = render_summary(
        _summary_doc([
            {"section": "now", "topic": "old", "text": "stale chatter", "touched": "2026-11-10"},
            {"section": "open", "topic": "bug", "text": "still unresolved", "touched": "2026-11-12"},
            {"section": "long_term", "topic": "style", "text": "team prefers ascii",
             "touched": "2020-01-01"},
        ]),
        today=datetime(2026, 11, 18).date(),
        decay_days=7,
        max_chars=1_500,
    )
    assert "stale chatter" not in rendered
    assert "still unresolved" in rendered
    assert "team prefers ascii" in rendered


def test_render_cap_drops_oldest_touched_first():
    """The render cap keeps the newest-touched items and drops older ones first."""
    base = len("NOW") + len("DECISIONS") + len("OPEN") + len("PEOPLE") + len("LONG-TERM")
    base += 5 * len("\n- none")
    line_cost = len("- t: item-aaa") + 1
    rendered = render_summary(
        _summary_doc([
            {"section": "now", "topic": "t", "text": "item-ccc", "touched": "2026-11-16"},
            {"section": "now", "topic": "t", "text": "item-bbb", "touched": "2026-11-17"},
            {"section": "now", "topic": "t", "text": "item-aaa", "touched": "2026-11-18"},
        ]),
        today=datetime(2026, 11, 18).date(),
        decay_days=60,
        max_chars=base + 2 * line_cost,
    )
    assert "item-aaa" in rendered
    assert "item-bbb" in rendered
    assert "item-ccc" not in rendered


def test_set_summary_refuses_json_over_double_cap(tmp_path):
    """set_summary refuses JSON larger than twice the render cap; the boundary itself stores."""
    store = Store(tmp_path / "state.db")
    oversized = _summary_doc([
        {"section": "now", "topic": "t", "text": "x" * 250, "touched": "2026-11-18"},
    ])
    with pytest.raises(ValueError):
        store.set_summary(111, oversized, 1, max_chars=100)
    at_boundary = "x" * 200
    store.set_summary(111, at_boundary, 1, max_chars=100)
    assert store.get_summary(111) == at_boundary
    store.close()


@pytest.mark.asyncio
async def test_unparsable_summary_is_skipped_and_reply_still_posts(tmp_path, monkeypatch):
    """A corrupt stored summary renders as nothing and never fails the reply."""
    watcher = make_watcher(tmp_path)
    watcher._store.set_summary(111, "not json at all", 1, max_chars=1_500)
    channel = FakeChannel(111)
    message = FakeMessage(1, channel, FakeAuthor(7), "<@99> inspect", [FakeMention(99)])
    channel._history = [message]
    captured = {}

    async def fake_run(
        target, author, question, excerpt, member_id=None, bot_name="", bot_id=None, open_channel_prompt=None
    ):
        """Capture the prompt context."""
        captured["excerpt"] = excerpt
        return Reply("answer", "sha")

    async def fake_deliver(self, **kwargs):
        """Accept the post."""
        assert kwargs["chunk"] == "answer"
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr("oi_agent.watch.discord_client.Poster.deliver_chunk", fake_deliver)
    await process_messages(watcher, channel, message)

    assert "ROLLING SUMMARY" not in captured["excerpt"]
