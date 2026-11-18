"""Mocked end-to-end tests for the Discord admission and OpenCode path."""

import asyncio

import pytest
from conftest import FakeAuthor, FakeChannel, FakeMention, FakeMessage
from conftest import make_watcher as _watcher
from conftest import process_messages as _process_messages

from oi_agent.agent.reply import Reply
from oi_agent.poster import DeliveryResult
from oi_agent.watch import discord_client


@pytest.fixture(autouse=True)
def immediate_burst_flush(monkeypatch):
    """Remove production debounce delay from event-path tests."""
    monkeypatch.setattr(discord_client, "BURST_QUIET_SECONDS", 0)


@pytest.mark.asyncio
async def test_direct_mention_reaches_runner_and_poster_once(tmp_path, monkeypatch):
    """An explicit mention runs OpenCode and posts once."""
    watcher = _watcher(tmp_path)
    channel = FakeChannel(111)
    message = FakeMessage(1, channel, FakeAuthor(7), "<@99> inspect", [FakeMention(99)])
    channel._history = [message]
    calls = []

    async def fake_run(target, author, question, excerpt, member_id=None, bot_name="", bot_id=None):
        """Return a deterministic successful OpenCode reply."""
        calls.append((author, question))
        return Reply("answer", "abc123")

    async def fake_deliver(self, channel_id, target_channel_id, chunk, nonce, reply_to_message_id=None, client=None):
        """Capture the Poster boundary."""
        calls.append((channel_id, target_channel_id, chunk))
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)
    await watcher.on_message(message)
    await asyncio.sleep(0.05)

    assert calls[0] == ("user1", "@ultron (user id 99) inspect")
    assert calls[-1] == (111, 111, "answer")


@pytest.mark.asyncio
async def test_burst_coalescing_keeps_latest_question(tmp_path, monkeypatch):
    """A quiet-period burst creates one run using the latest explicit question."""
    watcher = _watcher(tmp_path)
    channel = FakeChannel(111)
    first = FakeMessage(1, channel, FakeAuthor(7), "<@99> old", [FakeMention(99)])
    second = FakeMessage(2, channel, FakeAuthor(7), "<@99> latest", [FakeMention(99)])
    channel._history = [first, second]
    questions = []

    async def fake_run(*args, **kwargs):
        """Capture the single coalesced request."""
        questions.append(args[2])
        return Reply("answer", "sha")

    async def fake_deliver(self, *args, **kwargs):
        """Accept the post without network access."""
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)
    await watcher.on_message(first)
    await watcher.on_message(second)
    await asyncio.sleep(0.05)

    assert questions == ["@ultron (user id 99) latest"]


@pytest.mark.asyncio
async def test_trigger_during_run_gets_one_followup(tmp_path, monkeypatch):
    """A newer trigger waits for the current run and does not race it."""
    watcher = _watcher(tmp_path)
    channel = FakeChannel(111)
    first = FakeMessage(1, channel, FakeAuthor(7), "<@99> first", [FakeMention(99)])
    second = FakeMessage(2, channel, FakeAuthor(7), "<@99> follow-up", [FakeMention(99)])
    channel._history = [first, second]
    started = asyncio.Event()
    release = asyncio.Event()
    questions = []

    async def fake_run(target, author, question, excerpt, member_id=None, bot_name="", bot_id=None):
        """Hold the first run open while the second trigger arrives."""
        questions.append(question)
        if question == "@ultron (user id 99) first":
            started.set()
            await release.wait()
        return Reply("answer", "sha")

    async def fake_deliver(self, *args, **kwargs):
        """Accept each post."""
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)
    await watcher.on_message(first)
    await asyncio.sleep(0.02)
    assert started.is_set()
    await watcher.on_message(second)
    await asyncio.sleep(0)
    assert questions == ["@ultron (user id 99) first"]
    release.set()
    await asyncio.sleep(0.05)
    assert questions == [
        "@ultron (user id 99) first",
        "@ultron (user id 99) follow-up",
    ]


