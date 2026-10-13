"""An explicit public attempt with no evidence gets an honest limitation reply.

Regression tests for the confirmed review finding: ``PreparedExternal.refusal``
was never read, so a disabled gate, an ambiguous ask, a private-referential
ask, or a failed retrieval fell through to a normal repository audit that
resumed the conversation session. An explicit internet attempt must instead be
answered with an honest limitation: no resumed session, no writes, and never a
silent fallback to repository knowledge.
"""

import asyncio
from datetime import UTC, datetime

import pytest

from oi_agent.agent.reply import Reply
from oi_agent.config import Config, WatchTarget
from oi_agent.external import prepare as external_prepare
from oi_agent.poster import DeliveryResult
from oi_agent.store import Store
from oi_agent.watch import attachments as watch_attachments
from oi_agent.watch import discord_client
from oi_agent.watch.discord_client import OIWatcher


class FakeAuthor:
    """Minimal Discord author object."""

    def __init__(self, user_id, name="user1"):
        """Store author fields consumed by the watcher."""
        self.id = user_id
        self.display_name = name


class FakeMention:
    """Minimal Discord mention object."""

    def __init__(self, user_id, display_name="ultron"):
        """Store the mentioned user id and its display name."""
        self.id = user_id
        self.display_name = display_name


class FakeChannel:
    """Minimal channel whose history the watcher may refetch."""

    def __init__(self, channel_id, history=None):
        """Create a fake channel."""
        self.id = channel_id
        self._history = list(history or [])

    async def history(self, limit=50, after=None, oldest_first=False):
        """Yield fake messages."""
        msgs = list(self._history)
        if after is not None:
            msgs = [m for m in msgs if m.id > after.id]
        if limit is not None:
            msgs = msgs[:limit]
        for message in msgs:
            yield message

    async def fetch_message(self, message_id):
        """Fetch a fake message by snowflake id."""
        for m in self._history:
            if m.id == message_id:
                return m
        raise KeyError(message_id)


class FakeAttachment:
    """Minimal discord.py-shaped attachment object."""

    def __init__(self, attachment_id, filename, size, content_type, url):
        """Store attachment fields consumed by the adapter."""
        self.id = attachment_id
        self.filename = filename
        self.size = size
        self.content_type = content_type
        self.url = url


class FakeMessage:
    """Minimal incoming Discord message."""

    def __init__(self, message_id, channel, author, content, mentions, attachments=()):
        """Create a fake event message."""
        self.id = message_id
        self.channel = channel
        self.author = author
        self.content = content
        self.mentions = mentions
        self.created_at = datetime.now(UTC)
        self.attachments = list(attachments)
        self.embeds = []


@pytest.fixture(autouse=True)
def immediate_burst_flush(monkeypatch):
    """Remove production debounce delay from event-path tests."""
    monkeypatch.setattr(discord_client, "BURST_QUIET_SECONDS", 0)


def _watcher(tmp_path, external_inputs="disabled"):
    """Build a watcher with one configured target and isolated state."""
    cfg = Config()
    cfg.external_inputs = external_inputs
    cfg.watches = [WatchTarget(111, str(tmp_path / "repo"), 99, 3)]
    return OIWatcher(cfg, Store(tmp_path / "state.db"))


def _forbid_retrieval(monkeypatch, *names):
    """Make every named retrieval entry point fail loudly if called."""

    async def forbidden(*args, **kwargs):
        raise AssertionError(f"retrieval must not run here: {names}")

    for name in names:
        monkeypatch.setattr(external_prepare, name, forbidden)


