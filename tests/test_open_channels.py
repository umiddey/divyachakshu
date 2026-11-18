"""Tests for open channels: silence contract (P6) and the handler (P7).

P6 covers the prompt-side OPEN CHANNEL block, the ``[[OI_SILENT]]`` detection
helper, and the store-side silent completion. P7 covers the handler itself:
every message admitted, OI never answering itself, the consecutive-bot-turn
cap, the hourly run cap, and failure posting only for addressed triggers.
"""

import asyncio

import pytest
from conftest import FakeAuthor, FakeChannel, FakeMention, FakeMessage, make_watcher

from oi_agent.agent.reply import Reply
from oi_agent.config import WatchTarget
from oi_agent.opencode.prompt import (
    FINAL_OUTPUT_MARKER,
    OPEN_CHANNEL_SILENT,
    build_prompt,
    is_silent_reply,
)
from oi_agent.poster import DeliveryResult
from oi_agent.store import Store
from oi_agent.watch import discord_client


@pytest.fixture(autouse=True)
def immediate_burst_flush(monkeypatch):
    """Remove the debounce delay so event-path tests finish immediately."""
    monkeypatch.setattr(discord_client, "BURST_QUIET_SECONDS", 0)


def test_build_prompt_open_channel_block_only_when_enabled():
    """The OPEN CHANNEL contract and silence token appear only for open runs."""
    default = build_prompt()
    assert "OPEN CHANNEL MODE:" not in default
    assert OPEN_CHANNEL_SILENT not in default
    enabled = build_prompt(open_channel_prompt="none")
    assert OPEN_CHANNEL_SILENT in enabled
    assert FINAL_OUTPUT_MARKER in enabled


def test_prompt_silent_token_detection_exact():
    """A bare token after the final marker is silence, with or without marker."""
    assert is_silent_reply(f"{FINAL_OUTPUT_MARKER}\n{OPEN_CHANNEL_SILENT}")
    assert is_silent_reply(OPEN_CHANNEL_SILENT)


def test_prompt_silent_token_detection_whitespace():
    """Surrounding whitespace around the token still counts as silence."""
    assert is_silent_reply(f"{FINAL_OUTPUT_MARKER}\n\n  {OPEN_CHANNEL_SILENT}  \n")
    assert is_silent_reply(f"  {OPEN_CHANNEL_SILENT}\t")


def test_prompt_silent_token_detection_rejects_embedded_or_absent_token():
    """A token inside a sentence, or any other answer, is never silence."""
    assert not is_silent_reply(
        f"{FINAL_OUTPUT_MARKER} I would say {OPEN_CHANNEL_SILENT} mid-sentence, but no."
    )
    assert not is_silent_reply(f"{FINAL_OUTPUT_MARKER}\nhere is the real answer")
    assert not is_silent_reply("")
    # A token before the final marker is narration, not the answer.
    assert not is_silent_reply(
        f"considering {OPEN_CHANNEL_SILENT} {FINAL_OUTPUT_MARKER} actual answer"
    )


def test_silent_store_complete_jobs_silent_marks_done_without_delivery(tmp_path):
    """Silent completion ends leased jobs done with no batch or chunk rows."""
    store = Store(tmp_path / "state.db")
    try:
        store.admit_mention_job(11, 42, 42, tmp_path / "repo")
        claimed = store.claim_next_conversation_jobs()
        assert claimed is not None
        conv_id, jobs = claimed
        assert conv_id == 42
        assert [j["source_message_id"] for j in jobs] == [11]

        store.complete_jobs_silent([11])

        row = store._conn.execute(
            "SELECT status, batch_id, lease_until FROM mention_jobs "
            "WHERE source_message_id = 11"
        ).fetchone()
        assert row == ("done", None, 0)
        assert store._conn.execute("SELECT COUNT(*) FROM delivery_batches").fetchone()[0] == 0
        assert store._conn.execute("SELECT COUNT(*) FROM outbound_chunks").fetchone()[0] == 0

        # Repeating a silent completion stays idempotent and adds no delivery.
        store.complete_jobs_silent([11])
        assert store._conn.execute(
            "SELECT status FROM mention_jobs WHERE source_message_id = 11"
        ).fetchone()[0] == "done"
        assert store._conn.execute("SELECT COUNT(*) FROM delivery_batches").fetchone()[0] == 0
    finally:
        store.close()