@pytest.mark.parametrize(
    "failure_class",
    ["protocol_missing_final_marker", "protocol_empty_final_answer"],
)
@pytest.mark.asyncio
async def test_protocol_failure_is_retried_once(tmp_path, monkeypatch, failure_class):
    """A protocol failure gets exactly one immediate retry."""
    watcher = _watcher(tmp_path)
    channel = FakeChannel(111)
    message = FakeMessage(1, channel, FakeAuthor(7), "<@99> inspect", [FakeMention(99)])
    channel._history = [message]
    runs = []

    async def fake_run(target, author, question, excerpt, member_id=None, bot_name="", bot_id=None):
        """Fail once, then succeed fresh."""
        runs.append(failure_class)
        if len(runs) == 1:
            return Reply("failure", "sha", ok=False, error_class=failure_class)
        return Reply("answer", "sha")

    async def fake_deliver(self, *args, **kwargs):
        """Accept the final post."""
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)
    await watcher.on_message(message)
    await asyncio.sleep(0.05)

    assert len(runs) == 2


def test_resolve_user_mentions_resolves_known_and_unknown_ids():
    """Known mentions render as name + id; unknown ids degrade to an explicit marker."""
    from oi_agent.watch.discord_client import _resolve_user_mentions

    resolved = _resolve_user_mentions(
        "hey <@99> and <@!42> ping <@123456789>",
        [FakeMention(99, "ultron"), FakeAuthor(42, "sowa")],
    )
    assert resolved == (
        "hey @ultron (user id 99) and @sowa (user id 42) ping @unknown (user id 123456789)"
    )
    # No mention markup: content passes through untouched.
    assert _resolve_user_mentions("plain text", []) == "plain text"
    assert _resolve_user_mentions("", [FakeMention(99)]) == ""


@pytest.mark.asyncio
async def test_question_and_excerpt_reach_runner_with_resolved_mentions(tmp_path, monkeypatch):
    """Mention markup is resolved in both the question and every excerpt line."""
    watcher = _watcher(tmp_path)
    channel = FakeChannel(111)
    older = FakeMessage(1, channel, FakeAuthor(7), "hello <@99>", [FakeMention(99, "ultron")])
    latest = FakeMessage(2, channel, FakeAuthor(7), "<@99> status?", [FakeMention(99, "ultron")])
    channel._history = [older, latest]
    captured = {}

    async def fake_run(target, author, question, excerpt, member_id=None, bot_name="", bot_id=None):
        captured["question"] = question
        captured["excerpt"] = excerpt
        return Reply("answer", "sha")

    async def fake_deliver(self, *args, **kwargs):
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)
    await watcher.on_message(latest)
    await asyncio.sleep(0.05)

    assert captured["question"] == "@ultron (user id 99) status?"
    assert "@ultron (user id 99)" in captured["excerpt"]
    assert "<@99>" not in captured["excerpt"]


@pytest.mark.asyncio
async def test_runner_receives_bot_identity(tmp_path, monkeypatch):
    """The watcher passes its Discord display name and id to the runner."""
    watcher = _watcher(tmp_path)
    watcher._connection.user = FakeAuthor(99, "UltronBot")
    channel = FakeChannel(111)
    message = FakeMessage(1, channel, FakeAuthor(7), "<@99> inspect", [FakeMention(99)])
    channel._history = [message]
    captured = {}

    async def fake_run(target, author, question, excerpt, member_id=None, bot_name="", bot_id=None):
        captured["bot_name"] = bot_name
        captured["bot_id"] = bot_id
        return Reply("answer", "sha")

    async def fake_deliver(self, *args, **kwargs):
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)
    await watcher.on_message(message)
    await asyncio.sleep(0.05)

    assert captured == {"bot_name": "UltronBot", "bot_id": 99}


@pytest.mark.parametrize(
    "content",
    ["<@99> !fresh inspect", "<@!99> !new inspect", "!fresh <@99> inspect"],
)
@pytest.mark.asyncio
async def test_fresh_command_is_stripped_before_run(tmp_path, monkeypatch, content):
    """Reset tokens are consumed and never reach the model prompt."""
    watcher = _watcher(tmp_path)
    channel = FakeChannel(111)
    first = FakeMessage(1, channel, FakeAuthor(7), content, [FakeMention(99)])
    channel._history = [first]
    questions, posted = [], []

    async def fake_run(target, author, question, excerpt, member_id=None, bot_name="", bot_id=None):
        """Capture clean prompt context."""
        questions.append(question)
        assert "!fresh" not in question + excerpt
        assert "!new" not in question + excerpt
        assert "inspect" in question
        return Reply("answer", "sha")

    async def fake_deliver(self, **kwargs):
        """Accept the post."""
        posted.append(kwargs["chunk"])
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)
    await _process_messages(watcher, channel, first)
    assert first.content == content
    assert questions == ["@ultron (user id 99) inspect"]
    assert posted == ["answer"]