@pytest.mark.asyncio
async def test_disabled_gate_command_gets_limitation_without_a_repo_audit(
    tmp_path, monkeypatch
):
    """`!news` under the disabled gate is a limitation, never a resumed audit."""
    watcher = _watcher(tmp_path)
    channel = FakeChannel(111)
    message = FakeMessage(
        1, channel, FakeAuthor(7), "<@99> !news rust releases", [FakeMention(99)]
    )
    channel._history = [message]
    run_calls = []

    async def forbidden_run(*args, **kwargs):
        run_calls.append((args, kwargs))
        return Reply("repository answer", "abc123", session_id="resumed-session")

    async def forbidden_external(*args, **kwargs):
        raise AssertionError("external evidence run must not be used for a limitation")

    async def fake_deliver(self, channel_id, target_channel_id, chunk, nonce,
                           reply_to_message_id=None, client=None):
        return DeliveryResult(ok=True, discord_message_id=999)

    _forbid_retrieval(monkeypatch, "search_news", "search_web", "fetch_public_document")
    monkeypatch.setattr(watcher._runner, "run", forbidden_run)
    monkeypatch.setattr(watcher._runner, "run_external_evidence", forbidden_external)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)

    await watcher.on_message(message)
    await asyncio.sleep(0.05)

    assert run_calls == []
    assert watcher._store.get_opencode_session(111, tmp_path / "repo") is None


@pytest.mark.asyncio
async def test_disabled_gate_limitation_text_is_honest(tmp_path, monkeypatch):
    """The disabled-gate reply states the capability honestly."""
    watcher = _watcher(tmp_path)
    channel = FakeChannel(111)
    message = FakeMessage(
        1, channel, FakeAuthor(7), "<@99> !news rust releases", [FakeMention(99)]
    )
    channel._history = [message]
    delivered = []

    async def fake_deliver(self, channel_id, target_channel_id, chunk, nonce,
                           reply_to_message_id=None, client=None):
        delivered.append(chunk)
        return DeliveryResult(ok=True, discord_message_id=999)

    _forbid_retrieval(monkeypatch, "search_news", "search_web", "fetch_public_document")
    monkeypatch.setattr(
        watcher._runner, "run", _never_run("repository audit must not happen")
    )
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)

    await watcher.on_message(message)
    await asyncio.sleep(0.05)

    assert delivered and "disabled" in delivered[0].lower()


@pytest.mark.asyncio
async def test_ambiguous_command_gets_limitation_asking_for_public_topic(
    tmp_path, monkeypatch
):
    """A bare `!news` asks for a public topic instead of auditing the repo."""
    watcher = _watcher(tmp_path, external_inputs="read-only")
    channel = FakeChannel(111)
    message = FakeMessage(1, channel, FakeAuthor(7), "<@99> !news", [FakeMention(99)])
    channel._history = [message]
    run_calls = []
    delivered = []

    async def forbidden_run(*args, **kwargs):
        run_calls.append((args, kwargs))
        return Reply("repository answer", "abc123", session_id="resumed-session")

    async def fake_deliver(self, channel_id, target_channel_id, chunk, nonce,
                           reply_to_message_id=None, client=None):
        delivered.append(chunk)
        return DeliveryResult(ok=True, discord_message_id=999)

    _forbid_retrieval(monkeypatch, "search_news", "search_web", "fetch_public_document")
    monkeypatch.setattr(watcher._runner, "run", forbidden_run)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)

    await watcher.on_message(message)
    await asyncio.sleep(0.05)

    assert run_calls == []
    assert watcher._store.get_opencode_session(111, tmp_path / "repo") is None
    assert delivered and "public" in delivered[0].lower()


@pytest.mark.asyncio
async def test_private_referential_command_is_refused_without_a_repo_audit(
    tmp_path, monkeypatch
):
    """`!search the token above` is refused honestly; the repo is not audited."""
    watcher = _watcher(tmp_path, external_inputs="read-only")
    channel = FakeChannel(111)
    message = FakeMessage(
        1, channel, FakeAuthor(7), "<@99> !search the token above", [FakeMention(99)]
    )
    channel._history = [message]
    run_calls = []
    delivered = []

    async def forbidden_run(*args, **kwargs):
        run_calls.append((args, kwargs))
        return Reply("repository answer", "abc123", session_id="resumed-session")

    async def fake_deliver(self, channel_id, target_channel_id, chunk, nonce,
                           reply_to_message_id=None, client=None):
        delivered.append(chunk)
        return DeliveryResult(ok=True, discord_message_id=999)

    _forbid_retrieval(monkeypatch, "search_news", "search_web", "fetch_public_document")
    monkeypatch.setattr(watcher._runner, "run", forbidden_run)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)

    await watcher.on_message(message)
    await asyncio.sleep(0.05)

    assert run_calls == []
    assert watcher._store.get_opencode_session(111, tmp_path / "repo") is None
    assert delivered and "private" in delivered[0].lower()