def test_silent_store_complete_jobs_silent_empty_ids_is_noop(tmp_path):
    """An empty id list touches nothing."""
    store = Store(tmp_path / "state.db")
    try:
        store.complete_jobs_silent([])
        assert store._conn.execute("SELECT COUNT(*) FROM mention_jobs").fetchone()[0] == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_open_channel_thanks_stays_silent_and_posts_nothing(tmp_path, monkeypatch):
    """A human pleasantry runs once with open_channel_prompt set and posts nothing."""
    watcher = make_watcher(tmp_path, open_channels=(111,), post_hourly_cap=20)
    channel = FakeChannel(111)
    message = FakeMessage(1, channel, FakeAuthor(7), "thanks everyone!", [])
    channel._history = [message]
    posted = []

    async def fake_run(*args, **kwargs):
        """Declare model silence for the pleasantry."""
        assert kwargs.get("open_channel_prompt") == "none"
        return Reply(f"{FINAL_OUTPUT_MARKER}\n{OPEN_CHANNEL_SILENT}", "sha")

    async def fake_deliver(self, **kwargs):
        """Record any posting attempt; silence must never reach it."""
        posted.append(kwargs.get("chunk"))
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)
    await watcher.on_message(message)
    await asyncio.sleep(0.05)

    assert posted == []
    assert watcher._store._conn.execute(
        "SELECT status FROM mention_jobs WHERE source_message_id = 1"
    ).fetchone() == ("done",)
    assert watcher._store._conn.execute(
        "SELECT COUNT(*) FROM delivery_batches"
    ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_open_channel_question_gets_exactly_one_reply(tmp_path, monkeypatch):
    """A plain human question in an open channel produces exactly one reply."""
    watcher = make_watcher(tmp_path, open_channels=(111,), post_hourly_cap=20)
    channel = FakeChannel(111)
    message = FakeMessage(1, channel, FakeAuthor(7), "hey what broke the build?", [])
    channel._history = [message]
    runs, posted = [], []

    async def fake_run(target, author, question, excerpt, **kwargs):
        """Answer the open-channel question."""
        assert kwargs.get("open_channel_prompt") == "none"
        runs.append(question)
        return Reply("the flaky test again", "sha")

    async def fake_deliver(self, **kwargs):
        """Deliver the single reply."""
        posted.append(kwargs["chunk"])
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)
    await watcher.on_message(message)
    await asyncio.sleep(0.05)

    assert len(runs) == 1
    assert posted == ["the flaky test again"]


@pytest.mark.asyncio
async def test_non_open_channel_plain_message_still_ignored(tmp_path, monkeypatch):
    """Without open_channels a plain message stays ignored and never runs."""
    watcher = make_watcher(tmp_path, post_hourly_cap=20)
    channel = FakeChannel(111)
    message = FakeMessage(1, channel, FakeAuthor(7), "thanks everyone!", [])
    channel._history = [message]
    runs = []

    async def fake_run(*args, **kwargs):
        """Record any run; none may happen."""
        runs.append(1)
        return Reply("answer", "sha")

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    await watcher.on_message(message)
    await asyncio.sleep(0.05)

    assert runs == []
    assert watcher._store._conn.execute(
        "SELECT COUNT(*) FROM mention_jobs"
    ).fetchone()[0] == 0
    assert watcher._worker_task is None