@pytest.mark.parametrize(
    "content",
    ["<@99> explain !fresh", "<@99> !freshness inspect", "<@99> !new-session", "<@7> !fresh <@99> inspect"],
)
@pytest.mark.asyncio
async def test_noncommand_text_is_not_treated_as_reset(tmp_path, monkeypatch, content):
    """Later words, longer tokens, and another user's mention stay literal text."""
    watcher = _watcher(tmp_path)
    channel = FakeChannel(111)
    message = FakeMessage(1, channel, FakeAuthor(7), content, [FakeMention(99), FakeMention(7)])
    channel._history = [message]
    questions = []

    async def fake_run(target, author, question, excerpt, member_id=None, bot_name="", bot_id=None):
        """Preserve literal user text when it is not a leading command token."""
        questions.append(question)
        assert "!" in question
        return Reply("answer", "sha")

    async def fake_deliver(self, **kwargs):
        """Deliver the untouched answer."""
        assert kwargs["chunk"] == "answer"
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)
    await _process_messages(watcher, channel, message)
    assert len(questions) == 1


@pytest.mark.asyncio
async def test_earlier_burst_reset_token_is_stripped(tmp_path, monkeypatch):
    """An earlier admitted reset token is stripped even when a later question wins."""
    watcher = _watcher(tmp_path)
    channel = FakeChannel(111)
    first = FakeMessage(1, channel, FakeAuthor(7), "<@99> !new old", [FakeMention(99)])
    second = FakeMessage(2, channel, FakeAuthor(7), "<@99> latest", [FakeMention(99)])
    channel._history = [first, second]
    captured = []

    async def fake_run(target, author, question, excerpt, member_id=None, bot_name="", bot_id=None):
        """Capture the coalesced run with sanitized context."""
        captured.append((question, excerpt))
        assert "!new" not in excerpt
        return Reply("answer", "sha")

    async def fake_deliver(self, **kwargs):
        """Accept one coalesced batch."""
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)
    await _process_messages(watcher, channel, first, second)
    assert len(captured) == 1
    assert captured[0][0] == "@ultron (user id 99) latest"


@pytest.mark.asyncio
async def test_stateless_calls_pass_no_session_state(tmp_path, monkeypatch):
    """Two calls in one conversation pass no session id; no session table exists."""
    watcher = _watcher(tmp_path)
    channel = FakeChannel(111)
    first = FakeMessage(1, channel, FakeAuthor(7), "<@99> first", [FakeMention(99)])
    second = FakeMessage(2, channel, FakeAuthor(7), "<@99> again", [FakeMention(99)])
    channel._history = [first, second]
    run_kwargs = []

    async def fake_run(*args, **kwargs):
        """Record the runner kwargs for both calls."""
        run_kwargs.append(kwargs)
        return Reply("answer", "sha")

    async def fake_deliver(self, **kwargs):
        """Accept each post."""
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)
    await watcher.on_message(first)
    await asyncio.sleep(0.05)
    await watcher.on_message(second)
    await asyncio.sleep(0.05)

    assert len(run_kwargs) == 2
    assert all("session_id" not in kwargs for kwargs in run_kwargs)
    assert watcher._store._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opencode_sessions'"
    ).fetchone() is None


@pytest.mark.asyncio
async def test_fresh_review_dispatch_after_reset_strip(tmp_path, monkeypatch):
    """A reset review follows real review dispatch after the token is stripped."""
    from oi_agent.opencode import runner

    watcher = _watcher(tmp_path)
    channel = FakeChannel(111)
    message = FakeMessage(1, channel, FakeAuthor(7), "<@99> !fresh review PR #188", [FakeMention(99)])
    channel._history = [message]
    review_target = object()
    posted = []

    def resolve_review(question, repo_paths):
        """Recognize the review only after the control token is stripped."""
        assert "!fresh" not in question
        assert "review PR #188" in question
        return review_target

    async def review_only(target, author, question, excerpt, *, bot_name, bot_id):
        """Model the isolated review result."""
        assert "!fresh" not in excerpt
        return Reply("review answer", "sha")

    async def fake_deliver(self, **kwargs):
        """Deliver the review."""
        posted.append(kwargs["chunk"])
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(runner, "resolve_review_target", resolve_review)
    monkeypatch.setattr(watcher._runner, "_run_review_locked", review_only)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)
    await _process_messages(watcher, channel, message)
    assert posted == ["review answer"]
