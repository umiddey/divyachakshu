"""Shared fixtures, Discord fakes, and the durable-path helper for mocked watcher tests."""

import os
import sqlite3
from datetime import UTC, datetime

import discord
import pytest

from oi_agent.config import Config, WatchTarget
from oi_agent.store import Store
from oi_agent.watch.discord_client import OIWatcher


class FakeAuthor:
    """Minimal Discord author object."""

    def __init__(self, user_id, name="user1", bot=False):
        """Store author fields consumed by the watcher."""
        self.id = user_id
        self.display_name = name
        self.bot = bot


class FakeMention:
    """Minimal Discord mention object."""

    def __init__(self, user_id, display_name="ultron"):
        """Store the mentioned user ID and its display name."""
        self.id = user_id
        self.display_name = display_name


class FakeResponse:
    """Mock response for discord exception initialization."""

    def __init__(self, status=404, reason="Not Found"):
        self.status = status
        self.reason = reason


class FakeChannel:

    def __init__(self, channel_id, history=None, parent_id=None):
        """Create a fake channel."""
        self.id = channel_id
        self.parent_id = parent_id
        self._history = list(history or [])

    async def history(self, limit=50, after=None, oldest_first=False):
        """Yield fake messages with optional after filter."""
        msgs = list(self._history)
        if after is not None:
            after_id = after.id if hasattr(after, "id") else int(after)
            msgs = [m for m in msgs if m.id > after_id]
        if limit is not None:
            msgs = msgs[:limit]
        for message in msgs:
            yield message

    async def fetch_message(self, message_id):
        """Fetch a fake message by snowflake ID."""
        for m in self._history:
            if m.id == message_id:
                return m
        raise discord.NotFound(FakeResponse(404), "message not found")


class FakeMessage:
    """Minimal incoming Discord message."""

    def __init__(self, message_id, channel, author, content, mentions):
        """Create a fake event message."""
        self.id = message_id
        self.channel = channel
        self.author = author
        self.content = content
        self.mentions = mentions
        self.created_at = datetime.now(UTC)
        self.attachments = []
        self.embeds = []


def make_watcher(tmp_path, channel_id=111, open_channels=(), post_hourly_cap=None):
    """Build a watcher with one configured target and isolated state.

    Keyword-only extras: ``open_channels`` marks the target channel open and
    ``post_hourly_cap`` raises the default cap of 3, which would defer the
    extra replies open-channel test cases post.
    """
    cfg = Config()
    target = WatchTarget(channel_id, str(tmp_path / "repo"), 99, 3)
    if open_channels:
        target.open_channels = list(open_channels)
    if post_hourly_cap is not None:
        target.post_hourly_cap = post_hourly_cap
    cfg.watches = [target]
    return OIWatcher(cfg, Store(tmp_path / "state.db"))


async def process_messages(watcher, channel, *messages):
    """Run admitted messages through the durable processor without worker timing.

    Args:
        watcher (OIWatcher): Watcher backed by an isolated real store.
        channel (FakeChannel): Message source channel or thread.
        messages (FakeMessage): Messages admitted to this single batch.

    Returns:
        None: Processing and immediate delivery have completed.
    """
    watcher._channel_cache[channel.id] = channel
    target, owner = watcher._resolve_target(channel.id, channel)
    for message in messages:
        watcher._store.admit_mention_job(
            source_message_id=message.id,
            conversation_id=channel.id,
            owner_channel_id=owner,
            repo_path=target.primary_repo,
        )
    conversation_id, jobs = watcher._store.claim_next_conversation_jobs()
    await watcher._process_conversation_jobs(conversation_id, jobs)


@pytest.fixture(autouse=True)
def fake_opencode_on_path(tmp_path, monkeypatch):
    """Put a fake `opencode` executable first on PATH for every test.

    Config validation (``config._resolve_binary``) and OpenCode bootstrap
    discovery (``bootstrap.resolve_binary``) resolve the bare name
    ``opencode`` through PATH, so without this fixture every test that
    persists a config needs a real OpenCode install, and on machines that
    have one the real binary would be executed. The fake answers
    ``--version`` with a supported native v2 release and ``models`` with a
    canned listing, so the suite runs on a machine without OpenCode and
    never executes a real one.
    """
    bin_dir = tmp_path / "fake-opencode-bin"
    bin_dir.mkdir()
    fake = bin_dir / "opencode"
    fake.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  --version) echo 'opencode 2.0.5' ;;\n"
        "  models) echo 'openai/gpt-4o-mini' ;;\n"
        "  *) : ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    # A test that falls back to the default ``~/.local/state/oi/state.db`` must
    # never reach the operator's live database (init/watch-add open the store).
    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture
def seed_credential_db():
    """Seed an isolated native store with provider credential rows.

    Mirrors the real credential table shape (native.CREDENTIAL_COLUMNS)
    without touching any real secret material. Call once per row; the table
    is created on first use.
    """

    def _seed(data_home, integration_id, active=1):
        db = data_home / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS credential ("
                "id TEXT PRIMARY KEY, integration_id TEXT, label TEXT, value TEXT, "
                "connector_id TEXT, method_id TEXT, active INTEGER, "
                "time_created INTEGER, time_updated INTEGER)"
            )
            conn.execute(
                "INSERT INTO credential (id, integration_id, label, value, active) "
                "VALUES (?, ?, 'API key', 'test-fixture-not-a-secret', ?)",
                (f"cred_test_{integration_id}_{active}", integration_id, active),
            )
            conn.commit()
        finally:
            conn.close()
        return db

    return _seed