@pytest.mark.asyncio
async def test_open_channel_own_message_never_admitted(tmp_path, monkeypatch):
    """OI's own bot account is never admitted, even when the gate would respond."""
    watcher = make_watcher(tmp_path, open_channels=(111,), post_hourly_cap=20)
    channel = FakeChannel(111)
    # Author id 99 is the watch's bot_user_id; the content even mentions OI,
    # which the explicit-trigger gate would answer. Open mode must refuse it.
    message = FakeMessage(
        1, channel, FakeAuthor(99, "OI", bot=True), "<@99> answering myself", [FakeMention(99)]
    )
    channel._history = [message]
    runs = []

    async def fake_run(*args, **kwargs):
        """Record any run; OI's own message may not cause one."""
        runs.append(1)
        return Reply("answer", "sha")

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    await watcher.on_message(message)
    await asyncio.sleep(0.05)

    assert runs == []
    assert watcher._store._conn.execute(
        "SELECT COUNT(*) FROM mention_jobs"
    ).fetchone()[0] == 0
    # OI's own post still counts as a bot turn for the streak.
    assert watcher._open_bot_streak[111] == 1


@pytest.mark.asyncio
async def test_fifth_consecutive_bot_turn_not_admitted_human_resets(tmp_path, monkeypatch):
    """Four consecutive bot turns run; the fifth is refused until a human writes."""
    watcher = make_watcher(tmp_path, open_channels=(111,), post_hourly_cap=20)
    channel = FakeChannel(111)
    bot = FakeAuthor(55, "otherbot", bot=True)
    for i in range(1, 6):
        message = FakeMessage(i, channel, bot, f"bot chatter {i}", [])
        channel._history.append(message)
        await watcher.on_message(message)

    admitted = watcher._store._conn.execute(
        "SELECT source_message_id FROM mention_jobs ORDER BY source_message_id"
    ).fetchall()
    assert [r[0] for r in admitted] == [1, 2, 3, 4]
    assert watcher._open_bot_streak[111] == 5

    human = FakeMessage(6, channel, FakeAuthor(7), "ok humans are back", [])
    channel._history.append(human)
    await watcher.on_message(human)

    assert watcher._open_bot_streak[111] == 0
    admitted = watcher._store._conn.execute(
        "SELECT source_message_id FROM mention_jobs ORDER BY source_message_id"
    ).fetchall()
    assert [r[0] for r in admitted] == [1, 2, 3, 4, 6]


@pytest.mark.asyncio
async def test_open_channel_runs_per_hour_cap_defers(tmp_path, monkeypatch):
    """Runs beyond open_channel_runs_per_hour are deferred, not dropped."""
    watcher = make_watcher(tmp_path, open_channels=(111,), post_hourly_cap=20)
    watcher._cfg.open_channel_runs_per_hour = 1
    channel = FakeChannel(111)
    posted = []

    async def fake_run(*args, **kwargs):
        """Answer the one allowed run."""
        return Reply("answer", "sha")

    async def fake_deliver(self, **kwargs):
        """Deliver the allowed reply only."""
        posted.append(kwargs.get("chunk"))
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)
    first = FakeMessage(1, channel, FakeAuthor(7), "first question", [])
    channel._history = [first]
    await watcher.on_message(first)
    await asyncio.sleep(0.05)
    assert posted == ["answer"]

    second = FakeMessage(2, channel, FakeAuthor(7), "second question", [])
    channel._history.append(second)
    await watcher.on_message(second)
    await asyncio.sleep(0.05)

    assert posted == ["answer"]
    assert watcher._store._conn.execute(
        "SELECT status, last_error_class FROM mention_jobs WHERE source_message_id = 2"
    ).fetchone() == ("pending", "open_run_cap")