@pytest.mark.asyncio
async def test_failed_retrieval_gets_limitation_without_a_repo_audit(
    tmp_path, monkeypatch
):
    """A retrieval that fails with no evidence is a limitation, not an audit."""
    watcher = _watcher(tmp_path, external_inputs="read-only")
    channel = FakeChannel(111)
    message = FakeMessage(
        1, channel, FakeAuthor(7), "<@99> !news rust releases", [FakeMention(99)]
    )
    channel._history = [message]
    run_calls = []
    delivered = []

    async def failing_research(query, urls, *, max_sources, timeout=180):
        from oi_agent.external.research import ResearchResult
        return ResearchResult(False, category="no_results")

    async def forbidden_run(*args, **kwargs):
        run_calls.append((args, kwargs))
        return Reply("repository answer", "abc123", session_id="resumed-session")

    async def forbidden_external(*args, **kwargs):
        raise AssertionError("no evidence was collected; no external run may start")

    async def fake_deliver(self, channel_id, target_channel_id, chunk, nonce,
                           reply_to_message_id=None, client=None):
        delivered.append(chunk)
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watcher._runner, "run_web_research", failing_research)
    _forbid_retrieval(monkeypatch, "fetch_public_document")
    monkeypatch.setattr(watcher._runner, "run", forbidden_run)
    monkeypatch.setattr(watcher._runner, "run_external_evidence", forbidden_external)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)

    await watcher.on_message(message)
    await asyncio.sleep(0.05)

    assert run_calls == []
    assert watcher._store.get_opencode_session(111, tmp_path / "repo") is None
    assert delivered
    assert "retrieval unavailable" in delivered[0].lower()
    assert "no_results" in delivered[0]


@pytest.mark.asyncio
async def test_limitation_with_real_attachment_keeps_external_run_and_notes_it(
    tmp_path, monkeypatch
):
    """A refused public attempt with real attachments still serves the files.

    The external-evidence run happens for the attachments, and the limitation
    reaches the prompt as an explicit controller note so the model reports the
    real capability gap instead of answering the public part from memory.
    """
    watcher = _watcher(tmp_path, external_inputs="read-only")
    channel = FakeChannel(111)
    attachment = FakeAttachment(
        "att-1", "notes.txt", 32, "text/plain",
        "https://cdn.discordapp.com/attachments/1/2/notes.txt",
    )
    message = FakeMessage(
        1, channel, FakeAuthor(7), "<@99> !search the token above",
        [FakeMention(99)], attachments=[attachment],
    )
    channel._history = [message]
    run_calls = []
    external_prepared = []

    async def fake_fetch(url):
        return b"Quarterly notes: revenue 42 USD"

    async def forbidden_run(*args, **kwargs):
        run_calls.append((args, kwargs))
        return Reply("repository answer", "abc123", session_id="resumed-session")

    async def fake_external(prepared, **kwargs):
        external_prepared.append(prepared)
        return Reply("attachment answer", "none")

    async def fake_deliver(self, channel_id, target_channel_id, chunk, nonce,
                           reply_to_message_id=None, client=None):
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watch_attachments, "fetch_attachment_bytes", fake_fetch)
    monkeypatch.setattr(watcher._runner, "run", forbidden_run)
    monkeypatch.setattr(watcher._runner, "run_external_evidence", fake_external)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)

    await watcher.on_message(message)
    await asyncio.sleep(0.05)

    assert run_calls == []
    assert len(external_prepared) == 1
    prepared = external_prepared[0]
    assert prepared.inputs.attachments
    notes = " ".join(prepared.inputs.notes).lower()
    assert "private" in notes
    assert watcher._store.get_opencode_session(111, tmp_path / "repo") is None


def _never_run(message):
    """Return a runner replacement that always fails loudly."""

    async def forbidden(*args, **kwargs):
        raise AssertionError(message)

    return forbidden