@pytest.mark.asyncio
async def test_two_open_channels_run_third_closed_does_not(tmp_path, monkeypatch):
    """Both open channels run; a plain message in a closed third stays ignored."""
    watcher = make_watcher(tmp_path, open_channels=(111,), post_hourly_cap=20)
    repo = str(tmp_path / "repo")
    watcher._cfg.watches.append(WatchTarget(222, repo, 99, 20, open_channels=[222]))
    watcher._cfg.watches.append(WatchTarget(333, repo, 99, 20))
    ch1, ch2, ch3 = FakeChannel(111), FakeChannel(222), FakeChannel(333)
    m1 = FakeMessage(1, ch1, FakeAuthor(7), "question one", [])
    m2 = FakeMessage(2, ch2, FakeAuthor(7), "question two", [])
    m3 = FakeMessage(3, ch3, FakeAuthor(7), "plain chatter", [])
    ch1._history = [m1]
    ch2._history = [m2]
    ch3._history = [m3]
    runs = []

    async def fake_run(target, author, question, excerpt, **kwargs):
        """Answer each open-channel question once."""
        assert kwargs.get("open_channel_prompt") == "none"
        runs.append(question)
        return Reply("answer", "sha")

    async def fake_deliver(self, **kwargs):
        """Deliver each reply."""
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)
    await watcher.on_message(m1)
    await watcher.on_message(m2)
    await watcher.on_message(m3)
    await asyncio.sleep(0.1)

    assert len(runs) == 2
    conversations = {
        row[0]
        for row in watcher._store._conn.execute(
            "SELECT conversation_id FROM mention_jobs"
        ).fetchall()
    }
    assert conversations == {111, 222}


@pytest.mark.asyncio
async def test_failed_run_stays_silent_for_plain_open_traffic(tmp_path, monkeypatch):
    """A failed run for unaddressed open-channel traffic posts and retries nothing."""
    watcher = make_watcher(tmp_path, open_channels=(111,), post_hourly_cap=20)
    channel = FakeChannel(111)
    # A bot message with no explicit trigger: admitted by open mode only.
    message = FakeMessage(
        1, channel, FakeAuthor(55, "botly", bot=True), "noted the logs", []
    )
    channel._history = [message]
    posted = []

    async def fake_run(*args, **kwargs):
        """Fail the run without protocol or content."""
        return Reply("", "sha", ok=False, error_class="opencode_error")

    async def fake_deliver(self, **kwargs):
        """Record any posting attempt; failures must not post here."""
        posted.append(kwargs.get("chunk"))
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)
    await watcher.on_message(message)
    await asyncio.sleep(0.05)

    assert posted == []
    assert watcher._store._conn.execute(
        "SELECT status FROM mention_jobs WHERE source_message_id = 1"
    ).fetchone() == ("done",)
    assert watcher._store._conn.execute(
        "SELECT COUNT(*) FROM delivery_batches"
    ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_failed_run_still_posts_when_trigger_addressed(tmp_path, monkeypatch):
    """An addressed trigger keeps today's behavior: a failed run still posts."""
    watcher = make_watcher(tmp_path, open_channels=(111,), post_hourly_cap=20)
    channel = FakeChannel(111)
    message = FakeMessage(
        1, channel, FakeAuthor(7), "<@99> run the tests", [FakeMention(99)]
    )
    channel._history = [message]
    posted = []

    async def fake_run(*args, **kwargs):
        """Fail the run without protocol or content."""
        return Reply("", "sha", ok=False, error_class="opencode_error")

    async def fake_deliver(self, **kwargs):
        """Deliver the failure artifact for the addressed trigger."""
        posted.append(kwargs["chunk"])
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)
    await watcher.on_message(message)
    await asyncio.sleep(0.05)

    assert posted == ["(no response generated)"]


def test_bare_silence_token_without_marker_is_accepted_not_retried():
    """A model that says only the silence token skips the marker; no retry is needed."""
    from oi_agent.opencode.runner import _extract_marked_answer

    assert _extract_marked_answer(f"  {OPEN_CHANNEL_SILENT}\n") == (OPEN_CHANNEL_SILENT, None)
    # Any other unmarked text is still a protocol failure.
    assert _extract_marked_answer("hello there") == ("", "protocol_missing_final_marker")
    # Silence mixed with other text without the marker is not silence.
    assert _extract_marked_answer(f"ok {OPEN_CHANNEL_SILENT}") == ("", "protocol_missing_final_marker")
